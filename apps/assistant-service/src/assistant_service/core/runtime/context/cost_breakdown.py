"""Context token cost attribution for observability."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

from ...rag.context_engine import (
    estimate_message_tokens,
    estimate_tokens,
    serialize_tools_deterministic,
)


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
        source_summaries: list[dict[str, Any] | str] | None = None,
        tool_result_summaries: list[dict[str, Any] | str] | None = None,
        artifact_summaries: list[dict[str, Any] | str] | None = None,
        compaction_summary: str | None = None,
        source_records: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        contributors: list[ContextContributor] = []

        contributors.append(
            ContextContributor(
                name="system_prompt",
                category="system",
                chars=len(system_prompt or ""),
                tokens=estimate_message_tokens({"role": "system", "content": system_prompt}),
                metadata={},
            )
        )

        for idx, message in enumerate(messages or []):
            role = str(message.get("role") or "unknown")
            # ``system_prompt`` is already an explicit transport contributor.
            # Counting the system message again inflated the model-bound total.
            if role == "system":
                continue
            contributors.append(self._make_message_item(idx, role, message))

        tools_text = serialize_tools_deterministic(tool_definitions or [])
        contributors.append(
            self._make_item(
                "tool_schema",
                "tools",
                tools_text,
                {"tool_count": len(tool_definitions or [])},
            )
        )

        if source_records is not None:
            for record in source_records:
                kind = str(record.get("kind") or "source")
                source_id = str(record.get("source_id") or "unknown")
                category = {
                    "file": "injected_files",
                    "skill": "skills",
                    "memory_snippet": "memory",
                    "source_summary": "source_summaries",
                    "tool_result": "tool_results",
                    "artifact": "artifacts",
                    "compaction_summary": "compaction",
                }.get(kind, kind)
                contributors.append(
                    self._make_item(
                        f"source:{kind}:{source_id}",
                        category,
                        str(record.get("content") or ""),
                        {
                            "attribution_only": True,
                            "transport_overlap": "embedded_in_messages",
                            "reduction_decision": str(
                                record.get("reduction_decision") or "included"
                            ),
                            "original_tokens": max(0, int(record.get("original_tokens") or 0)),
                        },
                    )
                )

        for file_item in [] if source_records is not None else (injected_files or []):
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
                        "attribution_only": True,
                    },
                )
            )

        for skill in [] if source_records is not None else (skills_metadata or []):
            label = str(skill.get("name") or "skill")
            text = str(skill.get("summary") or skill)
            contributors.append(
                self._make_item(
                    f"skill:{label}",
                    "skills",
                    text,
                    {"name": label, "attribution_only": True},
                )
            )

        for idx, snippet in enumerate(
            [] if source_records is not None else (memory_snippets or [])
        ):
            contributors.append(
                self._make_item(
                    f"memory_{idx}",
                    "memory",
                    snippet,
                    {"index": idx, "attribution_only": True},
                )
            )

        for idx, summary in enumerate(
            [] if source_records is not None else (source_summaries or [])
        ):
            contributors.append(
                self._make_item(
                    f"source_summary_{idx}",
                    "source_summaries",
                    self._summary_text(summary),
                    {"index": idx, "attribution_only": True},
                )
            )

        for idx, summary in enumerate(
            [] if source_records is not None else (tool_result_summaries or [])
        ):
            contributors.append(
                self._make_item(
                    f"tool_result_{idx}",
                    "tool_results",
                    self._summary_text(summary),
                    {"index": idx, "attribution_only": True},
                )
            )

        for idx, summary in enumerate(
            [] if source_records is not None else (artifact_summaries or [])
        ):
            contributors.append(
                self._make_item(
                    f"artifact_{idx}",
                    "artifacts",
                    self._summary_text(summary),
                    {"index": idx, "attribution_only": True},
                )
            )

        if compaction_summary and source_records is None:
            contributors.append(
                self._make_item(
                    "compaction_summary",
                    "compaction",
                    compaction_summary,
                    {"attribution_only": True},
                )
            )

        contributors = [
            contributor
            for contributor in contributors
            if contributor.tokens > 0 or contributor.metadata.get("reduction_decision")
        ]
        contributors.sort(key=lambda item: item.tokens, reverse=True)

        transport_contributors = [
            item for item in contributors if not item.metadata.get("attribution_only")
        ]
        total_tokens = sum(item.tokens for item in transport_contributors)
        total_chars = sum(item.chars for item in transport_contributors)

        by_category: dict[str, int] = {}
        for item in contributors:
            by_category[item.category] = by_category.get(item.category, 0) + item.tokens

        return {
            "total_tokens": total_tokens,
            "total_chars": total_chars,
            "attributed_tokens": sum(item.tokens for item in contributors),
            "attribution_policy": "transport_total_excludes_embedded_source_overlays",
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

    @staticmethod
    def _make_message_item(
        index: int,
        role: str,
        message: dict[str, Any],
    ) -> ContextContributor:
        content = message.get("content")
        chars = len(
            json.dumps(content, ensure_ascii=False, default=str)
            if isinstance(content, (dict, list))
            else str(content or "")
        )
        return ContextContributor(
            name=f"message_{index}_{role}",
            category="messages",
            chars=chars,
            tokens=estimate_message_tokens(message),
            metadata={"role": role},
        )

    @staticmethod
    def _summary_text(item: dict[str, Any] | str) -> str:
        if isinstance(item, str):
            return item
        for key in ("summary", "content", "title", "name"):
            value = item.get(key)
            if value:
                return str(value)
        return str(item)
