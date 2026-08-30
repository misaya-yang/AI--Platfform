"""Strict lease-bound facade for the Agent model-only data plane."""

from __future__ import annotations

import contextlib
import logging
import sys
import time
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from datetime import datetime
from typing import Any

import httpx
from ai_gateway_contracts.agent_runtime import canonical_runtime_json as canonical_runtime_json
from ai_gateway_contracts.agent_runtime_lease import (
    RuntimeModelLeaseClaims,
    RuntimeModelLeaseSigner,
)
from ai_gateway_contracts.agent_runtime_lease import (
    RuntimeModelLeaseError as RuntimeModelLeaseError,
)
from ai_gateway_core.models import (
    ReasoningWireError as ReasoningWireError,
)
from ai_gateway_core.models import apply_reasoning_wire

from ..metrics.redaction import redact_sensitive_text as redact_sensitive_text
from .model import (
    accounting,
    authorization,
    chat_completions,
    native_responses,
    request_builder,
)
from .model import (
    timing as model_timing,
)
from .model.authorization import (
    _TOOL_NAME_RE as _TOOL_NAME_RE,
)
from .model.authorization import (
    KERNEL_TOOL_TRANSCRIPT_NAMES as KERNEL_TOOL_TRANSCRIPT_NAMES,
)
from .model.authorization import (
    KERNEL_TOOL_TRANSCRIPT_NAMESPACES as KERNEL_TOOL_TRANSCRIPT_NAMESPACES,
)
from .model.authorization import (
    AgentModelPlaneError,
    _AuthorizedCall,
    _Database,
)
from .model.native_responses import _ValidatedNativeTools
from .model.stream_projection import (
    _NativeResponsesStreamValidator as _NativeResponsesStreamValidator,
)
from .model.stream_projection import _NativeResponsesTerminal as _NativeResponsesTerminal
from .model.stream_projection import _ResponsesProjector as _ResponsesProjector
from .timing import ModelPlaneTiming

logger = logging.getLogger(__name__)
_FACADE_MODULE = sys.modules[__name__]


def _is_unnamespaced(value: Any) -> bool:
    return authorization._is_unnamespaced(value)


def _is_kernel_tool_identity(name: Any, namespace: Any = None) -> bool:
    return authorization._is_kernel_tool_identity(
        name, namespace, _helpers=_FACADE_MODULE
    )


def _is_allowed_tool_identity(
    name: Any,
    namespace: Any,
    *,
    allowed_tool_names: set[str] | None,
    allowed_namespaced_tools: set[tuple[str, str]] | None,
) -> bool:
    return authorization._is_allowed_tool_identity(
        name,
        namespace,
        allowed_tool_names=allowed_tool_names,
        allowed_namespaced_tools=allowed_namespaced_tools,
        _helpers=_FACADE_MODULE,
    )


def _runtime_snapshot(value: Any) -> dict[str, Any]:
    return authorization._runtime_snapshot(value)


