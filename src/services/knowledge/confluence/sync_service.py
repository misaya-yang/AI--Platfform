"""
Confluence Sync Service.

Provides synchronization functionality between Confluence and the knowledge base,
including:
- Connection management
- URL-based single page import
- Space-wide batch import
- Incremental synchronization
- Integration with KnowledgeService for document processing

This service coordinates between:
- ConfluenceClient for API calls
- StorageFormatParser for content conversion
- KnowledgeService for document creation
- KnowledgeWorker for background processing
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from ....config.settings import Settings
from ....core.auth.user_resolver import UserContext
from .client import ConfluenceClient, ConfluenceAPIError
from .models import (
    ConfluenceCredentials,
    ConfluencePage,
    ConfluenceSpace,
    SyncResult,
    ImageSegment,
)
from .parser import extract_plain_text, extract_markdown

if TYPE_CHECKING:
    from ....persistence.database import DatabaseStorage
    from ..knowledge_service import KnowledgeService
    from ..worker import KnowledgeWorker
    from .image_processor import ConfluenceImageProcessor


def _utc_now() -> datetime:
    """Get current UTC time as naive datetime for consistent DB storage."""
    return datetime.now(timezone.utc).replace(tzinfo=None)

logger = logging.getLogger(__name__)


class ConfluenceSyncError(Exception):
    """Confluence 同步错误"""
    pass


class ConfluenceAccessDeniedError(Exception):
    """Confluence 资源访问被拒绝"""
    def __init__(self, resource_type: str, resource_id: str):
        self.resource_type = resource_type
        self.resource_id = resource_id
        super().__init__(f"Access denied to {resource_type}: {resource_id}")


class ConfluenceSyncService:
    """
    Confluence 同步服务

    负责：
    1. 连接管理：创建、测试、删除 Confluence 连接
    2. URL 导入：解析 Confluence URL，获取页面内容并创建文档
    3. 空间导入：遍历整个 Space，批量创建文档
    4. 增量同步：检测变更，只更新修改的页面
    5. 与 KnowledgeService 集成：复用现有的分块和向量化流程
    """

    def __init__(
        self,
        settings: Settings,
        database: "DatabaseStorage",
        knowledge_service: "KnowledgeService",
        knowledge_worker: Optional["KnowledgeWorker"] = None,
        image_processor: Optional["ConfluenceImageProcessor"] = None,
        image_storage_service: Optional[Any] = None,
        vlm_service: Optional[Any] = None,
    ):
        self.settings = settings
        self.db = database
        self.knowledge_service = knowledge_service
        self.worker = knowledge_worker
        self.image_processor = image_processor
        self._clients: Dict[str, ConfluenceClient] = {}
        self._client_created_at: Dict[str, float] = {}  # Track client creation time for TTL
        self._client_locks: Dict[str, asyncio.Lock] = {}  # Prevent race conditions
        self._client_cache_ttl = getattr(settings.confluence, "client_cache_ttl_seconds", 300)

        # 图片处理相关服务 (Text-First RAG: 不再需要 multimodal_embedding)
        self._image_storage_service = image_storage_service
        self._vlm_service = vlm_service
        self._image_processors: Dict[str, "ConfluenceImageProcessor"] = {}

        # 检查 Worker 状态，如果未提供则记录警告
        if not knowledge_worker:
            logger.warning(
                "⚠️ KnowledgeWorker not provided to ConfluenceSyncService - "
                "documents will NOT be auto-indexed after sync!"
            )

        if image_processor:
            logger.info("Image processor configured - image sync enabled")
        elif image_storage_service:
            logger.info("Image storage service configured - image sync available")

        if vlm_service:
            logger.info("VLM service configured - image descriptions enabled")

    async def close(self) -> None:
        """关闭所有客户端连接"""
        for client in self._clients.values():
            try:
                await client.close()
            except Exception as e:
                logger.warning(f"Error closing client: {e}")
        self._clients.clear()
        self._client_created_at.clear()
        self._client_locks.clear()

    def _handle_task_exception(self, task: asyncio.Task) -> None:
        """Handle exceptions from background tasks to prevent silent failures."""
        try:
            # This will re-raise any exception that occurred in the task
            task.result()
        except asyncio.CancelledError:
            # Task was cancelled, this is expected
            pass
        except Exception as e:
            task_name = task.get_name()
            logger.error(f"Background task '{task_name}' failed with exception: {e}", exc_info=True)

    def _create_background_task(
        self,
        coro,
        name: str,
    ) -> asyncio.Task:
        """Create a background task with proper exception handling."""
        task = asyncio.create_task(coro, name=name)
        task.add_done_callback(self._handle_task_exception)
        return task

    async def _save_version_before_update(
        self,
        doc_id: str,
        new_confluence_version: int,
        change_reason: Optional[str] = None,
    ) -> None:
        """
        Save current document content as a version before updating.

        This creates a version snapshot for rollback capability.

        Args:
            doc_id: Document ID to save version for
            new_confluence_version: The new Confluence version being synced
            change_reason: Reason for the change (e.g., "Confluence sync (version X)")
        """
        import hashlib

        try:
            # Get current document content
            current_doc = await self.db.get_document(doc_id)
            if not current_doc or not current_doc.get("content"):
                logger.debug(f"No content to version for document {doc_id}")
                return

            content = current_doc.get("content", "")
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

            # Check if we already have this version (avoid duplicates)
            latest_version = await self.db.get_latest_document_version(doc_id)
            if latest_version and latest_version.get("content_hash") == content_hash:
                logger.debug(f"Content unchanged for document {doc_id}, skipping version")
                return

            # Create version snapshot
            await self.db.create_document_version(
                document_id=doc_id,
                content=content,
                content_hash=content_hash,
                change_type="updated",
                title=current_doc.get("title"),
                metadata=current_doc.get("metadata"),
                change_reason=change_reason or f"Confluence sync (version {new_confluence_version})",
                confluence_version=current_doc.get("confluence_version"),
                confluence_updated_at=current_doc.get("updated_at"),
            )

            logger.info(f"Saved version snapshot for document {doc_id} before Confluence sync")

        except Exception as e:
            # Don't fail the sync if versioning fails - just log and continue
            logger.warning(f"Failed to save version for document {doc_id}: {e}")

    async def _get_client(self, connection_id: str) -> ConfluenceClient:
        """
        获取或创建 Confluence 客户端（带 TTL 和并发保护）

        Thread-safety:
        - Uses per-connection locks to prevent duplicate client creation
        - TTL check and client cleanup both happen inside the lock
        - Lock creation itself is protected by a global lock
        """
        # Fast path: check if valid cached client exists (no lock needed for read)
        if connection_id in self._clients:
            created_at = self._client_created_at.get(connection_id, 0)
            if time.time() - created_at < self._client_cache_ttl:
                return self._clients[connection_id]

        # Get or create connection-specific lock atomically using setdefault
        # setdefault is atomic in CPython, preventing race conditions where
        # multiple coroutines could create different locks for the same connection_id
        lock = self._client_locks.setdefault(connection_id, asyncio.Lock())

        async with lock:
            # Double-check after acquiring lock (another coroutine may have created/refreshed)
            if connection_id in self._clients:
                created_at = self._client_created_at.get(connection_id, 0)
                if time.time() - created_at < self._client_cache_ttl:
                    return self._clients[connection_id]
                else:
                    # Client expired - close it inside the lock to prevent race
                    logger.debug(f"Client cache expired for {connection_id}, refreshing")
                    await self._close_client_unsafe(connection_id)

            # Create new client
            connection = await self.db.get_confluence_connection(connection_id)
            if not connection:
                raise ConfluenceSyncError(f"Connection not found: {connection_id}")

            credentials = ConfluenceCredentials(
                domain=connection["domain"],
                email=connection["email"],
                api_token=connection["api_token"],
            )

            client = ConfluenceClient(
                credentials,
                timeout=self.settings.confluence.request_timeout,
                max_retries=self.settings.confluence.max_retries,
            )
            self._clients[connection_id] = client
            self._client_created_at[connection_id] = time.time()
            return client

    async def _close_client_unsafe(self, connection_id: str) -> None:
        """
        Close and remove a cached client (internal, no lock).

        IMPORTANT: Only call this when already holding the connection lock.
        """
        if connection_id in self._clients:
            try:
                await self._clients[connection_id].close()
            except Exception as e:
                logger.warning(f"Error closing client {connection_id}: {e}")
            del self._clients[connection_id]
        self._client_created_at.pop(connection_id, None)

    async def _close_client(self, connection_id: str) -> None:
        """Close and remove a cached client (thread-safe)."""
        # Get or create lock atomically to prevent race with _get_client
        lock = self._client_locks.setdefault(connection_id, asyncio.Lock())

        async with lock:
            await self._close_client_unsafe(connection_id)

    def _verify_confluence_access(
        self,
        resource: Dict[str, Any],
        resource_type: str,
        user: UserContext,
    ) -> None:
        """
        验证用户对 Confluence 资源的访问权限

        访问规则：
        1. Admin 用户可访问所有资源
        2. 资源所有者 (owner_id 或 created_by 匹配) 可完全访问
        3. 其他用户拒绝访问

        Args:
            resource: 资源数据字典，需包含 owner_id 或 created_by
            resource_type: 资源类型 (connection, binding, task)
            user: 用户上下文

        Raises:
            ConfluenceAccessDeniedError: 访问被拒绝
        """
        resource_id = resource.get(f"{resource_type}_id", resource.get("id", "unknown"))

        # 1. Admin 用户可访问所有资源
        if user.tier == "admin":
            return

        # 2. 检查所有权
        owner_id = resource.get("owner_id") or resource.get("created_by")
        if owner_id and owner_id == user.user_id:
            return

        # 3. 默认拒绝
        raise ConfluenceAccessDeniedError(resource_type, resource_id)

    async def _verify_binding_access(
        self,
        binding_id: str,
        user: UserContext,
    ) -> Dict[str, Any]:
        """
        验证用户对 binding 的访问权限并返回 binding 数据

        Args:
            binding_id: 绑定 ID
            user: 用户上下文

        Returns:
            binding 数据

        Raises:
            ConfluenceSyncError: binding 不存在
            ConfluenceAccessDeniedError: 访问被拒绝
        """
        binding = await self.db.get_confluence_binding(binding_id)
        if not binding:
            raise ConfluenceSyncError(f"Binding not found: {binding_id}")
        self._verify_confluence_access(binding, "binding", user)
        return binding

    async def _verify_connection_access(
        self,
        connection_id: str,
        user: UserContext,
    ) -> Dict[str, Any]:
        """
        验证用户对 connection 的访问权限并返回 connection 数据

        Args:
            connection_id: 连接 ID
            user: 用户上下文

        Returns:
            connection 数据

        Raises:
            ConfluenceSyncError: connection 不存在
            ConfluenceAccessDeniedError: 访问被拒绝
        """
        connection = await self.db.get_confluence_connection(connection_id)
        if not connection:
            raise ConfluenceSyncError(f"Connection not found: {connection_id}")
        self._verify_confluence_access(connection, "connection", user)
        return connection

    async def _verify_page_access(
        self,
        page_record_id: str,
        user: UserContext,
    ) -> Dict[str, Any]:
        """
        验证用户对 page record 的访问权限并返回 page 数据

        通过 page 所属的 binding 来验证访问权限

        Args:
            page_record_id: 页面记录 ID
            user: 用户上下文

        Returns:
            page 数据

        Raises:
            ConfluenceSyncError: page 不存在
            ConfluenceAccessDeniedError: 访问被拒绝
        """
        page_record = await self.db.get_confluence_page(page_record_id)
        if not page_record:
            raise ConfluenceSyncError(f"Page record not found: {page_record_id}")

        # 通过 binding 验证访问权限
        binding_id = page_record.get("binding_id")
        if binding_id:
            await self._verify_binding_access(binding_id, user)
        else:
            # 没有 binding_id 的 page record 不应该存在，但为安全起见处理这种情况
            raise ConfluenceAccessDeniedError("page", page_record_id)

        return page_record

    async def get_page(
        self,
        page_record_id: str,
        user: UserContext,
    ) -> Dict[str, Any]:
        """
        获取页面记录并验证访问权限

        公开方法，封装 _verify_page_access 以供 API 层使用

        Args:
            page_record_id: 页面记录 ID
            user: 用户上下文

        Returns:
            page 数据

        Raises:
            ConfluenceSyncError: page 不存在
            ConfluenceAccessDeniedError: 访问被拒绝
        """
        return await self._verify_page_access(page_record_id, user)

    async def _get_image_processor(
        self, connection_id: str, max_image_size: Optional[int] = None
    ) -> Optional["ConfluenceImageProcessor"]:
        """
        获取或创建指定连接的图片处理器

        Args:
            connection_id: 连接 ID
            max_image_size: 可选的最大图片大小配置（字节）。
                           如果提供，将创建新的处理器而不使用缓存。

        Returns:
            图片处理器实例，如果无法创建则返回 None
        """
        # 如果已经有传入的 image_processor，直接使用
        if self.image_processor:
            return self.image_processor

        # 如果没有存储服务，无法创建
        if not self._image_storage_service:
            logger.debug("No image storage service available, cannot create image processor")
            return None

        # 如果提供了自定义 max_image_size，创建新的处理器（不缓存）
        if max_image_size is not None:
            try:
                from .image_processor import ConfluenceImageProcessor

                client = await self._get_client(connection_id)
                processor = ConfluenceImageProcessor(
                    confluence_client=client,
                    storage_service=self._image_storage_service,
                    vlm_service=self._vlm_service,
                    generate_vlm_descriptions=self._vlm_service is not None,
                    max_image_size=max_image_size,
                )
                logger.debug(
                    f"Created image processor for connection {connection_id} "
                    f"with custom max_image_size={max_image_size}, vlm={self._vlm_service is not None}"
                )
                return processor
            except Exception as e:
                logger.warning(f"Failed to create image processor: {e}")
                return None

        # 检查是否已缓存
        if connection_id in self._image_processors:
            return self._image_processors[connection_id]

        # 创建新的 image_processor（Text-First RAG: 无需 multimodal embedding）
        try:
            # 延迟导入以避免循环依赖
            from .image_processor import ConfluenceImageProcessor

            client = await self._get_client(connection_id)
            processor = ConfluenceImageProcessor(
                confluence_client=client,
                storage_service=self._image_storage_service,
                vlm_service=self._vlm_service,
                generate_vlm_descriptions=self._vlm_service is not None,
            )
            self._image_processors[connection_id] = processor
            logger.debug(
                f"Created image processor for connection {connection_id} "
                f"(vlm_enabled={self._vlm_service is not None})"
            )
            return processor
        except Exception as e:
            logger.warning(f"Failed to create image processor: {e}")
            return None

    def _invalidate_client(self, connection_id: str) -> None:
        """使客户端缓存失效"""
        if connection_id in self._clients:
            self._create_background_task(
                self._clients[connection_id].close(),
                name=f"close-client-{connection_id[:8]}"
            )
            del self._clients[connection_id]
        self._client_created_at.pop(connection_id, None)

    # ============ Connection Management ============

    async def create_connection(
        self,
        tenant_id: str,
        name: str,
        domain: str,
        email: str,
        api_token: str,
        sync_mode: str = "manual",
        polling_interval_minutes: int = 60,
        created_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        创建 Confluence 连接

        Args:
            tenant_id: 租户 ID
            name: 连接名称
            domain: Atlassian 域名
            email: 账户邮箱
            api_token: API Token
            sync_mode: 同步模式 (manual | polling)
            polling_interval_minutes: 轮询间隔
            created_by: 创建者 ID

        Returns:
            创建的连接信息
        """
        # 验证连接
        credentials = ConfluenceCredentials(domain=domain, email=email, api_token=api_token)
        async with ConfluenceClient(credentials) as client:
            test_result = await client.test_connection()
            if test_result["status"] != "success":
                raise ConfluenceSyncError(f"Connection validation failed: {test_result.get('message')}")

        connection_id = str(uuid.uuid4())
        connection = {
            "connection_id": connection_id,
            "tenant_id": tenant_id or "",
            "name": name,
            "domain": domain,
            "email": email,
            "api_token": api_token,
            "sync_mode": sync_mode,
            "polling_interval_minutes": polling_interval_minutes,
            "status": "active",
            "owner_id": created_by,  # ACL: 设置资源所有者
            "created_by": created_by,
        }

        await self.db.save_confluence_connection(connection)
        return await self.db.get_confluence_connection(connection_id)

    async def update_connection(
        self,
        connection_id: str,
        tenant_id: str,
        **updates,
    ) -> Dict[str, Any]:
        """更新连接配置"""
        connection = await self.db.get_confluence_connection(connection_id)
        if not connection:
            raise ConfluenceSyncError(f"Connection not found: {connection_id}")

        # 检查权限（只能更新自己租户的连接）
        if connection["tenant_id"] and connection["tenant_id"] != tenant_id:
            raise ConfluenceSyncError("Permission denied")

        # 如果更新了认证信息，需要重新验证
        if any(k in updates for k in ("domain", "email", "api_token")):
            domain = updates.get("domain", connection["domain"])
            email = updates.get("email", connection["email"])
            api_token = updates.get("api_token", connection["api_token"])

            credentials = ConfluenceCredentials(domain=domain, email=email, api_token=api_token)
            async with ConfluenceClient(credentials) as client:
                test_result = await client.test_connection()
                if test_result["status"] != "success":
                    raise ConfluenceSyncError(f"Connection validation failed: {test_result.get('message')}")

            # 使缓存失效
            self._invalidate_client(connection_id)

        await self.db.update_confluence_connection(connection_id, updates)
        return await self.db.get_confluence_connection(connection_id)

    async def delete_connection(self, connection_id: str, tenant_id: str) -> bool:
        """删除连接"""
        connection = await self.db.get_confluence_connection(connection_id)
        if not connection:
            return False

        if connection["tenant_id"] and connection["tenant_id"] != tenant_id:
            raise ConfluenceSyncError("Permission denied")

        self._invalidate_client(connection_id)
        return await self.db.delete_confluence_connection(connection_id)

    async def test_connection(self, connection_id: str, tenant_id: str) -> Dict[str, Any]:
        """测试连接"""
        try:
            connection = await self.db.get_confluence_connection(connection_id)
            if not connection:
                return {"status": "error", "message": "Connection not found"}

            if connection["tenant_id"] and connection["tenant_id"] != tenant_id:
                return {"status": "error", "message": "Access denied"}

            client = await self._get_client(connection_id)
            result = await client.test_connection()

            # 如果成功，获取一些空间信息
            if result["status"] == "success":
                spaces = await client.list_spaces(limit=5)
                result["spaces_count"] = len(spaces)
                result["sample_spaces"] = [
                    {"key": s.space_key, "name": s.name}
                    for s in spaces[:3]
                ]

            return result
        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def list_connections(
        self,
        user: UserContext,
        tenant_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        列出连接

        Args:
            user: 用户上下文
            tenant_id: 租户 ID（可选）
            status: 状态过滤

        Returns:
            用户有权访问的连接列表（admin 可见全部，普通用户仅可见自己创建的）
        """
        connections = await self.db.list_confluence_connections(tenant_id=tenant_id, status=status)

        # Admin 可见全部
        if user.tier == "admin":
            return connections

        # 普通用户只能看到自己创建的
        return [
            c for c in connections
            if (c.get("owner_id") or c.get("created_by")) == user.user_id
        ]

    async def get_connection(
        self,
        connection_id: str,
        user: UserContext,
    ) -> Optional[Dict[str, Any]]:
        """
        获取连接详情

        Args:
            connection_id: 连接 ID
            user: 用户上下文

        Returns:
            连接详情

        Raises:
            ConfluenceAccessDeniedError: 访问被拒绝
        """
        return await self._verify_connection_access(connection_id, user)

    # ============ Space Discovery ============

    async def discover_spaces(
        self,
        connection_id: str,
        tenant_id: str,
        type_filter: Optional[str] = None,
    ) -> List["ConfluenceSpace"]:
        """发现可用的空间"""
        connection = await self.db.get_confluence_connection(connection_id)
        if not connection:
            raise ConfluenceSyncError(f"Connection not found: {connection_id}")

        if connection["tenant_id"] and connection["tenant_id"] != tenant_id:
            raise ConfluenceSyncError("Access denied")

        client = await self._get_client(connection_id)
        return await client.list_spaces(type_filter=type_filter, limit=100)

    async def discover_pages(
        self,
        connection_id: str,
        space_key: str,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """预览空间中的页面"""
        client = await self._get_client(connection_id)
        space = await client.get_space_by_key(space_key)

        pages = []
        count = 0
        async for page_data in client.iter_space_pages(space.space_id, batch_size=25):
            pages.append({
                "page_id": page_data.get("id"),
                "title": page_data.get("title"),
                "status": page_data.get("status"),
                "version": page_data.get("version", {}).get("number"),
            })
            count += 1
            if count >= limit:
                break

        return pages

    async def discover_space_page_tree(
        self,
        connection_id: str,
        tenant_id: str,
        space_key: str,
        max_depth: int = 3,
    ) -> Dict[str, Any]:
        """
        发现空间中的页面层级结构

        Args:
            connection_id: 连接 ID
            tenant_id: 租户 ID
            space_key: 空间 Key
            max_depth: 最大深度

        Returns:
            页面树结构
        """
        connection = await self.db.get_confluence_connection(connection_id)
        if not connection:
            raise ConfluenceSyncError(f"Connection not found: {connection_id}")

        if connection["tenant_id"] and connection["tenant_id"] != tenant_id:
            raise ConfluenceSyncError("Access denied")

        client = await self._get_client(connection_id)
        space = await client.get_space_by_key(space_key)

        # 获取 domain 用于构建完整 URL
        domain = connection["domain"]

        # 获取所有页面
        all_pages: Dict[str, Dict[str, Any]] = {}
        async for page_data in client.iter_space_pages(space.space_id, batch_size=50):
            page_id = str(page_data.get("id"))
            # 构建完整的 web_url（API 返回的是相对路径）
            webui_path = page_data.get("_links", {}).get("webui")
            web_url = f"https://{domain}{webui_path}" if webui_path else None
            all_pages[page_id] = {
                "page_id": page_id,
                "title": page_data.get("title", ""),
                "parent_id": page_data.get("parentId"),
                "has_children": False,
                "children": [],
                "depth": 0,
                "web_url": web_url,
            }

        # 构建层级关系
        root_pages = []
        for page_id, page in all_pages.items():
            parent_id = page.get("parent_id")
            if parent_id and parent_id in all_pages:
                parent = all_pages[parent_id]
                parent["has_children"] = True
                parent["children"].append(page)
            else:
                root_pages.append(page)

        # 计算深度并裁剪
        def set_depth_and_trim(node: Dict[str, Any], depth: int) -> None:
            node["depth"] = depth
            if depth >= max_depth:
                # 当裁剪子节点时，标记有更多子节点被截断
                if node["children"]:
                    node["children_truncated"] = True
                node["children"] = []
                # 如果原来没有子节点，设置 has_children 为 False
                if not node.get("has_children"):
                    node["has_children"] = False
                return
            for child in node["children"]:
                set_depth_and_trim(child, depth + 1)

        for root in root_pages:
            set_depth_and_trim(root, 0)

        # 按标题排序
        def sort_children(node: Dict[str, Any]) -> None:
            node["children"].sort(key=lambda x: x.get("title", ""))
            for child in node["children"]:
                sort_children(child)

        root_pages.sort(key=lambda x: x.get("title", ""))
        for root in root_pages:
            sort_children(root)

        return {
            "space_key": space_key,
            "space_name": space.name,
            "root_pages": root_pages,
            "total_pages": len(all_pages),
        }

    # ============ URL Import ============

    async def import_from_url(
        self,
        url: str,
        dataset_id: str,
        connection_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        created_by: Optional[str] = None,
        include_children: bool = False,
        max_depth: int = 1,
    ) -> Dict[str, Any]:
        """
        从 Confluence URL 导入页面

        Args:
            url: Confluence 页面 URL
            dataset_id: 目标知识库 ID
            connection_id: 连接 ID（可选，自动匹配）
            tenant_id: 租户 ID
            metadata: 额外元数据
            created_by: 创建者 ID
            include_children: 是否包含子页面
            max_depth: 子页面深度限制

        Returns:
            导入结果
        """
        # 解析 URL 获取 domain
        domain = ConfluenceClient.parse_domain_from_url(url)

        # 获取或匹配 connection
        if connection_id:
            client = await self._get_client(connection_id)
        else:
            connection = await self.db.find_confluence_connection_by_domain(
                domain=domain,
                tenant_id=tenant_id,
            )
            if not connection:
                raise ConfluenceSyncError(
                    f"No connection found for domain: {domain}. "
                    "Please create a Confluence connection first."
                )
            connection_id = connection["connection_id"]
            client = await self._get_client(connection_id)

        # 获取页面
        try:
            page = await client.get_page_by_url(url)
        except ConfluenceAPIError as e:
            raise ConfluenceSyncError(f"Failed to get page: {e}")

        # 创建文档
        doc = await self._create_document_from_page(
            dataset_id=dataset_id,
            connection_id=connection_id,
            page=page,
            created_by=created_by,
            extra_metadata=metadata,
        )

        # 入队处理
        if self.worker:
            await self.worker.enqueue(dataset_id, doc["document_id"])

        result = {
            "status": "success",
            "document_id": doc["document_id"],
            "title": doc.get("title"),
            "page_info": {
                "page_id": page.page_id,
                "title": page.title,
                "version": page.version,
                "space_key": page.space_key,
            },
        }

        # 递归处理子页面
        if include_children and max_depth > 0:
            children = await client.get_page_children(page.page_id)
            child_docs = []
            for child in children[:20]:  # 限制子页面数量
                try:
                    child_page = await client.get_page(child["id"])
                    child_doc = await self._create_document_from_page(
                        dataset_id=dataset_id,
                        connection_id=connection_id,
                        page=child_page,
                        created_by=created_by,
                    )
                    if self.worker:
                        await self.worker.enqueue(dataset_id, child_doc["document_id"])
                    child_docs.append({"document_id": child_doc["document_id"], "title": child_doc.get("title")})
                except Exception as e:
                    logger.warning(f"Failed to import child page {child.get('id')}: {e}")

            result["children"] = child_docs
            result["children_count"] = len(child_docs)

        return result

    async def _create_document_from_page(
        self,
        dataset_id: str,
        connection_id: str,
        page: ConfluencePage,
        binding_id: Optional[str] = None,
        created_by: Optional[str] = None,
        extra_metadata: Optional[Dict[str, Any]] = None,
        sync_images: bool = True,
        tenant_id: Optional[str] = None,
        image_max_size_bytes: Optional[int] = None,
    ) -> Dict[str, Any]:
        """从 Confluence 页面创建文档"""
        # 检查 body_storage 是否为空
        if not page.body_storage or not page.body_storage.strip():
            logger.warning(
                f"Creating document from Confluence page with EMPTY body_storage! "
                f"Page ID: {page.page_id}, Title: {page.title}. "
                f"This will cause 'empty document' error during ingestion."
            )

        # 转换内容
        content_text = extract_plain_text(page.body_storage)
        content_markdown = extract_markdown(page.body_storage)

        # 日志记录内容长度
        logger.info(
            f"Document content extraction for page {page.page_id}: "
            f"body_storage={len(page.body_storage or '')}, "
            f"content_text={len(content_text)}, "
            f"content_markdown={len(content_markdown)}"
        )

        doc_id = str(uuid.uuid4())
        metadata = {
            "confluence_page_id": page.page_id,
            "confluence_space_key": page.space_key,
            "confluence_version": page.version,
            "confluence_labels": page.labels,
            "confluence_author": page.author_id,
            "confluence_updated_at": page.updated_at,
            "markdown_content": content_markdown,
        }
        # 合并额外元数据
        if extra_metadata:
            metadata.update(extra_metadata)

        doc = {
            "document_id": doc_id,
            "dataset_id": dataset_id,
            "title": page.title,
            "source_type": "confluence",
            "source_uri": page.web_url or f"confluence://{page.page_id}",
            "mime_type": "text/plain",
            "size_bytes": len(content_text.encode("utf-8")),
            "status": "uploaded",
            "progress": 0,
            "content": content_text,
            "metadata": metadata,
            # Confluence 专属字段
            "confluence_page_id": page.page_id,
            "confluence_binding_id": binding_id,
            "confluence_version": page.version,
            "confluence_web_url": page.web_url,
            "created_by": created_by,
        }

        await self.db.save_document(doc)

        # 记录同步状态
        if binding_id:
            await self.db.upsert_confluence_page(
                binding_id=binding_id,
                page_id=page.page_id,
                document_id=doc_id,
                space_key=page.space_key,
                title=page.title,
                version=page.version,
                content_hash=page.content_hash,
                parent_page_id=page.parent_id,
                labels=page.labels,
                web_url=page.web_url,
                author=page.author_id,
                confluence_updated_at=page.updated_at,
            )

        # 处理图片（如果启用）
        if sync_images:
            if not tenant_id:
                logger.warning(
                    f"Image sync enabled but tenant_id is missing for page {page.page_id}, "
                    "skipping image processing"
                )
            else:
                # 获取或创建 image_processor（使用 binding 配置的图片大小限制）
                img_processor = await self._get_image_processor(
                    connection_id, max_image_size=image_max_size_bytes
                )
                if not img_processor:
                    logger.debug(
                        f"Image processor not available for page {page.page_id}, "
                        "skipping image processing"
                    )
                else:
                    try:
                        image_result = await img_processor.process_page_images(
                            page_id=page.page_id,
                            document_id=doc_id,
                            tenant_id=tenant_id,
                            page_content=page.body_storage,
                            page_title=page.title,
                        )
                        if image_result.processed_images > 0:
                            vlm_count = sum(1 for s in image_result.segments if s.vlm_description)
                            logger.info(
                                f"Processed {image_result.processed_images} images for page {page.page_id} "
                                f"(vlm_descriptions={vlm_count})"
                            )
                            # Store image segments in database
                            for idx, segment in enumerate(image_result.segments):
                                await self._save_image_segment(segment, dataset_id, binding_id, position=idx)
                            doc["image_count"] = image_result.processed_images

                            # 修复：更新 confluence_pages 表的 image_count
                            if binding_id:
                                await self.db.update_confluence_page_image_count(
                                    binding_id, page.page_id, image_result.processed_images
                                )
                        if image_result.errors:
                            logger.warning(
                                f"Image processing errors for page {page.page_id}: {image_result.errors}"
                            )
                    except Exception as e:
                        logger.error(f"Failed to process images for page {page.page_id}: {e}")
                        # Don't fail the document sync for image errors

        return doc

    async def _save_image_segment(
        self,
        segment: ImageSegment,
        dataset_id: str,
        binding_id: Optional[str] = None,
        position: int = 0,
    ) -> None:
        """
        保存图片段到数据库并存入向量库

        Text-First RAG 架构:
        - 使用 VLM 描述作为文本内容进行 embedding
        - 统一使用 dataset 配置的 embedding provider (Gemini/DashScope)
        - 图片通过 S3 URL 存储，检索时返回 presigned URL
        """
        try:
            # Build text content from VLM description and context
            # VLM description is the primary searchable text for RAG
            text_content = ""
            if segment.vlm_description:
                text_content = segment.vlm_description
            if segment.context_text:
                if text_content:
                    text_content = f"{text_content}\n\n---\n上下文：{segment.context_text}"
                else:
                    text_content = segment.context_text

            # Generate text embedding and store in Qdrant for RAG search
            vector_id = None
            if text_content and self.knowledge_service:
                try:
                    # Get dataset info for collection name and embedding config
                    dataset = await self.db.get_dataset(dataset_id)
                    if dataset:
                        collection = dataset.get("collection_name")
                        if collection:
                            # Create embedder using dataset's embedding config
                            from ..embedding import create_embedding

                            embedding_provider = str(dataset.get("embedding_provider") or "gemini")
                            embedding_model = str(dataset.get("embedding_model") or "gemini-embedding-001")
                            embedding_config = dataset.get("embedding_config") or {}
                            if not isinstance(embedding_config, dict):
                                embedding_config = {}

                            econf = self.knowledge_service._resolve_embedding_config(
                                provider=embedding_provider,
                                model=embedding_model,
                                embedding_config=embedding_config,
                            )

                            collection_dim = int(dataset.get("embedding_dimension") or 1024)
                            vector = None

                            # Text-First RAG: Always use text embedding for VLM descriptions
                            embedder = create_embedding(
                                econf, dimension=collection_dim or None
                            )
                            try:
                                # For Gemini, use text_type="document" for optimal retrieval
                                if hasattr(embedder, 'embed_texts'):
                                    vectors = await embedder.embed_texts(
                                        [text_content], text_type="document"
                                    )
                                else:
                                    vectors = await embedder.embed_documents([text_content])
                                if vectors and len(vectors) > 0:
                                    vector = vectors[0]
                            finally:
                                await embedder.close()

                            if vector:
                                vector_id = segment.segment_id

                                # Import qdrant models
                                from qdrant_client import models as qmodels

                                # Prepare payload for Qdrant
                                payload = {
                                    "dataset_id": dataset_id,
                                    "document_id": segment.document_id,
                                    "segment_id": segment.segment_id,
                                    "text": text_content,
                                    "content_type": "image",
                                    "image_filename": segment.filename,
                                    "image_url": segment.storage_url,
                                    "vlm_description": segment.vlm_description,  # VLM-generated image description
                                }

                                # Upsert to Qdrant
                                await self.knowledge_service.vector_store.upsert(
                                    collection_name=collection,
                                    points=[qmodels.PointStruct(
                                        id=vector_id,
                                        vector=vector,
                                        payload=payload,
                                    )],
                                )
                                logger.info(
                                    f"Stored image vector in Qdrant: {segment.filename} "
                                    f"(collection={collection}, provider={embedding_provider}, "
                                    f"text_len={len(text_content) if text_content else 0})"
                                )
                except Exception as vec_err:
                    logger.warning(f"Failed to store image vector: {vec_err}")
                    # Continue to save segment to database even if vector storage fails

            metadata = {
                **segment.metadata,
                "has_vlm_description": bool(segment.vlm_description),
                "vlm_description_length": len(segment.vlm_description) if segment.vlm_description else 0,
                "vector_stored": vector_id is not None,
                "source_position": position,
            }
            if segment.context_text and "context_text" not in metadata:
                metadata["context_text"] = segment.context_text

            segment_data = {
                "segment_id": segment.segment_id,
                "document_id": segment.document_id,
                "dataset_id": dataset_id,
                "position": 100000 + position,  # Offset to avoid collision with text segments
                "content_type": "image",
                "image_url": segment.storage_url,
                "image_attachment_id": segment.attachment_id,
                "image_filename": segment.filename,
                "image_media_type": segment.media_type,
                "image_file_size": segment.file_size,
                "text": text_content,  # Contains VLM description for RAG search
                "vector_id": vector_id or segment.vector_id,
                "metadata": metadata,
            }
            await self.db.save_image_segment(segment_data)
            logger.debug(
                f"Saved image segment {segment.segment_id} "
                f"(vlm={'yes' if segment.vlm_description else 'no'}, vector={vector_id is not None})"
            )
        except Exception as e:
            logger.error(f"Failed to save image segment {segment.segment_id}: {e}")

    async def _check_image_updates_needed(
        self,
        document_id: str,
        connection_id: str,
        page_id: str,
    ) -> bool:
        """
        检查页面图片是否需要更新

        通过比较 Confluence 附件的更新时间与已存储段落的元数据来判断。

        Args:
            document_id: 文档 ID
            connection_id: 连接 ID
            page_id: Confluence 页面 ID

        Returns:
            是否需要重新处理图片
        """
        try:
            # 获取已存储的图片段
            existing_segments = await self.db.get_image_segments_by_document(document_id)
            if not existing_segments:
                # 没有已存储的图片，需要处理
                return True

            # 获取 Confluence 当前附件
            client = await self._get_client(connection_id)
            current_attachments = await client.get_page_image_attachments(
                page_id=page_id,
                embeddable_only=True
            )

            if not current_attachments:
                # 没有附件但有存储的段落，需要清理
                return True

            # 构建现有段落的附件ID映射
            existing_map = {
                seg.get("image_attachment_id"): seg
                for seg in existing_segments
                if seg.get("image_attachment_id")
            }

            # 检查是否有新增或更新的附件
            for attachment in current_attachments:
                existing_seg = existing_map.get(attachment.attachment_id)

                if not existing_seg:
                    # 新附件
                    logger.debug(f"New attachment detected: {attachment.filename}")
                    return True

                # 检查更新时间
                # 修复：metadata 可能是 JSON 字符串（数据库返回），需要安全处理
                metadata = existing_seg.get("metadata", {})
                if isinstance(metadata, str):
                    try:
                        metadata = json.loads(metadata)
                    except (json.JSONDecodeError, TypeError):
                        metadata = {}
                if not isinstance(metadata, dict):
                    metadata = {}

                stored_updated_at = metadata.get("attachment_updated_at")
                if stored_updated_at and attachment.updated_at:
                    if attachment.updated_at != stored_updated_at:
                        logger.debug(
                            f"Attachment updated: {attachment.filename} "
                            f"({stored_updated_at} -> {attachment.updated_at})"
                        )
                        return True

            # 检查是否有删除的附件
            current_ids = {a.attachment_id for a in current_attachments}
            for existing_id in existing_map:
                if existing_id not in current_ids:
                    logger.debug(f"Attachment removed: {existing_id}")
                    return True

            return False

        except Exception as e:
            logger.warning(f"Failed to check image updates: {e}, will reprocess")
            return True

    async def _reprocess_document_images(
        self,
        document_id: str,
        connection_id: str,
        page: "ConfluencePage",
        dataset_id: str,
        tenant_id: str,
        binding_id: Optional[str] = None,
        image_max_size_bytes: Optional[int] = None,
    ) -> int:
        """
        重新处理文档的图片（先删除旧图片）

        Args:
            document_id: 文档 ID
            connection_id: 连接 ID
            page: Confluence 页面
            dataset_id: 数据集 ID
            tenant_id: 租户 ID
            binding_id: 绑定 ID
            image_max_size_bytes: 图片最大大小限制（字节）

        Returns:
            处理的图片数量
        """
        img_processor = await self._get_image_processor(
            connection_id, max_image_size=image_max_size_bytes
        )
        if not img_processor:
            return 0

        try:
            # 先删除存储中的旧图片（S3/OSS）
            if self._image_storage_service and tenant_id:
                try:
                    deleted_count = await self._image_storage_service.delete_document_images(
                        tenant_id=tenant_id,
                        document_id=document_id,
                    )
                    if deleted_count > 0:
                        logger.info(
                            f"Deleted {deleted_count} old images from storage for document {document_id}"
                        )
                except Exception as storage_err:
                    logger.warning(f"Failed to delete old images from storage: {storage_err}")

            # 删除旧的图片段（数据库）
            await self.db.delete_image_segments_by_document(document_id)

            # 处理新图片
            image_result = await img_processor.process_page_images(
                page_id=page.page_id,
                document_id=document_id,
                tenant_id=tenant_id,
                page_content=page.body_storage,
                page_title=page.title,
            )

            if image_result.processed_images > 0:
                vlm_count = sum(1 for s in image_result.segments if s.vlm_description)
                logger.info(
                    f"Reprocessed {image_result.processed_images} images for page {page.page_id} "
                    f"(vlm_descriptions={vlm_count})"
                )
                # 保存新的图片段
                for idx, segment in enumerate(image_result.segments):
                    await self._save_image_segment(segment, dataset_id, binding_id, position=idx)

            # Update document segment_count after reprocessing images
            await self.db.refresh_document_segment_count(document_id)

            return image_result.processed_images

        except Exception as e:
            logger.error(f"Failed to reprocess images for page {page.page_id}: {e}")
            return 0

    # ============ Space Binding ============

    async def create_space_binding(
        self,
        connection_id: str,
        tenant_id: str,
        dataset_id: str,
        space_key: str,
        root_page_ids: Optional[List[str]] = None,
        include_patterns: Optional[List[str]] = None,
        exclude_patterns: Optional[List[str]] = None,
        max_depth: int = 10,
        include_attachments: bool = False,
        include_comments: bool = False,
        sync_images: bool = True,
        image_max_size_bytes: int = 3 * 1024 * 1024,  # 3 MB
        created_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        创建空间绑定

        Args:
            connection_id: 连接 ID
            tenant_id: 租户 ID
            dataset_id: 数据集 ID
            space_key: Confluence Space Key
            root_page_ids: 根页面 ID 列表（可选，支持多选，如果指定则只同步这些页面及其子页面）
            include_patterns: 包含规则
            exclude_patterns: 排除规则
            max_depth: 最大深度
            include_attachments: 是否包含附件
            include_comments: 是否包含评论
            sync_images: 是否同步并嵌入图片（需要多模态嵌入支持）
            image_max_size_bytes: 最大图片大小（默认 3 MB）
            created_by: 创建者 ID

        Returns:
            绑定信息
        """
        # 验证连接
        connection = await self.db.get_confluence_connection(connection_id)
        if not connection:
            raise ConfluenceSyncError(f"Connection not found: {connection_id}")

        # 检查租户权限
        if connection["tenant_id"] and connection["tenant_id"] != tenant_id:
            raise ConfluenceSyncError("Access denied: connection belongs to different tenant")

        # 检查是否已存在相同的绑定 (connection_id, space_key, dataset_id)
        existing_bindings = await self.db.list_confluence_bindings(
            connection_id=connection_id,
            dataset_id=dataset_id,
        )
        for binding in existing_bindings:
            if binding.get("space_key") == space_key:
                raise ConfluenceSyncError(
                    f"Binding already exists for space '{space_key}' and dataset '{dataset_id}'"
                )

        # 获取空间信息
        client = await self._get_client(connection_id)
        space = await client.get_space_by_key(space_key)

        # 处理根页面 IDs（支持多选）
        effective_root_page_ids = root_page_ids or []
        root_page_titles = []

        # 获取每个根页面的标题（保持与 IDs 数组对齐）
        for page_id in effective_root_page_ids:
            try:
                root_page = await client.get_page(page_id)
                root_page_titles.append(root_page.title)
            except Exception as e:
                logger.warning(f"Failed to get root page {page_id}: {e}")
                # 使用 page_id 作为后备显示名，保持数组长度一致
                root_page_titles.append(f"Page {page_id}")

        # 向后兼容：root_page_id 使用第一个元素（如果有）
        root_page_id = effective_root_page_ids[0] if effective_root_page_ids else None
        root_page_title = root_page_titles[0] if root_page_titles and root_page_titles[0] else None

        binding_id = str(uuid.uuid4())
        binding = {
            "binding_id": binding_id,
            "connection_id": connection_id,
            "tenant_id": tenant_id,
            "dataset_id": dataset_id,
            "space_key": space_key,
            "space_id": space.space_id,
            "space_name": space.name,
            "root_page_id": root_page_id,  # Backward compatibility
            "root_page_ids": effective_root_page_ids,  # New multi-select field
            "root_page_title": root_page_title,  # Backward compatibility
            "root_page_titles": root_page_titles,  # New multi-select field (aligned with root_page_ids)
            "include_patterns": include_patterns or [],
            "exclude_patterns": exclude_patterns or [],
            "max_depth": max_depth,
            "include_attachments": include_attachments,
            "include_comments": include_comments,
            "sync_images": sync_images,
            "image_max_size_bytes": image_max_size_bytes,
            "status": "pending",
            "owner_id": created_by,  # ACL: 设置资源所有者
            "created_by": created_by,
        }

        # 使用 RETURNING 在单个事务中保存并返回，确保原子性
        result = await self.db.save_confluence_binding(binding)
        if not result:
            raise ConfluenceSyncError("Failed to save binding")

        # 创建绑定后自动触发首次同步
        try:
            task_id = await self.trigger_sync(binding_id, force=False)
            logger.info(f"Auto-triggered initial sync for binding {binding_id}, task_id: {task_id}")
            result["initial_sync_task_id"] = task_id
        except Exception as e:
            # 同步失败不影响绑定创建，只记录警告
            logger.warning(f"Failed to auto-trigger sync for binding {binding_id}: {e}")

        return result

    async def delete_binding(
        self,
        binding_id: str,
        delete_documents: bool = False,
    ) -> bool:
        """
        删除空间绑定

        Args:
            binding_id: 绑定 ID
            delete_documents: 是否同时删除已导入的文档

        Returns:
            是否删除成功
        """
        binding = await self.db.get_confluence_binding(binding_id)
        if not binding:
            return False

        # 如果需要删除文档
        if delete_documents:
            pages = await self.db.list_confluence_pages(binding_id)
            for page in pages:
                doc_id = page.get("document_id")
                if doc_id:
                    try:
                        await self.knowledge_service.delete_document(
                            dataset_id=binding["dataset_id"],
                            document_id=doc_id,
                        )
                    except Exception as e:
                        logger.warning(f"Failed to delete document {doc_id}: {e}")

        return await self.db.delete_confluence_binding(binding_id)

    async def list_bindings(
        self,
        user: UserContext,
        connection_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        dataset_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        列出空间绑定

        Args:
            user: 用户上下文
            connection_id: 连接 ID（可选）
            tenant_id: 租户 ID（可选）
            dataset_id: 数据集 ID（可选）

        Returns:
            用户有权访问的绑定列表
        """
        bindings = await self.db.list_confluence_bindings(
            connection_id=connection_id,
            tenant_id=tenant_id,
            dataset_id=dataset_id,
        )

        # Admin 可见全部
        if user.tier == "admin":
            return bindings

        # 普通用户只能看到自己创建的
        return [
            b for b in bindings
            if (b.get("owner_id") or b.get("created_by")) == user.user_id
        ]

    async def list_all_bindings(
        self,
        user: UserContext,
        tenant_id: str,
        connection_id: Optional[str] = None,
        dataset_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        列出租户下所有空间绑定（带过滤）

        Args:
            user: 用户上下文
            tenant_id: 租户 ID
            connection_id: 连接 ID（可选）
            dataset_id: 数据集 ID（可选）
            status: 状态过滤（可选）

        Returns:
            用户有权访问的绑定列表
        """
        bindings = await self.db.list_confluence_bindings(
            connection_id=connection_id,
            tenant_id=tenant_id,
            dataset_id=dataset_id,
        )

        # Admin 可见全部，普通用户只能看到自己创建的
        if user.tier != "admin":
            bindings = [
                b for b in bindings
                if (b.get("owner_id") or b.get("created_by")) == user.user_id
            ]

        # Filter by status if specified
        if status:
            bindings = [b for b in bindings if b.get("status") == status]
        return bindings

    async def get_binding(
        self,
        binding_id: str,
        user: UserContext,
    ) -> Optional[Dict[str, Any]]:
        """
        获取空间绑定详情

        Args:
            binding_id: 绑定 ID
            user: 用户上下文

        Returns:
            绑定详情

        Raises:
            ConfluenceAccessDeniedError: 访问被拒绝
        """
        return await self._verify_binding_access(binding_id, user)

    async def refresh_binding_stats(self, binding_id: str) -> None:
        """
        刷新绑定的页面统计信息

        Args:
            binding_id: 绑定 ID
        """
        pages = await self.db.list_confluence_pages(
            binding_id=binding_id,
            limit=10000,  # 获取所有页面
        )

        total_count = len(pages)
        synced_count = sum(1 for p in pages if p.get("status") == "synced")

        await self.db.update_confluence_binding(binding_id, {
            "total_page_count": total_count,
            "synced_page_count": synced_count,
        })

    async def update_binding(
        self,
        binding_id: str,
        **updates,
    ) -> Optional[Dict[str, Any]]:
        """
        更新空间绑定配置

        Args:
            binding_id: 绑定 ID
            **updates: 要更新的字段

        Returns:
            更新后的绑定信息
        """
        binding = await self.db.get_confluence_binding(binding_id)
        if not binding:
            return None

        # 过滤允许更新的字段
        allowed_fields = {
            "root_page_id", "root_page_ids", "root_page_titles",  # 支持多选根页面
            "include_patterns", "exclude_patterns", "max_depth",
            "include_attachments", "include_comments", "status",
            "sync_images", "image_max_size_bytes",  # 图片同步配置
            "sync_mode", "polling_interval_minutes", "sync_enabled",  # 同步模式配置 (binding 级别)
        }
        filtered_updates = {k: v for k, v in updates.items() if k in allowed_fields}

        # 如果更新了 root_page_ids，需要同步更新 titles 和兼容字段
        if "root_page_ids" in filtered_updates:
            raw_ids = filtered_updates.get("root_page_ids") or []
            root_page_ids = raw_ids if isinstance(raw_ids, list) else [raw_ids]
            root_page_titles: List[str] = []
            if root_page_ids:
                connection_id = binding["connection_id"]
                client = await self._get_client(connection_id)
                for page_id in root_page_ids:
                    try:
                        root_page = await client.get_page(page_id)
                        root_page_titles.append(root_page.title)
                    except Exception as e:
                        logger.warning(f"Failed to get root page {page_id}: {e}")
                        root_page_titles.append(f"Page {page_id}")
            filtered_updates["root_page_ids"] = root_page_ids
            filtered_updates["root_page_titles"] = root_page_titles
            filtered_updates["root_page_id"] = root_page_ids[0] if root_page_ids else None
            filtered_updates["root_page_title"] = root_page_titles[0] if root_page_titles else None

        # 如果仅更新了 root_page_id（兼容老接口），补齐列表字段
        if "root_page_id" in filtered_updates and "root_page_ids" not in filtered_updates:
            root_page_id = filtered_updates["root_page_id"]
            if root_page_id:
                try:
                    connection_id = binding["connection_id"]
                    client = await self._get_client(connection_id)
                    root_page = await client.get_page(root_page_id)
                    root_title = root_page.title
                except Exception as e:
                    logger.warning(f"Failed to get root page {root_page_id}: {e}")
                    root_title = f"Page {root_page_id}"
                filtered_updates["root_page_title"] = root_title
                filtered_updates["root_page_ids"] = [root_page_id]
                filtered_updates["root_page_titles"] = [root_title]
            else:
                filtered_updates["root_page_title"] = None
                filtered_updates["root_page_ids"] = []
                filtered_updates["root_page_titles"] = []

        if filtered_updates:
            await self.db.update_confluence_binding(binding_id, filtered_updates)

        return await self.db.get_confluence_binding(binding_id)

    # ============ Space Import ============

    async def _get_page_descendants(
        self,
        client: ConfluenceClient,
        root_page_id: str,
        max_depth: int,
    ) -> List[str]:
        """
        递归获取页面及其所有子页面的 ID 列表

        Args:
            client: Confluence 客户端
            root_page_id: 根页面 ID
            max_depth: 最大深度

        Returns:
            页面 ID 列表
        """
        result = [root_page_id]

        async def collect_children(page_id: str, depth: int) -> None:
            if depth >= max_depth:
                return
            try:
                children = await client.get_page_children(page_id, limit=100)
                for child in children:
                    child_id = str(child.get("id"))
                    result.append(child_id)
                    await collect_children(child_id, depth + 1)
            except Exception as e:
                logger.warning(f"Failed to get children for page {page_id}: {e}")

        await collect_children(root_page_id, 0)
        return result

    async def import_space(
        self,
        binding_id: str,
        force_full_sync: bool = False,
    ) -> SyncResult:
        """
        导入整个空间

        Args:
            binding_id: 空间绑定 ID
            force_full_sync: 是否强制全量同步

        Returns:
            同步结果
        """
        binding = await self.db.get_confluence_binding(binding_id)
        if not binding:
            raise ConfluenceSyncError(f"Binding not found: {binding_id}")

        # 检查是否已有同步在进行
        if binding.get("status") == "syncing" and not force_full_sync:
            return SyncResult(
                started_at=_utc_now(),
                completed_at=_utc_now(),
                errors=[{"error": "Sync already in progress"}],
            )

        # 创建同步任务（继承 binding 的 owner_id 用于 ACL）
        task_id = str(uuid.uuid4())
        await self.db.create_confluence_sync_task(
            task_id=task_id,
            binding_id=binding_id,
            task_type="full_sync",
            priority=0,
            owner_id=binding.get("owner_id") or binding.get("created_by"),
        )

        # 更新绑定状态
        await self.db.update_confluence_binding(binding_id, {"status": "syncing"})

        # 启动后台同步
        self._create_background_task(
            self._sync_space_pages(binding_id, task_id),
            name=f"sync-space-{binding_id[:8]}"
        )

        return SyncResult(
            started_at=_utc_now(),
            task_id=task_id,
        )

    async def _sync_space_pages(
        self,
        binding_id: str,
        task_id: str,
    ) -> SyncResult:
        """同步空间中的所有页面（或指定根页面的子页面）"""
        result = SyncResult(started_at=_utc_now())

        binding = await self.db.get_confluence_binding(binding_id)
        if not binding:
            logger.error(f"Binding not found: {binding_id}")
            return result

        connection_id = binding["connection_id"]
        dataset_id = binding["dataset_id"]
        space_id = binding["space_id"]
        max_depth = binding.get("max_depth", 10)

        # 支持多选：优先使用 root_page_ids，向后兼容 root_page_id
        root_page_ids = binding.get("root_page_ids") or []
        if not root_page_ids and binding.get("root_page_id"):
            root_page_ids = [binding.get("root_page_id")]

        try:
            client = await self._get_client(connection_id)

            # 获取所有现有页面记录
            existing_pages = await self.db.list_confluence_pages(binding_id)
            existing_page_ids = {p["page_id"] for p in existing_pages}
            seen_page_ids = set()

            # 如果指定了 root_page_ids，则只同步这些页面及其子页面
            if root_page_ids:
                pages_to_sync = []
                for rid in root_page_ids:
                    try:
                        descendants = await self._get_page_descendants(
                            client, rid, max_depth
                        )
                        pages_to_sync.extend(descendants)
                    except Exception as e:
                        logger.warning(f"Failed to get descendants for root page {rid}: {e}")
                # 去重（不同根页面可能有重叠的子页面）
                pages_to_sync = list(dict.fromkeys(pages_to_sync))
            else:
                # 白名单模式：只同步已添加到 confluence_pages 表中的页面
                # 不再自动拉取整个空间的所有页面，避免同步用户不需要的内容
                pages_to_sync = list(existing_page_ids)

                if not pages_to_sync:
                    # 没有已添加的页面，直接返回成功
                    logger.info(f"No pages added to binding {binding_id}, nothing to sync")
                    result.completed_at = _utc_now()
                    await self.db.update_confluence_binding(binding_id, {
                        "status": "completed",
                        "last_sync_at": _utc_now(),
                    })
                    await self.db.update_confluence_sync_task(task_id, {
                        "status": "completed",
                        "progress": 100,
                        "completed_at": _utc_now(),
                        "result": result.to_dict(),
                    })
                    return result

            total_count = len(pages_to_sync)
            result.total_pages = total_count

            await self.db.update_confluence_sync_task(task_id, {
                "status": "processing",
                "total_items": total_count,
                "started_at": _utc_now(),
            })

            # 遍历并同步页面
            processed = 0
            for page_id in pages_to_sync:
                seen_page_ids.add(page_id)
                processed += 1

                try:
                    # 获取完整页面内容
                    page = await client.get_page(page_id)

                    # 检查是否需要更新
                    existing = await self.db.get_confluence_page_by_page_id(binding_id, page_id)

                    if existing:
                        # 检查内容是否变化
                        content_unchanged = existing.get("content_hash") == page.content_hash

                        if content_unchanged:
                            # 内容没变，但检查是否需要处理图片
                            doc_id = existing.get("document_id")
                            sync_images_needed = binding.get("sync_images", False)

                            if sync_images_needed and doc_id:
                                # 使用完整的图片更新检查（检查新增、更新、删除的附件）
                                try:
                                    image_updates_needed = await self._check_image_updates_needed(
                                        document_id=doc_id,
                                        connection_id=connection_id,
                                        page_id=page_id,
                                    )
                                    if image_updates_needed:
                                        logger.info(
                                            f"Page {page_id} content unchanged but image updates detected, "
                                            f"reprocessing images..."
                                        )
                                        tenant_id = binding.get("tenant_id")
                                        if not tenant_id:
                                            logger.warning(
                                                f"Binding {binding_id} has no tenant_id, skipping image reprocess"
                                            )
                                        else:
                                            await self._reprocess_document_images(
                                                document_id=doc_id,
                                                connection_id=connection_id,
                                                page=page,
                                                dataset_id=dataset_id,
                                                tenant_id=tenant_id,
                                                binding_id=binding_id,
                                                image_max_size_bytes=binding.get("image_max_size_bytes"),
                                            )
                                        result.synced_pages += 1
                                        continue
                                except Exception as img_err:
                                    logger.warning(f"Failed to check/process images: {img_err}")

                            result.skipped_pages += 1
                            continue

                        # 更新现有文档（内容变化时也需要重新处理图片）
                        doc_id = existing.get("document_id")
                        if doc_id:
                            # 保存版本快照（用于回滚）
                            await self._save_version_before_update(
                                doc_id, page.version,
                                f"Confluence sync: {page.title} (v{page.version})"
                            )

                            content_text = extract_plain_text(page.body_storage)
                            await self.db.update_document_fields(
                                doc_id,
                                {
                                    "content": content_text,
                                    "confluence_version": page.version,
                                    "status": "uploaded",
                                    "progress": 0,
                                }
                            )

                            # 内容变化时也重新处理图片（修复：之前只更新文本）
                            sync_images_enabled = binding.get("sync_images", False)
                            tenant_id = binding.get("tenant_id")
                            if sync_images_enabled and tenant_id:
                                try:
                                    image_count = await self._reprocess_document_images(
                                        document_id=doc_id,
                                        connection_id=connection_id,
                                        page=page,
                                        dataset_id=dataset_id,
                                        tenant_id=tenant_id,
                                        binding_id=binding_id,
                                        image_max_size_bytes=binding.get("image_max_size_bytes"),
                                    )
                                    if image_count > 0:
                                        # 更新 confluence_pages 表的 image_count
                                        await self.db.update_confluence_page_image_count(
                                            binding_id, page.page_id, image_count
                                        )
                                except Exception as img_err:
                                    logger.warning(
                                        f"Failed to reprocess images for updated page {page.page_id}: {img_err}"
                                    )

                            if self.worker:
                                await self.worker.enqueue(dataset_id, doc_id)
                            result.updated_documents.append(doc_id)
                    else:
                        # 创建新文档
                        doc = await self._create_document_from_page(
                            dataset_id=dataset_id,
                            connection_id=connection_id,
                            page=page,
                            binding_id=binding_id,
                            created_by=binding.get("created_by"),
                            sync_images=binding.get("sync_images", False),
                            tenant_id=binding.get("tenant_id"),
                            image_max_size_bytes=binding.get("image_max_size_bytes"),
                        )
                        if self.worker:
                            await self.worker.enqueue(dataset_id, doc["document_id"])
                        result.created_documents.append(doc["document_id"])

                    result.synced_pages += 1

                    # 更新进度
                    progress = (processed / total_count) * 100 if total_count > 0 else 0
                    await self.db.update_confluence_sync_task(task_id, {
                        "processed_items": processed,
                        "progress": progress,
                    })

                except Exception as e:
                    result.failed_pages += 1
                    result.errors.append({
                        "page_id": page_id,
                        "error": str(e),
                    })
                    logger.error(f"Failed to sync page {page_id}: {e}")

            # 处理删除的页面
            # 只在 root_page_ids 模式下自动删除（基于空间发现）
            # 白名单模式下跳过自动删除，避免因同步失败而误删页面
            if root_page_ids:
                deleted_page_ids = existing_page_ids - seen_page_ids
                for page_id in deleted_page_ids:
                    existing = await self.db.get_confluence_page_by_page_id(binding_id, page_id)
                    if existing and existing.get("document_id"):
                        doc_id = existing["document_id"]
                        try:
                            await self.knowledge_service.delete_document(
                                dataset_id=dataset_id,
                                document_id=doc_id,
                            )
                            result.deleted_documents.append(doc_id)
                        except Exception as e:
                            logger.warning(f"Failed to delete document {doc_id}: {e}")
                    await self.db.delete_confluence_page_by_page_id(binding_id, page_id)

            result.completed_at = _utc_now()

            # 更新绑定状态
            await self.db.update_confluence_binding(binding_id, {
                "status": "completed",
                "last_sync_at": _utc_now(),
                "synced_page_count": result.synced_pages,
                "total_page_count": result.total_pages,
            })

            # 更新任务状态
            await self.db.update_confluence_sync_task(task_id, {
                "status": "completed",
                "progress": 100,
                "completed_at": _utc_now(),
                "result": result.to_dict(),
            })

        except Exception as e:
            logger.exception(f"Space sync failed: {e}")
            result.errors.append({"error": str(e)})

            await self.db.update_confluence_binding(binding_id, {
                "status": "error",
                "last_error": str(e),
            })

            await self.db.update_confluence_sync_task(task_id, {
                "status": "failed",
                "error": str(e),
                "completed_at": _utc_now(),
            })

        return result

    # ============ Sync Status ============

    async def get_sync_status(
        self,
        binding_id: str,
        user: UserContext,
    ) -> Dict[str, Any]:
        """
        获取同步状态

        Args:
            binding_id: 绑定 ID
            user: 用户上下文

        Returns:
            同步状态信息

        Raises:
            ConfluenceAccessDeniedError: 访问被拒绝
        """
        # 验证访问权限
        binding = await self._verify_binding_access(binding_id, user)

        # 获取最新任务
        tasks = await self.db.list_confluence_sync_tasks(binding_id=binding_id, limit=1)
        current_task = tasks[0] if tasks else None

        # 获取页面统计
        pages = await self.db.list_confluence_pages(binding_id)
        synced = sum(1 for p in pages if p.get("status") == "synced")
        failed = sum(1 for p in pages if p.get("status") == "error")
        pending = sum(1 for p in pages if p.get("status") == "pending")

        return {
            "binding_id": binding_id,
            "status": binding["status"],
            "total_pages": binding.get("total_page_count", 0),
            "synced_pages": synced,
            "failed_pages": failed,
            "pending_pages": pending,
            "last_sync_at": binding.get("last_sync_at"),
            "current_task": {
                "task_id": current_task["task_id"],
                "status": current_task["status"],
                "progress": current_task.get("progress", 0),
                "started_at": current_task.get("started_at"),
            } if current_task else None,
        }

    # ============ Manual Sync ============

    async def trigger_sync(
        self,
        binding_id: str,
        force: bool = False,
        page_ids: Optional[List[str]] = None,
    ) -> str:
        """
        手动触发同步

        Args:
            binding_id: 绑定 ID
            force: 是否强制同步（忽略已在进行的同步）
            page_ids: 指定同步的页面 ID 列表（可选，为空则同步全部）

        Returns:
            任务 ID
        """
        binding = await self.db.get_confluence_binding(binding_id)
        if not binding:
            raise ConfluenceSyncError(f"Binding not found: {binding_id}")

        # 检查是否已有同步在进行
        if binding.get("status") == "syncing" and not force:
            raise ConfluenceSyncError("A sync is already in progress")

        # 创建同步任务（继承 binding 的 owner_id 用于 ACL）
        task_type = "partial_sync" if page_ids else "full_sync"
        task_id = str(uuid.uuid4())
        await self.db.create_confluence_sync_task(
            task_id=task_id,
            binding_id=binding_id,
            task_type=task_type,
            priority=1,
            owner_id=binding.get("owner_id") or binding.get("created_by"),
        )

        # 更新状态
        await self.db.update_confluence_binding(binding_id, {"status": "syncing"})

        # 启动同步
        if page_ids:
            self._create_background_task(
                self._sync_specific_pages(binding_id, task_id, page_ids),
                name=f"sync-pages-{binding_id[:8]}"
            )
        else:
            self._create_background_task(
                self._sync_space_pages(binding_id, task_id),
                name=f"trigger-sync-{binding_id[:8]}"
            )

        return task_id

    async def _sync_specific_pages(
        self,
        binding_id: str,
        task_id: str,
        page_ids: List[str],
    ) -> SyncResult:
        """同步指定的页面"""
        result = SyncResult(started_at=_utc_now())

        binding = await self.db.get_confluence_binding(binding_id)
        if not binding:
            logger.error(f"Binding not found: {binding_id}")
            return result

        connection_id = binding["connection_id"]
        dataset_id = binding["dataset_id"]

        try:
            client = await self._get_client(connection_id)
            result.total_pages = len(page_ids)

            await self.db.update_confluence_sync_task(task_id, {
                "status": "processing",
                "total_items": len(page_ids),
                "started_at": _utc_now(),
            })

            for i, page_id in enumerate(page_ids):
                try:
                    page = await client.get_page(page_id)
                    existing = await self.db.get_confluence_page_by_page_id(binding_id, page_id)

                    if existing and existing.get("document_id"):
                        # 更新现有文档
                        doc_id = existing["document_id"]

                        # 保存版本快照（用于回滚）
                        await self._save_version_before_update(
                            doc_id, page.version,
                            f"Confluence sync: {page.title} (v{page.version})"
                        )

                        content_text = extract_plain_text(page.body_storage)
                        await self.db.update_document_fields(
                            doc_id,
                            {
                                "content": content_text,
                                "confluence_version": page.version,
                                "status": "uploaded",
                                "progress": 0,
                            }
                        )
                        if self.worker:
                            await self.worker.enqueue(dataset_id, doc_id)
                        result.updated_documents.append(doc_id)
                    else:
                        # 创建新文档
                        doc = await self._create_document_from_page(
                            dataset_id=dataset_id,
                            connection_id=connection_id,
                            page=page,
                            binding_id=binding_id,
                            created_by=binding.get("created_by"),
                            sync_images=binding.get("sync_images", False),
                            tenant_id=binding.get("tenant_id"),
                            image_max_size_bytes=binding.get("image_max_size_bytes"),
                        )
                        if self.worker:
                            await self.worker.enqueue(dataset_id, doc["document_id"])
                        result.created_documents.append(doc["document_id"])

                    result.synced_pages += 1

                    # 更新进度
                    progress = ((i + 1) / len(page_ids)) * 100
                    await self.db.update_confluence_sync_task(task_id, {
                        "processed_items": i + 1,
                        "progress": progress,
                    })

                except Exception as e:
                    result.failed_pages += 1
                    result.errors.append({"page_id": page_id, "error": str(e)})
                    logger.error(f"Failed to sync page {page_id}: {e}")

            result.completed_at = _utc_now()

            await self.db.update_confluence_binding(binding_id, {
                "status": "completed",
                "last_sync_at": _utc_now(),
            })

            await self.db.update_confluence_sync_task(task_id, {
                "status": "completed",
                "progress": 100,
                "completed_at": _utc_now(),
                "result": result.to_dict(),
            })

        except Exception as e:
            logger.exception(f"Specific pages sync failed: {e}")
            result.errors.append({"error": str(e)})

            await self.db.update_confluence_binding(binding_id, {
                "status": "error",
                "last_error": str(e),
            })

            await self.db.update_confluence_sync_task(task_id, {
                "status": "failed",
                "error": str(e),
                "completed_at": _utc_now(),
            })

        return result

    # ============ Incremental Sync ============

    async def sync_page(
        self,
        page_record_id: str,
        user: Optional[UserContext] = None,
    ) -> Dict[str, Any]:
        """
        同步单个页面（通过页面记录 ID）

        Args:
            page_record_id: 页面记录 ID（confluence_pages 表的主键）
            user: 用户上下文（用于 ACL 验证）

        Returns:
            同步结果

        Raises:
            ConfluenceAccessDeniedError: 访问被拒绝
        """
        # 如果提供了用户上下文，验证访问权限
        if user:
            page_record = await self._verify_page_access(page_record_id, user)
        else:
            # 获取页面记录（内部调用不需要验证）
            page_record = await self.db.get_confluence_page(page_record_id)
            if not page_record:
                return {"status": "error", "message": "Page record not found"}

        binding_id = page_record["binding_id"]
        page_id = page_record["page_id"]

        binding = await self.db.get_confluence_binding(binding_id)
        if not binding:
            return {"status": "error", "message": "Binding not found"}

        connection_id = binding["connection_id"]
        dataset_id = binding["dataset_id"]

        try:
            client = await self._get_client(connection_id)
            page = await client.get_page(page_id)

            doc_id = page_record.get("document_id")

            if doc_id:
                # 保存版本快照（用于回滚）
                await self._save_version_before_update(
                    doc_id, page.version,
                    f"Confluence sync: {page.title} (v{page.version})"
                )

                # 更新现有文档
                content_text = extract_plain_text(page.body_storage)
                await self.db.update_document_fields(
                    doc_id,
                    {
                        "content": content_text,
                        "confluence_version": page.version,
                        "status": "uploaded",
                        "progress": 0,
                    }
                )

                # 处理图片（如果启用）- 修复：更新文档时也需要处理图片
                sync_images = binding.get("sync_images", False)
                tenant_id = binding.get("tenant_id")
                image_max_size_bytes = binding.get("image_max_size_bytes")
                image_count = 0

                if sync_images:
                    if not tenant_id:
                        logger.warning(
                            f"Image sync enabled but tenant_id is missing for page {page_id}, "
                            "skipping image processing"
                        )
                    else:
                        img_processor = await self._get_image_processor(
                            connection_id, max_image_size=image_max_size_bytes
                        )
                        if img_processor:
                            try:
                                # 清理旧的图片段（避免重复）
                                old_image_count = await self.db.delete_image_segments_by_document(doc_id)
                                if old_image_count > 0:
                                    logger.info(f"Cleaned up {old_image_count} old image segments for document {doc_id}")
                                    # 同时清理旧的图片关联
                                    await self.db.delete_image_associations_by_document(doc_id)

                                image_result = await img_processor.process_page_images(
                                    page_id=page_id,
                                    document_id=doc_id,
                                    tenant_id=tenant_id,
                                    page_content=page.body_storage,
                                    page_title=page.title,
                                )
                                if image_result.processed_images > 0:
                                    vlm_count = sum(1 for s in image_result.segments if s.vlm_description)
                                    logger.info(
                                        f"Processed {image_result.processed_images} images for page {page_id} "
                                        f"(vlm_descriptions={vlm_count})"
                                    )
                                    # Store image segments in database
                                    for idx, segment in enumerate(image_result.segments):
                                        await self._save_image_segment(segment, dataset_id, binding_id, position=idx)
                                    image_count = image_result.processed_images
                                if image_result.errors:
                                    logger.warning(
                                        f"Image processing errors for page {page_id}: {image_result.errors}"
                                    )
                            except Exception as e:
                                logger.error(f"Failed to process images for page {page_id}: {e}")

                if self.worker:
                    await self.worker.enqueue(dataset_id, doc_id)

                await self.db.upsert_confluence_page(
                    binding_id=binding_id,
                    page_id=page_id,
                    document_id=doc_id,
                    space_key=page.space_key,
                    title=page.title,
                    version=page.version,
                    content_hash=page.content_hash,
                    status="synced",
                    web_url=page.web_url,
                    image_count=image_count,
                )
                return {"status": "success", "action": "updated", "document_id": doc_id, "image_count": image_count}
            else:
                # 创建新文档
                doc = await self._create_document_from_page(
                    dataset_id=dataset_id,
                    connection_id=connection_id,
                    page=page,
                    binding_id=binding_id,
                    created_by=binding.get("created_by"),
                    sync_images=binding.get("sync_images", False),
                    tenant_id=binding.get("tenant_id"),
                    image_max_size_bytes=binding.get("image_max_size_bytes"),
                )
                if self.worker:
                    await self.worker.enqueue(dataset_id, doc["document_id"])
                return {"status": "success", "action": "created", "document_id": doc["document_id"]}

        except Exception as e:
            logger.error(f"Failed to sync page {page_id}: {e}")
            return {"status": "error", "message": str(e)}

    async def sync_page_by_id(
        self,
        binding_id: str,
        page_id: str,
        event_type: str = "updated",
    ) -> Optional[str]:
        """
        同步单个页面（用于 Webhook 或轮询，通过 binding_id 和 page_id）

        Args:
            binding_id: 绑定 ID
            page_id: Confluence 页面 ID
            event_type: 事件类型 (created | updated | removed | trashed)

        Returns:
            document_id if synced, None otherwise
        """
        binding = await self.db.get_confluence_binding(binding_id)
        if not binding:
            return None

        connection_id = binding["connection_id"]
        dataset_id = binding["dataset_id"]

        if event_type in ("removed", "trashed"):
            # 删除页面
            existing = await self.db.get_confluence_page_by_page_id(binding_id, page_id)
            if existing and existing.get("document_id"):
                doc_id = existing["document_id"]
                try:
                    await self.knowledge_service.delete_document(
                        dataset_id=dataset_id,
                        document_id=doc_id,
                    )
                except Exception as e:
                    logger.warning(f"Failed to delete document {doc_id}: {e}")
                await self.db.delete_confluence_page_by_page_id(binding_id, page_id)
                return doc_id
            return None

        # created / updated / restored
        client = await self._get_client(connection_id)
        page = await client.get_page(page_id)

        existing = await self.db.get_confluence_page_by_page_id(binding_id, page_id)

        if existing and existing.get("document_id"):
            # 更新现有文档
            doc_id = existing["document_id"]

            # 保存版本快照（用于回滚）
            await self._save_version_before_update(
                doc_id, page.version,
                f"Confluence sync: {page.title} (v{page.version})"
            )

            content_text = extract_plain_text(page.body_storage)
            await self.db.update_document_fields(
                doc_id,
                {
                    "content": content_text,
                    "confluence_version": page.version,
                    "status": "uploaded",
                    "progress": 0,
                }
            )
            if self.worker:
                await self.worker.enqueue(dataset_id, doc_id)

            await self.db.upsert_confluence_page(
                binding_id=binding_id,
                page_id=page_id,
                document_id=doc_id,
                space_key=page.space_key,
                title=page.title,
                version=page.version,
                content_hash=page.content_hash,
                status="synced",
                web_url=page.web_url,
            )
            return doc_id
        else:
            # 创建新文档
            doc = await self._create_document_from_page(
                dataset_id=dataset_id,
                connection_id=connection_id,
                page=page,
                binding_id=binding_id,
                created_by=binding.get("created_by"),
                sync_images=binding.get("sync_images", False),
                tenant_id=binding.get("tenant_id"),
                image_max_size_bytes=binding.get("image_max_size_bytes"),
            )
            if self.worker:
                await self.worker.enqueue(dataset_id, doc["document_id"])
            return doc["document_id"]

    # ============ Page Management ============

    async def list_pages(
        self,
        binding_id: str,
        user: UserContext,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
        synced_only: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        列出绑定下的页面记录

        Args:
            binding_id: 绑定 ID
            user: 用户上下文
            status: 状态过滤
            limit: 返回数量限制
            offset: 偏移量
            synced_only: 如果为 True，只返回已入库的页面（有 document_id）

        Returns:
            页面记录列表

        Raises:
            ConfluenceAccessDeniedError: 访问被拒绝
        """
        # 验证对 binding 的访问权限
        await self._verify_binding_access(binding_id, user)

        return await self.db.list_confluence_pages(
            binding_id=binding_id,
            status=status,
            limit=limit,
            offset=offset,
            synced_only=synced_only,
        )

    async def cleanup_unsynced_pages(
        self,
        binding_id: str,
        user: UserContext,
    ) -> Dict[str, Any]:
        """
        清理未同步的页面记录

        删除所有 document_id 为空的记录（从未真正同步到知识库的页面）

        Args:
            binding_id: 绑定 ID
            user: 用户上下文

        Returns:
            清理结果，包含删除的记录数
        """
        # 验证访问权限
        await self._verify_binding_access(binding_id, user)

        deleted = await self.db.cleanup_unsynced_confluence_pages(binding_id)

        logger.info(f"Cleaned up {deleted} unsynced pages for binding {binding_id}")

        return {
            "binding_id": binding_id,
            "deleted": deleted,
        }

    async def remove_pages(
        self,
        page_record_ids: List[str],
        user: UserContext,
        delete_documents: bool = True,
    ) -> Dict[str, Any]:
        """
        批量移除页面记录（从 confluence_pages 表中删除）

        Args:
            page_record_ids: confluence_pages 表中的记录 ID 列表
            user: 用户上下文
            delete_documents: 是否同时删除知识库中的对应文档

        Returns:
            移除结果统计

        Raises:
            ConfluenceAccessDeniedError: 访问被拒绝
        """
        if not page_record_ids:
            return {
                "removed": 0,
                "documents_deleted": 0,
                "errors": [],
            }

        removed = 0
        documents_deleted = 0
        errors = []

        for record_id in page_record_ids:
            try:
                # 获取页面记录
                page = await self.db.get_confluence_page(record_id)
                if not page:
                    errors.append({"id": record_id, "error": "Page not found"})
                    continue

                # 验证访问权限
                await self._verify_binding_access(page["binding_id"], user)

                # 如果需要删除文档，先删除知识库中的文档
                if delete_documents and page.get("document_id"):
                    try:
                        # 获取 binding 信息以获取 dataset_id
                        binding = await self.db.get_confluence_binding(page["binding_id"])
                        if binding and binding.get("dataset_id"):
                            await self.kb.delete_document(
                                binding["dataset_id"],
                                page["document_id"],
                            )
                            documents_deleted += 1
                    except Exception as e:
                        logger.warning(
                            f"Failed to delete document {page.get('document_id')}: {e}"
                        )

                # 删除页面记录
                success = await self.db.delete_confluence_page(record_id)
                if success:
                    removed += 1
                else:
                    errors.append({"id": record_id, "error": "Failed to delete"})

            except ConfluenceAccessDeniedError:
                errors.append({"id": record_id, "error": "Access denied"})
            except Exception as e:
                errors.append({"id": record_id, "error": str(e)})

        return {
            "removed": removed,
            "documents_deleted": documents_deleted,
            "errors": errors,
        }

    # ============ Batch Sync Operations ============

    async def batch_sync_pages(
        self,
        page_record_ids: List[str],
        force: bool = False,
        user: Optional[UserContext] = None,
    ) -> Dict[str, Any]:
        """
        批量同步多个页面（通过 page_record_id 列表）

        Args:
            page_record_ids: confluence_pages 表中的记录 ID 列表
            force: 是否强制同步（忽略内容哈希）
            user: 用户上下文（用于 ACL 验证）

        Returns:
            批量同步结果，包含 task_id 用于进度追踪

        Raises:
            ConfluenceAccessDeniedError: 访问被拒绝
        """
        if not page_record_ids:
            return {
                "status": "completed",
                "total": 0,
                "message": "No pages to sync",
            }

        # 获取所有页面记录，按 binding_id 分组
        pages_by_binding: Dict[str, List[Dict[str, Any]]] = {}
        not_found = []
        access_denied_bindings = set()

        for record_id in page_record_ids:
            page = await self.db.get_confluence_page(record_id)
            if page:
                binding_id = page["binding_id"]
                # 如果提供了用户上下文，验证对 binding 的访问权限
                if user and binding_id not in access_denied_bindings:
                    try:
                        await self._verify_binding_access(binding_id, user)
                    except ConfluenceAccessDeniedError:
                        access_denied_bindings.add(binding_id)
                        continue
                if binding_id in access_denied_bindings:
                    continue
                if binding_id not in pages_by_binding:
                    pages_by_binding[binding_id] = []
                pages_by_binding[binding_id].append(page)
            else:
                not_found.append(record_id)

        if not pages_by_binding:
            return {
                "status": "error",
                "message": "No valid pages found",
                "not_found": not_found,
            }

        # 对每个 binding 触发同步
        results = []
        for binding_id, pages in pages_by_binding.items():
            # 提取 Confluence page_ids
            page_ids = [p["page_id"] for p in pages]
            try:
                task_id = await self.trigger_sync(
                    binding_id=binding_id,
                    force=force,
                    page_ids=page_ids,
                )
                results.append({
                    "binding_id": binding_id,
                    "task_id": task_id,
                    "page_count": len(page_ids),
                    "status": "triggered",
                })
            except ConfluenceSyncError as e:
                results.append({
                    "binding_id": binding_id,
                    "error": str(e),
                    "page_count": len(page_ids),
                    "status": "failed",
                })

        total_pages = sum(r.get("page_count", 0) for r in results)
        triggered_count = sum(1 for r in results if r.get("status") == "triggered")

        return {
            "status": "triggered" if triggered_count > 0 else "failed",
            "total": total_pages,
            "bindings": results,
            "not_found": not_found if not_found else None,
        }

    # ============ Sync Task Management ============

    async def list_sync_tasks(
        self,
        user: UserContext,
        binding_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """
        列出同步任务

        Args:
            user: 用户上下文
            binding_id: 绑定 ID（可选）
            status: 状态过滤
            limit: 返回数量限制

        Returns:
            用户有权访问的任务列表
        """
        # 如果指定了 binding_id，先验证对该 binding 的访问权限
        if binding_id:
            await self._verify_binding_access(binding_id, user)

        tasks = await self.db.list_confluence_sync_tasks(
            binding_id=binding_id,
            status=status,
            limit=limit,
        )

        # 如果没有指定 binding_id，需要过滤任务
        if not binding_id and user.tier != "admin":
            # 获取用户有权访问的 binding_ids
            accessible_bindings = await self.list_bindings(user)
            accessible_binding_ids = {b["binding_id"] for b in accessible_bindings}
            tasks = [t for t in tasks if t.get("binding_id") in accessible_binding_ids]

        return tasks

    async def get_sync_task(
        self,
        task_id: str,
        user: UserContext,
    ) -> Optional[Dict[str, Any]]:
        """
        获取同步任务详情

        Args:
            task_id: 任务 ID
            user: 用户上下文

        Returns:
            任务详情

        Raises:
            ConfluenceAccessDeniedError: 访问被拒绝
        """
        task = await self.db.get_confluence_sync_task(task_id)
        if not task:
            return None

        # 通过 binding_id 验证访问权限
        binding_id = task.get("binding_id")
        if binding_id:
            await self._verify_binding_access(binding_id, user)
        else:
            # 没有 binding_id 的任务，检查 owner_id
            self._verify_confluence_access(task, "task", user)

        return task

    # ============ Incremental Sync (CQL-based) ============

    async def incremental_sync(
        self,
        binding_id: str,
        force: bool = False,
    ) -> str:
        """
        增量同步：只同步自上次同步以来修改的页面

        使用 CQL 查询 lastModified > "last_sync_at" 来获取变更页面，
        比全量同步更高效。

        Args:
            binding_id: 绑定 ID
            force: 是否强制同步（忽略正在进行的同步）

        Returns:
            任务 ID
        """
        binding = await self.db.get_confluence_binding(binding_id)
        if not binding:
            raise ConfluenceSyncError(f"Binding not found: {binding_id}")

        # 检查是否已有同步在进行
        if binding.get("status") == "syncing" and not force:
            raise ConfluenceSyncError("A sync is already in progress")

        # 创建同步任务（继承 binding 的 owner_id 用于 ACL）
        task_id = str(uuid.uuid4())
        await self.db.create_confluence_sync_task(
            task_id=task_id,
            binding_id=binding_id,
            task_type="incremental_sync",
            priority=1,
            owner_id=binding.get("owner_id") or binding.get("created_by"),
        )

        # 更新状态
        await self.db.update_confluence_binding(binding_id, {"status": "syncing"})

        # 启动后台同步
        self._create_background_task(
            self._incremental_sync_pages(binding_id, task_id),
            name=f"incremental-sync-{binding_id[:8]}"
        )

        return task_id

    async def _incremental_sync_pages(
        self,
        binding_id: str,
        task_id: str,
    ) -> SyncResult:
        """
        执行增量同步：只处理自上次同步以来修改的页面
        """
        result = SyncResult(started_at=_utc_now(), task_id=task_id)

        binding = await self.db.get_confluence_binding(binding_id)
        if not binding:
            logger.error(f"Binding not found: {binding_id}")
            return result

        connection_id = binding["connection_id"]
        dataset_id = binding["dataset_id"]
        space_key = binding["space_key"]
        last_sync_at = binding.get("last_sync_at")

        try:
            client = await self._get_client(connection_id)

            # 如果没有上次同步时间，执行全量同步
            if not last_sync_at:
                logger.info(f"No last_sync_at for binding {binding_id}, performing full sync")
                await self.db.update_confluence_sync_task(task_id, {
                    "status": "completed",
                    "result": {"message": "Redirected to full sync"},
                })
                # 改为全量同步
                await self._sync_space_pages(binding_id, task_id)
                return result

            # 确保 last_sync_at 是 datetime 对象
            if isinstance(last_sync_at, str):
                last_sync_at = datetime.fromisoformat(last_sync_at.replace("Z", "+00:00"))

            # 使用 CQL 查询修改过的页面
            logger.info(f"Incremental sync for binding {binding_id}, since {last_sync_at}")
            modified_pages = await client.search_pages_modified_since(
                space_key=space_key,
                since=last_sync_at,
            )

            result.total_pages = len(modified_pages)

            await self.db.update_confluence_sync_task(task_id, {
                "status": "processing",
                "total_items": len(modified_pages),
                "started_at": _utc_now(),
            })

            # 处理修改的页面
            for i, page_stub in enumerate(modified_pages):
                try:
                    # 获取完整页面内容（CQL 搜索只返回基本信息）
                    page = await client.get_page(page_stub.page_id)

                    existing = await self.db.get_confluence_page_by_page_id(binding_id, page.page_id)

                    if existing:
                        # 检查内容是否真的变化
                        if existing.get("content_hash") == page.content_hash:
                            result.skipped_pages += 1
                            continue

                        # 更新现有文档
                        doc_id = existing.get("document_id")
                        if doc_id:
                            # 保存版本快照（用于回滚）
                            await self._save_version_before_update(
                                doc_id, page.version,
                                f"Confluence sync: {page.title} (v{page.version})"
                            )

                            content_text = extract_plain_text(page.body_storage)
                            await self.db.update_document_fields(
                                doc_id,
                                {
                                    "content": content_text,
                                    "confluence_version": page.version,
                                    "status": "uploaded",
                                    "progress": 0,
                                }
                            )
                            if self.worker:
                                await self.worker.enqueue(dataset_id, doc_id)
                            result.updated_documents.append(doc_id)

                            # 更新页面记录
                            await self.db.upsert_confluence_page(
                                binding_id=binding_id,
                                page_id=page.page_id,
                                document_id=doc_id,
                                space_key=page.space_key,
                                title=page.title,
                                version=page.version,
                                content_hash=page.content_hash,
                                status="synced",
                                web_url=page.web_url,
                            )
                    else:
                        # 创建新文档
                        doc = await self._create_document_from_page(
                            dataset_id=dataset_id,
                            connection_id=connection_id,
                            page=page,
                            binding_id=binding_id,
                            created_by=binding.get("created_by"),
                            sync_images=binding.get("sync_images", False),
                            tenant_id=binding.get("tenant_id"),
                            image_max_size_bytes=binding.get("image_max_size_bytes"),
                        )
                        if self.worker:
                            await self.worker.enqueue(dataset_id, doc["document_id"])
                        result.created_documents.append(doc["document_id"])

                    result.synced_pages += 1

                    # 更新进度
                    progress = ((i + 1) / len(modified_pages)) * 100 if modified_pages else 0
                    await self.db.update_confluence_sync_task(task_id, {
                        "processed_items": i + 1,
                        "progress": progress,
                    })

                except Exception as e:
                    result.failed_pages += 1
                    result.errors.append({
                        "page_id": page_stub.page_id,
                        "error": str(e),
                    })
                    logger.error(f"Failed to sync page {page_stub.page_id}: {e}")

            # 检测并处理已删除的页面
            # 获取当前 binding 下所有已同步的页面
            existing_pages = await self.db.list_confluence_pages(binding_id)
            modified_page_ids = {p.page_id for p in modified_pages}

            # 检查每个已同步的页面是否仍存在于 Confluence
            deleted_count = 0
            for page_record in existing_pages:
                page_id = page_record.get("page_id")
                # 跳过本次已处理的页面
                if page_id in modified_page_ids:
                    continue

                try:
                    # 尝试获取页面，如果返回 404 则表示已删除
                    await client.get_page(page_id)
                except ConfluenceAPIError as e:
                    if e.status_code == 404:
                        # 页面已删除，从知识库中移除
                        doc_id = page_record.get("document_id")
                        if doc_id:
                            try:
                                # 直接从数据库删除文档和相关数据
                                await self.db.delete_document(doc_id)
                                result.deleted_documents.append(doc_id)
                                logger.info(f"Deleted document {doc_id} for removed page {page_id}")
                            except Exception as del_err:
                                logger.warning(f"Failed to delete document {doc_id}: {del_err}")
                        # 删除页面记录
                        await self.db.delete_confluence_page_by_page_id(binding_id, page_id)
                        deleted_count += 1
                        logger.info(f"Removed deleted page {page_id} during incremental sync")
                except Exception as check_err:
                    # 其他错误（如网络问题）不视为删除
                    logger.debug(f"Could not verify page {page_id}: {check_err}")

            if deleted_count > 0:
                logger.info(f"Removed {deleted_count} deleted pages during incremental sync")

            result.completed_at = _utc_now()

            # 更新绑定状态
            await self.db.update_confluence_binding(binding_id, {
                "status": "completed",
                "last_sync_at": _utc_now(),
                "last_sync_type": "incremental",
            })

            # 更新任务状态
            await self.db.update_confluence_sync_task(task_id, {
                "status": "completed",
                "progress": 100,
                "completed_at": _utc_now(),
                "result": result.to_dict(),
            })

            logger.info(
                f"Incremental sync completed for binding {binding_id}: "
                f"{result.synced_pages} synced, {result.skipped_pages} skipped, "
                f"{result.failed_pages} failed"
            )

        except Exception as e:
            logger.exception(f"Incremental sync failed: {e}")
            result.errors.append({"error": str(e)})

            await self.db.update_confluence_binding(binding_id, {
                "status": "error",
                "last_error": str(e),
            })

            await self.db.update_confluence_sync_task(task_id, {
                "status": "failed",
                "error": str(e),
                "completed_at": _utc_now(),
            })

        return result

    async def list_bindings_for_polling(self) -> List[Dict[str, Any]]:
        """
        获取所有需要轮询同步的绑定

        优先使用绑定级别的 sync_mode 配置，如果没有设置则回退到连接级别的配置。

        Returns:
            启用了轮询的绑定列表
        """
        all_bindings = await self.db.list_confluence_bindings()
        polling_bindings = []

        for binding in all_bindings:
            # 检查绑定是否禁用同步
            if binding.get("sync_enabled") is False:
                continue

            # 优先使用绑定级别的 sync_mode
            binding_sync_mode = binding.get("sync_mode")
            binding_interval = binding.get("polling_interval_minutes")

            if binding_sync_mode == "polling":
                # 绑定级别明确配置为 polling
                binding["polling_interval_minutes"] = binding_interval or 60
                polling_bindings.append(binding)
            elif binding_sync_mode is None:
                # 绑定级别未配置，回退到连接级别
                connection = await self.db.get_confluence_connection(binding["connection_id"])
                if not connection:
                    continue

                if connection.get("sync_mode") == "polling" and connection.get("status") == "active":
                    binding["polling_interval_minutes"] = connection.get("polling_interval_minutes", 60)
                    polling_bindings.append(binding)
            # 如果 binding_sync_mode == "manual"，则不加入轮询列表

        return polling_bindings
