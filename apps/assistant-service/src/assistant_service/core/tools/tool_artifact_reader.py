"""Tenant/session/user-scoped reader for redacted oversized tool output."""

from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import replace
from typing import Any

from ai_gateway_core.logging import record_internal_exception

from ..rag.context_engine import estimate_tokens
from .tool_registry import (
    ToolCallRequest,
    ToolCallResult,
    ToolCategory,
    ToolDefinition,
    ToolExecutor,
    ToolParameter,
    ToolRiskLevel,
    register_tool,
)

_ARTIFACT_ID_RE = re.compile(r"^art_[A-Za-z0-9]{8,64}$")
_MAX_ARTIFACT_BYTES = 2_000_000
_ABSOLUTE_MAX_LIMIT_TOKENS = 20_000


def _operator_max_tokens() -> int:
    try:
        configured = int(os.getenv("ASSISTANT_TOOL_ARTIFACT_READ_MAX_TOKENS", "8000"))
    except ValueError:
        configured = 8_000
    return min(_ABSOLUTE_MAX_LIMIT_TOKENS, max(256, configured))


_MAX_LIMIT_TOKENS = _operator_max_tokens()
_DEFAULT_LIMIT_TOKENS = min(4_000, _MAX_LIMIT_TOKENS)
_REQUIRED_METADATA = {
    "schema_version": "assistant-tool-output-artifact/v1",
    "redacted": True,
    "complete_redacted": True,
    "content_kind": "text",
}


READ_TOOL_ARTIFACT_DEFINITION = ToolDefinition(
    name="read_tool_artifact",
    description=(
        "Read a bounded UTF-8 character slice from a complete redacted tool-output artifact "
        "created in this exact tenant, user, and session. Use the artifact_id from a "
        "COMPLETE_REDACTED_ARTIFACT_RECEIPT; offset is a 0-based character offset."
    ),
    parameters=[
        ToolParameter(
            name="artifact_id",
            type="string",
            description="Opaque artifact id from a tool-output receipt (never a path or URL).",
            schema_constraints={"pattern": r"^art_[A-Za-z0-9]{8,64}$", "maxLength": 68},
        ),
        ToolParameter(
            name="offset",
            type="integer",
            description="0-based UTF-8 decoded character offset. Default 0.",
            required=False,
            default=0,
            schema_constraints={"minimum": 0, "maximum": _MAX_ARTIFACT_BYTES},
        ),
        ToolParameter(
            name="limit",
            type="integer",
            description=(
                f"Approximate token budget for this slice (1-{_MAX_LIMIT_TOKENS}); "
                f"default {_DEFAULT_LIMIT_TOKENS}."
            ),
            required=False,
            default=_DEFAULT_LIMIT_TOKENS,
            schema_constraints={"minimum": 1, "maximum": _MAX_LIMIT_TOKENS},
        ),
    ],
    category=ToolCategory.RETRIEVAL,
    risk_level=ToolRiskLevel.LOW,
    timeout_seconds=15,
    max_retries=0,
    capability_metadata={
        "operation_kind": "read",
        "read_only": True,
        "output_sensitivity": "non_sensitive",
        "persist_large_output": False,
    },
)


