"""Architecture guard against model and prompt hardcoding in hot paths."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
HOT_PATHS = (
    ROOT
    / "apps/assistant-service/src/assistant_service/core/models/capability_adapters.py",
    ROOT / "apps/assistant-service/src/assistant_service/core/models/thinking_policy.py",
    ROOT / "apps/assistant-service/src/assistant_service/core/models/model_registry.py",
    ROOT / "apps/assistant-service/src/assistant_service/core/tools/tool_selector.py",
)
MODEL_PREFIXES = ("qwen", "gpt-", "claude-", "gemini-", "deepseek-", "grok-")


def test_hot_paths_do_not_branch_on_model_identifiers() -> None:
    violations: list[str] = []
    for path in HOT_PATHS:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            normalized = node.value.strip().lower()
            if normalized.startswith(MODEL_PREFIXES):
                violations.append(f"{path.name}:{node.lineno}:{node.value}")
    assert violations == []


def test_tool_selector_does_not_inspect_user_prompt() -> None:
    source = HOT_PATHS[-1].read_text(encoding="utf-8")
    assert "_TOOL_KEYWORDS" not in source
    assert "relevance_keywords" not in source
    assert "message_lower" not in source
