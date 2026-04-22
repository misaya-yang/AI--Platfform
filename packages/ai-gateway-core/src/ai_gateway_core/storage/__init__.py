"""Storage contract.

File and artifact storage are provider-specific (S3, OSS, local disk).
These Protocols describe only the operations assistant-owned code uses.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class FileStorageLike(Protocol):
    """Contract for uploading/retrieving user-uploaded files."""

    async def put(self, key: str, data: bytes, content_type: str | None = None) -> str: ...
    async def get(self, key: str) -> bytes: ...
    async def delete(self, key: str) -> None: ...
    async def presigned_url(self, key: str, expires_in: int = 3600) -> str: ...


@runtime_checkable
class ArtifactStorageLike(Protocol):
    """Contract for persisting agent-generated artifacts (images, PPT, reports)."""

    async def save(self, key: str, data: bytes, metadata: dict[str, Any] | None = None) -> str: ...
    async def load(self, key: str) -> bytes: ...
    async def url_for(self, key: str) -> str: ...


__all__ = ["ArtifactStorageLike", "FileStorageLike"]
