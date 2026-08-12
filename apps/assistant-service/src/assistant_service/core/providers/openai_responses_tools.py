"""Strict OpenAI Responses ``computer`` and local ``shell`` translations.

The objects produced here are proposals, not execution receipts.  They must be
routed through the canonical Assistant runtime, policy/approval gateway, and a
trusted Local Node before any host side effect occurs.  In particular, a
provider safety check can only add a platform approval requirement; it never
authorizes an action by itself.
"""

from __future__ import annotations

import base64
import binascii
import re
import shlex
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit

PROVIDER_NAME = "openai-responses"
MAX_COMPUTER_ACTIONS = 64
MAX_DRAG_POINTS = 256
MAX_KEY_COUNT = 32
MAX_TEXT_LENGTH = 100_000
MAX_SHELL_COMMANDS = 16
MAX_SHELL_TIMEOUT_MS = 120_000
MAX_SHELL_OUTPUT_LENGTH = 1_048_576
MAX_SCREENSHOT_BYTES = 20 * 1024 * 1024

ComputerActionKind = Literal[
    "click",
    "double_click",
    "scroll",
    "type_text",
    "key_press",
    "drag",
    "move",
    "wait",
    "screenshot",
]
MouseButton = Literal["left", "right", "wheel", "back", "forward"]

_ACTION_TYPES = frozenset(
    {
        "click",
        "double_click",
        "scroll",
        "type",
        "keypress",
        "drag",
        "move",
        "wait",
        "screenshot",
    }
)
_BUTTONS = frozenset({"left", "right", "wheel", "back", "forward"})
_CALL_STATUSES = frozenset({"in_progress", "completed"})
_ENVIRONMENT_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_DOMAIN_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_SHELL_PUNCTUATION = frozenset({"|", "||", "&", "&&", ";", ";;", "<", ">", "<<", ">>", "(", ")"})


class ProviderToolContractError(ValueError):
    """Prompt-safe provider wire-contract failure."""

    def __init__(self, code: str) -> None:
        self.provider = PROVIDER_NAME
        self.code = code
        super().__init__(f"{PROVIDER_NAME} tool contract failed ({code})")


@dataclass(frozen=True, slots=True)
class ScreenPoint:
    """Validated point in the provider screenshot coordinate space."""

    x: int
    y: int


@dataclass(frozen=True, slots=True)
class ProviderSafetyCheckRequirement:
    """Provider warning mapped to a fail-closed platform approval requirement."""

    check_id: str
    code: str
    message: str
    source: str = field(default=PROVIDER_NAME, init=False)
    requires_platform_approval: bool = field(default=True, init=False)
    authoritative: bool = field(default=False, init=False)

    def to_acknowledgement(self) -> dict[str, str]:
        return {"id": self.check_id, "code": self.code, "message": self.message}


@dataclass(frozen=True, slots=True)
class ComputerActionRequest:
    """Provider-neutral candidate Computer Use action.

    Optional fields are validated against ``kind`` by the parser.  Keeping the
    provider call id and ordinal on every action makes batching auditable while
    preventing a result from being attached to a different provider call.
    """

    provider_call_id: str
    ordinal: int
    kind: ComputerActionKind
    x: int | None = None
    y: int | None = None
    button: MouseButton | None = None
    scroll_x: int | None = None
    scroll_y: int | None = None
    text: str | None = None
    keys: tuple[str, ...] = ()
    path: tuple[ScreenPoint, ...] = ()
    provider: str = field(default=PROVIDER_NAME, init=False)
    authoritative: bool = field(default=False, init=False)

    def normalized_arguments(self) -> dict[str, Any]:
        """Return the minimal platform action arguments for policy hashing."""

        arguments: dict[str, Any] = {}
        for name in ("x", "y", "button", "scroll_x", "scroll_y", "text"):
            value = getattr(self, name)
            if value is not None:
                arguments[name] = value
        if self.keys:
            arguments["keys"] = list(self.keys)
        if self.path:
            arguments["path"] = [{"x": point.x, "y": point.y} for point in self.path]
        return arguments


