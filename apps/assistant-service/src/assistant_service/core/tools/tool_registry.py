"""
Tool Registry - Agentic Tool Management System

Phase 2: Provides a centralized registry for tools with:
- Tool metadata and JSON Schema definitions
- Usage examples and risk labels
- Tool execution lifecycle management
- Observability and logging

References:
- https://docs.anthropic.com/en/docs/build-with-claude/tool-use
- https://platform.openai.com/docs/guides/function-calling
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from ai_gateway_core.logging import get_logger
from ai_gateway_core.security import redact_trace_text
from jsonschema import Draft202012Validator

if TYPE_CHECKING:
    from ai_gateway_core.auth import UserContextLike

logger = get_logger(__name__)

_MAX_PUBLIC_ERROR_CHARS = 200
_TRUNCATION_SUFFIX = "...[truncated]"
_PUBLIC_URL_RE = re.compile(r"https?://[^\s'\"]+")
_PUBLIC_INTERNAL_FIELD_RE = re.compile(r"(?i)(host|server|user)\s*=\s*\S+")
_UNTRUSTED_INSTRUCTION_RE = re.compile(
    r"(?i)(ignore\s+(?:all\s+)?previous|system\s+prompt|developer\s+message|jailbreak)"
)


def _tool_log_label(tool_name: Any) -> str:
    """Return a stable, non-reversible label for an untrusted tool name."""

    try:
        digest = hashlib.sha256(str(tool_name).encode("utf-8", errors="replace")).hexdigest()
    except Exception:
        return "tool_sha256=unavailable"
    return f"tool_sha256={digest[:16]}"


def _safe_public_error(value: Any, *, fallback: str = "Tool execution failed") -> str:
    """Return shared-redacted client text with a hard character bound."""

    try:
        text = redact_trace_text(value)
    except Exception:
        text = fallback
    if not text:
        text = fallback
    text = _PUBLIC_URL_RE.sub("[url]", text)
    text = _PUBLIC_INTERNAL_FIELD_RE.sub(r"\1=[redacted]", text)
    if len(text) <= _MAX_PUBLIC_ERROR_CHARS:
        return text
    return f"{text[: _MAX_PUBLIC_ERROR_CHARS - len(_TRUNCATION_SUFFIX)]}{_TRUNCATION_SUFFIX}"


class ToolRiskLevel(str, Enum):
    """Risk level for tool operations."""

    LOW = "low"  # Read-only operations, no side effects
    MEDIUM = "medium"  # May modify data but reversible
    HIGH = "high"  # Irreversible operations, requires confirmation


class ToolCategory(str, Enum):
    """Tool categories for organization."""

    RETRIEVAL = "retrieval"  # KB search, web search
    GENERATION = "generation"  # Content creation
    ANALYSIS = "analysis"  # Data analysis
    INTEGRATION = "integration"  # External system calls
    UTILITY = "utility"  # Helper functions
    SKILL = "skill"  # User-defined or builtin skills
    MCP = "mcp"  # External system tools via Model Context Protocol


@dataclass
class ToolParameter:
    """Definition of a tool parameter."""

    name: str
    type: str  # string, integer, number, boolean, array, object
    description: str
    required: bool = True
    default: Any = None
    enum: list[str] | None = None
    items: dict[str, Any] | None = None  # For array types
    properties: dict[str, Any] | None = None  # For object types
    schema_constraints: dict[str, Any] | None = None


@dataclass
class ToolExample:
    """Example usage of a tool."""

    description: str
    input: dict[str, Any]
    expected_output: str | None = None


@dataclass
class ToolDefinition:
    """Complete tool definition for the registry."""

    name: str
    description: str
    parameters: list[ToolParameter]

    # Metadata
    category: ToolCategory = ToolCategory.UTILITY
    risk_level: ToolRiskLevel = ToolRiskLevel.LOW
    requires_confirmation: bool = False

    # Usage guidance
    when_to_use: str | None = None
    when_not_to_use: str | None = None
    examples: list[ToolExample] = field(default_factory=list)

    # Relevance selection — used by tool_selector.py to decide whether to
    # expose this tool to the model on each request. Self-declared keywords
    # keep tool additions from needing a second registration in a central
    # keywords dict (historical trap: new tools silently got 0 score and
    # never reached the model). Leave empty to fall back on tool_selector's
    # name/description heuristics.
    relevance_keywords: list[str] = field(default_factory=list)

    # Execution hints
    timeout_seconds: int = 30
    max_retries: int = 2
    is_async: bool = True

    # Access control
    required_permissions: list[str] = field(default_factory=list)
    sandbox_profile: str = "none"
    audit_shape: dict[str, Any] = field(
        default_factory=lambda: {
            "input": "redacted_summary",
            "output": "status_only",
        }
    )
    redaction_policy: str = "standard"
    capability_metadata: dict[str, Any] = field(default_factory=dict)
    # Optional canonical JSON Schema. Dynamic MCP tools retain their exact
    # authorized schema here instead of losing nested constraints while being
    # converted to ToolParameter display metadata.
    argument_schema: dict[str, Any] | None = field(default=None, repr=False)

    def json_argument_schema(self) -> dict[str, Any]:
        """Return the exact model-facing argument schema."""

        if self.argument_schema is not None:
            return copy.deepcopy(self.argument_schema)
        return self._parameter_argument_schema()

    def _parameter_argument_schema(self) -> dict[str, Any]:
        properties: dict[str, Any] = {}
        required: list[str] = []
        for param in self.parameters:
            prop: dict[str, Any] = {
                "type": param.type,
                "description": param.description,
            }
            if param.enum:
                prop["enum"] = list(param.enum)
            if param.items:
                prop["items"] = copy.deepcopy(param.items)
            if param.properties:
                prop["properties"] = copy.deepcopy(param.properties)
            if param.schema_constraints:
                prop.update(copy.deepcopy(param.schema_constraints))
            properties[param.name] = prop
            if param.required:
                required.append(param.name)
        return {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }

    def model_argument_schema(self) -> dict[str, Any]:
        """Return a prompt-safe schema while retaining validation structure."""

        schema = self.json_argument_schema()
        if self.argument_schema is not None and not _schema_is_locally_safe(schema):
            # Keep the tool visible with its sanitized ToolParameter projection,
            # but the invocation boundary will still reject the unsafe exact
            # schema rather than dispatching against this projection.
            schema = self._parameter_argument_schema()
        return _sanitize_schema_for_model(schema)

    def to_openai_schema(self, compact: bool = False) -> dict[str, Any]:
        """Convert to OpenAI function calling schema."""
        parameters = self.model_argument_schema()
        if compact:
            for prop in (parameters.get("properties") or {}).values():
                if isinstance(prop, dict) and isinstance(prop.get("description"), str):
                    prop["description"] = self._compact_text(prop["description"], 140)

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self._compact_text(self.description, 220)
                if compact
                else self._build_full_description(),
                "parameters": parameters,
            },
        }

    def to_anthropic_schema(self, compact: bool = False) -> dict[str, Any]:
        """Convert to Anthropic tool use schema."""
        input_schema = self.model_argument_schema()
        if compact:
            for prop in (input_schema.get("properties") or {}).values():
                if isinstance(prop, dict) and isinstance(prop.get("description"), str):
                    prop["description"] = self._compact_text(prop["description"], 140)

        return {
            "name": self.name,
            "description": self._compact_text(self.description, 220)
            if compact
            else self._build_full_description(),
            "input_schema": input_schema,
        }

    def _build_full_description(self) -> str:
        """Build comprehensive description including usage guidance."""
        parts = [self.description]

        if self.when_to_use:
            parts.append(f"\n\nWhen to use: {self.when_to_use}")

        if self.when_not_to_use:
            parts.append(f"\n\nWhen NOT to use: {self.when_not_to_use}")

        if self.examples:
            parts.append("\n\nExamples:")
            for ex in self.examples[:2]:  # Limit examples in description
                parts.append(f"\n- {ex.description}: {json.dumps(ex.input)}")

        return "".join(parts)

    @staticmethod
    def _compact_text(text: str, max_len: int) -> str:
        """Compact long tool descriptions for low-latency streaming calls."""
        if not text:
            return ""
        value = " ".join(str(text).split())
        if len(value) <= max_len:
            return value
        return value[: max_len - 3].rstrip() + "..."


def _schema_is_locally_safe(schema: dict[str, Any]) -> bool:
    """Reject schemas that can resolve externally or consume unbounded work."""

    try:
        encoded = json.dumps(schema, sort_keys=True, separators=(",", ":"), default=str)
    except Exception:
        return False
    if len(encoded.encode("utf-8")) > 65_536:
        return False
    stack: list[tuple[Any, int]] = [(schema, 0)]
    nodes = 0
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if nodes > 2_048 or depth > 16:
            return False
        if isinstance(value, dict):
            if any(key in value for key in ("$ref", "$dynamicRef", "$recursiveRef")):
                return False
            pattern = value.get("pattern")
            if isinstance(pattern, str) and len(pattern) > 256:
                return False
            stack.extend((child, depth + 1) for child in value.values())
        elif isinstance(value, list):
            stack.extend((child, depth + 1) for child in value)
    return True


def _sanitize_schema_for_model(value: Any, *, key: str = "") -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for child_key, child in value.items():
            if child_key in {"$comment", "default", "examples"}:
                continue
            sanitized[str(child_key)] = _sanitize_schema_for_model(
                child,
                key=str(child_key),
            )
        return sanitized
    if isinstance(value, list):
        return [_sanitize_schema_for_model(child, key=key) for child in value]
    if isinstance(value, str) and key in {"description", "title"}:
        text = re.sub(r"[\x00-\x1f\x7f]", " ", redact_trace_text(value, limit=500))
        text = _UNTRUSTED_INSTRUCTION_RE.sub("[untrusted-instruction]", text)
        return " ".join(text.split())
    return copy.deepcopy(value)


def _safe_expected(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return value[:120]
    if (
        isinstance(value, list)
        and len(value) <= 12
        and all(item is None or isinstance(item, bool | int | float | str) for item in value)
    ):
        return [item[:120] if isinstance(item, str) else item for item in value]
    return None


def validate_tool_arguments(
    definition: ToolDefinition,
    arguments: Any,
) -> dict[str, Any]:
    """Validate without coercion and return a value-free machine receipt."""

    schema = definition.json_argument_schema()
    try:
        canonical = json.dumps(schema, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        canonical = ""
    schema_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest() if canonical else ""
    base = {
        "schema_version": "assistant-tool-arguments/v1",
        "schema_sha256": schema_hash,
    }
    if not isinstance(arguments, dict):
        return {
            **base,
            "valid": False,
            "code": "arguments_not_object",
            "issue_count": 1,
            "issues": [{"path": "$", "rule": "type", "expected": "object"}],
        }
    if not canonical or not _schema_is_locally_safe(schema):
        return {
            **base,
            "valid": False,
            "code": "schema_unavailable",
            "issue_count": 0,
            "issues": [],
        }
    try:
        Draft202012Validator.check_schema(schema)
        errors = sorted(
            Draft202012Validator(schema).iter_errors(arguments),
            key=lambda error: (list(error.absolute_path), str(error.validator or "")),
        )
    except Exception:
        return {
            **base,
            "valid": False,
            "code": "schema_unavailable",
            "issue_count": 0,
            "issues": [],
        }
    issues: list[dict[str, Any]] = []
    for error in errors[:8]:
        path = "$" + "".join(
            f"[{part}]" if isinstance(part, int) else f".{part}" for part in error.absolute_path
        )
        issue: dict[str, Any] = {
            "path": path,
            "rule": str(error.validator or "invalid"),
        }
        expected = _safe_expected(error.validator_value)
        if expected is not None:
            issue["expected"] = expected
        issues.append(issue)
    return {
        **base,
        "valid": not errors,
        "code": "ok" if not errors else "arguments_invalid",
        "issue_count": len(errors),
        "issues": issues,
    }


@dataclass
class ToolCallRequest:
    """A request to execute a tool."""

    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    user: UserContextLike | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolCallResult:
    """Result of a tool execution."""

    call_id: str
    tool_name: str
    success: bool
    result: Any = None
    error: str | None = None
    duration_ms: float = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    output_files: list[dict[str, Any]] = field(
        default_factory=list
    )  # [{filename, content_base64, mime_type, size_bytes}]

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "call_id": self.call_id,
            "tool_name": self.tool_name,
            "success": self.success,
            "result": self.result,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "metadata": self.metadata,
            "output_files": self.output_files,
        }


class ToolExecutor:
    """Base class for tool executors."""

    async def execute(
        self,
        request: ToolCallRequest,
    ) -> ToolCallResult:
        """Execute the tool and return result."""
        raise NotImplementedError

    def validate_arguments(
        self,
        definition: ToolDefinition,
        arguments: dict[str, Any],
    ) -> list[str]:
        """Validate arguments against schema. Returns list of errors."""
        errors = []

        # Check required parameters
        for param in definition.parameters:
            if param.required and param.name not in arguments:
                errors.append(f"Missing required parameter: {param.name}")
            elif param.name in arguments:
                value = arguments[param.name]
                # Type checking
                if param.type == "string" and not isinstance(value, str):
                    errors.append(f"Parameter {param.name} must be a string")
                elif param.type == "number" and (
                    isinstance(value, bool) or not isinstance(value, (int, float))
                ):
                    errors.append(f"Parameter {param.name} must be a number")
                elif param.type == "boolean" and not isinstance(value, bool):
                    errors.append(f"Parameter {param.name} must be a boolean")
                elif param.type == "array" and not isinstance(value, list):
                    errors.append(f"Parameter {param.name} must be an array")
                elif param.type == "object" and not isinstance(value, dict):
                    errors.append(f"Parameter {param.name} must be an object")

                # Enum validation
                if param.enum and value not in param.enum:
                    errors.append(f"Parameter {param.name} must be one of: {param.enum}")

                constraints = param.schema_constraints or {}
                if isinstance(value, str):
                    if len(value) < int(constraints.get("minLength", 0)):
                        errors.append(f"Parameter {param.name} is too short")
                    max_length = constraints.get("maxLength")
                    if max_length is not None and len(value) > int(max_length):
                        errors.append(f"Parameter {param.name} is too long")
                if isinstance(value, list):
                    min_items = constraints.get("minItems")
                    max_items = constraints.get("maxItems")
                    if min_items is not None and len(value) < int(min_items):
                        errors.append(f"Parameter {param.name} has too few items")
                    if max_items is not None and len(value) > int(max_items):
                        errors.append(f"Parameter {param.name} has too many items")
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    minimum = constraints.get("minimum")
                    maximum = constraints.get("maximum")
                    if minimum is not None and value < minimum:
                        errors.append(f"Parameter {param.name} is below the minimum")
                    if maximum is not None and value > maximum:
                        errors.append(f"Parameter {param.name} is above the maximum")

        return errors


class ToolRegistry:
    """
    Central registry for all available tools.

    Provides:
    - Tool registration and lookup
    - Schema generation for different providers
    - Tool execution with lifecycle management
    - Access control based on user permissions
    """

    def __init__(self):
        self._tools: dict[str, ToolDefinition] = {}
        self._executors: dict[str, ToolExecutor] = {}
        # Reentrant threading lock — register() is sync and may be called from
        # both sync startup code and async tool-activation paths; a threading
        # lock works in both contexts (asyncio.Lock would force all callers to
        # be async). RLock allows the same "thread" to re-enter safely.
        self._lock = threading.RLock()

    def register(
        self,
        definition: ToolDefinition,
        executor: ToolExecutor,
        *,
        allow_override: bool = False,
    ) -> None:
        """Register a tool with its executor (thread-safe)."""
        with self._lock:
            if definition.name in self._tools:
                if not allow_override:
                    raise ValueError(
                        _safe_public_error(
                            f"Tool already registered: {definition.name}. "
                            "Use allow_override=True only for trusted startup refresh flows.",
                            fallback="Tool already registered",
                        )
                    )
                logger.warning(
                    "tool_registry.overwrite (tool_label=%s)",
                    _tool_log_label(definition.name),
                )

            self._tools[definition.name] = definition
            self._executors[definition.name] = executor

        logger.info(
            "tool_registry.registered (tool_label=%s, category=%s, risk=%s)",
            _tool_log_label(definition.name),
            definition.category.value,
            definition.risk_level.value,
        )

    def unregister(self, name: str) -> bool:
        """Unregister a tool (thread-safe)."""
        with self._lock:
            if name in self._tools:
                del self._tools[name]
                del self._executors[name]
                logger.info(
                    "tool_registry.unregistered (tool_label=%s)",
                    _tool_log_label(name),
                )
                return True
            return False

    def get_tool(self, name: str) -> ToolDefinition | None:
        """Get tool definition by name."""
        with self._lock:
            return self._tools.get(name)

    def list_tools(
        self,
        category: ToolCategory | None = None,
        user: UserContextLike | None = None,
    ) -> list[ToolDefinition]:
        """List available tools, optionally filtered by category and user permissions."""
        with self._lock:
            tools = list(self._tools.values())

        if category:
            tools = [t for t in tools if t.category == category]

        # Filter by user permissions if provided
        if user:
            tools = [t for t in tools if self._user_has_required_permissions(user, t)]
        else:
            tools = [t for t in tools if not t.required_permissions]

        return tools

    @staticmethod
    def _user_has_required_permissions(user: UserContextLike, tool: ToolDefinition) -> bool:
        """Check required permissions with tier/role support."""
        required = tool.required_permissions or []
        if not required:
            return True

        user_roles = set(user.roles or [])
        user_tier = (user.tier or "anonymous").lower()
        tier_order = {
            "anonymous": 0,
            "normal": 1,
            "premium": 2,
            "enterprise": 3,
            "admin": 4,
        }

        def _has_one(permission: str) -> bool:
            if permission.startswith("role:"):
                role = permission.split(":", 1)[1].strip()
                return role in user_roles or "admin" in user_roles
            if permission.startswith("tier:"):
                tier = permission.split(":", 1)[1].strip().lower()
                return tier_order.get(user_tier, 0) >= tier_order.get(tier, 999)
            return permission in user_roles or "admin" in user_roles

        return all(_has_one(p) for p in required)

    def get_openai_schemas(
        self,
        tool_names: list[str] | None = None,
        user: UserContextLike | None = None,
    ) -> list[dict[str, Any]]:
        """Get OpenAI-compatible schemas for specified tools."""
        tools = self.list_tools(user=user)

        if tool_names:
            tools = [t for t in tools if t.name in tool_names]

        return [t.to_openai_schema() for t in tools]

    def get_anthropic_schemas(
        self,
        tool_names: list[str] | None = None,
        user: UserContextLike | None = None,
    ) -> list[dict[str, Any]]:
        """Get Anthropic-compatible schemas for specified tools."""
        tools = self.list_tools(user=user)

        if tool_names:
            tools = [t for t in tools if t.name in tool_names]

        return [t.to_anthropic_schema() for t in tools]

    async def execute(
        self,
        request: ToolCallRequest,
    ) -> ToolCallResult:
        """Execute a tool call."""
        start_time = time.time()

        # Get tool definition
        definition = self._tools.get(request.tool_name)
        if not definition:
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error=_safe_public_error(
                    f"Unknown tool: {request.tool_name}",
                    fallback="Unknown tool",
                ),
            )

        # Get executor (thread-safe snapshot)
        with self._lock:
            executor = self._executors.get(request.tool_name)
        if not executor:
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error=_safe_public_error(
                    f"No executor for tool: {request.tool_name}",
                    fallback="No executor for tool",
                ),
            )

        # Enforce required permissions if user context is available
        if definition.required_permissions and not request.user:
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error=_safe_public_error(
                    f"Permission context required for tool: {request.tool_name}",
                    fallback="Permission context required for tool",
                ),
            )
        if request.user and not self._user_has_required_permissions(request.user, definition):
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error=_safe_public_error(
                    f"Permission denied for tool: {request.tool_name}",
                    fallback="Permission denied for tool",
                ),
            )

        # Enforce the model-facing JSON Schema even for plain callables such
        # as static MCP closures. No implicit string/number/list coercion.
        validation = validate_tool_arguments(definition, request.arguments)
        if not validation["valid"]:
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                result=json.dumps(
                    {
                        "error": {
                            "code": "TOOL_ARGUMENT_VALIDATION_FAILED",
                            "validation": validation,
                        }
                    },
                    separators=(",", ":"),
                ),
                error="TOOL_ARGUMENT_VALIDATION_FAILED",
                metadata={"tool_argument_validation": validation},
            )

        if self._requires_gateway(definition) and not self._direct_execution_allowed(request):
            logger.warning(
                "tool_registry.direct_execution_denied "
                "(tool_label=%s, risk=%s, requires_confirmation=%s)",
                _tool_log_label(request.tool_name),
                definition.risk_level.value,
                definition.requires_confirmation,
            )
            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error=(
                    "Tool execution requires the AssistantExecutionGateway approval and audit path."
                ),
                metadata={
                    "direct_registry_denied": True,
                    "risk_level": definition.risk_level.value,
                    "requires_confirmation": definition.requires_confirmation,
                    "required_gateway": "AssistantExecutionGateway",
                },
            )

        # Execute tool with timeout enforcement
        try:
            logger.info(
                "tool_registry.execution_started (tool_label=%s, timeout_seconds=%s)",
                _tool_log_label(request.tool_name),
                definition.timeout_seconds,
            )

            # Enforce timeout from tool definition
            try:
                # Support both ToolExecutor instances (.execute) and plain callables (MCP closures)
                coro = (
                    executor.execute(request) if hasattr(executor, "execute") else executor(request)
                )
                result = await asyncio.wait_for(coro, timeout=definition.timeout_seconds)
            except asyncio.TimeoutError:
                duration_ms = definition.timeout_seconds * 1000
                logger.error(
                    "tool_registry.execution_timeout (tool_label=%s, timeout_seconds=%s)",
                    _tool_log_label(request.tool_name),
                    definition.timeout_seconds,
                )
                return ToolCallResult(
                    call_id=request.call_id,
                    tool_name=request.tool_name,
                    success=False,
                    error=f"Tool execution timed out after {definition.timeout_seconds}s",
                    duration_ms=duration_ms,
                )

            result.duration_ms = (time.time() - start_time) * 1000
            if result.error is not None:
                result.error = _safe_public_error(result.error)

            logger.info(
                "tool_registry.execution_completed (tool_label=%s, duration_ms=%.1f, success=%s)",
                _tool_log_label(request.tool_name),
                result.duration_ms,
                result.success,
            )

            return result

        except Exception as exc:
            duration_ms = (time.time() - start_time) * 1000
            logger.error(
                "tool_registry.execution_failed (tool_label=%s, exception_type=%s)",
                _tool_log_label(request.tool_name),
                type(exc).__name__,
            )

            return ToolCallResult(
                call_id=request.call_id,
                tool_name=request.tool_name,
                success=False,
                error=_safe_error_message(exc, tool_name=request.tool_name),
                duration_ms=duration_ms,
            )

    @staticmethod
    def _requires_gateway(definition: ToolDefinition) -> bool:
        # Medium means a reversible side effect, not an automatic approval.
        # Requiring the gateway for every medium tool made image generation,
        # sandboxed code, and other explicitly unattended tools unusable when
        # the optional gateway was disabled. High risk remains fail-closed.
        return definition.requires_confirmation or definition.risk_level is ToolRiskLevel.HIGH

    @staticmethod
    def _direct_execution_allowed(request: ToolCallRequest) -> bool:
        metadata = request.metadata or {}
        if metadata.get("execution_gateway_approved") is True:
            return True
        return metadata.get("direct_registry_bypass") == "test_only" and os.getenv(
            "PYTEST_CURRENT_TEST"
        )


def _safe_error_message(exc: BaseException, *, tool_name: str = "tool") -> str:
    """Return a client-safe error message that doesn't leak internal details.

    - Database/network/filesystem errors: return a generic "internal error"
      since their str() typically contains connection strings, hostnames, or paths
    - Any other exception: return a shared-redacted, bounded message
    """
    # Exception types whose str() often contains sensitive internal details
    sensitive_type_names = {
        "PostgresError",
        "InterfaceError",
        "OperationalError",
        "ConnectionError",
        "ConnectionRefusedError",
        "ConnectionResetError",
        "HTTPStatusError",
        "ConnectError",
        "ReadError",
        "WriteError",
        "PoolTimeout",
        "FileNotFoundError",
        "PermissionError",
        "OSError",
        "RuntimeError",
    }
    type_name = type(exc).__name__
    if type_name in sensitive_type_names or "asyncpg" in type(exc).__module__:
        return _safe_public_error(
            f"{tool_name} failed due to an internal error. "
            "Please retry; if the issue persists, contact support."
        )

    return _safe_public_error(
        exc,
        fallback=f"{type_name}: Tool execution failed",
    )


# Global registry instance
_registry: ToolRegistry | None = None


def get_tool_registry() -> ToolRegistry:
    """Get the global tool registry instance."""
    global _registry
    if _registry is None:
        _registry = ToolRegistry()
    return _registry


def register_tool(
    definition: ToolDefinition,
    executor: ToolExecutor,
    *,
    allow_override: bool = True,
) -> None:
    """Register a tool with the global registry."""
    get_tool_registry().register(definition, executor, allow_override=allow_override)