def _snapshot_parameters(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    return authorization._snapshot_parameters(snapshot)


def _snapshot_responses_tool_controls(
    snapshot: Mapping[str, Any],
) -> tuple[set[str] | None, str | dict[str, str], bool]:
    return authorization._snapshot_responses_tool_controls(snapshot)


def _timestamp_ms(value: datetime) -> int:
    return authorization._timestamp_ms(value)


def _provider_revision(value: Any) -> str:
    return authorization._provider_revision(value)


def _runtime_scope_sha256(tenant_id: str, user_id: str, session_id: str) -> str:
    return authorization._runtime_scope_sha256(tenant_id, user_id, session_id)


def _estimate_tokens(value: Any) -> int:
    return authorization._estimate_tokens(
        value, _canonical_runtime_json=canonical_runtime_json
    )


def _cost_microusd(
    input_tokens: int,
    output_tokens: int,
    *,
    input_price_per_1k: float,
    output_price_per_1k: float,
) -> int:
    return accounting._cost_microusd(
        input_tokens,
        output_tokens,
        input_price_per_1k=input_price_per_1k,
        output_price_per_1k=output_price_per_1k,
    )


def _chat_completions_url(base_url: str) -> str:
    return request_builder._chat_completions_url(base_url)


def _responses_url(base_url: str) -> str:
    return request_builder._responses_url(base_url)


def _validate_phase2_responses_input(
    body: Mapping[str, Any], *, allow_tool_transcript: bool = False
) -> None:
    request_builder._validate_phase2_responses_input(
        body, allow_tool_transcript=allow_tool_transcript
    )


def _function_tool(
    raw: Mapping[str, Any],
    *,
    wire_name: str | None = None,
    description_prefix: str = "",
) -> dict[str, Any]:
    return native_responses._function_tool(
        raw, wire_name=wire_name, description_prefix=description_prefix
    )


def _namespace_alias(namespace: str, name: str) -> str:
    return native_responses._namespace_alias(namespace, name)


def _validated_native_tools(
    value: Any, profile: Mapping[str, Any]
) -> _ValidatedNativeTools:
    return native_responses._validated_native_tools(
        value, profile, _helpers=_FACADE_MODULE
    )


def _native_tool_transcript(
    value: Any,
    *,
    aliases: Mapping[str, tuple[str, str]],
    wire_aliases: Mapping[tuple[str, str], str],
) -> Any:
    return native_responses._native_tool_transcript(
        value,
        aliases=aliases,
        wire_aliases=wire_aliases,
        _helpers=_FACADE_MODULE,
    )


def _native_responses_body(
    body: Mapping[str, Any],
    *,
    model_id: str,
    max_output_tokens: int,
    profile: Mapping[str, Any],
    reasoning_option: str,
    allowed_tool_names: set[str] | None = None,
    tool_choice: str | dict[str, str] = "auto",
    parallel_tool_calls: bool = True,
) -> tuple[dict[str, Any], dict[str, tuple[str, str]]]:
    return native_responses._native_responses_body(
        body,
        model_id=model_id,
        max_output_tokens=max_output_tokens,
        profile=profile,
        reasoning_option=reasoning_option,
        allowed_tool_names=allowed_tool_names,
        tool_choice=tool_choice,
        parallel_tool_calls=parallel_tool_calls,
        _apply_reasoning_wire=apply_reasoning_wire,
        _helpers=_FACADE_MODULE,
    )


def _provider_headers(profile: Mapping[str, Any], api_key: str) -> dict[str, str]:
    return request_builder._provider_headers(profile, api_key)


def _content_text(value: Any) -> str:
    return request_builder._content_text(value)


def _native_responses_input(value: Any) -> Any:
    return request_builder._native_responses_input(value)


def _chat_tools_from_runtime(
    raw_tools: Any,
    profile: Mapping[str, Any],
    *,
    allowed_tool_names: set[str] | None,
) -> list[dict[str, Any]]:
    return chat_completions._chat_tools_from_runtime(
        raw_tools,
        profile,
        allowed_tool_names=allowed_tool_names,
        _helpers=_FACADE_MODULE,
    )


def _validate_tool_transcript(
    raw_input: list[Any],
    *,
    allowed_tool_names: set[str] | None = None,
    allowed_namespaced_tools: set[tuple[str, str]] | None = None,
) -> None:
    request_builder._validate_tool_transcript(
        raw_input,
        allowed_tool_names=allowed_tool_names,
        allowed_namespaced_tools=allowed_namespaced_tools,
        _logger=logger,
        _helpers=_FACADE_MODULE,
    )


def _responses_input_to_messages(
    body: Mapping[str, Any],
    *,
    allowed_tool_names: set[str] | None = None,
) -> list[dict[str, Any]]:
    return chat_completions._responses_input_to_messages(
        body,
        allowed_tool_names=allowed_tool_names,
        _logger=logger,
        _helpers=_FACADE_MODULE,
    )


class AgentModelPlane:
    def __init__(
        self,
        *,
        database: _Database,
        provider_service: Any,
        lease_signer: RuntimeModelLeaseSigner,
        http_client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.database = database
        self.provider_service = provider_service
        self.lease_signer = lease_signer
        self.http_client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        )
        self._owns_http_client = http_client is None
        self._clock = clock

    async def close(self) -> None:
        if self._owns_http_client:
            await self.http_client.aclose()

    async def _validate_turn_thread_scope(
        self,
        *,
        claims: RuntimeModelLeaseClaims,
        turn_metadata: Mapping[str, Any],
    ) -> None:
        await authorization._validate_turn_thread_scope(
            self,
            claims=claims,
            turn_metadata=turn_metadata,
            _helpers=_FACADE_MODULE,
        )

    async def authorize_and_reserve(
        self,
        *,
        body: dict[str, Any],
        turn_metadata: dict[str, Any],
    ) -> _AuthorizedCall:
        return await authorization.authorize_and_reserve(
            self,
            body=body,
            turn_metadata=turn_metadata,
            _helpers=_FACADE_MODULE,
        )

    async def stream(
        self,
        *,
        body: dict[str, Any],
        turn_metadata: dict[str, Any],
        authorized_call: _AuthorizedCall | None = None,
    ) -> AsyncIterator[bytes]:
        chunks = chat_completions.stream(
            self,
            body=body,
            turn_metadata=turn_metadata,
            authorized_call=authorized_call,
            _helpers=_FACADE_MODULE,
        )
        async with contextlib.aclosing(chunks):
            async for chunk in chunks:
                yield chunk

    async def _stream_native_responses(
        self,
        *,
        call: _AuthorizedCall,
        timing: ModelPlaneTiming,
        body: dict[str, Any],
        profile: Mapping[str, Any],
        reasoning: Mapping[str, Any],
        api_key: str,
        base_url: str,
        allowed_tool_names: set[str] | None = None,
        tool_choice: str | dict[str, str] = "auto",
        parallel_tool_calls: bool = True,
    ) -> AsyncIterator[bytes]:
        chunks = native_responses._stream_native_responses(
            self,
            call=call,
            timing=timing,
            body=body,
            profile=profile,
            reasoning=reasoning,
            api_key=api_key,
            base_url=base_url,
            allowed_tool_names=allowed_tool_names,
            tool_choice=tool_choice,
            parallel_tool_calls=parallel_tool_calls,
            _helpers=_FACADE_MODULE,
        )
        async with contextlib.aclosing(chunks):
            async for chunk in chunks:
                yield chunk

    def _log_model_plane_timing(
        self, wire: str, call: _AuthorizedCall, timing: ModelPlaneTiming
    ) -> None:
        model_timing._log_model_plane_timing(
            self, wire, call, timing, _logger=logger
        )

    async def _complete_call(
        self,
        *,
        call: _AuthorizedCall,
        input_tokens: int,
        output_tokens: int,
        provider_request_id: str | None,
    ) -> None:
        await accounting._complete_call(
            self,
            call=call,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            provider_request_id=provider_request_id,
            _helpers=_FACADE_MODULE,
        )

    async def _fail_call(
        self, call_id: uuid.UUID, code: str, *, dispatched: bool
    ) -> None:
        await accounting._fail_call(self, call_id, code, dispatched=dispatched)

    async def _mark_unknown_if_dispatched(self, call_id: uuid.UUID) -> None:
        await accounting._mark_unknown_if_dispatched(self, call_id)


__all__ = ["AgentModelPlane", "AgentModelPlaneError"]