@dataclass(frozen=True, slots=True)
class ComputerCallPlan:
    """Non-authoritative action proposal parsed from one GA ``computer_call``."""

    provider_call_id: str
    actions: tuple[ComputerActionRequest, ...]
    approval_requirements: tuple[ProviderSafetyCheckRequirement, ...]
    provider_item_id: str | None = None
    provider_status: str = "completed"
    provider: str = field(default=PROVIDER_NAME, init=False)
    authoritative: bool = field(default=False, init=False)

    @property
    def requires_approval(self) -> bool:
        return bool(self.approval_requirements)


@dataclass(frozen=True, slots=True)
class ComputerScreenshotObservation:
    """Post-action screenshot selected for provider read-back."""

    image_url: str
    detail: Literal["original"] = "original"
    observation_ref: str | None = None


@dataclass(frozen=True, slots=True)
class NetworkPolicy:
    """Local process network policy resolved outside the provider request."""

    mode: Literal["deny", "allowlist"] = "deny"
    allowed_domains: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RestrictedProcessRequest:
    """One structured, non-interactive process proposal from a shell command."""

    provider_call_id: str
    command_index: int
    argv: tuple[str, ...]
    cwd: str
    timeout_ms: int
    max_output_length: int
    network_policy: NetworkPolicy
    environment: tuple[tuple[str, str], ...] = field(default=(), init=False)
    inherit_environment: bool = field(default=False, init=False)
    interactive: bool = field(default=False, init=False)
    requires_platform_approval: bool = field(default=True, init=False)
    provider: str = field(default=PROVIDER_NAME, init=False)
    authoritative: bool = field(default=False, init=False)


@dataclass(frozen=True, slots=True)
class ShellCallPlan:
    """Non-authoritative local process plan parsed from a GA ``shell_call``."""

    provider_call_id: str
    requests: tuple[RestrictedProcessRequest, ...]
    max_output_length: int
    provider_item_id: str | None = None
    provider_status: str = "in_progress"
    provider: str = field(default=PROVIDER_NAME, init=False)
    requires_platform_approval: bool = field(default=True, init=False)
    authoritative: bool = field(default=False, init=False)


@dataclass(frozen=True, slots=True)
class ProcessExecutionResult:
    """Canonical Local Node result projected back to ``shell_call_output``."""

    command_index: int
    stdout: str
    stderr: str
    outcome: Literal["exit", "timeout"]
    exit_code: int | None = None


@dataclass(frozen=True, slots=True)
class OpenAILocalToolDefinition:
    """Validated current Responses local tool declaration."""

    kind: Literal["computer", "shell"]
    environment: Literal["local"] | None = None


