"""Context Engine message construction for the Assistant service."""

from __future__ import annotations

from typing import Any

from ai_gateway_core.logging import get_logger

from .agent.runtime_context import compose_agent_system_prompt
from .assistant_context_keys import _context_receipt_key
from .assistant_models import AssistantConfig, RetrievedContext
from .files.file_processor import ProcessedFiles
from .models.model_registry import ChatMessage
from .prompts.system_prompt_v2 import ensure_external_content_boundary, get_ttft_optimized_prompt
from .rag.context_engine import ContextStructure
from .runtime.context.assembler import ContextAssemblerV2

logger = get_logger(__name__)


class AssistantContextMessageBuilderMixin:
    """Build Context Packet messages while relying on the host service state."""

    def _build_messages_with_context_engine(
        self,
        message: str,
        history: list[dict[str, str]],
        config: AssistantConfig,
        retrieved_contexts: list[RetrievedContext],
        web_search_context: str | None = None,
        processed_files: ProcessedFiles | None = None,
        model_supports_vision: bool = False,
        session_id: str | None = None,
        user_preferences: str | None = None,
        domain_rules: str = "",
        include_citations: bool = False,
        context_packet_receipt: dict[str, Any] | None = None,
        context_cache_scope: str | None = None,
        working_memory_scope: str | None = None,
    ) -> list[ChatMessage]:
        """Build messages using Context Engine for KV-Cache optimization.

        This method uses the ContextEngine class to construct messages with
        a stable prefix design that maximizes cache hit rates.

        Key differences from legacy _build_messages:
        - System prompt is built with layered structure (stable first)
        - User preferences and long-term memory are injected into system prompt
        - Working memory (task state) is included for multi-step task focus
        - KB/web context goes into current_context (end of user message)

        Args:
            message: The user's message text.
            history: Previous conversation history.
            config: Assistant configuration.
            retrieved_contexts: KB retrieval results.
            web_search_context: Web search results as formatted text.
            processed_files: Processed file contents.
            model_supports_vision: Whether the model supports vision.
            session_id: Session ID for working memory lookup.
            user_preferences: User preferences loaded from MemoryManager (formatted string).

        Returns:
            List of ChatMessage objects with optimized structure.
        """
        # Get provider from model_id to configure the shared Context Packet.
        provider = self._get_provider_from_model(config.model_id)

        # Build current context (KB + web search results)
        current_context_parts: list[str] = []
        if retrieved_contexts:
            context_text = self._format_context(
                retrieved_contexts,
                include_citations=include_citations,
            )
            current_context_parts.append(self.CONTEXT_TEMPLATE.format(context=context_text))
            logger.info(f"[CONTEXT ENGINE] KB context: {len(context_text)} chars")

        if web_search_context:
            current_context_parts.append(
                self.WEB_CONTEXT_TEMPLATE.format(context=web_search_context)
            )
            logger.info(f"[CONTEXT ENGINE] Web context: {len(web_search_context)} chars")

        client_prompt = (config.system_prompt or "").strip()
        if client_prompt:
            current_context_parts.append(
                "## User Custom Instructions (client-supplied, lower priority than system)\n"
                + client_prompt[:500]
            )

        injected_file_sources: list[dict[str, Any]] = []
        current_images: list[str] = []
        if processed_files:
            injected_file_sources.extend(dict(item) for item in processed_files.file_metadata or [])
            text_content = str(processed_files.text_content or "")
            if text_content:
                injected_file_sources.append(
                    {
                        "path": "uploaded-text",
                        "source_type": "upload",
                        "content": text_content,
                    }
                )
            if processed_files.image_descriptions and not model_supports_vision:
                descriptions = "\n".join(
                    f"- Image {index + 1}: {description}"
                    for index, description in enumerate(processed_files.image_descriptions)
                )
                if descriptions:
                    injected_file_sources.append(
                        {
                            "path": "image-descriptions",
                            "source_type": "derived",
                            "content": descriptions,
                        }
                    )
            if model_supports_vision and processed_files.has_images:
                current_images.extend(
                    f"data:{image.media_type};base64,{image.base64_data}"
                    for image in processed_files.images
                )
                current_images.extend(
                    f"data:{page.media_type};base64,{page.base64_data}"
                    for page in processed_files.pdf_pages
                )

        # Get working memory task state if available
        task_state: str | None = None
        working_memory_key = working_memory_scope or session_id
        if working_memory_key and working_memory_key in self._working_memories:
            working_memory = self._working_memories[working_memory_key]
            task_state = working_memory.to_markdown()
            logger.info(f"[CONTEXT ENGINE] Task state injected: {len(task_state)} chars")

        # Determine user_preferences: prefer loaded preferences from MemoryManager,
        # fallback to config.user_preferences
        effective_user_preferences = user_preferences or config.user_preferences
        if effective_user_preferences:
            logger.info(
                f"[CONTEXT ENGINE] User preferences: {len(effective_user_preferences)} chars"
            )

        # Build ContextStructure with layered content
        # Use TTFT-optimized prompt when context engine is enabled (no timestamps!)
        effective_system_prompt = ensure_external_content_boundary(
            (config.eval_system_prompt_override or "").strip()
            or get_ttft_optimized_prompt(
                user_role="user",
                available_datasets=config.kb_dataset_ids,
                scenario_rules=domain_rules,
            )
        )
        if config.agent_runtime is not None:
            effective_system_prompt = compose_agent_system_prompt(
                platform_prompt=effective_system_prompt,
                agent_instructions=config.trusted_agent_instructions,
                channel_instructions=config.trusted_channel_instructions,
                capability_instructions=config.trusted_capability_instructions,
            )
        logger.info("[CONTEXT ENGINE] Built trusted stable system prompt")

        context_structure = ContextStructure(
            system_prompt=effective_system_prompt,
            tool_definitions=[],  # Tool definitions handled separately
            user_preferences=effective_user_preferences,
            long_term_memory=config.long_term_memory,
            task_state=task_state,
            conversation_history=[
                dict(h)
                for h in history
                if h.get("role") in ("user", "assistant", "tool")
                and (h.get("role") == "tool" or h.get("content") or h.get("tool_calls"))
            ],
            current_context="\n\n".join(current_context_parts) if current_context_parts else None,
            current_query=message,
            current_images=current_images,
        )

        model_info = self.model_registry.get_model(config.model_id)
        context_window = int(getattr(model_info, "context_window", 0) or 128000)
        allowlist = config.capability_allowlist
        permission_snapshot: Any = (
            sorted(allowlist.tool_names)
            if allowlist is not None
            else "legacy-no-explicit-allowlist"
        )
        if config.agent_runtime is not None:
            permission_snapshot = {
                "runtime_fingerprint": config.agent_runtime.runtime_fingerprint,
                "allowlist": permission_snapshot,
            }
        cache_receipt_key = (
            _context_receipt_key(
                scope=context_cache_scope,
                model_id=config.model_id,
            )
            if context_cache_scope
            else None
        )
        previous_cache_receipt = (
            self._context_packet_receipts.get(cache_receipt_key)
            if cache_receipt_key is not None
            else None
        )
        packet = ContextAssemblerV2(provider=provider).build_packet(
            context=context_structure,
            model_context_window=context_window,
            injected_files=injected_file_sources,
            provenance=[
                {
                    "kind": "knowledge",
                    "trust": "untrusted",
                    "source_id": {
                        "dataset_id": item.dataset_id,
                        "dataset_name": item.dataset_name,
                    },
                }
                for item in retrieved_contexts
            ],
            cache_dimensions={
                "model": config.model_id,
                "permission_snapshot": permission_snapshot,
                "rule_revision": {
                    "domain_rules": domain_rules,
                    "agent_instructions": config.trusted_agent_instructions,
                    "channel_instructions": config.trusted_channel_instructions,
                    "capability_instructions": config.trusted_capability_instructions,
                },
            },
            previous_cache_receipt=previous_cache_receipt,
        )
        raw_messages = packet.materialize_messages()
        packet_receipt = packet.receipt()
        if cache_receipt_key is not None:
            self._context_packet_receipts[cache_receipt_key] = packet_receipt
        if context_packet_receipt is not None:
            context_packet_receipt.update(packet_receipt)

        # Convert to ChatMessage objects and handle file content
        messages: list[ChatMessage] = []
        for msg in raw_messages:
            role = msg["role"]
            messages.append(
                ChatMessage(
                    role=role,
                    content=msg.get("content", ""),
                    name=msg.get("name"),
                    tool_calls=msg.get("tool_calls"),
                    tool_call_id=msg.get("tool_call_id"),
                    images=msg.get("images"),
                    thought_signature=msg.get("thought_signature"),
                )
            )

        logger.info(f"[CONTEXT ENGINE] Built {len(messages)} messages with stable prefix design")
        return messages
