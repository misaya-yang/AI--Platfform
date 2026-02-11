"""
Artifact helpers shared by AssistantService and AgentLoop.

Centralizes:
- Persisting ToolCallResult.output_files into ArtifactStorageService
- Returning updated output_files with artifact_id + download_url
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

from ...core.auth.user_resolver import UserContext
from ...core.observability.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class ArtifactTypeAndFormat:
    type: str  # image | document | file
    format: str  # png | pdf | ...


def _infer_type_and_format(filename: str, mime_type: str) -> ArtifactTypeAndFormat:
    """Infer artifact type/format from filename + mime_type."""
    if mime_type.startswith("image/"):
        return ArtifactTypeAndFormat(type="image", format=mime_type.split("/")[-1] or "png")
    if mime_type == "application/pdf":
        return ArtifactTypeAndFormat(type="document", format="pdf")
    if mime_type in ("text/csv", "application/csv"):
        return ArtifactTypeAndFormat(type="file", format="csv")
    if filename and "." in filename:
        return ArtifactTypeAndFormat(type="file", format=filename.rsplit(".", 1)[-1] or "bin")
    return ArtifactTypeAndFormat(type="file", format="bin")


async def persist_output_files(
    artifact_storage: Any,
    user: UserContext,
    session_id: str,
    output_files: list[dict[str, Any]],
    source: str,
    expiry_seconds: int = 3600,
) -> list[dict[str, Any]]:
    """
    Persist tool output_files into ArtifactStorageService.

    Expected input file dict keys:
    - filename
    - content_base64
    - mime_type
    - size_bytes

    Returns:
        Updated list with added:
        - artifact_id
        - download_url
        - type
        - format
    """
    if not artifact_storage or not output_files:
        return output_files

    persisted: list[dict[str, Any]] = []
    for file_info in output_files:
        filename = str(file_info.get("filename") or "output")
        mime_type = str(file_info.get("mime_type") or "application/octet-stream")
        content_base64 = file_info.get("content_base64") or ""

        try:
            content = base64.b64decode(content_base64)
        except Exception:
            logger.warning("Failed to decode artifact base64 for %s (source=%s)", filename, source)
            persisted.append(file_info)
            continue

        inferred = _infer_type_and_format(filename=filename, mime_type=mime_type)
        try:
            artifact = await artifact_storage.create_artifact(
                session_id=session_id,
                tenant_id=user.tenant_id,
                user_id=user.user_id,
                type=inferred.type,
                format=inferred.format,
                title=filename,
                filename=filename,
                content=content,
                source=source,
            )

            download_url = await artifact_storage.get_presigned_download_url(
                artifact, expiry_seconds=expiry_seconds
            )

            persisted.append(
                {
                    **file_info,
                    "artifact_id": artifact.artifact_id,
                    "download_url": download_url,
                    "type": inferred.type,
                    "format": inferred.format,
                }
            )
        except Exception as e:
            logger.warning("Failed to persist artifact %s (source=%s): %s", filename, source, e)
            persisted.append(file_info)

    return persisted
