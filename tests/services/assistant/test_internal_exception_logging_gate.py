"""Source gate for diagnosable, non-leaking Assistant internal failures."""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SOURCE_ROOT = _REPO_ROOT / "apps" / "assistant-service" / "src" / "assistant_service"
_AI_GATEWAY_CORE_ROOT = _REPO_ROOT / "packages" / "ai-gateway-core" / "src" / "ai_gateway_core"

# ---------------------------------------------------------------------------
# Debt ledger for the shared ai-gateway-core package.
#
# The Assistant source tree is fully gated.  The shared core package still
# carries historical violations, tracked per file per category so that every
# migration shrinks the ledger and any new file starts fully gated. Entries
# pin the exact count that remains outstanding in each category:
#
#   missing_diagnostic        broad catch without the safe helper / re-raise
#   raw_traceback             logger.exception / exc_info with a traceback
#   type_only_diagnostic      "exception_type=..." without a safe fingerprint
#   broad_suppress            suppress(Exception/BaseException)
#   caught_exception_in_log   caught exception rendered by a logging call
#
# Update a count whenever debt is removed, and remove an entry when its file is
# clean in that category. Never raise a count to silence a NEW violation.
# ---------------------------------------------------------------------------
_AI_GATEWAY_CORE_DEBT_LIMITS: dict[str, dict[str, int]] = {
    "packages/ai-gateway-core/src/ai_gateway_core/agents/runtime.py": {"missing_diagnostic": 1},
    "packages/ai-gateway-core/src/ai_gateway_core/auth/gateway_secret.py": {
        "missing_diagnostic": 3
    },
    "packages/ai-gateway-core/src/ai_gateway_core/auth/gateway_secret_middleware.py": {
        "caught_exception_in_log": 1
    },
    "packages/ai-gateway-core/src/ai_gateway_core/comm/client.py": {
        "caught_exception_in_log": 1,
        "missing_diagnostic": 3,
    },
    "packages/ai-gateway-core/src/ai_gateway_core/connectors/connector_mcp.py": {
        "caught_exception_in_log": 1,
        "missing_diagnostic": 1,
    },
    "packages/ai-gateway-core/src/ai_gateway_core/eval/evaluator_executor.py": {
        "caught_exception_in_log": 2,
        "missing_diagnostic": 3,
    },
    "packages/ai-gateway-core/src/ai_gateway_core/eval/outbox_worker.py": {
        "caught_exception_in_log": 2,
        "missing_diagnostic": 2,
    },
    "packages/ai-gateway-core/src/ai_gateway_core/events/consumer.py": {
        "broad_suppress": 2,
        "caught_exception_in_log": 2,
        "missing_diagnostic": 1,
    },
    "packages/ai-gateway-core/src/ai_gateway_core/events/envelope.py": {"missing_diagnostic": 1},
    "packages/ai-gateway-core/src/ai_gateway_core/image/callback.py": {
        "caught_exception_in_log": 3,
        "missing_diagnostic": 2,
    },
    "packages/ai-gateway-core/src/ai_gateway_core/image/helpers.py": {"missing_diagnostic": 1},
    "packages/ai-gateway-core/src/ai_gateway_core/image/image_state.py": {
        "caught_exception_in_log": 11,
        "missing_diagnostic": 2,
    },
    "packages/ai-gateway-core/src/ai_gateway_core/image/thumbnail.py": {
        "caught_exception_in_log": 1,
        "missing_diagnostic": 1,
    },
    "packages/ai-gateway-core/src/ai_gateway_core/image/watermark.py": {
        "caught_exception_in_log": 1,
        "missing_diagnostic": 2,
    },
    "packages/ai-gateway-core/src/ai_gateway_core/knowledge/proxy_client.py": {
        "caught_exception_in_log": 5,
        "missing_diagnostic": 5,
    },
    "packages/ai-gateway-core/src/ai_gateway_core/logging/_core.py": {"missing_diagnostic": 2},
    "packages/ai-gateway-core/src/ai_gateway_core/logging/_exceptions.py": {
        "missing_diagnostic": 7
    },
    "packages/ai-gateway-core/src/ai_gateway_core/metrics/context_metrics.py": {
        "caught_exception_in_log": 1,
        "missing_diagnostic": 1,
    },
    "packages/ai-gateway-core/src/ai_gateway_core/metrics/realtime_metrics.py": {
        "caught_exception_in_log": 9,
        "missing_diagnostic": 10,
    },
    "packages/ai-gateway-core/src/ai_gateway_core/metrics/usage_recorder.py": {
        "caught_exception_in_log": 14,
        "missing_diagnostic": 16,
    },
    "packages/ai-gateway-core/src/ai_gateway_core/persistence/database.py": {
        "broad_suppress": 2,
        "caught_exception_in_log": 1,
    },
    "packages/ai-gateway-core/src/ai_gateway_core/persistence/redis.py": {
        "caught_exception_in_log": 1,
        "missing_diagnostic": 2,
    },
    "packages/ai-gateway-core/src/ai_gateway_core/persistence/repositories/api_key_repository.py": {
        "broad_suppress": 1
    },
    "packages/ai-gateway-core/src/ai_gateway_core/persistence/repositories/mcp_repository.py": {
        "missing_diagnostic": 1
    },
    "packages/ai-gateway-core/src/ai_gateway_core/proxy/base.py": {
        "broad_suppress": 1,
        "caught_exception_in_log": 2,
        "missing_diagnostic": 8,
    },
    "packages/ai-gateway-core/src/ai_gateway_core/proxy/drain.py": {"caught_exception_in_log": 1},
    "packages/ai-gateway-core/src/ai_gateway_core/proxy/sse_heartbeat.py": {
        "broad_suppress": 1,
        "missing_diagnostic": 1,
    },
    "packages/ai-gateway-core/src/ai_gateway_core/quiz/quiz_generator.py": {
        "caught_exception_in_log": 1
    },
    "packages/ai-gateway-core/src/ai_gateway_core/quiz/quiz_grader.py": {
        "caught_exception_in_log": 1,
        "missing_diagnostic": 1,
    },
    "packages/ai-gateway-core/src/ai_gateway_core/security/secrets.py": {
        "caught_exception_in_log": 3,
        "missing_diagnostic": 3,
    },
    "packages/ai-gateway-core/src/ai_gateway_core/skills/artifact_repository.py": {
        "missing_diagnostic": 1
    },
    "packages/ai-gateway-core/src/ai_gateway_core/storage/artifact_storage.py": {
        "caught_exception_in_log": 3,
        "missing_diagnostic": 5,
    },
    "packages/ai-gateway-core/src/ai_gateway_core/storage/image_storage.py": {
        "caught_exception_in_log": 8,
        "missing_diagnostic": 4,
    },
    "packages/ai-gateway-core/src/ai_gateway_core/tasks/task_manager.py": {
        "caught_exception_in_log": 1,
        "missing_diagnostic": 1,
    },
    "packages/ai-gateway-core/src/ai_gateway_core/tasks/task_types.py": {
        "caught_exception_in_log": 1,
        "missing_diagnostic": 1,
    },
    "packages/ai-gateway-core/src/ai_gateway_core/tracing/httpx_hooks.py": {
        "caught_exception_in_log": 1,
        "missing_diagnostic": 1,
    },
    "packages/ai-gateway-core/src/ai_gateway_core/tracing/init.py": {
        "caught_exception_in_log": 2,
        "missing_diagnostic": 4,
    },
    "packages/ai-gateway-core/src/ai_gateway_core/tracing/middleware.py": {"missing_diagnostic": 6},
}
_OBSERVED_AI_GATEWAY_CORE_DEBT: Counter[tuple[str, str]] = Counter()


