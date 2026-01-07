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
import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from ....config.settings import Settings
from .client import ConfluenceClient, ConfluenceAPIError
from .models import (
    ConfluenceCredentials,
    ConfluencePage,
    ConfluenceSpace,
    SyncResult,
)
from .parser import extract_plain_text, extract_markdown

if TYPE_CHECKING:
    from ....persistence.database import DatabaseStorage
    from ..knowledge_service import KnowledgeService
    from ..worker import KnowledgeWorker

logger = logging.getLogger(__name__)


class ConfluenceSyncError(Exception):
    """Confluence 同步错误"""
    pass


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
    ):
        self.settings = settings
        self.db = database
        self.knowledge_service = knowledge_service
        self.worker = knowledge_worker
        self._clients: Dict[str, ConfluenceClient] = {}

        # 检查 Worker 状态，如果未提供则记录警告
        if not knowledge_worker:
            logger.warning(
                "⚠️ KnowledgeWorker not provided to ConfluenceSyncService - "
                "documents will NOT be auto-indexed after sync!"
            )

    async def close(self) -> None:
        """关闭所有客户端连接"""
        for client in self._clients.values():
            await client.close()
        self._clients.clear()

    async def _get_client(self, connection_id: str) -> ConfluenceClient:
        """获取或创建 Confluence 客户端"""
        if connection_id in self._clients:
            return self._clients[connection_id]

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
        return client

    def _invalidate_client(self, connection_id: str) -> None:
        """使客户端缓存失效"""
        if connection_id in self._clients:
            asyncio.create_task(self._clients[connection_id].close())
            del self._clients[connection_id]

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
        tenant_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """列出连接"""
        return await self.db.list_confluence_connections(tenant_id=tenant_id, status=status)

    async def get_connection(self, connection_id: str) -> Optional[Dict[str, Any]]:
        """获取连接详情"""
        return await self.db.get_confluence_connection(connection_id)

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

        return doc

    # ============ Space Binding ============

    async def create_space_binding(
        self,
        connection_id: str,
        tenant_id: str,
        dataset_id: str,
        space_key: str,
        root_page_id: Optional[str] = None,
        include_patterns: Optional[List[str]] = None,
        exclude_patterns: Optional[List[str]] = None,
        max_depth: int = 10,
        include_attachments: bool = False,
        include_comments: bool = False,
        created_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        创建空间绑定

        Args:
            connection_id: 连接 ID
            tenant_id: 租户 ID
            dataset_id: 数据集 ID
            space_key: Confluence Space Key
            root_page_id: 根页面 ID（可选，如果指定则只同步该页面及其子页面）
            include_patterns: 包含规则
            exclude_patterns: 排除规则
            max_depth: 最大深度
            include_attachments: 是否包含附件
            include_comments: 是否包含评论
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

        # 如果指定了根页面 ID，获取页面标题
        root_page_title = None
        if root_page_id:
            try:
                root_page = await client.get_page(root_page_id)
                root_page_title = root_page.title
            except Exception as e:
                logger.warning(f"Failed to get root page {root_page_id}: {e}")

        binding_id = str(uuid.uuid4())
        binding = {
            "binding_id": binding_id,
            "connection_id": connection_id,
            "tenant_id": tenant_id,
            "dataset_id": dataset_id,
            "space_key": space_key,
            "space_id": space.space_id,
            "space_name": space.name,
            "root_page_id": root_page_id,
            "root_page_title": root_page_title,
            "include_patterns": include_patterns or [],
            "exclude_patterns": exclude_patterns or [],
            "max_depth": max_depth,
            "include_attachments": include_attachments,
            "include_comments": include_comments,
            "status": "pending",
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
        connection_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        dataset_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        列出空间绑定

        Args:
            connection_id: 连接 ID（可选）
            tenant_id: 租户 ID（可选）
            dataset_id: 数据集 ID（可选）

        Returns:
            绑定列表
        """
        return await self.db.list_confluence_bindings(
            connection_id=connection_id,
            tenant_id=tenant_id,
            dataset_id=dataset_id,
        )

    async def list_all_bindings(
        self,
        tenant_id: str,
        connection_id: Optional[str] = None,
        dataset_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        列出租户下所有空间绑定（带过滤）

        Args:
            tenant_id: 租户 ID
            connection_id: 连接 ID（可选）
            dataset_id: 数据集 ID（可选）
            status: 状态过滤（可选）

        Returns:
            绑定列表
        """
        bindings = await self.db.list_confluence_bindings(
            connection_id=connection_id,
            tenant_id=tenant_id,
            dataset_id=dataset_id,
        )
        # Filter by status if specified
        if status:
            bindings = [b for b in bindings if b.get("status") == status]
        return bindings

    async def get_binding(self, binding_id: str) -> Optional[Dict[str, Any]]:
        """获取空间绑定详情"""
        return await self.db.get_confluence_binding(binding_id)

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
            "root_page_id", "include_patterns", "exclude_patterns", "max_depth",
            "include_attachments", "include_comments", "status"
        }
        filtered_updates = {k: v for k, v in updates.items() if k in allowed_fields}

        # 如果更新了 root_page_id，需要获取页面标题
        if "root_page_id" in filtered_updates:
            root_page_id = filtered_updates["root_page_id"]
            if root_page_id:
                try:
                    connection_id = binding["connection_id"]
                    client = await self._get_client(connection_id)
                    root_page = await client.get_page(root_page_id)
                    filtered_updates["root_page_title"] = root_page.title
                except Exception as e:
                    logger.warning(f"Failed to get root page {root_page_id}: {e}")
                    filtered_updates["root_page_title"] = None
            else:
                filtered_updates["root_page_title"] = None

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
                started_at=datetime.utcnow(),
                completed_at=datetime.utcnow(),
                errors=[{"error": "Sync already in progress"}],
            )

        # 创建同步任务
        task_id = str(uuid.uuid4())
        await self.db.create_confluence_sync_task(
            task_id=task_id,
            binding_id=binding_id,
            task_type="full_sync",
            priority=0,
        )

        # 更新绑定状态
        await self.db.update_confluence_binding(binding_id, {"status": "syncing"})

        # 启动后台同步
        asyncio.create_task(self._sync_space_pages(binding_id, task_id))

        return SyncResult(
            started_at=datetime.utcnow(),
            task_id=task_id,
        )

    async def _sync_space_pages(
        self,
        binding_id: str,
        task_id: str,
    ) -> SyncResult:
        """同步空间中的所有页面（或指定根页面的子页面）"""
        result = SyncResult(started_at=datetime.utcnow())

        binding = await self.db.get_confluence_binding(binding_id)
        if not binding:
            logger.error(f"Binding not found: {binding_id}")
            return result

        connection_id = binding["connection_id"]
        dataset_id = binding["dataset_id"]
        space_id = binding["space_id"]
        root_page_id = binding.get("root_page_id")
        max_depth = binding.get("max_depth", 10)

        try:
            client = await self._get_client(connection_id)

            # 获取所有现有页面记录
            existing_pages = await self.db.list_confluence_pages(binding_id)
            existing_page_ids = {p["page_id"] for p in existing_pages}
            seen_page_ids = set()

            # 如果指定了 root_page_id，则只同步该页面及其子页面
            if root_page_id:
                pages_to_sync = await self._get_page_descendants(
                    client, root_page_id, max_depth
                )
            else:
                # 获取空间中的所有页面
                pages_to_sync = []
                async for page_data in client.iter_space_pages(space_id, batch_size=50):
                    pages_to_sync.append(str(page_data.get("id")))

            total_count = len(pages_to_sync)
            result.total_pages = total_count

            await self.db.update_confluence_sync_task(task_id, {
                "status": "processing",
                "total_items": total_count,
                "started_at": datetime.utcnow(),
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
                        if existing.get("content_hash") == page.content_hash:
                            result.skipped_pages += 1
                            continue

                        # 更新现有文档
                        doc_id = existing.get("document_id")
                        if doc_id:
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

            result.completed_at = datetime.utcnow()

            # 更新绑定状态
            await self.db.update_confluence_binding(binding_id, {
                "status": "completed",
                "last_sync_at": datetime.utcnow(),
                "synced_page_count": result.synced_pages,
                "total_page_count": result.total_pages,
            })

            # 更新任务状态
            await self.db.update_confluence_sync_task(task_id, {
                "status": "completed",
                "progress": 100,
                "completed_at": datetime.utcnow(),
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
                "completed_at": datetime.utcnow(),
            })

        return result

    # ============ Sync Status ============

    async def get_sync_status(self, binding_id: str) -> Dict[str, Any]:
        """获取同步状态"""
        binding = await self.db.get_confluence_binding(binding_id)
        if not binding:
            raise ConfluenceSyncError(f"Binding not found: {binding_id}")

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

        # 创建同步任务
        task_type = "partial_sync" if page_ids else "full_sync"
        task_id = str(uuid.uuid4())
        await self.db.create_confluence_sync_task(
            task_id=task_id,
            binding_id=binding_id,
            task_type=task_type,
            priority=1,
        )

        # 更新状态
        await self.db.update_confluence_binding(binding_id, {"status": "syncing"})

        # 启动同步
        if page_ids:
            asyncio.create_task(self._sync_specific_pages(binding_id, task_id, page_ids))
        else:
            asyncio.create_task(self._sync_space_pages(binding_id, task_id))

        return task_id

    async def _sync_specific_pages(
        self,
        binding_id: str,
        task_id: str,
        page_ids: List[str],
    ) -> SyncResult:
        """同步指定的页面"""
        result = SyncResult(started_at=datetime.utcnow())

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
                "started_at": datetime.utcnow(),
            })

            for i, page_id in enumerate(page_ids):
                try:
                    page = await client.get_page(page_id)
                    existing = await self.db.get_confluence_page_by_page_id(binding_id, page_id)

                    if existing and existing.get("document_id"):
                        # 更新现有文档
                        doc_id = existing["document_id"]
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

            result.completed_at = datetime.utcnow()

            await self.db.update_confluence_binding(binding_id, {
                "status": "completed",
                "last_sync_at": datetime.utcnow(),
            })

            await self.db.update_confluence_sync_task(task_id, {
                "status": "completed",
                "progress": 100,
                "completed_at": datetime.utcnow(),
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
                "completed_at": datetime.utcnow(),
            })

        return result

    # ============ Incremental Sync ============

    async def sync_page(
        self,
        page_record_id: str,
    ) -> Dict[str, Any]:
        """
        同步单个页面（通过页面记录 ID）

        Args:
            page_record_id: 页面记录 ID（confluence_pages 表的主键）

        Returns:
            同步结果
        """
        # 获取页面记录
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
                )
                return {"status": "success", "action": "updated", "document_id": doc_id}
            else:
                # 创建新文档
                doc = await self._create_document_from_page(
                    dataset_id=dataset_id,
                    connection_id=connection_id,
                    page=page,
                    binding_id=binding_id,
                    created_by=binding.get("created_by"),
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
            )
            if self.worker:
                await self.worker.enqueue(dataset_id, doc["document_id"])
            return doc["document_id"]

    # ============ Page Management ============

    async def list_pages(
        self,
        binding_id: str,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """
        列出绑定下的页面记录

        Args:
            binding_id: 绑定 ID
            status: 状态过滤
            limit: 返回数量限制
            offset: 偏移量

        Returns:
            页面记录列表
        """
        return await self.db.list_confluence_pages(
            binding_id=binding_id,
            status=status,
            limit=limit,
            offset=offset,
        )

    # ============ Batch Sync Operations ============

    async def batch_sync_pages(
        self,
        page_record_ids: List[str],
        force: bool = False,
    ) -> Dict[str, Any]:
        """
        批量同步多个页面（通过 page_record_id 列表）

        Args:
            page_record_ids: confluence_pages 表中的记录 ID 列表
            force: 是否强制同步（忽略内容哈希）

        Returns:
            批量同步结果，包含 task_id 用于进度追踪
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

        for record_id in page_record_ids:
            page = await self.db.get_confluence_page(record_id)
            if page:
                binding_id = page["binding_id"]
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
        binding_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """
        列出同步任务

        Args:
            binding_id: 绑定 ID（可选）
            status: 状态过滤
            limit: 返回数量限制

        Returns:
            任务列表
        """
        return await self.db.list_confluence_sync_tasks(
            binding_id=binding_id,
            status=status,
            limit=limit,
        )

    async def get_sync_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        """
        获取同步任务详情

        Args:
            task_id: 任务 ID

        Returns:
            任务详情
        """
        return await self.db.get_confluence_sync_task(task_id)
