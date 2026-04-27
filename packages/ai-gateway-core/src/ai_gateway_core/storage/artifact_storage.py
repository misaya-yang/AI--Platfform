"""
Artifact Storage Service.

Provides storage and database operations for conversation artifacts.
Artifacts include: images, documents, charts, code files, etc.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from .image_storage import (
    BaseStorageBackend,
    LocalStorageBackend,
    OSSStorageBackend,
    S3StorageBackend,
    StorageBackend,
    StorageConfig,
)

if TYPE_CHECKING:
    # Phase 5f Batch C will move ``DatabaseStorage`` into ai_gateway_core. Until
    # then keep the static-analysis hint pointing at the gateway location; the
    # ``from __future__ import annotations`` above keeps this out of runtime.
    from src.persistence.database import DatabaseStorage  # type: ignore[import]

logger = logging.getLogger(__name__)


@dataclass
class ArtifactInfo:
    """Artifact metadata."""

    artifact_id: str
    session_id: str
    tenant_id: str
    user_id: str
    type: str  # image, document, chart, code, file
    format: str  # png, pdf, docx, md, csv, json, etc.
    title: str
    filename: str
    storage_key: str
    size_bytes: int = 0
    mime_type: str | None = None
    source: str = "ai"  # ai | user | code_execution
    message_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for API response."""
        return {
            "artifact_id": self.artifact_id,
            "session_id": self.session_id,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "type": self.type,
            "format": self.format,
            "title": self.title,
            "filename": self.filename,
            "storage_key": self.storage_key,
            "size_bytes": self.size_bytes,
            "mime_type": self.mime_type,
            "source": self.source,
            "message_id": self.message_id,
            "metadata": self.metadata,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


# MIME type mappings
FORMAT_TO_MIME = {
    # Images
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "svg": "image/svg+xml",
    # Documents
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "doc": "application/msword",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls": "application/vnd.ms-excel",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    # Text
    "md": "text/markdown",
    "txt": "text/plain",
    "csv": "text/csv",
    "json": "application/json",
    "html": "text/html",
    # Code
    "py": "text/x-python",
    "js": "text/javascript",
    "ts": "text/typescript",
}


def get_mime_type(format: str) -> str:
    """Get MIME type from format."""
    return FORMAT_TO_MIME.get(format.lower(), "application/octet-stream")


class ArtifactStorageService:
    """
    Service for storing and managing conversation artifacts.

    Handles both file storage (S3/OSS/local) and database metadata.
    """

    def __init__(
        self,
        config: StorageConfig,
        database: DatabaseStorage | None = None,
    ):
        self.config = config
        self.database = database
        self._backend = self._create_backend()

    def _create_backend(self) -> BaseStorageBackend:
        """Create storage backend based on configuration."""
        kp = self.config.key_prefix
        if self.config.backend == StorageBackend.S3:
            return S3StorageBackend(
                bucket=self.config.s3_bucket,
                region=self.config.s3_region,
                access_key=self.config.s3_access_key,
                secret_key=self.config.s3_secret_key,
                endpoint_url=self.config.s3_endpoint_url,
                key_prefix=kp,
            )
        elif self.config.backend == StorageBackend.OSS:
            return OSSStorageBackend(
                bucket=self.config.oss_bucket,
                endpoint=self.config.oss_endpoint,
                access_key=self.config.oss_access_key,
                secret_key=self.config.oss_secret_key,
                key_prefix=kp,
            )
        else:
            return LocalStorageBackend(self.config.local_base_path, key_prefix=kp)

    @staticmethod
    def generate_artifact_id() -> str:
        """Generate unique artifact ID."""
        return f"art_{uuid.uuid4().hex[:16]}"

    @staticmethod
    def _generate_storage_key(
        tenant_id: str,
        session_id: str,
        artifact_id: str,
        filename: str,
    ) -> str:
        """Generate storage key for artifact."""
        # Sanitize filename
        safe_filename = "".join(c for c in filename if c.isalnum() or c in "._-")
        return f"artifacts/{tenant_id}/{session_id}/{artifact_id}_{safe_filename}"

    async def create_artifact(
        self,
        session_id: str,
        tenant_id: str,
        user_id: str,
        type: str,
        format: str,
        title: str,
        filename: str,
        content: bytes,
        source: str = "ai",
        message_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ArtifactInfo:
        """
        Create a new artifact: upload to storage and save metadata to database.

        Args:
            session_id: Conversation session ID
            tenant_id: Tenant ID
            user_id: User ID
            type: Artifact type (image, document, chart, code, file)
            format: File format (png, pdf, docx, etc.)
            title: Display title
            filename: Original filename
            content: File content bytes
            source: Source of artifact (ai, user, code_execution)
            message_id: Optional associated message ID
            metadata: Optional additional metadata

        Returns:
            ArtifactInfo with created artifact details
        """
        artifact_id = self.generate_artifact_id()
        storage_key = self._generate_storage_key(tenant_id, session_id, artifact_id, filename)
        mime_type = get_mime_type(format)

        # Upload to storage
        await self._backend.upload(
            key=storage_key,
            content=content,
            content_type=mime_type,
            metadata={
                "artifact_id": artifact_id,
                "session_id": session_id,
                "tenant_id": tenant_id,
                "type": type,
            },
        )

        logger.info(f"Uploaded artifact {artifact_id} to {storage_key} ({len(content)} bytes)")

        # Save to database
        artifact = ArtifactInfo(
            artifact_id=artifact_id,
            session_id=session_id,
            tenant_id=tenant_id,
            user_id=user_id,
            type=type,
            format=format,
            title=title,
            filename=filename,
            storage_key=storage_key,
            size_bytes=len(content),
            mime_type=mime_type,
            source=source,
            message_id=message_id,
            metadata=metadata or {},
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )

        if self.database and self.database._pool:
            await self._save_artifact_to_db(artifact)

        return artifact

    async def _save_artifact_to_db(self, artifact: ArtifactInfo) -> None:
        """Save artifact metadata to database."""
        import json

        async with self.database._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO artifacts (
                    artifact_id, session_id, message_id, tenant_id, user_id,
                    type, format, title, filename, storage_key, size_bytes,
                    mime_type, source, metadata, created_at, updated_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)
                ON CONFLICT (artifact_id) DO UPDATE SET
                    updated_at = EXCLUDED.updated_at
                """,
                artifact.artifact_id,
                artifact.session_id,
                artifact.message_id,
                artifact.tenant_id,
                artifact.user_id,
                artifact.type,
                artifact.format,
                artifact.title,
                artifact.filename,
                artifact.storage_key,
                artifact.size_bytes,
                artifact.mime_type,
                artifact.source,
                json.dumps(artifact.metadata),
                artifact.created_at,
                artifact.updated_at,
            )

    async def get_artifact(self, artifact_id: str) -> ArtifactInfo | None:
        """Get artifact by ID."""
        if not self.database or not self.database._pool:
            return None

        async with self.database._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM artifacts WHERE artifact_id = $1",
                artifact_id,
            )

        if not row:
            return None

        return self._row_to_artifact(row)

    async def get_session_artifacts(
        self,
        session_id: str,
        tenant_id: str | None = None,
    ) -> list[ArtifactInfo]:
        """Get all artifacts for a session."""
        if not self.database or not self.database._pool:
            return []

        async with self.database._pool.acquire() as conn:
            if tenant_id:
                rows = await conn.fetch(
                    """
                    SELECT * FROM artifacts
                    WHERE session_id = $1 AND tenant_id = $2
                    ORDER BY created_at ASC
                    """,
                    session_id,
                    tenant_id,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT * FROM artifacts
                    WHERE session_id = $1
                    ORDER BY created_at ASC
                    """,
                    session_id,
                )

        return [self._row_to_artifact(row) for row in rows]

    async def delete_artifact(self, artifact_id: str) -> bool:
        """Delete artifact from storage and database."""
        artifact = await self.get_artifact(artifact_id)
        if not artifact:
            return False

        # Delete from storage
        try:
            await self._backend.delete(artifact.storage_key)
        except Exception as e:
            logger.warning(f"Failed to delete artifact file {artifact.storage_key}: {e}")

        # Delete from database
        if self.database and self.database._pool:
            async with self.database._pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM artifacts WHERE artifact_id = $1",
                    artifact_id,
                )

        logger.info(f"Deleted artifact {artifact_id}")
        return True

    async def delete_session_artifacts(self, session_id: str) -> int:
        """Delete all artifacts for a session."""
        artifacts = await self.get_session_artifacts(session_id)

        count = 0
        for artifact in artifacts:
            if await self.delete_artifact(artifact.artifact_id):
                count += 1

        logger.info(f"Deleted {count} artifacts for session {session_id}")
        return count

    def get_download_url(
        self,
        artifact: ArtifactInfo,
        expiry_seconds: int = 3600,
    ) -> str:
        """Get download URL for artifact."""
        return self._backend.get_url(artifact.storage_key, expiry_seconds)

    async def get_presigned_download_url(
        self,
        artifact: ArtifactInfo,
        expiry_seconds: int = 3600,
    ) -> str | None:
        """Get presigned download URL (for S3)."""
        if isinstance(self._backend, S3StorageBackend):
            return await self._backend.generate_presigned_download_url(
                artifact.storage_key,
                expiry_seconds,
                artifact.filename,
            )
        return self.get_download_url(artifact, expiry_seconds)

    async def download_artifact(self, artifact_id: str) -> bytes | None:
        """Download artifact content."""
        artifact = await self.get_artifact(artifact_id)
        if not artifact:
            return None

        return await self._backend.download(artifact.storage_key)

    def _row_to_artifact(self, row) -> ArtifactInfo:
        """Convert database row to ArtifactInfo."""
        import json

        metadata = row["metadata"]
        if isinstance(metadata, str):
            metadata = json.loads(metadata)

        return ArtifactInfo(
            artifact_id=row["artifact_id"],
            session_id=row["session_id"],
            tenant_id=row["tenant_id"],
            user_id=row["user_id"],
            type=row["type"],
            format=row["format"],
            title=row["title"],
            filename=row["filename"],
            storage_key=row["storage_key"],
            size_bytes=row["size_bytes"],
            mime_type=row["mime_type"],
            source=row["source"],
            message_id=row["message_id"],
            metadata=metadata,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def close(self) -> None:
        """Close storage backend."""
        if self._backend:
            await self._backend.close()


# Global instance
_artifact_storage: ArtifactStorageService | None = None


def get_artifact_storage() -> ArtifactStorageService | None:
    """Get global artifact storage instance."""
    return _artifact_storage


def init_artifact_storage(
    config: StorageConfig,
    database: DatabaseStorage | None = None,
) -> ArtifactStorageService:
    """Initialize global artifact storage."""
    global _artifact_storage
    _artifact_storage = ArtifactStorageService(config, database)
    logger.info(f"Initialized artifact storage with backend: {config.backend}")
    return _artifact_storage
