"""S3-compatible artifact store (AWS / MinIO / R2 / Aliyun OSS).

Kept as a thin implementation — boto3 / aioboto3 handles the heavy lifting.
We do NOT make boto3 a hard dependency; the class imports lazily so that
tests and LocalArtifactStore users don't need it installed.
"""

from __future__ import annotations

import io
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .base import Artifact, ArtifactStore, ArtifactStoreError, compute_sha256


def _new_artifact_id() -> str:
    return secrets.token_urlsafe(12)


class S3ArtifactStore(ArtifactStore):
    def __init__(
        self,
        *,
        bucket: str,
        region: Optional[str] = None,
        endpoint_url: Optional[str] = None,
        aws_access_key_id: Optional[str] = None,
        aws_secret_access_key: Optional[str] = None,
        prefix: str = "docgen",
    ) -> None:
        try:
            import boto3  # type: ignore
        except ImportError as e:  # pragma: no cover — depends on env
            raise ArtifactStoreError("boto3 is required for S3ArtifactStore") from e

        self._bucket = bucket
        self._prefix = prefix.strip("/")
        self._s3 = boto3.client(
            "s3",
            region_name=region,
            endpoint_url=endpoint_url,
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
        )

    def _key(self, artifact: Artifact, *, suffix: str = "") -> str:
        return f"{self._prefix}/{artifact.key_path()}{suffix}"

    # -------------------------- put

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
            thumbnails=[],
            extra=dict(extra or {}),
        )
        key = self._key(artifact)
        with open(path, "rb") as fh:
            self._s3.upload_fileobj(fh, self._bucket, key, ExtraArgs={"ContentType": "application/octet-stream"})

        # Thumbnails
        thumb_keys: list[str] = []
        for thumb in thumbnails or []:
            if not thumb.is_file():
                continue
            tk = f"{self._prefix}/{tenant_id}/{session_id}/{turn_id}/thumbs/{thumb.name}"
            with open(thumb, "rb") as fh:
                self._s3.upload_fileobj(fh, self._bucket, tk, ExtraArgs={"ContentType": "image/jpeg"})
            thumb_keys.append(tk)
        artifact.thumbnails = thumb_keys

        meta_key = self._key(artifact, suffix=".meta.json")
        self._s3.put_object(
            Bucket=self._bucket,
            Key=meta_key,
            Body=json.dumps(artifact.to_dict(), ensure_ascii=False).encode("utf-8"),
            ContentType="application/json",
        )
        return artifact

    # -------------------------- get / list / delete

    async def get(self, artifact_id: str, *, tenant_id: str) -> Optional[Artifact]:
        prefix = f"{self._prefix}/{tenant_id}/"
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            for obj in page.get("Contents", []) or []:
                key = obj["Key"]
                if key.endswith(".meta.json"):
                    meta = self._s3.get_object(Bucket=self._bucket, Key=key)
                    data = json.loads(meta["Body"].read())
                    if data.get("artifact_id") == artifact_id:
                        return self._artifact_from_dict(data)
        return None

    async def list_session(self, *, tenant_id: str, session_id: str) -> list[Artifact]:
        prefix = f"{self._prefix}/{tenant_id}/{session_id}/"
        paginator = self._s3.get_paginator("list_objects_v2")
        artifacts: list[Artifact] = []
        for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            for obj in page.get("Contents", []) or []:
                if obj["Key"].endswith(".meta.json"):
                    data = json.loads(self._s3.get_object(Bucket=self._bucket, Key=obj["Key"])["Body"].read())
                    artifacts.append(self._artifact_from_dict(data))
        artifacts.sort(key=lambda a: a.created_at)
        return artifacts

    async def delete(self, artifact_id: str, *, tenant_id: str) -> bool:
        art = await self.get(artifact_id, tenant_id=tenant_id)
        if art is None:
            return False
        for key in (self._key(art), self._key(art, suffix=".meta.json")):
            self._s3.delete_object(Bucket=self._bucket, Key=key)
        for tk in art.thumbnails:
            try:
                self._s3.delete_object(Bucket=self._bucket, Key=tk)
            except Exception:
                pass
        return True

    async def download_url(self, artifact_id: str, *, tenant_id: str, ttl_seconds: int = 3600) -> str:
        art = await self.get(artifact_id, tenant_id=tenant_id)
        if art is None:
            raise ArtifactStoreError(f"unknown artifact_id: {artifact_id}")
        return self._s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket, "Key": self._key(art)},
            ExpiresIn=ttl_seconds,
        )

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
