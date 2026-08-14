"""Source gate for diagnosable, non-leaking Assistant internal failures."""

from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SOURCE_ROOT = _REPO_ROOT / "apps" / "assistant-service" / "src" / "assistant_service"
_AI_GATEWAY_CORE_ROOT = (
    _REPO_ROOT / "packages" / "ai-gateway-core" / "src" / "ai_gateway_core"
)

# ---------------------------------------------------------------------------
# Debt ledger for the shared ai-gateway-core package.
#
# The Assistant source tree is fully gated.  The shared core package still
# carries historical violations, tracked per file per category so that every
# migration shrinks the ledger and any new file starts fully gated.  Entries
# list the categories that remain outstanding:
#
#   missing_diagnostic        broad catch without the safe helper / re-raise
#   raw_traceback             logger.exception / exc_info with a traceback
#   type_only_diagnostic      "exception_type=..." without a safe fingerprint
#   broad_suppress            suppress(Exception/BaseException)
#   caught_exception_in_log   caught exception rendered by a logging call
#
# Remove an entry only when its file is clean in that category; never add a
# file here to silence a NEW violation.
# ---------------------------------------------------------------------------
_AI_GATEWAY_CORE_DEBT_WHITELIST: dict[str, frozenset[str]] = {
    "packages/ai-gateway-core/src/ai_gateway_core/agents/runtime.py": frozenset({"missing_diagnostic"}),  # 1 broad catch
    "packages/ai-gateway-core/src/ai_gateway_core/auth/gateway_secret.py": frozenset({"missing_diagnostic"}),  # 3 broad catches
    "packages/ai-gateway-core/src/ai_gateway_core/auth/gateway_secret_middleware.py": frozenset({"caught_exception_in_log"}),  # 1 caught-in-log
    "packages/ai-gateway-core/src/ai_gateway_core/comm/client.py": frozenset({"caught_exception_in_log", "missing_diagnostic"}),  # 1+3
    "packages/ai-gateway-core/src/ai_gateway_core/connectors/connector_mcp.py": frozenset({"caught_exception_in_log", "missing_diagnostic"}),  # 1+1
    "packages/ai-gateway-core/src/ai_gateway_core/eval/evaluator_executor.py": frozenset({"caught_exception_in_log", "missing_diagnostic"}),  # 2+3
    "packages/ai-gateway-core/src/ai_gateway_core/eval/outbox_worker.py": frozenset({"caught_exception_in_log", "missing_diagnostic"}),  # 2+2
    "packages/ai-gateway-core/src/ai_gateway_core/events/bus.py": frozenset({"missing_diagnostic", "raw_traceback"}),  # 1+1
    "packages/ai-gateway-core/src/ai_gateway_core/events/consumer.py": frozenset({"broad_suppress", "caught_exception_in_log", "missing_diagnostic"}),  # 2+2+1
    "packages/ai-gateway-core/src/ai_gateway_core/events/envelope.py": frozenset({"missing_diagnostic"}),  # 1
    "packages/ai-gateway-core/src/ai_gateway_core/image/callback.py": frozenset({"caught_exception_in_log", "missing_diagnostic"}),  # 3+2
    "packages/ai-gateway-core/src/ai_gateway_core/image/helpers.py": frozenset({"missing_diagnostic"}),  # 1
    "packages/ai-gateway-core/src/ai_gateway_core/image/image_state.py": frozenset({"caught_exception_in_log", "missing_diagnostic"}),  # 11+2
    "packages/ai-gateway-core/src/ai_gateway_core/image/thumbnail.py": frozenset({"caught_exception_in_log", "missing_diagnostic"}),  # 1+1
    "packages/ai-gateway-core/src/ai_gateway_core/image/watermark.py": frozenset({"caught_exception_in_log", "missing_diagnostic"}),  # 1+2
    "packages/ai-gateway-core/src/ai_gateway_core/knowledge/proxy_client.py": frozenset({"caught_exception_in_log", "missing_diagnostic"}),  # 5+5
    "packages/ai-gateway-core/src/ai_gateway_core/logging/_core.py": frozenset({"missing_diagnostic"}),  # 2 (logging subsystem internals)
    "packages/ai-gateway-core/src/ai_gateway_core/logging/_exceptions.py": frozenset({"missing_diagnostic"}),  # 7 (helper's own defensive handlers)
    "packages/ai-gateway-core/src/ai_gateway_core/metrics/context_metrics.py": frozenset({"caught_exception_in_log", "missing_diagnostic"}),  # 1+1
    "packages/ai-gateway-core/src/ai_gateway_core/metrics/realtime_metrics.py": frozenset({"caught_exception_in_log", "missing_diagnostic"}),  # 9+10
    "packages/ai-gateway-core/src/ai_gateway_core/metrics/usage_recorder.py": frozenset({"caught_exception_in_log", "missing_diagnostic"}),  # 14+16
    "packages/ai-gateway-core/src/ai_gateway_core/persistence/database.py": frozenset({"broad_suppress", "caught_exception_in_log", "raw_traceback"}),  # 2+1+36
    "packages/ai-gateway-core/src/ai_gateway_core/persistence/redis.py": frozenset({"caught_exception_in_log", "missing_diagnostic"}),  # 1+2
    "packages/ai-gateway-core/src/ai_gateway_core/persistence/repositories/api_key_repository.py": frozenset({"broad_suppress"}),  # 1
    "packages/ai-gateway-core/src/ai_gateway_core/persistence/repositories/mcp_repository.py": frozenset({"missing_diagnostic"}),  # 1
    "packages/ai-gateway-core/src/ai_gateway_core/proxy/base.py": frozenset({"broad_suppress", "caught_exception_in_log", "missing_diagnostic", "raw_traceback"}),  # 1+2+9+1
    "packages/ai-gateway-core/src/ai_gateway_core/proxy/drain.py": frozenset({"caught_exception_in_log"}),  # 1
    "packages/ai-gateway-core/src/ai_gateway_core/proxy/sse_heartbeat.py": frozenset({"broad_suppress", "missing_diagnostic"}),  # 1+1
    "packages/ai-gateway-core/src/ai_gateway_core/quiz/quiz_generator.py": frozenset({"caught_exception_in_log"}),  # 1
    "packages/ai-gateway-core/src/ai_gateway_core/quiz/quiz_grader.py": frozenset({"caught_exception_in_log", "missing_diagnostic"}),  # 1+1
    "packages/ai-gateway-core/src/ai_gateway_core/security/secrets.py": frozenset({"caught_exception_in_log", "missing_diagnostic"}),  # 3+3
    "packages/ai-gateway-core/src/ai_gateway_core/skills/artifact_repository.py": frozenset({"missing_diagnostic"}),  # 1
    "packages/ai-gateway-core/src/ai_gateway_core/storage/artifact_storage.py": frozenset({"caught_exception_in_log", "missing_diagnostic"}),  # 3+5
    "packages/ai-gateway-core/src/ai_gateway_core/storage/image_storage.py": frozenset({"caught_exception_in_log", "missing_diagnostic", "type_only_diagnostic"}),  # 10+6+2
    "packages/ai-gateway-core/src/ai_gateway_core/tasks/task_manager.py": frozenset({"caught_exception_in_log", "missing_diagnostic"}),  # 1+1
    "packages/ai-gateway-core/src/ai_gateway_core/tasks/task_types.py": frozenset({"caught_exception_in_log", "missing_diagnostic"}),  # 1+1
    "packages/ai-gateway-core/src/ai_gateway_core/tracing/httpx_hooks.py": frozenset({"caught_exception_in_log", "missing_diagnostic"}),  # 1+1
    "packages/ai-gateway-core/src/ai_gateway_core/tracing/init.py": frozenset({"caught_exception_in_log", "missing_diagnostic"}),  # 2+4
    "packages/ai-gateway-core/src/ai_gateway_core/tracing/middleware.py": frozenset({"missing_diagnostic"}),  # 6 broad catches
}