def _object(value: Any, code: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProviderToolContractError(code)
    return value


def _reject_unknown(raw: dict[str, Any], allowed: set[str] | frozenset[str], code: str) -> None:
    if set(raw) - set(allowed):
        raise ProviderToolContractError(code)


def _string(value: Any, code: str, *, maximum: int = 1_024) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or "\x00" in value:
        raise ProviderToolContractError(code)
    return value


def _integer(
    value: Any,
    code: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProviderToolContractError(code)
    if minimum is not None and value < minimum:
        raise ProviderToolContractError(code)
    if maximum is not None and value > maximum:
        raise ProviderToolContractError(code)
    return value


def _status(raw: dict[str, Any]) -> str:
    status = raw.get("status")
    if status not in _CALL_STATUSES:
        raise ProviderToolContractError("invalid_provider_call_status")
    return str(status)


def _coordinate(raw: dict[str, Any], name: str) -> int:
    return _integer(raw.get(name), "invalid_computer_coordinate", minimum=0, maximum=1_000_000)


def _keys(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not value or len(value) > MAX_KEY_COUNT:
        raise ProviderToolContractError("invalid_computer_keys")
    keys = tuple(_string(key, "invalid_computer_keys", maximum=64) for key in value)
    return keys


def _button(value: Any) -> MouseButton:
    button = "left" if value is None else value
    if button not in _BUTTONS:
        raise ProviderToolContractError("unsupported_computer_button")
    return cast(MouseButton, button)


def _point(value: Any) -> ScreenPoint:
    if isinstance(value, list):
        if len(value) != 2:
            raise ProviderToolContractError("invalid_computer_drag_path")
        return ScreenPoint(
            x=_integer(value[0], "invalid_computer_coordinate", minimum=0, maximum=1_000_000),
            y=_integer(value[1], "invalid_computer_coordinate", minimum=0, maximum=1_000_000),
        )
    point = _object(value, "invalid_computer_drag_path")
    _reject_unknown(point, {"x", "y"}, "invalid_computer_drag_path")
    return ScreenPoint(x=_coordinate(point, "x"), y=_coordinate(point, "y"))


def _drag_path(value: Any) -> tuple[ScreenPoint, ...]:
    if not isinstance(value, list) or not 2 <= len(value) <= MAX_DRAG_POINTS:
        raise ProviderToolContractError("invalid_computer_drag_path")
    return tuple(_point(point) for point in value)


def _parse_computer_action(
    raw_value: Any,
    *,
    call_id: str,
    ordinal: int,
) -> ComputerActionRequest:
    raw = _object(raw_value, "invalid_computer_action")
    provider_type = raw.get("type")
    if provider_type not in _ACTION_TYPES:
        raise ProviderToolContractError("unsupported_computer_action")

    common = {"type"}
    if provider_type in {"click", "double_click", "move"}:
        allowed = common | {"x", "y", "keys"}
        if provider_type != "move":
            allowed.add("button")
        _reject_unknown(raw, allowed, "invalid_computer_action")
        return ComputerActionRequest(
            provider_call_id=call_id,
            ordinal=ordinal,
            kind=provider_type,
            x=_coordinate(raw, "x"),
            y=_coordinate(raw, "y"),
            button=_button(raw.get("button")) if provider_type != "move" else None,
            keys=_keys(raw.get("keys")),
        )

    if provider_type == "scroll":
        _reject_unknown(
            raw,
            common | {"x", "y", "scroll_x", "scroll_y", "keys"},
            "invalid_computer_action",
        )
        return ComputerActionRequest(
            provider_call_id=call_id,
            ordinal=ordinal,
            kind="scroll",
            x=_coordinate(raw, "x"),
            y=_coordinate(raw, "y"),
            scroll_x=_integer(
                raw.get("scroll_x"),
                "invalid_computer_scroll",
                minimum=-1_000_000,
                maximum=1_000_000,
            ),
            scroll_y=_integer(
                raw.get("scroll_y"),
                "invalid_computer_scroll",
                minimum=-1_000_000,
                maximum=1_000_000,
            ),
            keys=_keys(raw.get("keys")),
        )

    if provider_type == "type":
        _reject_unknown(raw, common | {"text"}, "invalid_computer_action")
        text = raw.get("text")
        if not isinstance(text, str) or len(text) > MAX_TEXT_LENGTH or "\x00" in text:
            raise ProviderToolContractError("invalid_computer_text")
        return ComputerActionRequest(
            provider_call_id=call_id,
            ordinal=ordinal,
            kind="type_text",
            text=text,
        )

    if provider_type == "keypress":
        _reject_unknown(raw, common | {"keys"}, "invalid_computer_action")
        return ComputerActionRequest(
            provider_call_id=call_id,
            ordinal=ordinal,
            kind="key_press",
            keys=_keys(raw.get("keys")),
        )

    if provider_type == "drag":
        _reject_unknown(raw, common | {"path", "keys"}, "invalid_computer_action")
        return ComputerActionRequest(
            provider_call_id=call_id,
            ordinal=ordinal,
            kind="drag",
            path=_drag_path(raw.get("path")),
            keys=_keys(raw.get("keys")),
        )

    _reject_unknown(raw, common, "invalid_computer_action")
    return ComputerActionRequest(
        provider_call_id=call_id,
        ordinal=ordinal,
        kind=provider_type,
    )


def _parse_safety_checks(value: Any) -> tuple[ProviderSafetyCheckRequirement, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > 64:
        raise ProviderToolContractError("invalid_provider_safety_checks")
    requirements: list[ProviderSafetyCheckRequirement] = []
    seen: set[str] = set()
    for raw_value in value:
        raw = _object(raw_value, "invalid_provider_safety_check")
        _reject_unknown(raw, {"id", "code", "message"}, "invalid_provider_safety_check")
        check_id = _string(raw.get("id"), "invalid_provider_safety_check")
        if check_id in seen:
            raise ProviderToolContractError("duplicate_provider_safety_check")
        seen.add(check_id)
        requirements.append(
            ProviderSafetyCheckRequirement(
                check_id=check_id,
                code=_string(raw.get("code"), "invalid_provider_safety_check"),
                message=_string(
                    raw.get("message"),
                    "invalid_provider_safety_check",
                    maximum=8_192,
                ),
            )
        )
    return tuple(requirements)


def parse_openai_computer_call(raw_value: Any) -> ComputerCallPlan:
    """Parse a current GA ``computer_call`` into a platform action proposal."""

    raw = _object(raw_value, "invalid_computer_call")
    item_type = raw.get("type")
    if item_type in {"computer_use_preview", "computer_use_preview_call"}:
        raise ProviderToolContractError("legacy_computer_use_preview_unsupported")
    if item_type != "computer_call":
        raise ProviderToolContractError("invalid_computer_call_type")
    _reject_unknown(
        raw,
        {"id", "type", "call_id", "actions", "status", "pending_safety_checks"},
        "invalid_computer_call",
    )
    call_id = _string(raw.get("call_id"), "invalid_provider_call_id")
    actions_raw = raw.get("actions")
    if not isinstance(actions_raw, list) or not 1 <= len(actions_raw) <= MAX_COMPUTER_ACTIONS:
        raise ProviderToolContractError("invalid_computer_actions")
    item_id_raw = raw.get("id")
    item_id = _string(item_id_raw, "invalid_provider_item_id") if item_id_raw is not None else None
    return ComputerCallPlan(
        provider_call_id=call_id,
        provider_item_id=item_id,
        provider_status=_status(raw),
        actions=tuple(
            _parse_computer_action(action, call_id=call_id, ordinal=index)
            for index, action in enumerate(actions_raw)
        ),
        approval_requirements=_parse_safety_checks(raw.get("pending_safety_checks")),
    )


def _validate_screenshot(observation: ComputerScreenshotObservation) -> None:
    if observation.detail != "original":
        raise ProviderToolContractError("unsupported_computer_screenshot_detail")
    image_url = observation.image_url
    if not isinstance(image_url, str) or not image_url:
        raise ProviderToolContractError("invalid_computer_screenshot")
    if image_url.startswith("data:"):
        prefix = "data:image/png;base64,"
        if not image_url.startswith(prefix):
            raise ProviderToolContractError("invalid_computer_screenshot")
        try:
            decoded = base64.b64decode(image_url[len(prefix) :], validate=True)
        except (ValueError, binascii.Error):
            raise ProviderToolContractError("invalid_computer_screenshot") from None
        if not decoded or len(decoded) > MAX_SCREENSHOT_BYTES:
            raise ProviderToolContractError("invalid_computer_screenshot")
        return
    parsed = urlsplit(image_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ProviderToolContractError("invalid_computer_screenshot")


def build_openai_computer_call_output(
    plan: ComputerCallPlan,
    observation: ComputerScreenshotObservation,
    *,
    approved_safety_check_ids: Sequence[str] = (),
) -> dict[str, Any]:
    """Build a GA screenshot output after exact platform safety approval.

    The output call id is taken only from ``plan``.  Pending provider checks are
    never auto-acknowledged: callers must supply the exact approved id set after
    the canonical platform approval flow has completed.
    """

    _validate_screenshot(observation)
    required = {requirement.check_id for requirement in plan.approval_requirements}
    approved_list = list(approved_safety_check_ids)
    if any(not isinstance(item, str) or not item for item in approved_list):
        raise ProviderToolContractError("invalid_provider_safety_approval")
    approved = set(approved_list)
    if len(approved) != len(approved_list):
        raise ProviderToolContractError("invalid_provider_safety_approval")
    if approved != required:
        code = (
            "provider_safety_approval_required"
            if required and not approved
            else "provider_safety_approval_mismatch"
        )
        raise ProviderToolContractError(code)

    output: dict[str, Any] = {
        "type": "computer_call_output",
        "call_id": plan.provider_call_id,
        "output": {
            "type": "computer_screenshot",
            "image_url": observation.image_url,
            "detail": "original",
        },
    }
    if plan.approval_requirements:
        output["acknowledged_safety_checks"] = [
            requirement.to_acknowledgement() for requirement in plan.approval_requirements
        ]
    return output


def _canonical_workspace_path(workspace_root: str | Path, cwd: str | Path) -> str:
    root = Path(workspace_root).expanduser().resolve(strict=False)
    candidate = Path(cwd).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError:
        raise ProviderToolContractError("shell_cwd_outside_workspace") from None
    return str(candidate)


def _domain(value: Any) -> str:
    domain = _string(value, "invalid_shell_network_domain", maximum=253).lower().rstrip(".")
    if "://" in domain or "/" in domain or ":" in domain:
        raise ProviderToolContractError("invalid_shell_network_domain")
    labels = domain.split(".")
    if len(labels) < 2 or any(not _DOMAIN_LABEL.fullmatch(label) for label in labels):
        raise ProviderToolContractError("invalid_shell_network_domain")
    return domain


def _network_policy(network_allowlist: Sequence[str]) -> NetworkPolicy:
    domains = tuple(dict.fromkeys(_domain(value) for value in network_allowlist))
    if not domains:
        return NetworkPolicy()
    return NetworkPolicy(mode="allowlist", allowed_domains=domains)


def _command_argv(command: Any) -> tuple[str, ...]:
    command_text = _string(command, "invalid_shell_command", maximum=100_000)
    if "\n" in command_text or "\r" in command_text:
        raise ProviderToolContractError("unsupported_shell_syntax")
    try:
        lexer = shlex.shlex(command_text, posix=True, punctuation_chars="|&;<>()")
        lexer.whitespace_split = True
        lexer.commenters = ""
        argv = tuple(lexer)
    except ValueError:
        raise ProviderToolContractError("invalid_shell_command") from None
    if not argv:
        raise ProviderToolContractError("invalid_shell_command")
    if any(token in _SHELL_PUNCTUATION for token in argv):
        raise ProviderToolContractError("unsupported_shell_syntax")
    if _ENVIRONMENT_ASSIGNMENT.match(argv[0]):
        raise ProviderToolContractError("unsupported_shell_environment_assignment")
    if any("\x00" in token for token in argv):
        raise ProviderToolContractError("invalid_shell_command")
    return argv


def parse_openai_shell_call(
    raw_value: Any,
    *,
    workspace_root: str | Path,
    cwd: str | Path,
    network_allowlist: Sequence[str] = (),
) -> ShellCallPlan:
    """Parse a current local ``shell_call`` into restricted process requests."""

    raw = _object(raw_value, "invalid_shell_call")
    item_type = raw.get("type")
    if item_type in {"local_shell", "local_shell_call"}:
        raise ProviderToolContractError("legacy_local_shell_unsupported")
    if item_type != "shell_call":
        raise ProviderToolContractError("invalid_shell_call_type")
    _reject_unknown(raw, {"id", "type", "call_id", "action", "status"}, "invalid_shell_call")
    call_id = _string(raw.get("call_id"), "invalid_provider_call_id")
    item_id_raw = raw.get("id")
    item_id = _string(item_id_raw, "invalid_provider_item_id") if item_id_raw is not None else None
    action = _object(raw.get("action"), "invalid_shell_action")
    _reject_unknown(
        action,
        {"commands", "timeout_ms", "max_output_length"},
        "invalid_shell_action",
    )
    commands = action.get("commands")
    if not isinstance(commands, list) or not 1 <= len(commands) <= MAX_SHELL_COMMANDS:
        raise ProviderToolContractError("invalid_shell_commands")
    timeout_ms = _integer(
        action.get("timeout_ms"),
        "invalid_shell_timeout",
        minimum=1,
        maximum=MAX_SHELL_TIMEOUT_MS,
    )
    max_output_length = _integer(
        action.get("max_output_length"),
        "invalid_shell_output_limit",
        minimum=1,
        maximum=MAX_SHELL_OUTPUT_LENGTH,
    )
    canonical_cwd = _canonical_workspace_path(workspace_root, cwd)
    policy = _network_policy(network_allowlist)
    requests = tuple(
        RestrictedProcessRequest(
            provider_call_id=call_id,
            command_index=index,
            argv=_command_argv(command),
            cwd=canonical_cwd,
            timeout_ms=timeout_ms,
            max_output_length=max_output_length,
            network_policy=policy,
        )
        for index, command in enumerate(commands)
    )
    return ShellCallPlan(
        provider_call_id=call_id,
        provider_item_id=item_id,
        provider_status=_status(raw),
        requests=requests,
        max_output_length=max_output_length,
    )


def build_openai_shell_call_output(
    plan: ShellCallPlan,
    results: Sequence[ProcessExecutionResult],
) -> dict[str, Any]:
    """Project exact Local Node process results back to ``shell_call_output``."""

    if len(results) != len(plan.requests):
        raise ProviderToolContractError("shell_result_count_mismatch")
    by_index: dict[int, ProcessExecutionResult] = {}
    for result in results:
        if not isinstance(result, ProcessExecutionResult):
            raise ProviderToolContractError("invalid_shell_result")
        if result.command_index in by_index:
            raise ProviderToolContractError("duplicate_shell_result")
        if not isinstance(result.stdout, str) or not isinstance(result.stderr, str):
            raise ProviderToolContractError("invalid_shell_result")
        if len(result.stdout) + len(result.stderr) > plan.max_output_length:
            raise ProviderToolContractError("shell_result_exceeds_output_limit")
        if result.outcome == "exit":
            _integer(result.exit_code, "invalid_shell_exit_code", minimum=-32_768, maximum=32_767)
        elif result.outcome == "timeout":
            if result.exit_code is not None:
                raise ProviderToolContractError("invalid_shell_timeout_result")
        else:
            raise ProviderToolContractError("invalid_shell_outcome")
        by_index[result.command_index] = result

    expected_indexes = {request.command_index for request in plan.requests}
    if set(by_index) != expected_indexes:
        raise ProviderToolContractError("shell_result_index_mismatch")

    output: list[dict[str, Any]] = []
    for request in plan.requests:
        result = by_index[request.command_index]
        outcome: dict[str, Any] = {"type": result.outcome}
        if result.outcome == "exit":
            outcome["exit_code"] = result.exit_code
        output.append(
            {
                "stdout": result.stdout,
                "stderr": result.stderr,
                "outcome": outcome,
            }
        )
    return {
        "type": "shell_call_output",
        "call_id": plan.provider_call_id,
        "max_output_length": plan.max_output_length,
        "output": output,
    }


def validate_openai_local_tool_definition(raw_value: Any) -> OpenAILocalToolDefinition:
    """Accept only current GA ``computer`` and current ``shell`` local mode."""

    raw = _object(raw_value, "invalid_openai_local_tool_definition")
    tool_type = raw.get("type")
    if tool_type == "computer_use_preview":
        raise ProviderToolContractError("legacy_computer_use_preview_unsupported")
    if tool_type == "local_shell":
        raise ProviderToolContractError("legacy_local_shell_unsupported")
    if tool_type == "computer":
        _reject_unknown(raw, {"type"}, "invalid_computer_tool_definition")
        return OpenAILocalToolDefinition(kind="computer")
    if tool_type == "shell":
        _reject_unknown(raw, {"type", "environment"}, "invalid_shell_tool_definition")
        environment = _object(raw.get("environment"), "invalid_shell_tool_definition")
        _reject_unknown(environment, {"type"}, "invalid_shell_tool_definition")
        if environment.get("type") != "local":
            raise ProviderToolContractError("unsupported_shell_environment")
        return OpenAILocalToolDefinition(kind="shell", environment="local")
    raise ProviderToolContractError("unsupported_openai_local_tool")
