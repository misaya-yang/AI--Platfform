"""In-memory artifact store — for unit tests only."""

from __future__ import annotations

import asyncio
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .base import Artifact, ArtifactStore, ArtifactStoreError, compute_sha256


def _new_artifact_id() -> str:
    return secrets.token_urlsafe(12)


class MemoryArtifactStore(ArtifactStore):
    def __init__(self) -> None:
        self._items: dict[tuple[str, str], Artifact] = {}
        self._blobs: dict[str, bytes] = {}
        self._lock = asyncio.Lock()

    async def put(
        self,
        path: Path,
        *,
        tenant_id: str,
        session_id: str,
        turn_id: str,
        doc_type: str,
        critic_score: Optional[float] = None,
        critic_passed: Optional[bool] = None,
        thumbnails: Optional[list[Path]] = None,
        extra: Optional[dict] = None,
    ) -> Artifact:
        if not path.is_file():
            raise ArtifactStoreError(f"artifact source missing: {path}")
        artifact_id = _new_artifact_id()
        async with self._lock:
            self._blobs[artifact_id] = path.read_bytes()
            artifact = Artifact(
                artifact_id=artifact_id,
                tenant_id=tenant_id,
                session_id=session_id,
                turn_id=turn_id,
                doc_type=doc_type,
                filename=path.name,
                size_bytes=path.stat().st_size,
                sha256=compute_sha256(path),
                created_at=datetime.now(timezone.utc),
                critic_score=critic_score,
                critic_passed=critic_passed,
                thumbnails=[str(t) for t in (thumbnails or [])],
                extra=dict(extra or {}),
            )
            self._items[(tenant_id, artifact_id)] = artifact
        return artifact

    async def get(self, artifact_id: str, *, tenant_id: str) -> Optional[Artifact]:
        return self._items.get((tenant_id, artifact_id))

    async def download_url(self, artifact_id: str, *, tenant_id: str, ttl_seconds: int = 3600) -> str:
        if (tenant_id, artifact_id) not in self._items:
            raise ArtifactStoreError(f"unknown artifact_id: {artifact_id}")
        return f"memory://{tenant_id}/{artifact_id}?ttl={ttl_seconds}"

    async def list_session(self, *, tenant_id: str, session_id: str) -> list[Artifact]:
        return sorted(
            [a for (t, _), a in self._items.items() if t == tenant_id and a.session_id == session_id],
            key=lambda a: a.created_at,
        )

    async def delete(self, artifact_id: str, *, tenant_id: str) -> bool:
        key = (tenant_id, artifact_id)
        if key in self._items:
            del self._items[key]
            self._blobs.pop(artifact_id, None)
            return True
        return False

    def _blob(self, artifact_id: str) -> Optional[bytes]:
        return self._blobs.get(artifact_id)