def _whitelisted(relative: Path, category: str) -> bool:
    """Whether this specific violation stays within the pinned debt budget."""

    relative_path = str(relative)
    limits = _AI_GATEWAY_CORE_DEBT_LIMITS.get(relative_path)
    if limits is None or category not in limits:
        return False
    key = (relative_path, category)
    _OBSERVED_AI_GATEWAY_CORE_DEBT[key] += 1
    return _OBSERVED_AI_GATEWAY_CORE_DEBT[key] <= limits[category]


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
    _OBSERVED_AI_GATEWAY_CORE_DEBT.clear()
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
                    if not (
                        isinstance(keyword.value, ast.Constant)
                        and keyword.value.value in {False, None}
                    ) and not _whitelisted(relative, "raw_traceback"):
                        raw_traceback.append(f"{relative}:{node.lineno}:exc_info")

        for handler in (
            candidate for candidate in ast.walk(tree) if isinstance(candidate, ast.ExceptHandler)
        ):
            if handler.name:
                for statement in handler.body:
                    for candidate in ast.walk(statement):
                        if not isinstance(candidate, ast.Call) or not _logging_call(candidate):
                            continue
                        if any(
                            isinstance(descendant, ast.Name) and descendant.id == handler.name
                            for descendant in ast.walk(candidate)
                        ) and not _whitelisted(relative, "caught_exception_in_log"):
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
    ledger_drift = []
    for relative, limits in _AI_GATEWAY_CORE_DEBT_LIMITS.items():
        for category, expected in limits.items():
            observed = _OBSERVED_AI_GATEWAY_CORE_DEBT[(relative, category)]
            if observed != expected:
                ledger_drift.append(
                    f"{relative}:{category}: expected={expected}, observed={observed}"
                )
    if ledger_drift:
        failures.append(
            "exception-logging debt ledger changed; lower fixed counts or reject new debt:\n"
            + "\n".join(ledger_drift)
        )
    assert not failures, "\n\n".join(failures)


def test_exception_logging_debt_budget_rejects_same_category_growth() -> None:
    _OBSERVED_AI_GATEWAY_CORE_DEBT.clear()
    relative = Path("packages/ai-gateway-core/src/ai_gateway_core/agents/runtime.py")

    assert _whitelisted(relative, "missing_diagnostic") is True
    assert _whitelisted(relative, "missing_diagnostic") is False
