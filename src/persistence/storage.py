from __future__ import annotations

from typing import Any, Optional


class FileStorage:
    def __init__(self, base_path: str):
        self.base_path = base_path

    async def save_bytes(self, path: str, data: bytes) -> str:
        raise NotImplementedError

    async def get_bytes(self, path: str) -> Optional[bytes]:
        raise NotImplementedError
