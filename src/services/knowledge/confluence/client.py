"""
Confluence REST API v2 Client.

Provides async HTTP client for interacting with Confluence Cloud REST API.
Supports authentication, page retrieval, space management, and pagination.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urljoin, urlparse

import httpx

from .models import ConfluenceCredentials, ConfluencePage, ConfluenceSpace

logger = logging.getLogger(__name__)


class ConfluenceAPIError(Exception):
    """Confluence API 错误"""

    def __init__(self, message: str, status_code: Optional[int] = None, response_body: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class ConfluenceClient:
    """
    Confluence REST API v2 客户端

    使用 httpx 进行异步 HTTP 请求，支持：
    - Basic Auth 认证
    - 页面获取和列表
    - 空间管理
    - 游标分页
    - 自动重试
    """

    def __init__(
        self,
        credentials: ConfluenceCredentials,
        timeout: float = 30.0,
        max_retries: int = 3,
    ):
        self.credentials = credentials
        self.timeout = timeout
        self.max_retries = max_retries
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def _auth_header(self) -> str:
        """生成 Basic Auth header"""
        credentials = f"{self.credentials.email}:{self.credentials.api_token}"
        encoded = base64.b64encode(credentials.encode()).decode()
        return f"Basic {encoded}"

    async def _get_client(self) -> httpx.AsyncClient:
        """获取或创建 HTTP 客户端"""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout),
                headers={
                    "Authorization": self._auth_header,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    async def close(self) -> None:
        """关闭客户端连接"""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> "ConfluenceClient":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()

    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        use_v1: bool = False,
    ) -> Dict[str, Any]:
        """
        发送 API 请求 (带重试)

        Args:
            method: HTTP 方法
            endpoint: API 端点路径
            params: 查询参数
            json_data: JSON 请求体
            use_v1: 是否使用 v1 API

        Returns:
            响应 JSON 数据

        Raises:
            ConfluenceAPIError: API 请求失败
        """
        client = await self._get_client()
        base = self.credentials.api_v1_url if use_v1 else self.credentials.api_v2_url
        url = urljoin(base + "/", endpoint.lstrip("/"))

        last_error = None
        for attempt in range(self.max_retries):
            try:
                resp = await client.request(
                    method,
                    url,
                    params=params,
                    json=json_data,
                )

                if resp.status_code == 429:
                    # Rate limited
                    retry_after = int(resp.headers.get("Retry-After", 5))
                    logger.warning(f"Rate limited, retrying after {retry_after}s")
                    await asyncio.sleep(retry_after)
                    continue

                if resp.status_code >= 500 and attempt < self.max_retries - 1:
                    # Server error, retry
                    await asyncio.sleep(2 ** attempt)
                    continue

                if resp.status_code >= 400:
                    raise ConfluenceAPIError(
                        f"API request failed: {resp.status_code}",
                        status_code=resp.status_code,
                        response_body=resp.text,
                    )

                return resp.json()

            except httpx.RequestError as e:
                last_error = e
                if attempt < self.max_retries - 1:
                    logger.warning(f"Request error, retrying: {e}")
                    await asyncio.sleep(2 ** attempt)
                else:
                    raise ConfluenceAPIError(f"Request failed after {self.max_retries} retries: {e}")

        raise ConfluenceAPIError(f"Request failed: {last_error}")

    # ============ Connection Test ============

    async def test_connection(self) -> Dict[str, Any]:
        """
        测试连接是否有效

        Returns:
            包含连接状态和可用空间数量的字典
        """
        try:
            data = await self._request("GET", "/spaces", params={"limit": 1})
            return {
                "status": "success",
                "message": "Connection successful",
                "spaces_available": True,
            }
        except ConfluenceAPIError as e:
            return {
                "status": "error",
                "message": str(e),
                "status_code": e.status_code,
            }

    # ============ Page Operations ============

    async def get_page(
        self,
        page_id: str,
        body_format: str = "storage",
        include_labels: bool = True,
    ) -> ConfluencePage:
        """
        获取单个页面

        Args:
            page_id: 页面 ID
            body_format: 内容格式 (storage | atlas_doc_format | view)
            include_labels: 是否获取标签

        Returns:
            ConfluencePage 对象
        """
        params = {"body-format": body_format}
        data = await self._request("GET", f"/pages/{page_id}", params=params)

        # 调试日志：检查 API 返回的数据结构
        logger.info(f"Confluence API response for page {page_id}:")
        logger.info(f"  - title: {data.get('title')}")
        logger.info(f"  - body keys: {list(data.get('body', {}).keys()) if data.get('body') else 'NO BODY'}")
        if data.get("body"):
            for key, val in data["body"].items():
                if isinstance(val, dict):
                    logger.info(f"  - body.{key} keys: {list(val.keys())}")
                    if "value" in val:
                        logger.info(f"  - body.{key}.value length: {len(val.get('value', ''))}")
                else:
                    logger.info(f"  - body.{key} = {type(val)}")

        labels = []
        if include_labels:
            try:
                labels_data = await self._request("GET", f"/pages/{page_id}/labels")
                labels = [label.get("name", "") for label in labels_data.get("results", [])]
            except Exception as e:
                logger.warning(f"Failed to get labels for page {page_id}: {e}")

        # 获取 space key
        space_id = data.get("spaceId")
        space_key = ""
        if space_id:
            try:
                space_data = await self._request("GET", f"/spaces/{space_id}")
                space_key = space_data.get("key", "")
            except Exception:
                pass

        # 提取 body 内容 - 增强处理多种可能的结构
        body = data.get("body", {})
        body_content = ""

        if body:
            # 首选：使用请求的格式
            if body_format in body:
                format_data = body[body_format]
                if isinstance(format_data, dict):
                    body_content = format_data.get("value", "")
                elif isinstance(format_data, str):
                    body_content = format_data

            # 备选：尝试其他常见格式
            if not body_content:
                for fallback_format in ["storage", "view", "atlas_doc_format"]:
                    if fallback_format in body and fallback_format != body_format:
                        format_data = body[fallback_format]
                        if isinstance(format_data, dict):
                            body_content = format_data.get("value", "")
                        elif isinstance(format_data, str):
                            body_content = format_data
                        if body_content:
                            logger.info(f"Used fallback format '{fallback_format}' for page {page_id}")
                            break

        # 警告：body 为空
        if not body_content:
            logger.warning(
                f"Empty body for page {page_id} (title: {data.get('title')}). "
                f"API returned body: {body}. Check Confluence permissions or page content."
            )
        else:
            logger.info(f"Page {page_id} body extracted successfully, length: {len(body_content)}")

        version_info = data.get("version", {})

        # 构建完整的 web_url（API 返回的是相对路径）
        webui_path = data.get("_links", {}).get("webui")
        web_url = f"https://{self.credentials.domain}{webui_path}" if webui_path else None

        return ConfluencePage(
            page_id=str(data.get("id")),
            space_key=space_key,
            space_id=space_id,
            title=data.get("title", ""),
            version=version_info.get("number", 1),
            body_storage=body_content,
            parent_id=data.get("parentId"),
            author_id=data.get("authorId"),
            created_at=data.get("createdAt"),
            updated_at=version_info.get("createdAt"),
            labels=labels,
            web_url=web_url,
        )

    async def get_page_by_url(self, url: str) -> ConfluencePage:
        """
        根据 URL 获取页面

        支持的 URL 格式：
        - https://domain.atlassian.net/wiki/spaces/SPACE/pages/12345/Title
        - https://domain.atlassian.net/wiki/x/AbCdEf (短链接需要额外处理)

        Args:
            url: Confluence 页面 URL

        Returns:
            ConfluencePage 对象
        """
        page_id = self.parse_page_id_from_url(url)
        return await self.get_page(page_id)

    async def get_page_children(
        self,
        page_id: str,
        limit: int = 100,
        fetch_all: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        获取页面的子页面（支持分页）

        Args:
            page_id: 父页面 ID
            limit: 每页返回数量限制
            fetch_all: 是否获取所有子页面（自动分页）

        Returns:
            子页面列表
        """
        all_children: List[Dict[str, Any]] = []
        cursor: Optional[str] = None

        while True:
            params: Dict[str, Any] = {"limit": limit}
            if cursor:
                params["cursor"] = cursor

            data = await self._request(
                "GET",
                f"/pages/{page_id}/children",
                params=params,
            )

            results = data.get("results", [])
            all_children.extend(results)

            # 如果不需要获取所有或没有更多结果，退出循环
            if not fetch_all:
                break

            # 检查是否有下一页
            next_link = data.get("_links", {}).get("next")
            if not next_link or len(results) < limit:
                break

            # 从 next link 提取 cursor
            parsed = urlparse(next_link)
            qs = parse_qs(parsed.query)
            cursor = qs.get("cursor", [None])[0]
            if not cursor:
                break

        return all_children

    async def list_space_pages(
        self,
        space_id: str,
        status: str = "current",
        limit: int = 25,
        cursor: Optional[str] = None,
        body_format: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """
        列出空间中的页面 (分页)

        Args:
            space_id: 空间 ID
            status: 页面状态 (current | archived | trashed)
            limit: 每页数量 (最大 250)
            cursor: 分页游标
            body_format: 是否获取内容 (storage | atlas_doc_format)

        Returns:
            (页面列表, 下一页游标)
        """
        params: Dict[str, Any] = {
            "status": status,
            "limit": min(limit, 250),
        }
        if cursor:
            params["cursor"] = cursor
        if body_format:
            params["body-format"] = body_format

        data = await self._request("GET", f"/spaces/{space_id}/pages", params=params)
        pages = data.get("results", [])

        # 提取下一页游标
        next_cursor = None
        links = data.get("_links", {})
        if "next" in links:
            next_url = links["next"]
            parsed = urlparse(next_url)
            qs = parse_qs(parsed.query)
            next_cursor = qs.get("cursor", [None])[0]

        return pages, next_cursor

    async def iter_space_pages(
        self,
        space_id: str,
        status: str = "current",
        batch_size: int = 25,
        body_format: Optional[str] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """
        迭代空间中的所有页面

        Args:
            space_id: 空间 ID
            status: 页面状态
            batch_size: 每批数量
            body_format: 是否获取内容

        Yields:
            页面数据字典
        """
        cursor = None
        while True:
            pages, cursor = await self.list_space_pages(
                space_id=space_id,
                status=status,
                limit=batch_size,
                cursor=cursor,
                body_format=body_format,
            )
            for page in pages:
                yield page
            if not cursor:
                break

    # ============ Space Operations ============

    async def get_space(self, space_id: str) -> ConfluenceSpace:
        """
        获取空间信息

        Args:
            space_id: 空间 ID

        Returns:
            ConfluenceSpace 对象
        """
        data = await self._request("GET", f"/spaces/{space_id}")
        return ConfluenceSpace(
            space_id=str(data.get("id")),
            space_key=data.get("key", ""),
            name=data.get("name", ""),
            type=data.get("type", "global"),
            status=data.get("status", "current"),
            homepage_id=data.get("homepageId"),
            description=data.get("description", {}).get("plain", {}).get("value"),
        )

    async def get_space_by_key(self, space_key: str) -> ConfluenceSpace:
        """
        根据 Space Key 获取空间

        Args:
            space_key: 空间 Key (如 "HFDSH")

        Returns:
            ConfluenceSpace 对象

        Raises:
            ConfluenceAPIError: 空间不存在
        """
        data = await self._request("GET", "/spaces", params={"keys": space_key})
        spaces = data.get("results", [])
        if not spaces:
            raise ConfluenceAPIError(f"Space not found: {space_key}")

        space_data = spaces[0]
        return ConfluenceSpace(
            space_id=str(space_data.get("id")),
            space_key=space_data.get("key", ""),
            name=space_data.get("name", ""),
            type=space_data.get("type", "global"),
            status=space_data.get("status", "current"),
            homepage_id=space_data.get("homepageId"),
        )

    async def list_spaces(
        self,
        type_filter: Optional[str] = None,
        status: str = "current",
        limit: int = 25,
    ) -> List[ConfluenceSpace]:
        """
        列出所有可访问的空间

        Args:
            type_filter: 类型过滤 (global | personal)
            status: 状态过滤
            limit: 返回数量

        Returns:
            空间列表
        """
        params: Dict[str, Any] = {"status": status, "limit": limit}
        if type_filter:
            params["type"] = type_filter

        data = await self._request("GET", "/spaces", params=params)
        return [
            ConfluenceSpace(
                space_id=str(s.get("id")),
                space_key=s.get("key", ""),
                name=s.get("name", ""),
                type=s.get("type", "global"),
                status=s.get("status", "current"),
                homepage_id=s.get("homepageId"),
            )
            for s in data.get("results", [])
        ]

    # ============ Search ============

    async def search_pages(
        self,
        cql: str,
        limit: int = 25,
        start: int = 0,
    ) -> List[Dict[str, Any]]:
        """
        使用 CQL 搜索页面 (使用 v1 API)

        Args:
            cql: Confluence Query Language 查询
            limit: 返回数量
            start: 起始位置

        Returns:
            搜索结果列表

        Example CQL:
            - space=HFDSH AND type=page
            - lastModified > "2024-01-01"
            - title ~ "FAQ"
        """
        data = await self._request(
            "GET",
            "/content/search",
            params={"cql": cql, "limit": limit, "start": start},
            use_v1=True,
        )
        return data.get("results", [])

    # ============ URL Parsing ============

    @staticmethod
    def parse_page_id_from_url(url: str) -> str:
        """
        从 URL 中解析页面 ID

        支持格式：
        - /wiki/spaces/SPACE/pages/12345/Title
        - /wiki/spaces/SPACE/pages/12345

        Args:
            url: Confluence URL

        Returns:
            页面 ID

        Raises:
            ValueError: 无法解析 URL
        """
        parsed = urlparse(url)
        path = parsed.path

        # 格式: /wiki/spaces/SPACE/pages/12345/Title
        pattern = r"/wiki/spaces/[^/]+/pages/(\d+)"
        match = re.search(pattern, path)
        if match:
            return match.group(1)

        # 格式: /pages/12345
        pattern2 = r"/pages/(\d+)"
        match2 = re.search(pattern2, path)
        if match2:
            return match2.group(1)

        raise ValueError(f"Cannot parse page ID from URL: {url}")

    @staticmethod
    def parse_space_key_from_url(url: str) -> Optional[str]:
        """
        从 URL 中解析 Space Key

        Args:
            url: Confluence URL

        Returns:
            Space Key 或 None
        """
        parsed = urlparse(url)
        path = parsed.path

        # 格式: /wiki/spaces/SPACE/...
        pattern = r"/wiki/spaces/([^/]+)"
        match = re.search(pattern, path)
        if match:
            return match.group(1)

        return None

    @staticmethod
    def parse_domain_from_url(url: str) -> str:
        """
        从 URL 中解析域名

        Args:
            url: Confluence URL

        Returns:
            域名 (如 'yourcompany.atlassian.net')
        """
        parsed = urlparse(url)
        return parsed.netloc
