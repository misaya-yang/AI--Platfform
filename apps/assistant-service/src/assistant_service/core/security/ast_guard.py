"""Hermes-inspired AST Security Scanner & Code Execution Guardrails.

Inspects generated or user-provided code artifacts using Python's `ast` parser
to block high-risk operations (forbidden modules, dangerous builtins, arbitrary
file modifications, and subprocess forks) prior to sandbox execution.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SecurityViolation:
    rule_id: str
    severity: str  # "CRITICAL", "HIGH", "MEDIUM"
    message: str
    line_number: int | None = None
    node_type: str | None = None


@dataclass(frozen=True)
class ASTSafetyReport:
    is_safe: bool
    violations: list[SecurityViolation] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_safe": self.is_safe,
            "violations": [
                {
                    "rule_id": v.rule_id,
                    "severity": v.severity,
                    "message": v.message,
                    "line_number": v.line_number,
                    "node_type": v.node_type,
                }
                for v in self.violations
            ],
            "metadata": self.metadata,
        }


# High-risk modules forbidden in general agent execution sandboxes
FORBIDDEN_MODULES = frozenset({
    "subprocess",
    "socket",
    "pty",
    "posix",
    "ctypes",
    "_thread",
    "threading",
    "multiprocessing",
    "winreg",
    "signal",
})

# Dangerous built-in function names
DANGEROUS_BUILTINS = frozenset({
    "eval",
    "exec",
    "compile",
    "__import__",
    "breakpoint",
})

# Dangerous os / sys function names
DANGEROUS_OS_CALLS = frozenset({
    "system",
    "popen",
    "spawn",
    "fork",
    "kill",
    "chmod",
    "chown",
    "rmdir",
    "unlink",
    "remove",
})

# Dangerous reflection / dunder attributes
DANGEROUS_ATTRIBUTES = frozenset({
    "__subclasses__",
    "__globals__",
    "__code__",
    "__bases__",
    "__mro__",
    "__builtins__",
    "__import__",
})


class CodeSecurityVisitor(ast.NodeVisitor):
    """Traverses Python AST to flag dangerous imports, calls, and attribute accesses."""

    def __init__(self) -> None:
        self.violations: list[SecurityViolation] = []

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            base_module = alias.name.split(".")[0]
            if base_module in FORBIDDEN_MODULES:
                self.violations.append(
                    SecurityViolation(
                        rule_id="SEC_FORBIDDEN_MODULE",
                        severity="CRITICAL",
                        message=f"Import of forbidden module '{alias.name}' is blocked",
                        line_number=node.lineno,
                        node_type="Import",
                    )
                )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module:
            base_module = node.module.split(".")[0]
            if base_module in FORBIDDEN_MODULES:
                self.violations.append(
                    SecurityViolation(
                        rule_id="SEC_FORBIDDEN_MODULE_FROM",
                        severity="CRITICAL",
                        message=f"Import from forbidden module '{node.module}' is blocked",
                        line_number=node.lineno,
                        node_type="ImportFrom",
                    )
                )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        # Check direct builtin calls (e.g. eval(), exec())
        if isinstance(node.func, ast.Name):
            if node.func.id in DANGEROUS_BUILTINS:
                self.violations.append(
                    SecurityViolation(
                        rule_id="SEC_DANGEROUS_BUILTIN",
                        severity="CRITICAL",
                        message=f"Call to dangerous builtin '{node.func.id}()' is blocked",
                        line_number=node.lineno,
                        node_type="Call",
                    )
                )

        # Check attribute calls (e.g. os.system(), shutil.rmtree())
        elif isinstance(node.func, ast.Attribute):
            attr_name = node.func.attr
            if attr_name in DANGEROUS_OS_CALLS:
                self.violations.append(
                    SecurityViolation(
                        rule_id="SEC_DANGEROUS_OS_CALL",
                        severity="HIGH",
                        message=f"Potentially destructive system call '.{attr_name}()' detected",
                        line_number=node.lineno,
                        node_type="AttributeCall",
                    )
                )
            elif attr_name in ("rmtree", "move") and isinstance(node.func.value, ast.Name) and node.func.value.id == "shutil":
                self.violations.append(
                    SecurityViolation(
                        rule_id="SEC_DESTRUCTIVE_SHUTIL",
                        severity="HIGH",
                        message=f"Destructive file operation 'shutil.{attr_name}' detected",
                        line_number=node.lineno,
                        node_type="AttributeCall",
                    )
                )

        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in DANGEROUS_ATTRIBUTES:
            self.violations.append(
                SecurityViolation(
                    rule_id="SEC_DUNDER_TRAVERSAL",
                    severity="CRITICAL",
                    message=f"Access to dangerous reflection attribute '{node.attr}' is blocked",
                    line_number=node.lineno,
                    node_type="Attribute",
                )
            )
        self.generic_visit(node)


class ASTSecurityGuard:
    """Static security analyzer for agent-generated code snippets."""

    @classmethod
    def audit_python_code(cls, code: str) -> ASTSafetyReport:
        if not code or not code.strip():
            return ASTSafetyReport(is_safe=True)

        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            return ASTSafetyReport(
                is_safe=False,
                violations=[
                    SecurityViolation(
                        rule_id="SEC_SYNTAX_ERROR",
                        severity="MEDIUM",
                        message=f"Code fails AST parsing: {e.msg}",
                        line_number=e.lineno,
                        node_type="SyntaxError",
                    )
                ],
                metadata={"parse_error": True},
            )

        visitor = CodeSecurityVisitor()
        visitor.visit(tree)

        is_safe = len(visitor.violations) == 0
        return ASTSafetyReport(
            is_safe=is_safe,
            violations=visitor.violations,
            metadata={"nodes_checked": len(tree.body)},
        )
