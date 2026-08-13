"""Internal OpenAI Responses local-tool bridge for the canonical AgentLoop.

OpenAI ``computer_call`` and ``shell_call`` items are model proposals.  This
module projects them into the already-authorized request-scoped Local Node
tools; it never executes host actions itself.  The resulting canonical tool
calls therefore retain the ordinary ToolRegistry, ExecutionGateway, approval,
command-ledger, and Local Node recheck boundaries.

The provider wire items deliberately omit platform grant IDs and target
handles.  Those values may only come from an injected trusted binding resolver.
An absent or ambiguous binding produces no native provider tool declaration.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, cast, runtime_checkable

from ai_gateway_core.enums import ModelProvider
from ai_gateway_core.logging import record_internal_exception

from ..local_node.tool_bridge import LocalNodeRunScope
from .openai_responses_tools import (
    ComputerActionRequest,
    ComputerCallPlan,
    ComputerScreenshotObservation,
    ProcessExecutionResult,
    ProviderToolContractError,
    build_openai_computer_call_output,
    build_openai_shell_call_output,
    parse_openai_computer_call,
    parse_openai_shell_call,
    validate_openai_local_tool_definition,
)

OPENAI_LOCAL_PROVIDER_BLOCK = "openai_responses_local_call"
OPENAI_LOCAL_RESULT_BLOCK = "openai_responses_local_result"
OPENAI_LOCAL_BLOCK_VERSION = 1


class OpenAIResponsesRuntimeError(ValueError):
    """Prompt-safe internal runtime contract failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"openai-responses local runtime failed ({code})")


@dataclass(frozen=True, slots=True)
class OpenAIResponsesComputerBinding:
    """Trusted target for one request-scoped Computer Use continuation."""

    app_grant_id: str
    window_id: str
    observation_id: str

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value and len(value) <= 200
            for value in (self.app_grant_id, self.window_id, self.observation_id)
        ):
            raise ValueError("OpenAI Responses computer binding is invalid")


@dataclass(frozen=True, slots=True)
class OpenAIResponsesShellBinding:
    """Trusted workspace grant and platform-owned shell restrictions."""

    grant_id: str
    workspace_root: str
    cwd: str = "."
    network_allowlist: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.grant_id, str) or not self.grant_id or len(self.grant_id) > 200:
            raise ValueError("OpenAI Responses shell grant is invalid")
        if not isinstance(self.workspace_root, str) or not self.workspace_root:
            raise ValueError("OpenAI Responses shell workspace is invalid")
        if not isinstance(self.cwd, str) or not self.cwd:
            raise ValueError("OpenAI Responses shell cwd is invalid")
        if any(not isinstance(value, str) or not value for value in self.network_allowlist):
            raise ValueError("OpenAI Responses shell network policy is invalid")


@dataclass(frozen=True, slots=True)
class OpenAIResponsesLocalBindings:
    """Exact capability-specific bindings selected by trusted server state."""

    scope: LocalNodeRunScope
    computer: OpenAIResponsesComputerBinding | None = None
    shell: OpenAIResponsesShellBinding | None = None


@runtime_checkable
class OpenAIResponsesLocalBindingResolver(Protocol):
    """Resolve deterministic local targets without consulting model/Web input.

    Implementations must fail closed when more than one grant is eligible for
    a requested capability.  Selecting the first matching grant is forbidden.
    """

    async def resolve(
        self,
        scope: LocalNodeRunScope,
        *,
        required_tool_names: frozenset[str],
    ) -> OpenAIResponsesLocalBindings | None: ...


@dataclass(frozen=True, slots=True)
class OpenAIResponsesLocalReadiness:
    """Non-secret OS-A22 evidence for the native provider path."""

    status: Literal["ready", "not_run"]
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"status": self.status, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class OpenAIResponsesToolProjection:
    """One provider call plus the canonical calls that must satisfy it."""

    provider_block: dict[str, Any]
    tool_calls: tuple[dict[str, Any], ...]