def _whitelisted(relative: Path, category: str) -> bool:
    """Whether this violation category is covered by the debt ledger."""

    allowed = _AI_GATEWAY_CORE_DEBT_WHITELIST.get(str(relative))
    return allowed is not None and category in allowed


def _service_process_sources() -> list[Path]:
    """Return Python loaded by the service process, excluding sandbox scripts.

    ``core/skills/*/scripts`` are copied third-party command assets executed in
    isolated child processes.  They are not imported into the Assistant server
    process and have their own CLI failure contracts.
    """

    sources = []
    for path in sorted(_SOURCE_ROOT.rglob("*.py")):
        relative_parts = path.relative_to(_SOURCE_ROOT).parts
        if (
            len(relative_parts) >= 4
            and relative_parts[:2] == ("core", "skills")
            and "scripts" in relative_parts[2:]
        ):
            continue
        sources.append(path)
    core_sources = sorted(_AI_GATEWAY_CORE_ROOT.rglob("*.py"))
    return [*sources, *core_sources]


def _catches_broad_exception(node: ast.expr | None) -> bool:
    if node is None:
        return True
    if isinstance(node, ast.Name):
        return node.id in {"Exception", "BaseException"}
    if isinstance(node, ast.Tuple):
        return any(_catches_broad_exception(element) for element in node.elts)
    return False


