"""Context assembler V2 with deterministic cost attribution."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ...rag.context_engine import (
    CONTEXT_PACKET_ORDER,
    ContextBudgetManager,
    ContextEngine,
    ContextStructure,
    _trim_history_preserving_tool_pairs,
    estimate_message_tokens,
    estimate_tokens,
    serialize_tools_deterministic,
)
from .cost_breakdown import ContextCostBreakdown
from .external_content import ExternalContent

CONTEXT_PACKET_SCHEMA_VERSION = "assistant-context-packet/v1"
PROTECTED_CONTEXT_COMPONENTS = (
    "stable_system_policy",
    "current_request",
    "complete_tool_exchanges",
    "effective_capability_snapshot",
)
UNTRUSTED_CONTEXT_PREAMBLE = "## External context"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _canonical_model_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Return the exact deterministic tool list used for cache and transport."""

    serialized = serialize_tools_deterministic(copy.deepcopy(tools or []))
    return json.loads(serialized) if serialized else []


def _boundary_safe_json(value: Any) -> str:
    """Serialize untrusted text without allowing it to close prompt boundaries."""

    return (
        _canonical_json(value)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()[:24]


class ContextPacketOverflowError(ValueError):
    """Protected model input cannot fit the selected model context window."""

    def __init__(self, *, model_context_window: int, overflow_tokens: int) -> None:
        self.model_context_window = int(model_context_window)
        self.overflow_tokens = int(overflow_tokens)
        super().__init__(
            "protected_context_exceeds_model_window: "
            f"window={self.model_context_window} overflow_tokens={self.overflow_tokens}"
        )


class ContextPacketIntegrityError(ValueError):
    """Model-bound messages violate the packet's complete tool-pair contract."""


@dataclass(frozen=True)
class ContextPacket:
    """Immutable model-bound packet with prompt-free inspection receipts."""

    packet_id: str
    assembly_plan_id: str
    cache_key: str
    provider: str
    _messages_json: str = field(repr=False)
    _tools_json: str = field(repr=False)
    _budget_json: str = field(repr=False)
    _detail_json: str = field(repr=False)
    _provenance_json: str = field(repr=False)
    _cache_json: str = field(repr=False)
    _model_context_window: int = field(repr=False)
    _reserved_output_tokens: int = field(repr=False)
    _protected_start_index: int = field(repr=False)
    # SPO-03 / A5: fingerprint of the last bound inputs (system + protected
    # suffix + effective tools + cache dimensions). An identical incoming
    # boundary can skip the whole rebind.
    _boundary_fingerprint: str | None = field(repr=False, default=None)

    @property
    def schema_version(self) -> str:
        return CONTEXT_PACKET_SCHEMA_VERSION

    def materialize_messages(self) -> list[dict[str, Any]]:
        return json.loads(self._messages_json)

    def materialize_tools(self) -> list[dict[str, Any]]:
        return json.loads(self._tools_json)

    @property
    def budget_event(self) -> dict[str, Any]:
        return json.loads(self._budget_json)

    @property
    def cost_detail(self) -> dict[str, Any]:
        return json.loads(self._detail_json)

    @property
    def cache_contract(self) -> dict[str, Any]:
        return json.loads(self._cache_json)

    @property
    def model_context_window(self) -> int:
        return self._model_context_window

    @property
    def reserved_output_tokens(self) -> int:
        return self._reserved_output_tokens

    @property
    def protected_start_index(self) -> int:
        return self._protected_start_index

    def receipt(self) -> dict[str, Any]:
        """Return bounded evidence without prompt, memory, file, or source text."""

        messages = self.materialize_messages()
        tools = self.materialize_tools()
        detail = self.cost_detail
        return {
            "schema_version": self.schema_version,
            "packet_id": self.packet_id,
            "assembly_plan_id": self.assembly_plan_id,
            "provider": self.provider,
            "message_count": len(messages),
            "tool_count": len(tools),
            "model_boundary": {
                "model_context_window": self.model_context_window,
                "reserved_output_tokens": self.reserved_output_tokens,
                "protected_start_index": self.protected_start_index,
            },
            "protected_components": list(PROTECTED_CONTEXT_COMPONENTS),
            "budget": self.budget_event,
            "cost": {
                "total_tokens": int(detail.get("total_tokens") or 0),
                "tokens_by_category": dict(detail.get("tokens_by_category") or {}),
                "attribution_policy": str(detail.get("attribution_policy") or ""),
            },
            "provenance": json.loads(self._provenance_json),
            "cache": self.cache_contract,
        }


class ContextAssemblerV2:
    """Compose messages and detailed cost output for model calls."""

    def __init__(
        self,
        *,
        provider: str,
        budget_manager: ContextBudgetManager | None = None,
        context_engine: ContextEngine | None = None,
        cost_breakdown: ContextCostBreakdown | None = None,
    ) -> None:
        self.provider = provider
        self.budget_manager = budget_manager or ContextBudgetManager()
        self.context_engine = context_engine or ContextEngine(provider=provider)
        self.cost_breakdown = cost_breakdown or ContextCostBreakdown()
        # SPO-03 / A5: per-message token estimates are pure functions of the
        # message dict; reuse them across boundary rebinds instead of paying
        # the CJK-aware estimate for the whole history on every model turn.
        self._message_token_cache: dict[str, int] = {}
        self._MESSAGE_TOKEN_CACHE_MAX = 512

    def _cached_message_tokens(self, message: dict[str, Any]) -> int:
        key = _canonical_json(message)
        cached = self._message_token_cache.get(key)
        if cached is not None:
            return cached
        estimated = estimate_message_tokens(message)
        if len(self._message_token_cache) >= self._MESSAGE_TOKEN_CACHE_MAX:
            self._message_token_cache.clear()
        self._message_token_cache[key] = estimated
        return estimated

    def boundary_fingerprint(
        self,
        *,
        packet: ContextPacket,
        messages: list[dict[str, Any]],
        tool_definitions: list[dict[str, Any]],
        cache_dimensions: Mapping[str, Any] | None = None,
        previous_cache_receipt: Mapping[str, Any] | None = None,
    ) -> str:
        """Fingerprint the inputs that fully determine a rebind outcome.

        ``previous_cache_receipt`` is accepted for call-site compatibility
        but is not part of the digest: it is an *output* of the last bind,
        so including it makes the next turn's incoming digest never match.
        """
        del previous_cache_receipt
        system = str(messages[0].get("content") or "") if messages else ""
        suffix = messages[packet.protected_start_index :]
        return _digest(
            {
                "system": system,
                "suffix": suffix,
                "tools": _canonical_model_tools(tool_definitions),
                "cache_dimensions": dict(cache_dimensions or {}),
            }
        )

    def build(
        self,
        *,
        context: ContextStructure,
        model_context_window: int,
        tool_definitions: list[dict[str, Any]] | None = None,
        injected_files: list[dict[str, Any]] | None = None,
        skills_metadata: list[dict[str, Any]] | None = None,
        memory_snippets: list[str] | None = None,
        source_summaries: list[dict[str, Any] | str] | None = None,
        tool_result_summaries: list[dict[str, Any] | str] | None = None,
        artifact_summaries: list[dict[str, Any] | str] | None = None,
        compaction_summary: str | None = None,
        provenance: list[dict[str, Any]] | None = None,
        cache_dimensions: Mapping[str, Any] | None = None,
        previous_cache_receipt: Mapping[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
        """Build model messages, budget plan, and context cost detail."""
        packet = self.build_packet(
            context=context,
            model_context_window=model_context_window,
            tool_definitions=tool_definitions,
            injected_files=injected_files,
            skills_metadata=skills_metadata,
            memory_snippets=memory_snippets,
            source_summaries=source_summaries,
            tool_result_summaries=tool_result_summaries,
            artifact_summaries=artifact_summaries,
            compaction_summary=compaction_summary,
            provenance=provenance,
            cache_dimensions=cache_dimensions,
            previous_cache_receipt=previous_cache_receipt,
        )
        return packet.materialize_messages(), packet.budget_event, packet.cost_detail

    def build_packet(
        self,
        *,
        context: ContextStructure,
        model_context_window: int,
        tool_definitions: list[dict[str, Any]] | None = None,
        injected_files: list[dict[str, Any]] | None = None,
        skills_metadata: list[dict[str, Any]] | None = None,
        memory_snippets: list[str] | None = None,
        source_summaries: list[dict[str, Any] | str] | None = None,
        tool_result_summaries: list[dict[str, Any] | str] | None = None,
        artifact_summaries: list[dict[str, Any] | str] | None = None,
        compaction_summary: str | None = None,
        provenance: list[dict[str, Any]] | None = None,
        cache_dimensions: Mapping[str, Any] | None = None,
        previous_cache_receipt: Mapping[str, Any] | None = None,
    ) -> ContextPacket:
        """Compile one immutable packet shared by streaming and buffered calls."""

        effective_tools = _canonical_model_tools(
            context.tool_definitions if tool_definitions is None else tool_definitions
        )
        lower_priority_context, source_records = self._compose_request_context(
            current_context=context.current_context,
            user_preferences=context.user_preferences,
            long_term_memory=context.long_term_memory,
            task_state=context.task_state,
            injected_files=injected_files,
            skills_metadata=skills_metadata,
            memory_snippets=memory_snippets,
            source_summaries=source_summaries,
            tool_result_summaries=tool_result_summaries,
            artifact_summaries=artifact_summaries,
            compaction_summary=compaction_summary,
        )
        working = ContextStructure(
            system_prompt=str(context.system_prompt or ""),
            tool_definitions=effective_tools,
            # User-controlled and retrieved values stay out of the privileged
            # system prefix; they are rendered in lower_priority_context.
            user_preferences=None,
            long_term_memory=None,
            task_state=None,
            conversation_history=copy.deepcopy(context.conversation_history or []),
            current_context=lower_priority_context,
            current_query=str(context.current_query or ""),
            current_images=list(context.current_images or []),
        )
        plan = self.budget_manager.create_plan(
            context=working,
            model_context_window=model_context_window,
        )
        if plan.budget_status == "protected_overflow":
            raise ContextPacketOverflowError(
                model_context_window=plan.model_context_window,
                overflow_tokens=plan.protected_overflow_tokens,
            )
        working.conversation_history = copy.deepcopy(plan.trimmed_history)
        original_source_records = copy.deepcopy(source_records)
        available_tokens = max(
            1,
            plan.model_context_window - plan.reserved_output_tokens,
        )
        source_free = copy.deepcopy(working)
        source_free.current_context = None
        source_free_messages = self.context_engine.build_messages(source_free)
        protected_transport_tokens = sum(
            estimate_message_tokens(message) for message in source_free_messages
        ) + estimate_tokens(serialize_tools_deterministic(effective_tools))
        if protected_transport_tokens > available_tokens:
            raise ContextPacketOverflowError(
                model_context_window=plan.model_context_window,
                overflow_tokens=protected_transport_tokens - available_tokens,
            )
        source_budget = max(0, available_tokens - protected_transport_tokens)
        working.current_context, source_records = self._reduce_source_records(
            original_source_records,
            max_tokens=source_budget,
        )

        messages = self.context_engine.build_messages(working)
        actual_transport_tokens = sum(
            estimate_message_tokens(message) for message in messages
        ) + estimate_tokens(serialize_tools_deterministic(effective_tools))
        while actual_transport_tokens > available_tokens and source_budget > 0:
            source_budget = max(
                0,
                source_budget - (actual_transport_tokens - available_tokens) - 8,
            )
            working.current_context, source_records = self._reduce_source_records(
                original_source_records,
                max_tokens=source_budget,
            )
            messages = self.context_engine.build_messages(working)
            actual_transport_tokens = sum(
                estimate_message_tokens(message) for message in messages
            ) + estimate_tokens(serialize_tools_deterministic(effective_tools))
        if actual_transport_tokens > available_tokens:
            raise ContextPacketOverflowError(
                model_context_window=plan.model_context_window,
                overflow_tokens=actual_transport_tokens - available_tokens,
            )
        source_reduction_decisions = {
            "pruned_budget",
            "truncated_budget",
            "pruned_source_limit",
            "truncated_source_limit",
        }
        reduced_source_records = [
            record
            for record in source_records
            if record.get("reduction_decision") in source_reduction_decisions
        ]
        plan.dropped_request_context_chars = sum(
            max(
                0,
                int(record.get("original_chars") or 0) - int(record.get("included_chars") or 0),
            )
            for record in reduced_source_records
        )
        plan.trimmed_current_context = working.current_context
        compaction_reasons: list[str] = []
        if plan.dropped_history_messages or plan.dropped_invalid_tool_messages:
            compaction_reasons.append("history exceeded budget or was invalid")
        if plan.dropped_request_context_chars:
            if any(
                record.get("reduction_decision") in {"pruned_budget", "truncated_budget"}
                for record in reduced_source_records
            ):
                compaction_reasons.append("lower-priority request context exceeded budget")
            else:
                compaction_reasons.append("lower-priority sources exceeded source limits")
        plan.compacted = bool(compaction_reasons)
        plan.compaction_reason = "; ".join(compaction_reasons) or None
        plan.budget_status = "compacted" if plan.compacted else "within_budget"
        plan.used_tokens["request"] = estimate_message_tokens(messages[-1])
        detail = self.cost_breakdown.analyze(
            system_prompt=working.system_prompt,
            messages=messages,
            tool_definitions=effective_tools,
            injected_files=injected_files,
            skills_metadata=skills_metadata,
            memory_snippets=memory_snippets,
            source_summaries=source_summaries,
            tool_result_summaries=tool_result_summaries,
            artifact_summaries=artifact_summaries,
            compaction_summary=compaction_summary,
            source_records=source_records,
        )

        budget_event = plan.to_budget_event()
        provenance_receipt = self._provenance_receipt(
            context=context,
            effective_tools=effective_tools,
            supplied=provenance,
            source_records=source_records,
            history=context.conversation_history,
            trimmed_history=plan.trimmed_history,
        )
        cache_contract = self._cache_contract(
            system_prompt=working.system_prompt,
            effective_tools=effective_tools,
            cache_dimensions=cache_dimensions,
            previous_cache_receipt=previous_cache_receipt,
        )
        messages_json = _canonical_json(messages)
        tools_json = _canonical_json(effective_tools)
        assembly_plan_id = f"cap_{_digest({'budget': budget_event, 'order': CONTEXT_PACKET_ORDER})}"
        packet_id = "ctxp_" + _digest(
            {"messages": messages, "tools": effective_tools, "plan": assembly_plan_id}
        )
        return ContextPacket(
            packet_id=packet_id,
            assembly_plan_id=assembly_plan_id,
            cache_key=str(cache_contract["cache_key"]),
            provider=self.provider,
            _messages_json=messages_json,
            _tools_json=tools_json,
            _budget_json=_canonical_json(budget_event),
            _detail_json=_canonical_json(detail),
            _provenance_json=_canonical_json(provenance_receipt),
            _cache_json=_canonical_json(cache_contract),
            _model_context_window=plan.model_context_window,
            _reserved_output_tokens=plan.reserved_output_tokens,
            _protected_start_index=max(1, len(messages) - 1),
        )

    def bind_model_boundary(
        self,
        *,
        packet: ContextPacket,
        messages: list[dict[str, Any]],
        tool_definitions: list[dict[str, Any]],
        trusted_system_prompt: str | None = None,
        cache_dimensions: Mapping[str, Any] | None = None,
        previous_cache_receipt: Mapping[str, Any] | None = None,
    ) -> ContextPacket:
        """Rebind an active turn after late tool-policy mutations.

        Old history may be reduced only as complete units. The current request
        and every active-turn tool exchange remain an immutable suffix.
        """

        boundary_messages = copy.deepcopy(messages)
        if not boundary_messages or boundary_messages[0].get("role") != "system":
            raise ContextPacketIntegrityError("context packet requires one leading system message")
        effective_tools = _canonical_model_tools(tool_definitions)
        original_messages = packet.materialize_messages()
        original_suffix = original_messages[packet.protected_start_index :]
        boundary_body = boundary_messages[1:]
        suffix_size = len(original_suffix)
        matching_starts = [
            index
            for index in range(0, len(boundary_body) - suffix_size + 1)
            if _canonical_json(boundary_body[index : index + suffix_size])
            == _canonical_json(original_suffix)
        ]
        if not matching_starts:
            raise ContextPacketIntegrityError(
                "active turn replaced the packet's protected current request or tool exchange"
            )
        protected_start = 1 + matching_starts[-1]
        old_history = boundary_messages[1:protected_start]
        protected_suffix = boundary_messages[protected_start:]
        validated_suffix, _, invalid_suffix = _trim_history_preserving_tool_pairs(
            protected_suffix,
            max_tokens=10**9,
            min_recent_messages=0,
        )
        if invalid_suffix or _canonical_json(validated_suffix) != _canonical_json(protected_suffix):
            raise ContextPacketIntegrityError(
                "active turn contains an orphan or incomplete tool exchange"
            )

        original_system = str(original_messages[0].get("content") or "")
        boundary_system = str(boundary_messages[0].get("content") or "")
        if boundary_system != original_system and boundary_system != trusted_system_prompt:
            raise ContextPacketIntegrityError(
                "stable system policy changed outside the trusted prompt compiler"
            )
        available = max(
            1,
            packet.model_context_window - packet.reserved_output_tokens,
        )

        # SPO-03 / A5: reuse the per-message token cache across rebinds.
        message_tokens = self._cached_message_tokens

        protected_tokens = (
            message_tokens(boundary_messages[0])
            + sum(message_tokens(message) for message in protected_suffix)
            + estimate_tokens(serialize_tools_deterministic(effective_tools))
        )
        if protected_tokens > available:
            raise ContextPacketOverflowError(
                model_context_window=packet.model_context_window,
                overflow_tokens=protected_tokens - available,
            )
        trimmed_old_history, dropped, invalid_old = _trim_history_preserving_tool_pairs(
            old_history,
            max_tokens=available - protected_tokens,
            min_recent_messages=0,
        )
        rebound_messages = [
            boundary_messages[0],
            *trimmed_old_history,
            *protected_suffix,
        ]
        final_transport_tokens = sum(
            message_tokens(item) for item in rebound_messages
        ) + estimate_tokens(serialize_tools_deterministic(effective_tools))
        if final_transport_tokens > available:
            raise ContextPacketOverflowError(
                model_context_window=packet.model_context_window,
                overflow_tokens=final_transport_tokens - available,
            )

        budget_event = packet.budget_event
        compaction = dict(budget_event.get("compaction") or {})
        compaction["dropped_history_messages"] = (
            int(compaction.get("dropped_history_messages") or 0) + dropped
        )
        compaction["dropped_invalid_tool_messages"] = (
            int(compaction.get("dropped_invalid_tool_messages") or 0) + invalid_old
        )
        compaction["remaining_history_messages"] = len(trimmed_old_history)
        budget_event["compaction"] = compaction
        if dropped or invalid_old:
            budget_event["compacted"] = True
            budget_event["budget_status"] = "compacted"
        budget_event["model_boundary"] = {
            "protected_suffix_messages": len(protected_suffix),
            "effective_tool_count": len(effective_tools),
            "estimated_input_tokens": final_transport_tokens,
        }

        system_prompt = str(rebound_messages[0].get("content") or "")
        cache_contract = self._cache_contract(
            system_prompt=system_prompt,
            effective_tools=effective_tools,
            cache_dimensions=cache_dimensions,
            previous_cache_receipt=previous_cache_receipt,
        )
        provenance = json.loads(packet._provenance_json)
        for item in provenance:
            if item.get("kind") == "system_policy":
                item["digest"] = _digest(system_prompt)
                item["size_chars"] = len(system_prompt)
                item["size_tokens"] = estimate_tokens(system_prompt)
            elif item.get("kind") == "effective_capabilities":
                item["digest"] = _digest(effective_tools)
                item["count"] = len(effective_tools)
                item["size_chars"] = len(_canonical_json(effective_tools))
                item["size_tokens"] = estimate_tokens(
                    serialize_tools_deterministic(effective_tools)
                )
            elif item.get("kind") == "conversation_history":
                if dropped or invalid_old:
                    item["reduction_decision"] = "pruned_complete_units"
                item["included_count"] = len(trimmed_old_history)
                item["digest"] = _digest(trimmed_old_history)
                item["size_chars"] = len(_canonical_json(trimmed_old_history))
                item["size_tokens"] = estimate_tokens(_canonical_json(trimmed_old_history))

        active_suffix_receipt = {
            "kind": "active_turn_suffix",
            "role": "conversation",
            "scope": "request",
            "trust": "untrusted",
            "digest": _digest(protected_suffix),
            "freshness": "current",
            "size_chars": len(_canonical_json(protected_suffix)),
            "size_tokens": sum(message_tokens(item) for item in protected_suffix),
            "cacheability": "none",
            "owner": "runtime",
            "conflict_policy": "current_request_wins",
            "reduction_decision": "protected",
            "included_count": len(protected_suffix),
        }
        provenance = [item for item in provenance if item.get("kind") != "active_turn_suffix"]
        provenance.append(active_suffix_receipt)

        detail = self.cost_breakdown.analyze(
            system_prompt=system_prompt,
            messages=rebound_messages,
            tool_definitions=effective_tools,
        )
        # Boundary rebinding recomputes transport truth after late tool-policy
        # changes. Preserve source-level attribution overlays from the initial
        # compile so operators can still explain which file/memory/RAG/tool
        # contributors shaped the model input without double-counting them in
        # ``total_tokens``.
        attribution_overlays = [
            copy.deepcopy(contributor)
            for contributor in packet.cost_detail.get("contributors", [])
            if isinstance(contributor, dict)
            and isinstance(contributor.get("metadata"), dict)
            and contributor["metadata"].get("attribution_only")
        ]
        if attribution_overlays:
            detail["contributors"].extend(attribution_overlays)
            detail["contributors"].sort(
                key=lambda item: int(item.get("tokens") or 0),
                reverse=True,
            )
            attributed_tokens = int(detail.get("total_tokens") or 0)
            for contributor in attribution_overlays:
                category = str(contributor.get("category") or "other")
                tokens = max(0, int(contributor.get("tokens") or 0))
                detail["tokens_by_category"][category] = (
                    int(detail["tokens_by_category"].get(category) or 0) + tokens
                )
                attributed_tokens += tokens
            detail["attributed_tokens"] = attributed_tokens
        assembly_plan_id = f"cap_{_digest({'budget': budget_event, 'order': CONTEXT_PACKET_ORDER})}"
        packet_id = "ctxp_" + _digest(
            {
                "messages": rebound_messages,
                "tools": effective_tools,
                "plan": assembly_plan_id,
            }
        )
        return ContextPacket(
            packet_id=packet_id,
            assembly_plan_id=assembly_plan_id,
            cache_key=str(cache_contract["cache_key"]),
            provider=self.provider,
            _messages_json=_canonical_json(rebound_messages),
            _tools_json=_canonical_json(effective_tools),
            _budget_json=_canonical_json(budget_event),
            _detail_json=_canonical_json(detail),
            _provenance_json=_canonical_json(provenance),
            _cache_json=_canonical_json(cache_contract),
            _model_context_window=packet.model_context_window,
            _reserved_output_tokens=packet.reserved_output_tokens,
            _protected_start_index=1 + len(trimmed_old_history),
            _boundary_fingerprint=_digest(
                {
                    "system": system_prompt,
                    "suffix": protected_suffix,
                    "tools": effective_tools,
                    "cache_dimensions": dict(cache_dimensions or {}),
                }
            ),
        )

    @classmethod
    def _compose_request_context(
        cls,
        *,
        current_context: str | None,
        user_preferences: str | None,
        long_term_memory: str | None,
        task_state: str | None,
        injected_files: list[dict[str, Any]] | None,
        skills_metadata: list[dict[str, Any]] | None,
        memory_snippets: list[str] | None,
        source_summaries: list[dict[str, Any] | str] | None,
        tool_result_summaries: list[dict[str, Any] | str] | None,
        artifact_summaries: list[dict[str, Any] | str] | None,
        compaction_summary: str | None,
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """Build escaped, source-addressable lower-priority context."""

        records: list[dict[str, Any]] = []

        def add(
            *,
            kind: str,
            heading: str,
            value: Any,
            role: str = "data",
            scope: str = "request",
            freshness: str = "request_time",
            owner: str = "request",
            cacheability: str = "dynamic",
            conflict_policy: str = "current_request_wins",
            max_chars: int = 2000,
            visible: bool = True,
            included_by_source_limit: bool = True,
        ) -> None:
            external = ExternalContent(
                content=str(value or ""),
                source=kind,
                scope=scope,
            ).normalized()
            text = external.content.strip()
            if not text:
                return
            bounded = text[:max_chars]
            decision = "included" if len(bounded) == len(text) else "truncated_source_limit"
            if not included_by_source_limit:
                bounded = ""
                decision = "pruned_source_limit"
            elif not visible:
                bounded = ""
                decision = "externalized"
            source_id = f"src_{_digest({'kind': kind, 'index': len(records), 'text': text})[:16]}"
            records.append(
                {
                    "source_id": source_id,
                    "kind": kind,
                    "heading": heading,
                    "role": role,
                    "scope": scope,
                    "trust": "untrusted",
                    "external_content_schema": "assistant-external-content/v1",
                    "freshness": freshness,
                    "owner": owner,
                    "cacheability": cacheability,
                    "conflict_policy": conflict_policy,
                    "digest": _digest(text),
                    "original_chars": len(text),
                    "original_tokens": estimate_tokens(text),
                    "included_chars": len(bounded),
                    "included_tokens": estimate_tokens(bounded),
                    "reduction_decision": decision,
                    "content": bounded,
                }
            )

        add(
            kind="user_preferences",
            heading="User Preferences",
            value=user_preferences,
            freshness="saved",
            owner="memory",
            conflict_policy="conversation_history_over_saved_preferences",
        )
        add(
            kind="task_state",
            heading="Current Task State",
            value=task_state,
            freshness="derived",
            owner="runtime",
            conflict_policy="current_request_over_stale_state",
        )

        for index, skill in enumerate(skills_metadata or []):
            add(
                kind="skill",
                heading="Selected Skill",
                value=cls._summary_item_text(skill),
                freshness=str(skill.get("version") or "selected"),
                owner="skill_registry",
                max_chars=2400,
                included_by_source_limit=index < 5,
            )
        for index, snippet in enumerate(memory_snippets or []):
            add(
                kind="memory_snippet",
                heading="Historical Conversation Memory",
                value=snippet,
                freshness="historical",
                owner="memory",
                conflict_policy="conversation_history_over_historical_memory",
                max_chars=600,
                included_by_source_limit=index < 6,
            )

        # Structured key/value memory is an up-to-date projection, while
        # semantic snippets come from historical conversation logs and can
        # contain superseded values. Place the current projection after those
        # snippets so the model receives one deterministic freshness order.
        add(
            kind="long_term_memory",
            heading="Current Structured User Memory",
            value=long_term_memory,
            freshness="current",
            owner="memory",
            conflict_policy="conversation_then_structured_then_historical",
        )

        add(
            kind="request_context",
            heading="Request Context",
            value=current_context,
            owner="retrieval",
            max_chars=12000,
        )
        for index, file_item in enumerate(injected_files or []):
            file_content = file_item.get("content")
            file_summary = file_content or cls._summary_item_text(file_item)
            add(
                kind="file",
                heading="Uploaded File",
                value=file_summary,
                freshness=str(file_item.get("freshness") or "uploaded"),
                owner="workspace",
                max_chars=12000,
                visible=bool(file_content),
                included_by_source_limit=index < 8,
            )

        for heading, kind, owner, items in (
            ("Source Summaries", "source_summary", "retrieval", source_summaries),
            ("Recent Tool Results", "tool_result", "tool_runtime", tool_result_summaries),
            ("Recent Artifacts", "artifact", "artifact_store", artifact_summaries),
        ):
            for index, item in enumerate(items or []):
                add(
                    kind=kind,
                    heading=heading,
                    value=cls._summary_item_text(item),
                    freshness=(
                        str(item.get("freshness") or "request_time")
                        if isinstance(item, dict)
                        else "request_time"
                    ),
                    owner=owner,
                    # These inputs are already summaries. Keep the legacy
                    # 360-character bound after adding per-source trust
                    # envelopes so wrapper metadata cannot inflate a compact
                    # request beyond the previous public behavior.
                    max_chars=360,
                    included_by_source_limit=index < 5,
                )

        add(
            kind="compaction_summary",
            heading="Compaction Summary",
            value=compaction_summary,
            freshness="derived_staleable",
            owner="runtime",
            conflict_policy="current_request_over_stale_summary",
            max_chars=600,
        )
        rendered, records = cls._reduce_source_records(records, max_tokens=10**9)
        return rendered, records

    @classmethod
    def _render_source_record(cls, record: Mapping[str, Any]) -> str:
        source_id = str(record.get("source_id") or "source")
        heading = str(record.get("heading") or record.get("kind") or "Context")
        conflict_policy = str(record.get("conflict_policy") or "current_request_wins")
        precedence = (
            ' precedence="conversation"' if conflict_policy.startswith("conversation") else ""
        )
        payload = _boundary_safe_json({"content": str(record.get("content") or "")})
        return (
            f"## {heading} [external data]\n"
            f'<ctx-source id="{source_id}"{precedence}>\n{payload}\n</ctx-source>'
        )

    @classmethod
    def _reduce_source_records(
        cls,
        records: list[dict[str, Any]],
        *,
        max_tokens: int,
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """Reduce whole source units while preserving escaped closing boundaries."""

        remaining = max(0, int(max_tokens))
        preamble_tokens = estimate_tokens(UNTRUSTED_CONTEXT_PREAMBLE)
        include_preamble = bool(records) and remaining >= preamble_tokens
        if include_preamble:
            remaining -= preamble_tokens
        rendered: list[str] = []
        updated: list[dict[str, Any]] = []
        visible_remaining = sum(
            1
            for record in records
            if record.get("reduction_decision") not in {"externalized", "pruned_source_limit"}
        )
        for raw in records:
            record = copy.deepcopy(raw)
            if record.get("reduction_decision") in {
                "externalized",
                "pruned_source_limit",
            }:
                updated.append(record)
                continue
            target_tokens = remaining // max(1, visible_remaining)
            visible_remaining -= 1
            if remaining <= 0 or target_tokens <= 0:
                record["content"] = ""
                record["included_chars"] = 0
                record["included_tokens"] = 0
                record["reduction_decision"] = "pruned_budget"
                updated.append(record)
                continue

            full = cls._render_source_record(record)
            full_tokens = estimate_tokens(full)
            if full_tokens <= target_tokens:
                rendered.append(full)
                remaining -= full_tokens
                updated.append(record)
                continue

            original = str(record.get("content") or "")
            marker = " ...[source truncated by context budget]"
            low, high = 0, len(original)
            candidate = ""
            while low < high:
                midpoint = (low + high + 1) // 2
                record["content"] = original[:midpoint].rstrip() + marker
                probe = cls._render_source_record(record)
                if estimate_tokens(probe) <= target_tokens:
                    low = midpoint
                    candidate = probe
                else:
                    high = midpoint - 1
            if low > 0:
                record["content"] = original[:low].rstrip() + marker
                record["included_chars"] = low
                record["included_tokens"] = estimate_tokens(record["content"])
                record["reduction_decision"] = "truncated_budget"
                rendered_value = candidate or cls._render_source_record(record)
                rendered.append(rendered_value)
                remaining -= estimate_tokens(rendered_value)
            else:
                record["content"] = ""
                record["included_chars"] = 0
                record["included_tokens"] = 0
                record["reduction_decision"] = "pruned_budget"
            updated.append(record)

        if not rendered:
            return None, updated
        prefix = [UNTRUSTED_CONTEXT_PREAMBLE] if include_preamble else []
        return "\n\n".join([*prefix, *rendered]), updated

    @classmethod
    def _provenance_receipt(
        cls,
        *,
        context: ContextStructure,
        effective_tools: list[dict[str, Any]],
        supplied: list[dict[str, Any]] | None,
        source_records: list[dict[str, Any]],
        history: list[dict[str, Any]],
        trimmed_history: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        def safe_label(value: Any, fallback: str, max_length: int) -> str:
            text = str(value or "")[:max_length]
            if text and all(character.isalnum() or character in "._-" for character in text):
                return text
            return fallback

        def receipt(
            *,
            kind: str,
            role: str,
            scope: str,
            trust: str,
            digest: str,
            freshness: str,
            size_chars: int,
            size_tokens: int,
            cacheability: str,
            owner: str,
            conflict_policy: str,
            reduction_decision: str,
            **extra: Any,
        ) -> dict[str, Any]:
            return {
                "kind": kind,
                "role": role,
                "scope": scope,
                "trust": trust,
                "digest": digest,
                "freshness": freshness,
                "size_chars": max(0, int(size_chars)),
                "size_tokens": max(0, int(size_tokens)),
                "cacheability": cacheability,
                "owner": owner,
                "conflict_policy": conflict_policy,
                "reduction_decision": reduction_decision,
                **extra,
            }

        items: list[dict[str, Any]] = [
            receipt(
                kind="system_policy",
                role="system",
                scope="platform",
                trust="trusted",
                digest=_digest(context.system_prompt),
                freshness="rule_revision",
                size_chars=len(context.system_prompt or ""),
                size_tokens=estimate_tokens(context.system_prompt or ""),
                cacheability="stable_prefix",
                owner="platform",
                conflict_policy="highest_authority",
                reduction_decision="protected",
            ),
            receipt(
                kind="current_request",
                role="user_instruction",
                scope="request",
                trust="user_instruction",
                digest=_digest(context.current_query),
                freshness="current",
                size_chars=len(context.current_query or ""),
                size_tokens=estimate_tokens(context.current_query or ""),
                cacheability="dynamic",
                owner="user",
                conflict_policy="wins_over_stale_context",
                reduction_decision="protected",
            ),
            receipt(
                kind="effective_capabilities",
                role="capability_snapshot",
                scope="request",
                trust="authorized",
                digest=_digest(effective_tools),
                freshness="permission_snapshot",
                size_chars=len(_canonical_json(effective_tools)),
                size_tokens=estimate_tokens(serialize_tools_deterministic(effective_tools)),
                cacheability="permission_scoped",
                owner="tool_policy",
                conflict_policy="cannot_be_granted_by_context",
                reduction_decision="protected",
                count=len(effective_tools),
            ),
        ]
        if history:
            items.append(
                receipt(
                    kind="conversation_history",
                    role="conversation",
                    scope="session",
                    trust="untrusted",
                    digest=_digest(history),
                    freshness="session",
                    size_chars=len(_canonical_json(history)),
                    size_tokens=estimate_tokens(_canonical_json(history)),
                    cacheability="append_only",
                    owner="session",
                    conflict_policy="current_request_wins",
                    reduction_decision=(
                        "included"
                        if len(trimmed_history) == len(history)
                        else "pruned_complete_units"
                    ),
                    included_count=len(trimmed_history),
                    original_count=len(history),
                )
            )
        for record in source_records:
            items.append(
                receipt(
                    kind=str(record.get("kind") or "source")[:64],
                    role=str(record.get("role") or "data")[:32],
                    scope=str(record.get("scope") or "request")[:64],
                    trust="untrusted",
                    digest=str(record.get("digest") or _digest(record.get("content"))),
                    freshness=str(record.get("freshness") or "unknown")[:64],
                    size_chars=int(record.get("included_chars") or 0),
                    size_tokens=int(record.get("included_tokens") or 0),
                    cacheability=str(record.get("cacheability") or "dynamic")[:32],
                    owner=str(record.get("owner") or "request")[:64],
                    conflict_policy=str(record.get("conflict_policy") or "current_request_wins")[
                        :96
                    ],
                    reduction_decision=str(record.get("reduction_decision") or "included")[:48],
                    original_size_chars=int(record.get("original_chars") or 0),
                    original_size_tokens=int(record.get("original_tokens") or 0),
                    source_id=str(record.get("source_id") or "")[:32],
                )
            )
        if context.current_images:
            items.append(
                receipt(
                    kind="image_attachment",
                    role="attachment",
                    scope="request",
                    trust="untrusted",
                    digest=_digest(context.current_images),
                    freshness="uploaded",
                    size_chars=0,
                    size_tokens=0,
                    cacheability="none",
                    owner="workspace",
                    conflict_policy="data_only",
                    reduction_decision="externalized_binary",
                    count=len(context.current_images),
                )
            )
        for value in supplied or []:
            items.append(
                receipt(
                    kind=safe_label(value.get("kind"), "external_source", 64),
                    role=safe_label(value.get("role"), "data", 32),
                    scope=safe_label(value.get("scope"), "request", 64),
                    # Caller metadata is itself untrusted; it may never promote
                    # the source into a policy or capability authority.
                    trust="untrusted",
                    digest=_digest(value.get("digest") or value.get("source_id") or value),
                    freshness=safe_label(value.get("freshness"), "unknown", 64),
                    size_chars=max(0, int(value.get("size_chars") or 0)),
                    size_tokens=max(0, int(value.get("size_tokens") or 0)),
                    cacheability=safe_label(value.get("cacheability"), "dynamic", 32),
                    owner=safe_label(value.get("owner"), "external", 64),
                    conflict_policy="current_request_wins",
                    reduction_decision=safe_label(
                        value.get("reduction_decision"), "metadata_only", 48
                    ),
                )
            )
        if len(items) <= 64:
            return items
        omitted = items[63:]
        overflow = receipt(
            kind="provenance_overflow",
            role="receipt_summary",
            scope="packet",
            trust="trusted",
            digest=_digest(omitted),
            freshness="current",
            size_chars=sum(int(item.get("size_chars") or 0) for item in omitted),
            size_tokens=sum(int(item.get("size_tokens") or 0) for item in omitted),
            cacheability="none",
            owner="runtime",
            conflict_policy="metadata_only",
            reduction_decision="receipt_aggregated",
            omitted_count=len(omitted),
        )
        return [*items[:63], overflow]

    def _cache_contract(
        self,
        *,
        system_prompt: str,
        effective_tools: list[dict[str, Any]],
        cache_dimensions: Mapping[str, Any] | None,
        previous_cache_receipt: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        dimensions: dict[str, Any] = {
            str(key): value for key, value in dict(cache_dimensions or {}).items()
        }
        dimensions.setdefault("model", "")
        dimensions.setdefault("permission_snapshot", "")
        dimensions.setdefault("rule_revision", "")
        # These dimensions are derived from the actual model-bound payload and
        # cannot be overridden by caller-supplied cache metadata.
        dimensions["provider"] = self.provider
        dimensions["system_rules"] = system_prompt
        dimensions["effective_tools"] = serialize_tools_deterministic(effective_tools)
        dimension_hashes = {key: _digest(value) for key, value in sorted(dimensions.items())}
        cache_key = f"ctxc_{_digest(dimension_hashes)}"

        previous_cache = dict(previous_cache_receipt or {}).get("cache")
        if not isinstance(previous_cache, Mapping):
            previous_cache = previous_cache_receipt
        previous_hashes = (
            dict(previous_cache or {}).get("dimension_hashes")
            if isinstance(previous_cache, Mapping)
            else None
        )
        if not isinstance(previous_hashes, Mapping):
            status = "cold"
            invalidation_reasons = ["no_previous_packet"]
        else:
            invalidation_reasons = [
                key
                for key in sorted(set(previous_hashes) | set(dimension_hashes))
                if str(previous_hashes.get(key) or "") != str(dimension_hashes.get(key) or "")
            ]
            status = "reusable" if not invalidation_reasons else "invalidated"
        return {
            "cache_key": cache_key,
            "status": status,
            "dimension_hashes": dimension_hashes,
            "invalidation_reasons": invalidation_reasons,
        }

    @classmethod
    def _format_summary_section(
        cls,
        heading: str,
        items: list[dict[str, Any] | str] | None,
        *,
        max_items: int = 5,
    ) -> str:
        if not items:
            return ""
        lines = [
            f"- {cls._bounded_text(cls._summary_item_text(item), max_chars=360)}"
            for item in items[:max_items]
        ]
        return f"## {heading}\n" + "\n".join(lines)

    @staticmethod
    def _summary_item_text(item: dict[str, Any] | str) -> str:
        if isinstance(item, str):
            return item
        keys = (
            "source_type",
            "citation",
            "freshness",
            "tool_name",
            "artifact_id",
            "name",
            "version",
            "title",
            "description",
            "summary",
            "path",
            "content",
        )
        parts = [f"{key}: {item[key]}" for key in keys if item.get(key)]
        return "; ".join(parts) if parts else str(item)

    @staticmethod
    def _bounded_text(value: Any, *, max_chars: int) -> str:
        text = str(value).replace("\n", " ").strip()
        if len(text) > max_chars:
            return f"{text[: max_chars - 3]}..."
        return text
