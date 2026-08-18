"""Stateless progressive disclosure for the authorized tool catalog.

The model always sees these three small bridge schemas.  The full catalog is
rebuilt at call time through ``RegistryToolInvoker`` so no session-global tool
state can drift, and every search/describe/call observes the same tenant,
permission and Agent capability filters as a direct tool call.
"""

from __future__ import annotations

import contextlib
import inspect
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ai_gateway_core.security import redact_trace_text

from .tool_registry import (
    ToolCallRequest,
    ToolCallResult,
    ToolCategory,
    ToolDefinition,
    ToolParameter,
    ToolRiskLevel,
)

if TYPE_CHECKING:
    from .tool_registry import ToolRegistry

TOOL_SEARCH = "tool_search"
TOOL_DESCRIBE = "tool_describe"
TOOL_CALL = "tool_call"
DISCOVERY_TOOL_NAMES = frozenset({TOOL_SEARCH, TOOL_DESCRIBE, TOOL_CALL})

_MAX_RESULTS = 20
_TOKEN_RE = re.compile(r"[a-z0-9]+|[\u3400-\u9fff]", re.IGNORECASE)


def is_tool_discovery_bridge(name: str) -> bool:
    """Return whether *name* is a platform-owned discovery bridge."""

    return name in DISCOVERY_TOOL_NAMES


def _definition(name: str, description: str, parameters: list[ToolParameter]) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=description,
        parameters=parameters,
        category=ToolCategory.UTILITY,
        risk_level=ToolRiskLevel.LOW,
        requires_confirmation=False,
        timeout_seconds=30,
        max_retries=0,
        capability_metadata={
            "kind": "platform_tool_discovery",
            # The bridge itself only routes. The underlying tool is separately
            # authorized, approved, audited and side-effect fenced.
            "operation_kind": "read",
            "read_only": True,
        },
    )


def tool_discovery_definitions() -> list[ToolDefinition]:
    """Return the small, provider-neutral bridge catalog."""

    return [
        _definition(
            TOOL_SEARCH,
            "Search the current authorized tool catalog. Use this when the needed "
            "capability is not already listed; an empty query browses the catalog.",
            [
                ToolParameter(
                    name="query",
                    type="string",
                    description="Capability, action, or tool name to find.",
                    required=False,
                    default="",
                ),
                ToolParameter(
                    name="limit",
                    type="integer",
                    description="Maximum matches (1-20, default 5).",
                    required=False,
                    default=5,
                    schema_constraints={"minimum": 1, "maximum": _MAX_RESULTS},
                ),
            ],
        ),
        _definition(
            TOOL_DESCRIBE,
            "Return the current full parameter schema for one exact tool name from tool_search.",
            [
                ToolParameter(
                    name="name",
                    type="string",
                    description="Exact tool name returned by tool_search.",
                )
            ],
        ),
        _definition(
            TOOL_CALL,
            "Invoke one tool returned by tool_search. The underlying tool keeps its normal "
            "tenant authorization, approval, sandbox, audit, and retry policy.",
            [
                ToolParameter(
                    name="name",
                    type="string",
                    description="Exact tool name returned by tool_search.",
                ),
                ToolParameter(
                    name="arguments",
                    type="object",
                    description="Arguments matching the schema returned by tool_describe.",
                    properties={},
                    schema_constraints={"additionalProperties": True},
                ),
            ],
        ),
    ]


def _safe_text(value: Any, *, limit: int) -> str:
    text = " ".join(str(redact_trace_text(value, limit=limit)).split())
    return text[:limit]


def _search_text(definition: ToolDefinition) -> str:
    metadata = definition.capability_metadata or {}
    properties = definition.model_argument_schema().get("properties") or {}
    values = [
        definition.name.replace("_", " "),
        definition.description,
        definition.when_to_use or "",
        " ".join(definition.relevance_keywords or []),
        " ".join(str(name) for name in properties),
        metadata.get("summary") or "",
        metadata.get("mcp_server") or metadata.get("server_id") or "",
        metadata.get("mcp_tool") or metadata.get("tool_id") or "",
    ]
    return " ".join(str(value) for value in values if value).lower()