def _calls_safe_helper(statements: list[ast.stmt]) -> bool:
    for statement in statements:
        for node in ast.walk(statement):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id in {
                "log_internal_exception",
                "record_internal_exception",
            }:
                return True
            if isinstance(node.func, ast.Attribute) and node.func.attr in {
                "log_internal_exception",
                "record_internal_exception",
            }:
                return True
    return False


def _directly_reraises(statements: list[ast.stmt]) -> bool:
    """Allow cleanup followed by a bare re-raise to an outer logged boundary."""

    return bool(statements and isinstance(statements[-1], ast.Raise) and statements[-1].exc is None)


def _logging_call(node: ast.Call) -> bool:
    return isinstance(node.func, ast.Attribute) and node.func.attr in {
        "debug",
        "info",
        "warning",
        "error",
        "critical",
        "exception",
        "log",
    }


def _is_broad_suppress(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, (ast.Name, ast.Attribute))
        and (
            (isinstance(node.func, ast.Name) and node.func.id == "suppress")
            or (isinstance(node.func, ast.Attribute) and node.func.attr == "suppress")
        )
        and any(_catches_broad_exception(argument) for argument in node.args)
    )


def test_assistant_internal_exception_logging_source_gate() -> None:
    missing_diagnostic: list[str] = []
    raw_traceback: list[str] = []
    type_only_diagnostic: list[str] = []
    broad_suppress: list[str] = []
    caught_exception_in_log: list[str] = []

    for path in _service_process_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative = path.relative_to(_REPO_ROOT)

        for node in ast.walk(tree):
            if isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    if _is_broad_suppress(item.context_expr) and not _whitelisted(
                        relative, "broad_suppress"
                    ):
                        broad_suppress.append(f"{relative}:{node.lineno}")

            if isinstance(node, ast.Call) and _logging_call(node):
                if (
                    node.args
                    and "exception_type" in ast.unparse(node.args[0])
                    and not _whitelisted(relative, "type_only_diagnostic")
                ):
                    type_only_diagnostic.append(f"{relative}:{node.lineno}")
                if (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "exception"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in {"logger", "log"}
                    and not _whitelisted(relative, "raw_traceback")
                ):
                    raw_traceback.append(f"{relative}:{node.lineno}:logger.exception")
                for keyword in node.keywords:
                    if keyword.arg != "exc_info":
                        continue
                    if (
                        not (
                            isinstance(keyword.value, ast.Constant)
                            and keyword.value.value in {False, None}
                        )
                        and not _whitelisted(relative, "raw_traceback")
                    ):
                        raw_traceback.append(f"{relative}:{node.lineno}:exc_info")

        for handler in (
            candidate for candidate in ast.walk(tree) if isinstance(candidate, ast.ExceptHandler)
        ):
            if handler.name:
                for statement in handler.body:
                    for candidate in ast.walk(statement):
                        if not isinstance(candidate, ast.Call) or not _logging_call(candidate):
                            continue
                        if (
                            any(
                                isinstance(descendant, ast.Name) and descendant.id == handler.name
                                for descendant in ast.walk(candidate)
                            )
                            and not _whitelisted(relative, "caught_exception_in_log")
                        ):
                            caught_exception_in_log.append(f"{relative}:{candidate.lineno}")

            if not _catches_broad_exception(handler.type):
                continue
            if (
                not _calls_safe_helper(handler.body)
                and not _directly_reraises(handler.body)
                and not _whitelisted(relative, "missing_diagnostic")
            ):
                missing_diagnostic.append(f"{relative}:{handler.lineno}")

    failures = []
    if missing_diagnostic:
        failures.append("broad catches without safe diagnostic:\n" + "\n".join(missing_diagnostic))
    if raw_traceback:
        failures.append("raw traceback logging:\n" + "\n".join(raw_traceback))
    if type_only_diagnostic:
        failures.append(
            "type-only exception logging without a safe fingerprint:\n"
            + "\n".join(type_only_diagnostic)
        )
    if broad_suppress:
        failures.append("broad exception suppression:\n" + "\n".join(broad_suppress))
    if caught_exception_in_log:
        failures.append(
            "caught exception rendered by logger:\n" + "\n".join(caught_exception_in_log)
        )
    assert not failures, "\n\n".join(failures)
