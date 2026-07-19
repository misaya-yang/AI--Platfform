"""Filesystem-backed artifact store.

Layout::

  <root>/
    index.json                  — small JSON index of all artifacts
    <tenant>/<session>/<turn>/
      <filename>                — the raw file
      <filename>.meta.json      — per-artifact metadata
      thumbs/                   — optional thumbnails
"""

from __future__ import annotations

import asyncio
import json
import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .base import Artifact, ArtifactStore, ArtifactStoreError, compute_sha256


def _new_artifact_id() -> str:
    return secrets.token_urlsafe(12)


class LocalArtifactStore(ArtifactStore):
    def __init__(self, root: Path, *, base_download_url: str = "file://") -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._base_download_url = base_download_url.rstrip("/")
        self._lock = asyncio.Lock()

    # -------------------------- put

    async def put(
        self,
        path: Path,
        *,
        tenant_id: str,
        session_id: str,
        turn_id: str,
        doc_type: str,
        critic_score: float | None = None,
        critic_passed: bool | None = None,
        thumbnails: list[Path] | None = None,
        extra: dict | None = None,
    ) -> Artifact:
        if not path.is_file():
            raise ArtifactStoreError(f"artifact source missing: {path}")

        artifact_id = _new_artifact_id()
        dest_dir = self._root / tenant_id / session_id / turn_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        filename = path.name
        dest = dest_dir / filename
        if dest.exists():
            # keep deterministic; append artifact id to avoid clobber.
            filename = f"{path.stem}__{artifact_id}{path.suffix}"
            dest = dest_dir / filename
        shutil.copy2(path, dest)

        thumb_paths: list[str] = []
        if thumbnails:
            thumbs_dir = dest_dir / "thumbs"
            thumbs_dir.mkdir(parents=True, exist_ok=True)
            for src in thumbnails:
                if not src.is_file():
                    continue
                t_dest = thumbs_dir / src.name
                shutil.copy2(src, t_dest)
                thumb_paths.append(str(t_dest.relative_to(self._root)))

        artifact = Artifact(
            artifact_id=artifact_id,
            tenant_id=tenant_id,
            session_id=session_id,
            turn_id=turn_id,
            doc_type=doc_type,
            filename=filename,
            size_bytes=dest.stat().st_size,
            sha256=compute_sha256(dest),
            created_at=datetime.now(timezone.utc),
            critic_score=critic_score,
            critic_passed=critic_passed,
            thumbnails=thumb_paths,
            extra=dict(extra or {}),
        )

        meta_path = dest.with_suffix(dest.suffix + ".meta.json")
        meta_path.write_text(json.dumps(artifact.to_dict(), ensure_ascii=False, indent=2))

        async with self._lock:
            await self._update_index(artifact, "put")

        return artifact

    # -------------------------- get / list / delete

    async def get(self, artifact_id: str, *, tenant_id: str) -> Artifact | None:
        for meta_path in (self._root / tenant_id).rglob("*.meta.json"):
            data = json.loads(meta_path.read_text())
            if data.get("artifact_id") == artifact_id:
                return self._artifact_from_dict(data)
        return None

    async def list_session(self, *, tenant_id: str, session_id: str) -> list[Artifact]:
        base = self._root / tenant_id / session_id
        if not base.is_dir():
            return []
        out: list[Artifact] = []
        for meta_path in sorted(base.rglob("*.meta.json")):
            data = json.loads(meta_path.read_text())
            out.append(self._artifact_from_dict(data))
        return out

    async def delete(self, artifact_id: str, *, tenant_id: str) -> bool:
        artifact = await self.get(artifact_id, tenant_id=tenant_id)
        if artifact is None:
            return False
        target = self._root / artifact.key_path()
        meta = target.with_suffix(target.suffix + ".meta.json")
        for p in (target, meta):
            if p.exists():
                p.unlink()
        async with self._lock:
            await self._update_index(artifact, "delete")
        return True

    # -------------------------- download url

    async def download_url(self, artifact_id: str, *, tenant_id: str, ttl_seconds: int = 3600) -> str:
        artifact = await self.get(artifact_id, tenant_id=tenant_id)
        if artifact is None:
            raise ArtifactStoreError(f"unknown artifact_id: {artifact_id}")
        # Local store can't sign URLs; we expose a file:// URL for dev + a
        # signed-ish `?sig=...&exp=...` query for parity with the S3 backend.
        from .signing import sign_url

        return sign_url(f"{self._base_download_url}/{artifact.key_path()}", ttl_seconds=ttl_seconds, tenant_id=tenant_id)

    # -------------------------- internals

    def _artifact_from_dict(self, data: dict) -> Artifact:
        return Artifact(
            artifact_id=data["artifact_id"],
            tenant_id=data["tenant_id"],
            session_id=data["session_id"],
            turn_id=data["turn_id"],
            doc_type=data["doc_type"],
            filename=data["filename"],
            size_bytes=int(data["size_bytes"]),
            sha256=data["sha256"],
            created_at=datetime.fromisoformat(data["created_at"]),
            critic_score=data.get("critic_score"),
            critic_passed=data.get("critic_passed"),
            thumbnails=list(data.get("thumbnails", [])),
            extra=dict(data.get("extra", {})),
        )

    async def _update_index(self, artifact: Artifact, op: str) -> None:
        index = self._root / "index.json"
        data: dict[str, dict] = {}
        if index.exists():
            try:
                data = json.loads(index.read_text())
            except json.JSONDecodeError:
                data = {}
        if op == "put":
            data[artifact.artifact_id] = artifact.to_dict()
        elif op == "delete":
            data.pop(artifact.artifact_id, None)
        index.write_text(json.dumps(data, ensure_ascii=False, indent=2))
