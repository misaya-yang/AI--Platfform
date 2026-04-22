"""Context token cost attribution for observability."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from ...rag.context_engine import estimate_tokens, serialize_tools_deterministic


@dataclass
class ContextContributor:
    """Single context contributor in cost accounting."""

    name: str
    category: str
    chars: int
    tokens: int
    metadata: dict[str, Any]


class ContextCostBreakdown:
    """Compute detailed context contributors and token totals."""

    def analyze(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tool_definitions: list[dict[str, Any]] | None = None,
        injected_files: list[dict[str, Any]] | None = None,
        skills_metadata: list[dict[str, Any]] | None = None,
        memory_snippets: list[str] | None = None,
    ) -> dict[str, Any]:
        contributors: list[ContextContributor] = []

        contributors.append(self._make_item("system_prompt", "system", system_prompt, {}))

        for idx, message in enumerate(messages or []):
            role = str(message.get("role") or "unknown")
            content = message.get("content")
            if isinstance(content, list):
                text = " ".join(
                    str(item.get("text") or "")
                    for item in content
                    if isinstance(item, dict) and item.get("type") == "text"
                )
            else:
                text = str(content or "")
            contributors.append(
                self._make_item(
                    name=f"message_{idx}_{role}",
                    category="messages",
                    text=text,
                    metadata={"role": role},
                )
            )

        tools_text = serialize_tools_deterministic(tool_definitions or [])
        contributors.append(
            self._make_item(
                "tool_schema",
                "tools",
                tools_text,
                {"tool_count": len(tool_definitions or [])},
            )
        )

        for file_item in injected_files or []:
            path = str(file_item.get("path") or "unknown")
            content = str(file_item.get("content") or "")
            contributors.append(
                self._make_item(
                    f"file:{path}",
                    "injected_files",
                    content,
                    {
                        "path": path,
                        "source_type": file_item.get("source_type") or "workspace",
                    },
                )
            )

        for skill in skills_metadata or []:
            label = str(skill.get("name") or "skill")
            text = str(skill.get("summary") or skill)
            contributors.append(
                self._make_item(
                    f"skill:{label}",
                    "skills",
                    text,
                    {"name": label},
                )
            )

        for idx, snippet in enumerate(memory_snippets or []):
            contributors.append(
                self._make_item(
                    f"memory_{idx}",
                    "memory",
                    snippet,
                    {"index": idx},
                )
            )

        contributors = [c for c in contributors if c.chars > 0]
        contributors.sort(key=lambda item: item.tokens, reverse=True)

        total_tokens = sum(item.tokens for item in contributors)
        total_chars = sum(item.chars for item in contributors)

        by_category: dict[str, int] = {}
        for item in contributors:
            by_category[item.category] = by_category.get(item.category, 0) + item.tokens

        return {
            "total_tokens": total_tokens,
            "total_chars": total_chars,
            "contributors": [asdict(item) for item in contributors],
            "tokens_by_category": by_category,
        }

    @staticmethod
    def _make_item(
        name: str,
        category: str,
        text: str,
        metadata: dict[str, Any],
    ) -> ContextContributor:
        chars = len(text or "")
        tokens = estimate_tokens(text)
        return ContextContributor(
            name=name,
            category=category,
            chars=chars,
            tokens=tokens,
            metadata=metadata,
        )
