"""Storage contract.

File and artifact storage are provider-specific (S3, OSS, local disk).
These Protocols describe the operations assistant-owned code actually
calls on storage instances.

NoOp reference impls are provided so an un-injected AssistantService
degrades to silent no-op behavior instead of NoneType-crashing.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class FileStorageLike(Protocol):
    """Contract for uploading/retrieving user-uploaded files."""

    # ``config`` attribute access (``.config.backend``, ``.config.local_base_path``)
    # leaks from two diagnostic-logging lines in the assistant. Typed as Any so
    # Protocol-satisfying null impls can expose a lightweight stand-in without
    # dragging the concrete StorageConfig class into ai-gateway-core.
    config: Any

    async def download_file(self, path: str) -> bytes: ...


@runtime_checkable
class ArtifactStorageLike(Protocol):
    """Contract for persisting agent-generated artifacts (images, PPT, reports)."""

    async def create_artifact(self, **fields: Any) -> Any: ...

    async def get_presigned_download_url(
        self, artifact: Any, expiry_seconds: int = 3600
    ) -> str: ...


class _NoOpConfig:
    """Minimal stand-in for StorageConfig used by NoOpFileStorage."""

    class _NoOpBackend:
        value = "noop"

    backend = _NoOpBackend()
    local_base_path = "/tmp"


class NoOpFileStorage:
    """Protocol-satisfying null FileStorage. download_file returns empty bytes."""

    config: Any = _NoOpConfig()

    async def download_file(self, path: str) -> bytes:
        return b""


class NoOpArtifactStorage:
    """Protocol-satisfying null ArtifactStorage. Persist calls return None-like stubs."""

    async def create_artifact(self, **fields: Any) -> Any:
        return None

    async def get_presigned_download_url(
        self, artifact: Any, expiry_seconds: int = 3600
    ) -> str:
        return ""


__all__ = [
    "ArtifactStorageLike",
    "FileStorageLike",
    "NoOpArtifactStorage",
    "NoOpFileStorage",
]