def _canonical_call(
    *,
    call_id: str,
    tool_name: str,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "index": 0,
        "id": call_id,
        "type": "function",
        "function": {
            "name": tool_name,
            "arguments": json.dumps(
                dict(arguments),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        },
    }


def _safety_checks(plan: ComputerCallPlan) -> list[dict[str, str]]:
    return [item.to_acknowledgement() for item in plan.approval_requirements]


def _canonical_computer_action(action: ComputerActionRequest) -> dict[str, Any]:
    """Narrow a provider proposal to the concrete Local Node action contract.

    The provider accepts a wider action vocabulary than the first Local Node
    backend.  Unsupported semantics are rejected instead of being silently
    approximated (for example, a right-click cannot become a left-click).
    """

    arguments = action.normalized_arguments()
    if action.kind in {"click", "double_click"}:
        if arguments.get("button", "left") != "left" or arguments.get("keys"):
            raise OpenAIResponsesRuntimeError("unsupported_local_computer_action")
        return {"type": action.kind, "x": action.x, "y": action.y}
    if action.kind == "scroll":
        if arguments.get("scroll_x") != 0 or arguments.get("keys"):
            raise OpenAIResponsesRuntimeError("unsupported_local_computer_action")
        return {
            "type": "scroll",
            "x": action.x,
            "y": action.y,
            "scroll_y": action.scroll_y,
        }
    if action.kind == "type_text":
        return {"type": "type", "text": action.text}
    if action.kind == "key_press":
        return {"type": "keypress", "key": "+".join(action.keys)}
    if action.kind == "drag":
        if arguments.get("keys") or len(action.path) < 2:
            raise OpenAIResponsesRuntimeError("unsupported_local_computer_action")
        return {
            "type": "drag",
            "from_x": action.path[0].x,
            "from_y": action.path[0].y,
            "to_x": action.path[-1].x,
            "to_y": action.path[-1].y,
        }
    if action.kind == "wait":
        return {"type": "wait", "duration_ms": 1_000}
    raise OpenAIResponsesRuntimeError("unsupported_local_computer_action")


def _decode_result(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise OpenAIResponsesRuntimeError("invalid_canonical_tool_result") from exc
    if not isinstance(value, dict):
        raise OpenAIResponsesRuntimeError("invalid_canonical_tool_result")
    # Canonical tool content is normally wrapped as untrusted external content
    # before it is inserted into model history.  A provider result block stores
    # the pre-envelope result, but accepting the exact envelope keeps resume and
    # checkpoint replay compatible without treating its contents as authority.
    if value.get("schema_version") == "assistant-external-content/v1":
        content = value.get("content")
        if not isinstance(content, str):
            raise OpenAIResponsesRuntimeError("invalid_canonical_tool_result")
        try:
            value = json.loads(content)
        except json.JSONDecodeError as exc:
            raise OpenAIResponsesRuntimeError("invalid_canonical_tool_result") from exc
        if not isinstance(value, dict):
            raise OpenAIResponsesRuntimeError("invalid_canonical_tool_result")
    return value


def _first_mapping(value: Mapping[str, Any], *names: str) -> Mapping[str, Any]:
    for name in names:
        candidate = value.get(name)
        if isinstance(candidate, Mapping):
            return candidate
    return value


def _computer_observation(result: Any) -> ComputerScreenshotObservation:
    value = _decode_result(result)
    observation = _first_mapping(value, "observation", "after_observation", "screenshot")
    image_url = observation.get("image_url") or value.get("image_url")
    observation_ref = (
        observation.get("observation_id")
        or observation.get("observation_ref")
        or value.get("observation_id")
        or value.get("observation_ref")
    )
    if not isinstance(image_url, str) or not image_url:
        raise OpenAIResponsesRuntimeError("computer_screenshot_result_required")
    if observation_ref is not None and (
        not isinstance(observation_ref, str) or not observation_ref
    ):
        raise OpenAIResponsesRuntimeError("invalid_computer_observation_ref")
    return ComputerScreenshotObservation(
        image_url=image_url,
        observation_ref=observation_ref,
    )


def _process_result(result: Any, *, command_index: int) -> ProcessExecutionResult:
    value = _decode_result(result)
    stdout = value.get("stdout", "")
    stderr = value.get("stderr", "")
    if not isinstance(stdout, str) or not isinstance(stderr, str):
        raise OpenAIResponsesRuntimeError("invalid_process_result")
    raw_outcome = value.get("outcome")
    exit_code = value.get("exit_code")
    outcome: str
    if isinstance(raw_outcome, Mapping):
        outcome = str(raw_outcome.get("type") or "")
        exit_code = raw_outcome.get("exit_code", exit_code)
    elif isinstance(raw_outcome, str):
        outcome = raw_outcome
    elif value.get("status") in {"timeout", "timed_out"} or value.get("error_code") == "timeout":
        outcome = "timeout"
    else:
        outcome = "exit"
    if outcome == "timeout":
        exit_code = None
    if outcome not in {"exit", "timeout"}:
        raise OpenAIResponsesRuntimeError("invalid_process_result")
    if outcome == "exit" and (isinstance(exit_code, bool) or not isinstance(exit_code, int)):
        raise OpenAIResponsesRuntimeError("invalid_process_result")
    return ProcessExecutionResult(
        command_index=command_index,
        stdout=stdout,
        stderr=stderr,
        outcome=outcome,  # type: ignore[arg-type]
        exit_code=exit_code,
    )


@dataclass(slots=True)
class OpenAIResponsesLocalRuntime:
    """Mutable per-run native adapter state; it owns no execution authority."""

    scope: LocalNodeRunScope
    bindings: OpenAIResponsesLocalBindings
    enabled_tool_names: frozenset[str]
    readiness: OpenAIResponsesLocalReadiness
    _projections: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)

    @property
    def ready(self) -> bool:
        return self.readiness.status == "ready"

    def tool_definitions(self) -> list[dict[str, Any]]:
        if not self.ready:
            return []
        definitions: list[dict[str, Any]] = []
        if self.bindings.computer is not None and {
            "local_screen_observe",
            "local_app_control",
        }.issubset(self.enabled_tool_names):
            computer_definition: dict[str, Any] = {"type": "computer"}
            validate_openai_local_tool_definition(computer_definition)
            definitions.append(computer_definition)
        if self.bindings.shell is not None and "local_process_run" in self.enabled_tool_names:
            shell_definition: dict[str, Any] = {
                "type": "shell",
                "environment": {"type": "local"},
            }
            validate_openai_local_tool_definition(shell_definition)
            definitions.append(shell_definition)
        return definitions

    def hidden_function_tool_names(self) -> frozenset[str]:
        hidden: set[str] = set()
        definitions = self.tool_definitions()
        if any(item["type"] == "computer" for item in definitions):
            hidden.update({"local_screen_observe", "local_app_control"})
        if any(item["type"] == "shell" for item in definitions):
            hidden.add("local_process_run")
        return frozenset(hidden)

    def project_provider_item(self, item: Mapping[str, Any]) -> OpenAIResponsesToolProjection:
        """Convert one completed provider item into canonical local tool calls."""

        if not self.ready:
            raise OpenAIResponsesRuntimeError("native_local_tools_not_ready")
        item_type = item.get("type")
        if item_type == "computer_call":
            projection = self._project_computer(item)
        elif item_type == "shell_call":
            projection = self._project_shell(item)
        elif item_type in {
            "computer_use_preview",
            "computer_use_preview_call",
            "local_shell",
            "local_shell_call",
        }:
            raise OpenAIResponsesRuntimeError("legacy_provider_local_tool_unsupported")
        else:
            raise OpenAIResponsesRuntimeError("unsupported_provider_local_tool")
        provider_call_id = str(projection.provider_block["provider_call_id"])
        if provider_call_id in self._projections:
            raise OpenAIResponsesRuntimeError("provider_call_rebinding")
        self._projections[provider_call_id] = copy.deepcopy(projection.provider_block)
        return projection

    def _project_computer(
        self,
        item: Mapping[str, Any],
    ) -> OpenAIResponsesToolProjection:
        binding = self.bindings.computer
        if binding is None or not {
            "local_screen_observe",
            "local_app_control",
        }.issubset(self.enabled_tool_names):
            raise OpenAIResponsesRuntimeError("computer_binding_unavailable")
        try:
            plan = parse_openai_computer_call(dict(item))
        except ProviderToolContractError as exc:
            raise OpenAIResponsesRuntimeError(exc.code) from exc
        controls = [action for action in plan.actions if action.kind != "screenshot"]
        checks = _safety_checks(plan)
        if controls:
            arguments: dict[str, Any] = {
                "app_grant_id": binding.app_grant_id,
                "window_id": binding.window_id,
                "observation_id": binding.observation_id,
                "actions": [_canonical_computer_action(action) for action in controls],
            }
            tool_name = "local_app_control"
        else:
            arguments = {
                "app_grant_id": binding.app_grant_id,
                "window_id": binding.window_id,
                "include_screenshot": True,
            }
            tool_name = "local_screen_observe"
        if checks:
            # This field is part of the canonical argument/approval hash.  It
            # can only increase restrictions and is never an approval itself.
            arguments["provider_safety_checks"] = checks
            # This is a server-owned Gateway control argument.  The Gateway
            # strips it before schema validation/dispatch, while the safety
            # check payload itself remains in the exact approval hash.
            arguments["_middleware_approval_required"] = True
        canonical_id = f"{plan.provider_call_id}:local"
        block = {
            "type": OPENAI_LOCAL_PROVIDER_BLOCK,
            "version": OPENAI_LOCAL_BLOCK_VERSION,
            "provider": "openai-responses",
            "provider_call_id": plan.provider_call_id,
            "provider_item": copy.deepcopy(dict(item)),
            "kind": "computer",
            "canonical_calls": [{"id": canonical_id, "tool_name": tool_name, "command_index": 0}],
        }
        return OpenAIResponsesToolProjection(
            provider_block=block,
            tool_calls=(
                _canonical_call(
                    call_id=canonical_id,
                    tool_name=tool_name,
                    arguments=arguments,
                ),
            ),
        )

    def _project_shell(self, item: Mapping[str, Any]) -> OpenAIResponsesToolProjection:
        binding = self.bindings.shell
        if binding is None or "local_process_run" not in self.enabled_tool_names:
            raise OpenAIResponsesRuntimeError("shell_binding_unavailable")
        try:
            plan = parse_openai_shell_call(
                dict(item),
                workspace_root=binding.workspace_root,
                cwd=binding.cwd,
                network_allowlist=binding.network_allowlist,
            )
        except ProviderToolContractError as exc:
            raise OpenAIResponsesRuntimeError(exc.code) from exc
        root = Path(binding.workspace_root).expanduser().resolve(strict=False)
        calls: list[dict[str, Any]] = []
        canonical_calls: list[dict[str, Any]] = []
        for request in plan.requests:
            try:
                relative_cwd = Path(request.cwd).relative_to(root)
            except ValueError as exc:
                raise OpenAIResponsesRuntimeError("shell_cwd_outside_workspace") from exc
            cwd = str(relative_cwd) or "."
            canonical_id = f"{plan.provider_call_id}:command:{request.command_index}"
            arguments = {
                "grant_id": binding.grant_id,
                "argv": list(request.argv),
                "cwd": cwd,
                "timeout_ms": request.timeout_ms,
                "network_policy": (
                    "deny" if request.network_policy.mode == "deny" else "allow_granted_domains"
                ),
            }
            calls.append(
                _canonical_call(
                    call_id=canonical_id,
                    tool_name="local_process_run",
                    arguments=arguments,
                )
            )
            canonical_calls.append(
                {
                    "id": canonical_id,
                    "tool_name": "local_process_run",
                    "command_index": request.command_index,
                }
            )
        for index, call in enumerate(calls):
            call["index"] = index
        block = {
            "type": OPENAI_LOCAL_PROVIDER_BLOCK,
            "version": OPENAI_LOCAL_BLOCK_VERSION,
            "provider": "openai-responses",
            "provider_call_id": plan.provider_call_id,
            "provider_item": copy.deepcopy(dict(item)),
            "kind": "shell",
            "canonical_calls": canonical_calls,
        }
        return OpenAIResponsesToolProjection(
            provider_block=block,
            tool_calls=tuple(calls),
        )

    @staticmethod
    def _validated_provider_block(value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict) or value.get("type") != OPENAI_LOCAL_PROVIDER_BLOCK:
            return None
        if (
            value.get("version") != OPENAI_LOCAL_BLOCK_VERSION
            or value.get("provider") != "openai-responses"
            or value.get("kind") not in {"computer", "shell"}
            or not isinstance(value.get("provider_call_id"), str)
            or not value.get("provider_call_id")
            or not isinstance(value.get("provider_item"), dict)
            or not isinstance(value.get("canonical_calls"), list)
            or not value["canonical_calls"]
        ):
            raise OpenAIResponsesRuntimeError("invalid_provider_continuation_block")
        seen: set[str] = set()
        for call in value["canonical_calls"]:
            if (
                not isinstance(call, dict)
                or not isinstance(call.get("id"), str)
                or not call.get("id")
                or call["id"] in seen
                or not isinstance(call.get("tool_name"), str)
                or not call.get("tool_name")
                or isinstance(call.get("command_index"), bool)
                or not isinstance(call.get("command_index"), int)
                or call["command_index"] < 0
            ):
                raise OpenAIResponsesRuntimeError("invalid_provider_continuation_block")
            seen.add(call["id"])
        return value

    @staticmethod
    def result_block(
        *,
        provider_blocks: Sequence[Mapping[str, Any]],
        call_id: str,
        tool_name: str,
        success: bool,
        result: Any,
        error: str | None,
        metadata: Mapping[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Build a server-owned continuation receipt for one canonical result."""

        owner: dict[str, Any] | None = None
        for raw_block in provider_blocks:
            block = OpenAIResponsesLocalRuntime._validated_provider_block(raw_block)
            if block is None:
                continue
            for call in block["canonical_calls"]:
                if call["id"] == call_id and call["tool_name"] == tool_name:
                    if owner is not None:
                        raise OpenAIResponsesRuntimeError("canonical_call_rebinding")
                    owner = block
        if owner is None:
            return None
        safe_metadata = dict(metadata or {})
        gateway_decision = safe_metadata.get("gateway_decision")
        execution_authorized = bool(
            isinstance(gateway_decision, Mapping)
            and gateway_decision.get("allowed") is True
            and str(safe_metadata.get("queue_state") or "")
            in {
                "dispatched",
                "running",
                "observed",
                "succeeded",
                "result_recorded_succeeded",
            }
        )
        return {
            "type": OPENAI_LOCAL_RESULT_BLOCK,
            "version": OPENAI_LOCAL_BLOCK_VERSION,
            "provider": "openai-responses",
            "provider_call_id": owner["provider_call_id"],
            "canonical_call_id": call_id,
            "tool_name": tool_name,
            "success": bool(success),
            "result": copy.deepcopy(result),
            "error": str(error or "") or None,
            "gateway_approved": execution_authorized,
            "approval_consumed": safe_metadata.get("approval_consumed") is True,
        }

    def build_provider_output(
        self,
        provider_block: Mapping[str, Any],
        result_blocks: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Build one exact GA call output after every canonical result exists."""

        block = self._validated_provider_block(dict(provider_block))
        if block is None:
            raise OpenAIResponsesRuntimeError("invalid_provider_continuation_block")
        expected = {call["id"]: call for call in block["canonical_calls"]}
        results: dict[str, Mapping[str, Any]] = {}
        for result in result_blocks:
            if (
                not isinstance(result, Mapping)
                or result.get("type") != OPENAI_LOCAL_RESULT_BLOCK
                or result.get("version") != OPENAI_LOCAL_BLOCK_VERSION
                or result.get("provider") != "openai-responses"
                or result.get("provider_call_id") != block["provider_call_id"]
            ):
                continue
            call_id = result.get("canonical_call_id")
            if not isinstance(call_id, str) or call_id not in expected or call_id in results:
                raise OpenAIResponsesRuntimeError("canonical_result_rebinding")
            if result.get("tool_name") != expected[call_id]["tool_name"]:
                raise OpenAIResponsesRuntimeError("canonical_result_rebinding")
            results[call_id] = result
        if set(results) != set(expected):
            raise OpenAIResponsesRuntimeError("canonical_result_incomplete")
        if any(
            result.get("success") is not True or result.get("gateway_approved") is not True
            for result in results.values()
        ):
            raise OpenAIResponsesRuntimeError("canonical_local_execution_failed")

        provider_item = block["provider_item"]
        if block["kind"] == "computer":
            try:
                computer_plan = parse_openai_computer_call(provider_item)
            except ProviderToolContractError as exc:
                raise OpenAIResponsesRuntimeError(exc.code) from exc
            canonical_result = next(iter(results.values()))
            approved_ids: Sequence[str] = ()
            if computer_plan.approval_requirements:
                if canonical_result.get("approval_consumed") is not True:
                    raise OpenAIResponsesRuntimeError("provider_safety_approval_required")
                approved_ids = [item.check_id for item in computer_plan.approval_requirements]
            try:
                return cast(
                    dict[str, Any],
                    build_openai_computer_call_output(
                        computer_plan,
                        _computer_observation(canonical_result.get("result")),
                        approved_safety_check_ids=approved_ids,
                    ),
                )
            except ProviderToolContractError as exc:
                raise OpenAIResponsesRuntimeError(exc.code) from exc

        binding = self.bindings.shell
        if binding is None:
            raise OpenAIResponsesRuntimeError("shell_binding_unavailable")
        try:
            shell_plan = parse_openai_shell_call(
                provider_item,
                workspace_root=binding.workspace_root,
                cwd=binding.cwd,
                network_allowlist=binding.network_allowlist,
            )
            process_results = [
                _process_result(
                    results[call["id"]].get("result"),
                    command_index=call["command_index"],
                )
                for call in block["canonical_calls"]
            ]
            return cast(
                dict[str, Any],
                build_openai_shell_call_output(shell_plan, process_results),
            )
        except ProviderToolContractError as exc:
            raise OpenAIResponsesRuntimeError(exc.code) from exc


def provider_readiness(model_registry: Any, model_id: str) -> OpenAIResponsesLocalReadiness:
    """Check configured provider state without reading or exposing credentials."""

    model = model_registry.get_model(model_id) if model_registry is not None else None
    if model is None or getattr(model, "provider", None) != ModelProvider.OPENAI:
        return OpenAIResponsesLocalReadiness("not_run", "model_provider_is_not_openai")
    if not model_registry.is_provider_configured(ModelProvider.OPENAI):
        return OpenAIResponsesLocalReadiness("not_run", "openai_provider_not_configured")
    uses_responses = getattr(model_registry, "_uses_responses_v1", None)
    if not callable(uses_responses) or not uses_responses(ModelProvider.OPENAI):
        return OpenAIResponsesLocalReadiness("not_run", "openai_responses_v1_not_configured")
    return OpenAIResponsesLocalReadiness("ready", "openai_responses_v1_configured")


async def prepare_openai_responses_local_runtime(
    *,
    scope: LocalNodeRunScope,
    model_registry: Any,
    model_id: str,
    resolver: OpenAIResponsesLocalBindingResolver | None,
    selected_tool_names: Sequence[str],
) -> tuple[OpenAIResponsesLocalRuntime | None, OpenAIResponsesLocalReadiness]:
    """Prepare an internal native runtime or return an exact ``not_run`` receipt."""

    readiness = provider_readiness(model_registry, model_id)
    if readiness.status != "ready":
        return None, readiness
    if resolver is None or not isinstance(resolver, OpenAIResponsesLocalBindingResolver):
        return None, OpenAIResponsesLocalReadiness("not_run", "trusted_binding_resolver_absent")
    selected = frozenset(str(value) for value in selected_tool_names if str(value))
    native_candidates = selected & {
        "local_screen_observe",
        "local_app_control",
        "local_process_run",
    }
    if not native_candidates:
        return None, OpenAIResponsesLocalReadiness("not_run", "no_selected_local_tools")
    try:
        bindings = await resolver.resolve(scope, required_tool_names=native_candidates)
    except Exception as exc:
        record_internal_exception(
            __name__, "assistant.core.providers.openai_responses_runtime.internal_failure", exc
        )
        return None, OpenAIResponsesLocalReadiness("not_run", "trusted_binding_unavailable")
    if not isinstance(bindings, OpenAIResponsesLocalBindings) or bindings.scope != scope:
        return None, OpenAIResponsesLocalReadiness("not_run", "trusted_binding_unavailable")
    runtime = OpenAIResponsesLocalRuntime(
        scope=scope,
        bindings=bindings,
        enabled_tool_names=selected,
        readiness=readiness,
    )
    if not runtime.tool_definitions():
        return None, OpenAIResponsesLocalReadiness("not_run", "trusted_binding_incomplete")
    return runtime, readiness


def native_result_blocks(message: Any) -> list[dict[str, Any]]:
    """Return only well-formed server-owned local result blocks from a message."""

    blocks = getattr(message, "provider_content_blocks", None)
    if not isinstance(blocks, list):
        return []
    return [
        copy.deepcopy(block)
        for block in blocks
        if isinstance(block, dict) and block.get("type") == OPENAI_LOCAL_RESULT_BLOCK
    ]
