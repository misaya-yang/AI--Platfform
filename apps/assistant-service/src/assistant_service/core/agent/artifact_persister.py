"""
Persist tool output files into ArtifactStorage and emit ARTIFACT_CREATED events.

Extracted from AgentLoop._execute_streaming_first. The function is side-effect
free w.r.t. the agent loop — it returns the persisted files, the events the
loop should yield, and the new artifact IDs.
"""

from __future__ import annotations

from typing import Any

from ai_gateway_core.logging import get_logger

from ..artifacts import persist_output_files

logger = get_logger(__name__)


# Map tool names to an artifact "source" tag for storage metadata.
_SOURCE_MAP: dict[str, str] = {
    "execute_python_code": "code_execution",
    "generate_image": "image_generation",
    "generate_document": "document_generation",
    "generate_pptx": "pptx_generation",
}


def ensure_artifact_metadata_database(
    artifact_storage: Any | None,
    database: Any | None,
) -> bool:
    """Bind artifact metadata writes to the live Assistant database pool.

    Artifact bytes and metadata form one externally visible receipt. A storage
    singleton created without a connected database can upload bytes while the
    Gateway session-artifact API permanently returns an empty list. Rebind that
    stale singleton at the composition root, where both dependencies are
    authoritative, and report whether metadata persistence is actually ready.
    """

    if artifact_storage is None or getattr(database, "_pool", None) is None:
        return False

    bound_database = getattr(artifact_storage, "database", None)
    if getattr(bound_database, "_pool", None) is None:
        try:
            artifact_storage.database = database
        except (AttributeError, TypeError):
            return False

    return getattr(getattr(artifact_storage, "database", None), "_pool", None) is not None


def resolve_artifact_storage_for_persistence(
    artifact_storage: Any | None,
    database: Any | None,
) -> Any | None:
    """Expose storage to the runtime only when metadata writes are authoritative."""

    if not ensure_artifact_metadata_database(artifact_storage, database):
        return None
    return artifact_storage


def sanitize_output_files(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove inline bytes once an owner-scoped artifact receipt exists.

    Images used to keep ``content_base64`` in the SSE result even after they
    had an ``artifact_id`` and authenticated ``download_url``. A normal PNG
    therefore exceeded the 64 KiB transport ceiling after successful provider
    generation and turned a completed write into a failed run. The Web client
    already resolves image artifact URLs with an authenticated blob fetch, so
    every persisted output can use the same bounded receipt-only contract.
    """
    sanitized: list[dict[str, Any]] = []
    for f in files:
        if f.get("artifact_id"):
            sanitized.append({**f, "content_base64": ""})
        else:
            sanitized.append(f)
    return sanitized


def _browser_safe_download_url(file_info: dict[str, Any]) -> str | None:
    """Return a URL the browser can actually render an `<img>` from.

    LocalStorageBackend produces `file://` URLs which the browser refuses
    to load for security. In that case, fall back to the authenticated
    gateway route; the frontend has a blob-fetch path that attaches the
    Bearer token and renders via a blob: URL.
    """
    raw = file_info.get("download_url")
    artifact_id = file_info.get("artifact_id")
    if isinstance(raw, str) and raw:
        if raw.startswith("file://"):
            # Never ship file:// to the browser / model — it 100% fails.
            if artifact_id:
                return f"/api/v1/assistant/artifacts/{artifact_id}/download"
            return None
        return raw
    if artifact_id:
        return f"/api/v1/assistant/artifacts/{artifact_id}/download"
    return None


def _build_artifact_event_data(file_info: dict[str, Any], source: str) -> dict[str, Any]:
    mime = str(file_info.get("mime_type") or "")
    mime_tail = mime.split("/")[-1] if mime else "bin"
    return {
        "artifact_id": file_info.get("artifact_id"),
        "type": file_info.get("type") or ("image" if mime.startswith("image/") else "file"),
        "format": file_info.get("format") or (mime_tail if mime else "bin"),
        "title": file_info.get("filename", "output"),
        "filename": file_info.get("filename"),
        "mime_type": file_info.get("mime_type"),
        "size_bytes": file_info.get("size_bytes"),
        "source": source,
        "download_url": _browser_safe_download_url(file_info),
    }


async def persist_and_collect_events(
    *,
    artifact_storage: Any,
    user: Any,
    session_id: str,
    tool_name: str,
    tool_output_files: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """
    Persist tool output files into artifact storage and build the event payloads
    the agent loop should yield as ARTIFACT_CREATED.

    Returns:
        (persisted_files, artifact_event_payloads, created_artifact_ids)
        - persisted_files: original output_files augmented with artifact_id /
          download_url (or passed through if storage is unavailable).
        - artifact_event_payloads: one dict per created artifact, ready to drop
          into AgentLoopEvent(data=...).
        - created_artifact_ids: string IDs of newly persisted artifacts.
    """
    # Always log entry so "did persistence run?" is answerable from logs.
    logger.info(
        "[artifact_persister] tool=%s output_files_in=%d storage=%s metadata_db=%s",
        tool_name,
        len(tool_output_files or []),
        "s3" if artifact_storage is not None else "none",
        bool(
            getattr(
                getattr(artifact_storage, "database", None),
                "_pool",
                None,
            )
        ),
    )

    if not tool_output_files:
        return [], [], []

    if artifact_storage is None:
        logger.warning(
            "[AgentLoop] %d output files from %s but artifact_storage is None — "
            "files will NOT be persisted",
            len(tool_output_files),
            tool_name,
        )
        return tool_output_files, [], []

    source = _SOURCE_MAP.get(tool_name, "tool_output")
    persisted = await persist_output_files(
        artifact_storage=artifact_storage,
        user=user,
        session_id=session_id,
        output_files=tool_output_files,
        source=source,
    )

    event_payloads: list[dict[str, Any]] = []
    created_ids: list[str] = []
    # Rewrite any file:// URLs in-place so the agent loop's Artifact-URL
    # block (which gets appended to tool_result_for_model) never leaks a
    # file:// URL to the model. The model otherwise copies it verbatim into
    # its markdown, producing `![chart](file:///...)` that no browser can
    # render — the "Image failed to load" saga.
    for file_info in persisted:
        safe_url = _browser_safe_download_url(file_info)
        if safe_url is not None:
            file_info["download_url"] = safe_url

    for file_info in persisted:
        artifact_id = file_info.get("artifact_id")
        if artifact_id:
            created_ids.append(str(artifact_id))
            event_payloads.append(_build_artifact_event_data(file_info, source))

    return persisted, event_payloads, created_ids