class ReadToolArtifactExecutor(ToolExecutor):
    def __init__(
        self,
        artifact_storage: Any,
        *,
        max_limit_tokens: int = _MAX_LIMIT_TOKENS,
    ) -> None:
        self.artifact_storage = artifact_storage
        self.max_limit_tokens = min(
            _ABSOLUTE_MAX_LIMIT_TOKENS,
            max(256, int(max_limit_tokens)),
        )
        self.default_limit_tokens = min(4_000, self.max_limit_tokens)

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        started = time.monotonic()
        artifact_id = str(request.arguments.get("artifact_id") or "")
        scope = {
            key: str(request.metadata.get(key) or "")
            for key in ("tenant_id", "session_id", "user_id")
        }
        offset = request.arguments.get("offset", 0)
        limit = request.arguments.get("limit", self.default_limit_tokens)
        invalid = (
            _ARTIFACT_ID_RE.fullmatch(artifact_id) is None
            or not all(scope.values())
            or isinstance(offset, bool)
            or not isinstance(offset, int)
            or not 0 <= offset <= _MAX_ARTIFACT_BYTES
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= self.max_limit_tokens
        )
        if invalid:
            return self._error(request, "ARTIFACT_READ_INVALID", started)

        scoped_reader = getattr(self.artifact_storage, "read_artifact_scoped", None)
        if not callable(scoped_reader):
            return self._error(request, "ARTIFACT_READ_UNAVAILABLE", started)
        try:
            scoped = await scoped_reader(
                artifact_id,
                max_bytes=_MAX_ARTIFACT_BYTES,
                **scope,
            )
        except Exception as exc:
            record_internal_exception(
                __name__, "assistant.core.tools.tool_artifact_reader.internal_failure", exc
            )
            return self._error(request, "ARTIFACT_READ_UNAVAILABLE", started)
        if scoped is None:
            return self._error(request, "ARTIFACT_NOT_FOUND", started)
        artifact, raw = scoped
        metadata = dict(getattr(artifact, "metadata", None) or {})
        if (
            getattr(artifact, "source", None) != "tool_output_spill"
            or any(metadata.get(key) != value for key, value in _REQUIRED_METADATA.items())
            or len(raw) > _MAX_ARTIFACT_BYTES
        ):
            return self._error(request, "ARTIFACT_NOT_READABLE", started)

        digest = hashlib.sha256(raw).hexdigest()
        if metadata.get("content_sha256") != digest:
            return self._error(request, "ARTIFACT_INTEGRITY_FAILED", started)
        receipt_id = str(metadata.get("host_receipt_id") or "")
        if not receipt_id or str(getattr(artifact, "turn_id", "") or "") != receipt_id:
            return self._error(request, "ARTIFACT_INTEGRITY_FAILED", started)
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return self._error(request, "ARTIFACT_NOT_READABLE", started)
        if metadata.get("content_chars") != len(text):
            return self._error(request, "ARTIFACT_INTEGRITY_FAILED", started)
        if offset > len(text):
            return self._error(request, "ARTIFACT_OFFSET_OUT_OF_RANGE", started)

        end = self._end_for_token_budget(text, offset=offset, limit_tokens=limit)
        return ToolCallResult(
            call_id=request.call_id,
            tool_name=request.tool_name,
            success=True,
            result={
                "artifact_id": artifact_id,
                "content": text[offset:end],
                "offset": offset,
                "next_offset": end if end < len(text) else None,
                "total_chars": len(text),
                "content_sha256": digest,
                "complete": end == len(text),
                "artifact_complete": True,
                "redacted": True,
                "returned_tokens_estimate": estimate_tokens(text[offset:end]),
                "redaction_receipt": {
                    "schema_version": _REQUIRED_METADATA["schema_version"],
                    "complete_redacted": True,
                    "host_verified": True,
                },
            },
            duration_ms=(time.monotonic() - started) * 1000,
            metadata={"artifact_read_verified": True},
        )

    @staticmethod
    def _end_for_token_budget(text: str, *, offset: int, limit_tokens: int) -> int:
        """Find the largest character boundary inside the conservative token budget."""

        high = min(len(text), offset + (limit_tokens * 4) + 8)
        if high == len(text) and estimate_tokens(text[offset:high]) <= limit_tokens:
            return high
        low = offset
        while low < high:
            mid = (low + high + 1) // 2
            if estimate_tokens(text[offset:mid]) <= limit_tokens:
                low = mid
            else:
                high = mid - 1
        return low

    @staticmethod
    def _error(request: ToolCallRequest, code: str, started: float) -> ToolCallResult:
        return ToolCallResult(
            call_id=request.call_id,
            tool_name=request.tool_name,
            success=False,
            error=code,
            duration_ms=(time.monotonic() - started) * 1000,
        )


def register_tool_artifact_reader(
    artifact_storage: Any,
    *,
    max_limit_tokens: int = _MAX_LIMIT_TOKENS,
) -> bool:
    if not artifact_storage or not callable(
        getattr(artifact_storage, "read_artifact_scoped", None)
    ):
        return False
    scope_gate = getattr(artifact_storage, "supports_scoped_artifact_reads", None)
    try:
        scope_is_safe = callable(scope_gate) and bool(scope_gate())
    except Exception as exc:
        record_internal_exception(
            __name__, "assistant.core.tools.tool_artifact_reader.internal_failure", exc
        )
        return False
    if not scope_is_safe:
        return False
    resolved_max = min(_ABSOLUTE_MAX_LIMIT_TOKENS, max(256, int(max_limit_tokens)))
    parameters = [replace(parameter) for parameter in READ_TOOL_ARTIFACT_DEFINITION.parameters]
    for parameter in parameters:
        if parameter.name != "limit":
            continue
        parameter.default = min(4_000, resolved_max)
        parameter.description = (
            f"Approximate token budget for this slice (1-{resolved_max}); "
            f"default {parameter.default}."
        )
        parameter.schema_constraints = {"minimum": 1, "maximum": resolved_max}
    definition = replace(READ_TOOL_ARTIFACT_DEFINITION, parameters=parameters)
    register_tool(
        definition,
        ReadToolArtifactExecutor(
            artifact_storage,
            max_limit_tokens=resolved_max,
        ),
    )
    return True


__all__ = [
    "READ_TOOL_ARTIFACT_DEFINITION",
    "ReadToolArtifactExecutor",
    "register_tool_artifact_reader",
]
