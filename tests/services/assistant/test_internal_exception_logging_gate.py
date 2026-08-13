"""Source gate for diagnosable, non-leaking Assistant internal failures."""

from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SOURCE_ROOT = _REPO_ROOT / "apps" / "assistant-service" / "src" / "assistant_service"
_SHARED_RUNTIME_SOURCES = (
    _REPO_ROOT
    / "packages"
    / "ai-gateway-core"
    / "src"
    / "ai_gateway_core"
    / "memory"
    / "service.py",
    _REPO_ROOT
    / "packages"
    / "ai-gateway-core"
    / "src"
    / "ai_gateway_core"
    / "skills"
    / "executor.py",
)


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
    return [*sources, *_SHARED_RUNTIME_SOURCES]


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
                    if _is_broad_suppress(item.context_expr):
                        broad_suppress.append(f"{relative}:{node.lineno}")

            if isinstance(node, ast.Call) and _logging_call(node):
                if node.args and "exception_type" in ast.unparse(node.args[0]):
                    type_only_diagnostic.append(f"{relative}:{node.lineno}")
                if (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "exception"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in {"logger", "log"}
                ):
                    raw_traceback.append(f"{relative}:{node.lineno}:logger.exception")
                for keyword in node.keywords:
                    if keyword.arg != "exc_info":
                        continue
                    if not (
                        isinstance(keyword.value, ast.Constant)
                        and keyword.value.value in {False, None}
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
                        if any(
                            isinstance(descendant, ast.Name) and descendant.id == handler.name
                            for descendant in ast.walk(candidate)
                        ):
                            caught_exception_in_log.append(f"{relative}:{candidate.lineno}")

            if not _catches_broad_exception(handler.type):
                continue
            if not _calls_safe_helper(handler.body) and not _directly_reraises(handler.body):
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
