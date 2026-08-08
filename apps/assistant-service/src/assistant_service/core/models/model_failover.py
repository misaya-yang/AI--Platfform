"""Conservative, pre-output model failover for Assistant streams."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx
from ai_gateway_core.enums import ModelAccessLevel
from ai_gateway_core.logging import get_logger

from .model_registry import ModelInfo, ProviderStreamError, StreamDelta

logger = get_logger(__name__)

_ACCESS_RANK = {
    ModelAccessLevel.PUBLIC: 0,
    ModelAccessLevel.PREMIUM: 1,
    ModelAccessLevel.ADMIN: 2,
}
_TRANSIENT_PROVIDER_ERRORS = frozenset(
    {"api_error", "overloaded_error", "rate_limit_error", "server_error"}
)


@dataclass(frozen=True)
class ModelFailoverNotice:
    """Bounded routing receipt safe for public metadata and traces."""

    requested_model: str
    failed_model: str
    served_model: str
    failure_class: str
    attempt: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_type": "model_failover",
            "requested_model": self.requested_model,
            "failed_model": self.failed_model,
            "served_model": self.served_model,
            "failure_class": self.failure_class,
            "attempt": self.attempt,
        }


@dataclass(frozen=True)
class ModelStreamItem:
    """One model delta or one pre-delta routing notice."""

    delta: StreamDelta | None = None
    notice: ModelFailoverNotice | None = None
    model_id: str | None = None


def parse_model_fallbacks(raw: str | None = None) -> dict[str, tuple[str, ...]]:
    """Parse an explicit ordered primary-to-candidates map, failing closed."""

    source = os.getenv("ASSISTANT_MODEL_FALLBACKS_JSON", "") if raw is None else raw
    if not source.strip():
        return {}
    try:
        payload = json.loads(source)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Ignoring invalid ASSISTANT_MODEL_FALLBACKS_JSON")
        return {}
    if not isinstance(payload, dict):
        logger.warning("Ignoring non-object ASSISTANT_MODEL_FALLBACKS_JSON")
        return {}

    parsed: dict[str, tuple[str, ...]] = {}
    for raw_primary, raw_candidates in payload.items():
        if not isinstance(raw_primary, str) or not raw_primary.strip():
            continue
        primary = raw_primary.strip()
        if not isinstance(raw_candidates, list):
            continue
        candidates: list[str] = []
        for value in raw_candidates[:8]:
            if not isinstance(value, str):
                continue
            candidate = value.strip()
            if candidate and candidate != primary and candidate not in candidates:
                candidates.append(candidate)
        if candidates:
            parsed[primary] = tuple(candidates)
    return parsed


def classify_failover_failure(exc: BaseException) -> str | None:
    """Return the only failure classes eligible for a pre-delta failover."""

    if isinstance(exc, asyncio.CancelledError):
        return None
    if isinstance(exc, httpx.HTTPStatusError):
        status = int(getattr(exc.response, "status_code", 0) or 0)
        if status == 429:
            return "rate_limited"
        if 500 <= status <= 599:
            return "provider_5xx"
        return None
    if isinstance(exc, httpx.TimeoutException | TimeoutError):
        return "timeout_before_delta"
    if isinstance(exc, httpx.RequestError):
        return "transport_error"
    if isinstance(exc, ProviderStreamError) and exc.error_type in _TRANSIENT_PROVIDER_ERRORS:
        return "provider_transient"
    return None


def _caller_access_rank(user: Any | None) -> int:
    roles = {str(value).strip().lower() for value in (getattr(user, "roles", None) or [])}
    tier = (
        str(getattr(user, "user_tier", None) or getattr(user, "tier", None) or "").strip().lower()
    )
    if tier == "admin" or "admin" in roles:
        return _ACCESS_RANK[ModelAccessLevel.ADMIN]
    if tier in {"premium", "enterprise"}:
        return _ACCESS_RANK[ModelAccessLevel.PREMIUM]
    return _ACCESS_RANK[ModelAccessLevel.PUBLIC]


def _model_is_eligible(
    registry: Any,
    model: ModelInfo | None,
    *,
    user: Any | None,
    requires_tools: bool,
    requires_vision: bool,
    requires_native_search: bool,
    min_context_window: int,
    required_output_tokens: int,
) -> bool:
    if model is None or not registry.is_provider_configured(model.provider):
        return False
    access_rank = _ACCESS_RANK.get(model.access_level)
    if access_rank is None or access_rank > _caller_access_rank(user):
        return False
    if requires_tools and not model.supports_tools:
        return False
    if requires_vision and not model.supports_vision:
        return False
    if requires_native_search and not model.supports_native_search:
        return False
    if int(model.max_output_tokens or 0) < max(0, int(required_output_tokens)):
        return False
    return int(model.context_window or 0) >= max(0, int(min_context_window))


def _has_semantic_delta(delta: StreamDelta) -> bool:
    return bool(
        delta.content
        or delta.thinking_content
        or delta.tool_calls
        or delta.finish_reason
        or delta.provider_content_blocks
    )


def _candidate_kwargs(
    registry: Any,
    model_id: str,
    base: Mapping[str, Any],
    *,
    requires_native_search: bool,
) -> dict[str, Any]:
    values = dict(base)
    if requires_native_search:
        model = registry.get_model(model_id)
        values["native_search_config"] = getattr(model, "native_search_config", None)
    if model_id != values.get("model_id"):
        values["thinking_level"] = None
    values["model_id"] = model_id
    return values


async def stream_with_failover(
    *,
    registry: Any,
    requested_model: str,
    fallbacks: Mapping[str, Sequence[str]],
    enabled: bool,
    user: Any | None,
    min_context_window: int,
    requires_vision: bool,
    stream_kwargs: Mapping[str, Any],
) -> AsyncIterator[ModelStreamItem]:
    """Stream from the requested model and fail over only before semantic output."""

    candidates = list(fallbacks.get(requested_model, ())) if enabled else []
    models = [requested_model, *candidates]
    requires_tools = bool(stream_kwargs.get("tools"))
    requires_native_search = bool(stream_kwargs.get("native_search_config"))
    required_output_tokens = max(0, int(stream_kwargs.get("max_tokens") or 0))
    attempted: set[str] = set()
    provider_attempt = 0

    for position, model_id in enumerate(models):
        if model_id in attempted:
            continue
        attempted.add(model_id)
        if model_id != requested_model and not _model_is_eligible(
            registry,
            registry.get_model(model_id),
            user=user,
            requires_tools=requires_tools,
            requires_vision=requires_vision,
            requires_native_search=requires_native_search,
            min_context_window=min_context_window,
            required_output_tokens=required_output_tokens,
        ):
            continue

        provider_attempt += 1
        semantic_delta_seen = False
        try:
            values = _candidate_kwargs(
                registry,
                model_id,
                stream_kwargs,
                requires_native_search=requires_native_search,
            )
            async for delta in registry.chat_stream(**values):
                semantic_delta_seen = semantic_delta_seen or _has_semantic_delta(delta)
                yield ModelStreamItem(delta=delta, model_id=model_id)
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure_class = classify_failover_failure(exc)
            if semantic_delta_seen or failure_class is None:
                raise
            next_model = next(
                (
                    candidate
                    for candidate in models[position + 1 :]
                    if candidate not in attempted
                    and _model_is_eligible(
                        registry,
                        registry.get_model(candidate),
                        user=user,
                        requires_tools=requires_tools,
                        requires_vision=requires_vision,
                        requires_native_search=requires_native_search,
                        min_context_window=min_context_window,
                        required_output_tokens=required_output_tokens,
                    )
                ),
                None,
            )
            if next_model is None:
                raise
            yield ModelStreamItem(
                notice=ModelFailoverNotice(
                    requested_model=requested_model,
                    failed_model=model_id,
                    served_model=next_model,
                    failure_class=failure_class,
                    attempt=provider_attempt + 1,
                )
            )
