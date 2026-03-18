"""Shim: ImageStorageService stub.

Provides a no-op implementation for KB service standalone mode.
Full S3/OSS support can be added when needed.
"""
from typing import Optional


class ImageStorageService:
    """Stub image storage — returns local file paths."""

    def __init__(self, backend: str = "local", **kwargs):
        self.backend = backend

    async def upload_image(
        self, data: bytes, filename: str, content_type: str = "image/png",
        prefix: str = "images",
    ) -> str:
        """Store image and return a reference path."""
        import os, hashlib
        base_dir = os.environ.get("KNOWLEDGE_STORAGE__LOCAL_BASE_PATH", "./data/files")
        os.makedirs(os.path.join(base_dir, prefix), exist_ok=True)
        file_hash = hashlib.sha256(data).hexdigest()[:16]
        path = os.path.join(base_dir, prefix, f"{file_hash}_{filename}")
        with open(path, "wb") as f:
            f.write(data)
        return path

    async def get_presigned_url(self, key: str, expiry: int = 3600) -> Optional[str]:
        """Return presigned URL or local path."""
        return key

    async def delete_image(self, key: str) -> bool:
        import os
        try:
            os.remove(key)
            return True
        except FileNotFoundError:
            return False
