"""Safe persistence for oversized, explicitly non-sensitive text results."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from collections.abc import Callable
from dataclasses import is_dataclass, replace
from typing import TYPE_CHECKING, Any

from ai_gateway_core.logging import record_internal_exception
from ai_gateway_core.security import redact_trace_text

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..agent_loop import AgentLoopContext

_DEFAULT_THRESHOLD_CHARS = 100_000
_HARD_MAX_BYTES = 2_000_000
_INLINE_PREVIEW_CHARS = 6_000


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
    return min(_HARD_MAX_BYTES, max(4_000, value))


class ToolOutputSpillMiddleware:
    """Persist oversized output only when its capability metadata is safe.

    The artifact contains the complete *redacted* payload.  The result carried
    forward to the model is a balanced head/tail preview, so a response cap
    cannot silently discard terminal citations while the full evidence remains
    available through a tenant-scoped artifact receipt.
    """

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
            _env_flag("ASSISTANT_TOOL_OUTPUT_SPILL_ENABLED", default=True)
            if enabled is None
            else bool(enabled)
        )
        self.threshold_chars = (
            _threshold_from_env()
            if threshold_chars is None
            else min(_HARD_MAX_BYTES, max(4_000, int(threshold_chars)))
        )

    async def on_tool_result(
        self,
        ctx: AgentLoopContext,
        tool_name: str,
        arguments: dict[str, Any],
        result: Any,
    ) -> Any:
        del arguments
        if result is None:
            return None

        # This receipt is host-owned evidence.  A tool must not be able to
        # forge a downloadable/complete claim in its own metadata.
        result_metadata = dict(getattr(result, "metadata", None) or {})
        sanitized_result = None
        if "tool_output_artifact" in result_metadata:
            result_metadata.pop("tool_output_artifact", None)
            sanitized_result = self._replace(result, metadata=result_metadata)
            result = sanitized_result

        if not self.enabled or not self.artifact_storage:
            return sanitized_result
        scoped_reader = getattr(self.artifact_storage, "read_artifact_scoped", None)
        scope_gate = getattr(self.artifact_storage, "supports_scoped_artifact_reads", None)
        try:
            scope_is_safe = callable(scope_gate) and bool(scope_gate())
        except Exception as exc:
            record_internal_exception(
                __name__,
                "assistant.core.agent.middlewares.tool_output_spill.internal_failure",
                exc,
            )
            return sanitized_result
        if not callable(scoped_reader) or not scope_is_safe:
            return sanitized_result
        if not bool(getattr(result, "success", False)):
            return sanitized_result
        payload = getattr(result, "result", None)
        if not isinstance(payload, str):
            return sanitized_result

        try:
            definition = self.definition_resolver(ctx, tool_name)
        except Exception as exc:
            record_internal_exception(
                __name__,
                "assistant.core.agent.middlewares.tool_output_spill.internal_failure",
                exc,
            )
            return sanitized_result
        metadata = dict(getattr(definition, "capability_metadata", None) or {})
        risk = str(getattr(getattr(definition, "risk_level", None), "value", "unknown"))
        if not (
            metadata.get("output_sensitivity") == "non_sensitive"
            and metadata.get("operation_kind") == "read"
            and risk == "low"
        ):
            return sanitized_result
        receipt_id = f"spill_{uuid.uuid4().hex}"

        persisted_payload = payload
        coverage = "tool_result"
        contexts = result_metadata.get("contexts")
        if tool_name == "search_knowledge_base" and isinstance(contexts, list):
            persisted_payload = json.dumps(
                {
                    "query": result_metadata.get("query"),
                    "contexts": contexts,
                    "tool_result": payload,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            coverage = "knowledge_contexts"

        if len(persisted_payload) <= self.threshold_chars:
            return sanitized_result
        redacted = redact_trace_text(persisted_payload)
        encoded = redacted.encode("utf-8")
        if len(encoded) > _HARD_MAX_BYTES:
            return sanitized_result
        content_sha256 = hashlib.sha256(encoded).hexdigest()
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
                content=encoded,
                source="tool_output_spill",
                metadata={
                    "schema_version": "assistant-tool-output-artifact/v1",
                    "redacted": True,
                    "complete_redacted": True,
                    "content_kind": "text",
                    "content_chars": len(redacted),
                    "content_sha256": content_sha256,
                    "coverage": coverage,
                    "host_receipt_id": receipt_id,
                    "run_id": ctx.run_id,
                },
                turn_id=receipt_id,
            )
            artifact_id = str(artifact.artifact_id)
            verified = await scoped_reader(
                artifact_id,
                tenant_id=ctx.tenant_id,
                session_id=ctx.session_id,
                user_id=ctx.user_id,
                max_bytes=_HARD_MAX_BYTES,
            )
            if verified is None:
                raise RuntimeError("Scoped artifact verification failed")
            verified_artifact, verified_content = verified
            verified_metadata = dict(getattr(verified_artifact, "metadata", None) or {})
            if (
                verified_content != encoded
                or getattr(verified_artifact, "source", None) != "tool_output_spill"
                or getattr(verified_artifact, "turn_id", None) != receipt_id
                or verified_metadata.get("host_receipt_id") != receipt_id
            ):
                raise RuntimeError("Scoped artifact integrity verification failed")
            download_url = await self.artifact_storage.get_presigned_download_url(artifact)
            download_url = download_url or (f"/api/v1/assistant/artifacts/{artifact_id}/download")
            output_file = {
                "artifact_id": artifact_id,
                "filename": filename,
                "mime_type": "text/plain",
                "size_bytes": len(encoded),
                "content_base64": "",
                "download_url": download_url,
                "type": "file",
                "format": "txt",
            }
            spill_receipt = {
                "artifact_id": artifact_id,
                "download_path": f"/api/v1/assistant/artifacts/{artifact_id}/download",
                "size_bytes": output_file["size_bytes"],
                "content_chars": len(redacted),
                "content_sha256": content_sha256,
                "coverage": coverage,
                "complete_redacted": True,
                "host_verified": True,
                "redacted": True,
            }
            new_metadata = {
                **result_metadata,
                "tool_output_artifact": spill_receipt,
            }
            output_files = [*list(getattr(result, "output_files", None) or []), output_file]
            preview = self._balanced_preview(redacted)
            return self._replace(
                result,
                result=preview,
                metadata=new_metadata,
                output_files=output_files,
            )
        except Exception as exc:
            record_internal_exception(
                __name__,
                "assistant.core.agent.middlewares.tool_output_spill.internal_failure",
                exc,
            )
            if artifact is not None:
                try:
                    await self.artifact_storage.delete_artifact(str(artifact.artifact_id))
                except Exception as exc:
                    record_internal_exception(
                        __name__,
                        "assistant.core.agent.middlewares.tool_output_spill.suppressed_failure",
                        exc,
                        level=logging.DEBUG,
                    )
            return sanitized_result

    @staticmethod
    def _replace(tool_result: Any, **values: Any) -> Any:
        if is_dataclass(tool_result):
            return replace(tool_result, **values)
        for name, value in values.items():
            setattr(tool_result, name, value)
        return tool_result

    @staticmethod
    def _balanced_preview(value: str) -> str:
        if len(value) <= _INLINE_PREVIEW_CHARS:
            return value
        marker = (
            "\n\n[… middle omitted from inline context; complete redacted artifact available …]\n\n"
        )
        remaining = _INLINE_PREVIEW_CHARS - len(marker)
        head_chars = remaining // 2
        tail_chars = remaining - head_chars
        return f"{value[:head_chars]}{marker}{value[-tail_chars:]}"


__all__ = ["ToolOutputSpillMiddleware"]