def _tokens(value: str) -> list[str]:
    return _TOKEN_RE.findall(value.lower())


@dataclass(frozen=True)
class _RankedTool:
    score: float
    definition: ToolDefinition


def rank_authorized_tools(
    definitions: list[ToolDefinition],
    query: str,
    *,
    limit: int = 5,
) -> list[ToolDefinition]:
    """Rank a live authorized catalog with deterministic no-match fallback."""

    catalog = sorted(
        (item for item in definitions if not is_tool_discovery_bridge(item.name)),
        key=lambda item: item.name.casefold(),
    )
    if not catalog:
        return []
    bounded_limit = max(1, min(_MAX_RESULTS, int(limit or 5)))
    normalized_query = " ".join(str(query or "").lower().split())
    query_tokens = set(_tokens(normalized_query))
    if not normalized_query or not query_tokens:
        return catalog[:bounded_limit]

    ranked: list[_RankedTool] = []
    for definition in catalog:
        text = _search_text(definition)
        text_tokens = set(_tokens(text))
        overlap = len(query_tokens & text_tokens)
        score = overlap / max(len(query_tokens), 1)
        name = definition.name.casefold()
        if normalized_query in name:
            score += 2.0
        elif normalized_query in text:
            score += 1.0
        ranked.append(_RankedTool(score=score, definition=definition))

    matches = [item for item in ranked if item.score > 0]
    # An opaque or multilingual MCP catalog may share no lexical tokens with
    # the request. Returning a stable browse slice is more capable than the
    # old zero-score behavior, which made every such tool unreachable.
    if not matches:
        return catalog[:bounded_limit]
    matches.sort(key=lambda item: (-item.score, item.definition.name.casefold()))
    return [item.definition for item in matches[:bounded_limit]]


CatalogProvider = Callable[[Any], Awaitable[list[ToolDefinition]] | list[ToolDefinition]]
ToolCaller = Callable[[str, dict[str, Any], Any], Awaitable[ToolCallResult]]


