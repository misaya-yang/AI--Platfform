"""Opt-in persistence for oversized, explicitly non-sensitive text results."""

from __future__ import annotations

import contextlib
import os
import uuid
from collections.abc import Callable
from dataclasses import is_dataclass, replace
from typing import TYPE_CHECKING, Any

from ai_gateway_core.security import redact_trace_text

if TYPE_CHECKING:
    from ..agent_loop import AgentLoopContext

_DEFAULT_THRESHOLD_CHARS = 100_000
_HARD_MAX_CHARS = 2_000_000


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _threshold_from_env() -> int:
    try:
        value = int(
            os.getenv("ASSISTANT_TOOL_OUTPUT_SPILL_THRESHOLD_CHARS", "") or _DEFAULT_THRESHOLD_CHARS
        )
    except ValueError:
        return _DEFAULT_THRESHOLD_CHARS
    return min(_HARD_MAX_CHARS, max(4_000, value))


class ToolOutputSpillMiddleware:
    """Persist only low-risk, read-only text whose metadata opts in."""

    name = "tool_output_spill"

    def __init__(
        self,
        *,
        artifact_storage: Any,
        definition_resolver: Callable[[AgentLoopContext, str], Any | None],
        enabled: bool | None = None,
        threshold_chars: int | None = None,
    ) -> None:
        self.artifact_storage = artifact_storage
        self.definition_resolver = definition_resolver
        self.enabled = (
            _env_flag("ASSISTANT_TOOL_OUTPUT_SPILL_ENABLED") if enabled is None else bool(enabled)
        )
        self.threshold_chars = (
            _threshold_from_env()
            if threshold_chars is None
            else min(_HARD_MAX_CHARS, max(4_000, int(threshold_chars)))
        )

    async def on_tool_result(
        self,
        ctx: AgentLoopContext,
        tool_name: str,
        arguments: dict[str, Any],
        result: Any,
    ) -> Any:
        del arguments
        if not self.enabled or not self.artifact_storage or result is None:
            return None
        if not bool(getattr(result, "success", False)):
            return None
        payload = getattr(result, "result", None)
        if not isinstance(payload, str) or len(payload) <= self.threshold_chars:
            return None
        if len(payload) > _HARD_MAX_CHARS:
            return None

        definition = self.definition_resolver(ctx, tool_name)
        metadata = dict(getattr(definition, "capability_metadata", None) or {})
        risk = str(getattr(getattr(definition, "risk_level", None), "value", "unknown"))
        if not (
            metadata.get("persist_large_output") is True
            and metadata.get("output_sensitivity") == "non_sensitive"
            and metadata.get("operation_kind") == "read"
            and risk == "low"
        ):
            return None

        redacted = redact_trace_text(payload)
        filename = f"tool-output-{uuid.uuid4().hex[:16]}.txt"
        artifact = None
        try:
            artifact = await self.artifact_storage.create_artifact(
                session_id=ctx.session_id,
                tenant_id=ctx.tenant_id,
                user_id=ctx.user_id,
                type="file",
                format="txt",
                title="Tool output",
                filename=filename,
                content=redacted.encode("utf-8"),
                source="tool_output_spill",
                metadata={
                    "redacted": True,
                    "content_kind": "text",
                    "run_id": ctx.run_id,
                },
            )
            artifact_id = str(artifact.artifact_id)
            download_url = await self.artifact_storage.get_presigned_download_url(artifact)
            download_url = download_url or (f"/api/v1/assistant/artifacts/{artifact_id}/download")
            output_file = {
                "artifact_id": artifact_id,
                "filename": filename,
                "mime_type": "text/plain",
                "size_bytes": len(redacted.encode("utf-8")),
                "content_base64": "",
                "download_url": download_url,
                "type": "file",
                "format": "txt",
            }
            spill_receipt = {
                "artifact_id": artifact_id,
                "download_path": f"/api/v1/assistant/artifacts/{artifact_id}/download",
                "size_bytes": output_file["size_bytes"],
                "redacted": True,
            }
            new_metadata = {
                **dict(getattr(result, "metadata", None) or {}),
                "tool_output_artifact": spill_receipt,
            }
            output_files = [*list(getattr(result, "output_files", None) or []), output_file]
            if is_dataclass(result):
                return replace(result, metadata=new_metadata, output_files=output_files)
            result.metadata = new_metadata
            result.output_files = output_files
            return result
        except Exception:
            if artifact is not None:
                with contextlib.suppress(Exception):
                    await self.artifact_storage.delete_artifact(str(artifact.artifact_id))
            return None


__all__ = ["ToolOutputSpillMiddleware"]
