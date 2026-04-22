"""Artifact Protocol + shared error type."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Protocol


class ArtifactStoreError(RuntimeError):
    pass


@dataclass
class Artifact:
    artifact_id: str
    tenant_id: str
    session_id: str
    turn_id: str
    doc_type: str                       # docx | pptx | xlsx | pdf
    filename: str
    size_bytes: int
    sha256: str
    created_at: datetime
    critic_score: Optional[float] = None
    critic_passed: Optional[bool] = None
    thumbnails: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    def key_path(self) -> str:
        """Storage-side path: tenant/session/turn/filename."""
        return f"{self.tenant_id}/{self.session_id}/{self.turn_id}/{self.filename}"

    def to_dict(self) -> dict:
        return {
            "artifact_id": self.artifact_id,
            "tenant_id": self.tenant_id,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "doc_type": self.doc_type,
            "filename": self.filename,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "created_at": self.created_at.isoformat(),
            "critic_score": self.critic_score,
            "critic_passed": self.critic_passed,
            "thumbnails": list(self.thumbnails),
            "extra": dict(self.extra),
        }


def compute_sha256(path: Path, *, chunk: int = 64 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            buf = fh.read(chunk)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


class ArtifactStore(Protocol):
    """Minimal storage surface.

    Concrete backends handle the persistence + signed-URL generation.
    """

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
    ) -> Artifact: ...

    async def get(self, artifact_id: str, *, tenant_id: str) -> Optional[Artifact]: ...

    async def download_url(self, artifact_id: str, *, tenant_id: str, ttl_seconds: int = 3600) -> str: ...

    async def list_session(self, *, tenant_id: str, session_id: str) -> list[Artifact]: ...

    async def delete(self, artifact_id: str, *, tenant_id: str) -> bool: ...