class ToolDiscoveryExecutor:
    """Generic executor; all request-specific authority arrives as private metadata."""

    @staticmethod
    async def _catalog(request: ToolCallRequest) -> list[ToolDefinition] | None:
        metadata = request.metadata or {}
        context = metadata.get("_tool_discovery_context")
        provider = metadata.get("_tool_discovery_catalog_provider")
        if context is None or not callable(provider):
            return None
        value = provider(context)
        if inspect.isawaitable(value):
            value = await value
        if not isinstance(value, list):
            return None
        return [item for item in value if isinstance(item, ToolDefinition)]

    @staticmethod
    def _result(
        request: ToolCallRequest,
        payload: dict[str, Any],
        *,
        success: bool = True,
        error: str | None = None,
    ) -> ToolCallResult:
        return ToolCallResult(
            call_id=request.call_id,
            tool_name=request.tool_name,
            success=success,
            result=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            error=error,
        )

    async def execute(self, request: ToolCallRequest) -> ToolCallResult:
        catalog = await self._catalog(request)
        if catalog is None:
            return self._result(
                request,
                {"error": "TOOL_DISCOVERY_CONTEXT_UNAVAILABLE"},
                success=False,
                error="TOOL_DISCOVERY_CONTEXT_UNAVAILABLE",
            )
        catalog = [item for item in catalog if not is_tool_discovery_bridge(item.name)]

        if request.tool_name == TOOL_SEARCH:
            try:
                limit = int(request.arguments.get("limit", 5) or 5)
            except (TypeError, ValueError):
                limit = 5
            query = str(request.arguments.get("query") or "").strip()
            matches = rank_authorized_tools(catalog, query, limit=limit)
            return self._result(
                request,
                {
                    "query": query,
                    "total_available": len(catalog),
                    "matches": [
                        {
                            "name": item.name,
                            "description": _safe_text(item.description, limit=400),
                            "category": item.category.value,
                            "risk_level": item.risk_level.value,
                            "requires_confirmation": item.requires_confirmation,
                        }
                        for item in matches
                    ],
                },
            )

        name = str(request.arguments.get("name") or "").strip()
        definition = next((item for item in catalog if item.name == name), None)
        if definition is None:
            return self._result(
                request,
                {"error": "TOOL_NOT_AVAILABLE", "name": name},
                success=False,
                error="TOOL_NOT_AVAILABLE",
            )

        if request.tool_name == TOOL_DESCRIBE:
            return self._result(
                request,
                {
                    "name": definition.name,
                    "description": _safe_text(definition.description, limit=1200),
                    "parameters": definition.model_argument_schema(),
                    "risk_level": definition.risk_level.value,
                    "requires_confirmation": definition.requires_confirmation,
                },
            )

        if request.tool_name != TOOL_CALL:
            return self._result(
                request,
                {"error": "UNKNOWN_DISCOVERY_TOOL"},
                success=False,
                error="UNKNOWN_DISCOVERY_TOOL",
            )

        arguments = request.arguments.get("arguments")
        if not isinstance(arguments, dict):
            return self._result(
                request,
                {"error": "TOOL_ARGUMENTS_MUST_BE_OBJECT", "name": name},
                success=False,
                error="TOOL_ARGUMENTS_MUST_BE_OBJECT",
            )
        metadata = request.metadata or {}
        context = metadata.get("_tool_discovery_context")
        caller: ToolCaller | None = metadata.get("_tool_discovery_caller")
        if context is None or not callable(caller):
            return self._result(
                request,
                {"error": "TOOL_DISCOVERY_CALL_UNAVAILABLE", "name": name},
                success=False,
                error="TOOL_DISCOVERY_CALL_UNAVAILABLE",
            )
        underlying = await caller(name, arguments, context)
        projected_result: Any = underlying.result
        if isinstance(projected_result, str):
            with contextlib.suppress(json.JSONDecodeError):
                projected_result = json.loads(projected_result)
        projected_metadata = {
            key: value
            for key, value in dict(underlying.metadata or {}).items()
            if key
            in {
                "execution_id",
                "status",
                "exit_code",
                "duration_ms",
                "output_files_count",
                "side_effect_state",
            }
            and isinstance(value, (str, int, float, bool, type(None)))
        }
        return ToolCallResult(
            call_id=request.call_id,
            tool_name=request.tool_name,
            success=underlying.success,
            result=json.dumps(
                {
                    "invoked_tool": name,
                    "status": "success" if underlying.success else "error",
                    "result": projected_result,
                    "error": underlying.error,
                    "execution": projected_metadata,
                },
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            ),
            error=underlying.error,
            metadata={
                **dict(underlying.metadata or {}),
                "discovered_tool_name": name,
            },
            output_files=list(underlying.output_files or []),
        )


def register_tool_discovery_tools(registry: ToolRegistry | None = None) -> None:
    """Register the platform bridges, rejecting name collisions fail-closed."""

    if registry is None:
        from .tool_registry import get_tool_registry

        registry = get_tool_registry()
    executor = ToolDiscoveryExecutor()
    for definition in tool_discovery_definitions():
        existing = registry.get_tool(definition.name)
        if existing is not None:
            if (existing.capability_metadata or {}).get("kind") != "platform_tool_discovery":
                raise ValueError(
                    f"Reserved tool discovery name is already registered: {definition.name}"
                )
            continue
        registry.register(definition, executor)


__all__ = [
    "DISCOVERY_TOOL_NAMES",
    "TOOL_CALL",
    "TOOL_DESCRIBE",
    "TOOL_SEARCH",
    "ToolDiscoveryExecutor",
    "is_tool_discovery_bridge",
    "rank_authorized_tools",
    "register_tool_discovery_tools",
    "tool_discovery_definitions",
]
