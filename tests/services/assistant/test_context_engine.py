"""
Context Engine Tests

Tests for ContextStructure dataclass and ContextEngine class:
- ContextStructure initialization and default values
- ContextEngine message building with various providers
- Cache control hints for Anthropic
- System content building with all layers
- Edge cases and empty values handling
"""

from types import SimpleNamespace

import pytest
from assistant_service.core.context_engine import (
    ContextBudgetManager,
    ContextEngine,
    ContextStructure,
    create_context_engine,
)
from assistant_service.core.files.file_processor import FileProcessor
from assistant_service.core.runtime.context.assembler import ContextAssemblerV2

# =============================================================================
# ContextStructure Tests
# =============================================================================


class TestContextStructure:
    """Test ContextStructure dataclass."""

    def test_minimal_initialization(self):
        """Test ContextStructure with only required field."""
        context = ContextStructure(system_prompt="You are a helpful assistant.")

        assert context.system_prompt == "You are a helpful assistant."
        assert context.tool_definitions == []
        assert context.user_preferences is None
        assert context.long_term_memory is None
        assert context.task_state is None
        assert context.conversation_history == []
        assert context.current_context is None
        assert context.current_query == ""

    def test_full_initialization(self):
        """Test ContextStructure with all fields."""
        tool_defs = [{"name": "search", "description": "Search the web"}]
        history = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ]

        context = ContextStructure(
            system_prompt="You are a helpful assistant.",
            tool_definitions=tool_defs,
            user_preferences="Prefer concise responses",
            long_term_memory="User is a software engineer",
            task_state="Working on API implementation",
            conversation_history=history,
            current_context="RAG results: ...",
            current_query="What's next?",
        )

        assert context.system_prompt == "You are a helpful assistant."
        assert context.tool_definitions == tool_defs
        assert context.user_preferences == "Prefer concise responses"
        assert context.long_term_memory == "User is a software engineer"
        assert context.task_state == "Working on API implementation"
        assert context.conversation_history == history
        assert context.current_context == "RAG results: ..."
        assert context.current_query == "What's next?"

    def test_default_factory_isolation(self):
        """Test that default_factory creates independent lists for each instance."""
        context1 = ContextStructure(system_prompt="Prompt 1")
        context2 = ContextStructure(system_prompt="Prompt 2")

        # Modify context1's lists
        context1.tool_definitions.append({"name": "tool1"})
        context1.conversation_history.append({"role": "user", "content": "test"})

        # context2 should not be affected
        assert context2.tool_definitions == []
        assert context2.conversation_history == []


# =============================================================================
# ContextEngine Tests
# =============================================================================


class TestContextEngineInit:
    """Test ContextEngine initialization."""

    def test_provider_lowercase(self):
        """Test that provider is normalized to lowercase."""
        engine1 = ContextEngine(provider="ANTHROPIC")
        engine2 = ContextEngine(provider="Anthropic")
        engine3 = ContextEngine(provider="anthropic")

        assert engine1.provider == "anthropic"
        assert engine2.provider == "anthropic"
        assert engine3.provider == "anthropic"

    def test_different_providers(self):
        """Test initialization with different providers."""
        anthropic_engine = ContextEngine(provider="anthropic")
        openai_engine = ContextEngine(provider="openai")
        other_engine = ContextEngine(provider="other")

        assert anthropic_engine.provider == "anthropic"
        assert openai_engine.provider == "openai"
        assert other_engine.provider == "other"


class TestContextEngineBuildMessages:
    """Test ContextEngine.build_messages method."""

    def test_minimal_messages_anthropic(self):
        """Test building messages with minimal context for Anthropic."""
        engine = ContextEngine(provider="anthropic")
        context = ContextStructure(
            system_prompt="You are a helpful assistant.",
            current_query="Hello",
        )

        messages = engine.build_messages(context)

        assert len(messages) == 2

        # System message
        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == "You are a helpful assistant."
        assert messages[0]["cache_control"] == {"type": "ephemeral"}

        # User message
        assert messages[1]["role"] == "user"
        assert messages[1]["content"] == "Hello"

    def test_minimal_messages_openai(self):
        """Test building messages with minimal context for OpenAI."""
        engine = ContextEngine(provider="openai")
        context = ContextStructure(
            system_prompt="You are a helpful assistant.",
            current_query="Hello",
        )

        messages = engine.build_messages(context)

        assert len(messages) == 2

        # System message (no cache_control for OpenAI)
        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == "You are a helpful assistant."
        assert "cache_control" not in messages[0]

        # User message
        assert messages[1]["role"] == "user"
        assert messages[1]["content"] == "Hello"

    def test_with_conversation_history(self):
        """Test building messages with conversation history."""
        engine = ContextEngine(provider="anthropic")
        context = ContextStructure(
            system_prompt="You are a helpful assistant.",
            conversation_history=[
                {"role": "user", "content": "What is 2+2?"},
                {"role": "assistant", "content": "2+2 equals 4."},
            ],
            current_query="And 3+3?",
        )

        messages = engine.build_messages(context)

        assert len(messages) == 4

        # System message
        assert messages[0]["role"] == "system"

        # Conversation history
        assert messages[1]["role"] == "user"
        assert messages[1]["content"] == "What is 2+2?"
        assert messages[2]["role"] == "assistant"
        assert messages[2]["content"] == "2+2 equals 4."

        # Current query
        assert messages[3]["role"] == "user"
        assert messages[3]["content"] == "And 3+3?"

    def test_with_current_context(self):
        """Test that current_context is prepended to current_query."""
        engine = ContextEngine(provider="openai")
        context = ContextStructure(
            system_prompt="You are a helpful assistant.",
            current_context="RAG Results:\n- Document 1\n- Document 2",
            current_query="Summarize these documents.",
        )

        messages = engine.build_messages(context)

        assert len(messages) == 2

        # User message should have context + query
        assert messages[1]["role"] == "user"
        expected_content = "RAG Results:\n- Document 1\n- Document 2\n\nSummarize these documents."
        assert messages[1]["content"] == expected_content

    def test_no_current_query(self):
        """Test building messages without current query."""
        engine = ContextEngine(provider="anthropic")
        context = ContextStructure(
            system_prompt="You are a helpful assistant.",
            conversation_history=[
                {"role": "user", "content": "Previous message"},
            ],
        )

        messages = engine.build_messages(context)

        # System/history remain stable and the current empty user turn is
        # still explicit for provider conversation-shape compatibility.
        assert len(messages) == 3
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        assert messages[1]["content"] == "Previous message"
        assert messages[2] == {"role": "user", "content": ""}

    def test_empty_current_query(self):
        """Test that an empty current user turn preserves provider shape."""
        engine = ContextEngine(provider="anthropic")
        context = ContextStructure(
            system_prompt="You are a helpful assistant.",
            current_query="",
        )

        messages = engine.build_messages(context)

        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[1] == {"role": "user", "content": ""}


class TestContextEngineBuildSystemContent:
    """Test ContextEngine._build_system_content method."""

    def test_system_prompt_only(self):
        """Test system content with only base prompt."""
        engine = ContextEngine(provider="anthropic")
        context = ContextStructure(system_prompt="You are a helpful assistant.")

        content = engine._build_system_content(context)

        assert content == "You are a helpful assistant."

    def test_with_user_preferences(self):
        """Test system content with user preferences."""
        engine = ContextEngine(provider="anthropic")
        context = ContextStructure(
            system_prompt="You are a helpful assistant.",
            user_preferences="Prefer concise responses.",
        )

        content = engine._build_system_content(context)

        expected = "You are a helpful assistant.\n\n## User Preferences\nPrefer concise responses."
        assert content == expected

    def test_with_long_term_memory(self):
        """Test system content with long-term memory."""
        engine = ContextEngine(provider="anthropic")
        context = ContextStructure(
            system_prompt="You are a helpful assistant.",
            long_term_memory="User is a software engineer.",
        )

        content = engine._build_system_content(context)

        expected = (
            "You are a helpful assistant.\n\n## Background Knowledge\nUser is a software engineer."
        )
        assert content == expected

    def test_with_task_state(self):
        """Test system content with task state."""
        engine = ContextEngine(provider="anthropic")
        context = ContextStructure(
            system_prompt="You are a helpful assistant.",
            task_state="Working on API implementation.",
        )

        content = engine._build_system_content(context)

        expected = (
            "You are a helpful assistant.\n\n## Current Task State\nWorking on API implementation."
        )
        assert content == expected

    def test_with_all_layers(self):
        """Test system content with all optional layers."""
        engine = ContextEngine(provider="anthropic")
        context = ContextStructure(
            system_prompt="You are a helpful assistant.",
            user_preferences="Prefer concise responses.",
            long_term_memory="User is a software engineer.",
            task_state="Working on API implementation.",
        )

        content = engine._build_system_content(context)

        expected = (
            "You are a helpful assistant.\n"
            "\n## User Preferences\nPrefer concise responses.\n"
            "\n## Background Knowledge\nUser is a software engineer.\n"
            "\n## Current Task State\nWorking on API implementation."
        )
        assert content == expected

    def test_layer_ordering(self):
        """Test that layers are in correct order (User Preferences -> Background -> Task State)."""
        engine = ContextEngine(provider="anthropic")
        context = ContextStructure(
            system_prompt="Base",
            user_preferences="Prefs",
            long_term_memory="Memory",
            task_state="State",
        )

        content = engine._build_system_content(context)

        # Check ordering by finding indices
        prefs_idx = content.find("## User Preferences")
        memory_idx = content.find("## Background Knowledge")
        state_idx = content.find("## Current Task State")

        assert prefs_idx < memory_idx < state_idx


# =============================================================================
# Cache Control Tests
# =============================================================================


class TestCacheControl:
    """Test cache control functionality."""

    def test_anthropic_cache_control(self):
        """Test that Anthropic gets cache_control in system message."""
        engine = ContextEngine(provider="anthropic")
        context = ContextStructure(
            system_prompt="Test prompt",
            current_query="Test query",
        )

        messages = engine.build_messages(context)

        assert "cache_control" in messages[0]
        assert messages[0]["cache_control"] == {"type": "ephemeral"}

    def test_openai_no_cache_control(self):
        """Test that OpenAI does not get cache_control."""
        engine = ContextEngine(provider="openai")
        context = ContextStructure(
            system_prompt="Test prompt",
            current_query="Test query",
        )

        messages = engine.build_messages(context)

        assert "cache_control" not in messages[0]

    def test_unknown_provider_no_cache_control(self):
        """Test that unknown providers don't get cache_control."""
        engine = ContextEngine(provider="unknown_provider")
        context = ContextStructure(
            system_prompt="Test prompt",
            current_query="Test query",
        )

        messages = engine.build_messages(context)

        assert "cache_control" not in messages[0]

    def test_cache_breakpoints_constant(self):
        """Test that CACHE_BREAKPOINTS has expected values."""
        assert ContextEngine.CACHE_BREAKPOINTS["anthropic"] == {"type": "ephemeral"}
        assert ContextEngine.CACHE_BREAKPOINTS["openai"] is None


# =============================================================================
# Factory Function Tests
# =============================================================================


class TestCreateContextEngine:
    """Test create_context_engine factory function."""

    def test_create_anthropic_engine(self):
        """Test creating Anthropic engine via factory."""
        engine = create_context_engine("anthropic")

        assert isinstance(engine, ContextEngine)
        assert engine.provider == "anthropic"

    def test_create_openai_engine(self):
        """Test creating OpenAI engine via factory."""
        engine = create_context_engine("openai")

        assert isinstance(engine, ContextEngine)
        assert engine.provider == "openai"

    def test_factory_preserves_case_normalization(self):
        """Test that factory preserves case normalization."""
        engine = create_context_engine("ANTHROPIC")

        assert engine.provider == "anthropic"


# =============================================================================
# RAG Source Scope Tests
# =============================================================================


class TestRAGSourceScope:
    """Test source-aware RAG context behavior for NGA-F008."""

    @pytest.mark.asyncio
    async def test_long_uploaded_document_creates_session_kb_with_scope(self, tmp_path):
        """Long uploaded files create a session-scoped temporary KB when supported."""

        class FakeKnowledgeService:
            def __init__(self):
                self.calls = []

            async def create_session_dataset(self, *, user, session_id, documents, metadata):
                self.calls.append(
                    {
                        "user": user,
                        "session_id": session_id,
                        "documents": documents,
                        "metadata": metadata,
                    }
                )
                return {"dataset_id": "session-kb-1"}

        uploads_dir = tmp_path / "uploads" / "tenant-a" / "user-a"
        uploads_dir.mkdir(parents=True)
        long_doc = uploads_dir / "long.md"
        long_doc.write_text(("alpha beta gamma\n" * 20), encoding="utf-8")

        user = SimpleNamespace(tenant_id="tenant-a", user_id="user-a")
        kb = FakeKnowledgeService()
        processor = FileProcessor(knowledge_service=kb, storage_base_path=tmp_path)

        async def fake_process_document(file_path, api_path, max_text_chars):
            return (
                "",
                True,
                {
                    "file_path": api_path,
                    "file_name": file_path.name,
                    "file_type": "document",
                    "requires_rag": True,
                },
            )

        processor._process_document = fake_process_document

        result = await processor.process_files(
            file_paths=["uploads/tenant-a/user-a/long.md"],
            session_id="session-a",
            user=user,
            model_supports_vision=False,
            max_text_chars=32,
        )

        assert result.requires_rag is True
        assert result.session_kb_id == "session-kb-1"
        assert kb.calls[0]["documents"] == ["uploads/tenant-a/user-a/long.md"]
        assert kb.calls[0]["metadata"]["source_type"] == "session_file"
        assert kb.calls[0]["metadata"]["scope"] == {
            "tenant_id": "tenant-a",
            "user_id": "user-a",
            "session_id": "session-a",
        }

# =============================================================================
# Context Packet Budget Tests
# =============================================================================


class TestContextPacketBudget:
    """Test bounded context packet assembly for NGA-F009."""

    def test_context_packet_includes_bounded_summaries_and_budget_telemetry(self):
        """Assembler keeps summary context ordered and records contributor cost."""
        history = [
            {
                "role": "user" if idx % 2 == 0 else "assistant",
                "content": f"older message {idx} " + ("token " * 120),
            }
            for idx in range(8)
        ]
        context = ContextStructure(
            system_prompt="Stable system policy.",
            user_preferences="Prefers concise answers.",
            long_term_memory="Semantic memory: works on assistant runtime.",
            task_state="Session state: implementing context telemetry.",
            conversation_history=history,
            current_context="RAG snippet: scoped retrieval result.",
            current_query="What should be assembled next?",
        )
        assembler = ContextAssemblerV2(
            provider="openai",
            budget_manager=ContextBudgetManager(
                reserved_output_tokens=0,
                min_recent_messages=2,
            ),
        )
        long_raw_output = "raw-output " * 100

        messages, budget_event, detail = assembler.build(
            context=context,
            model_context_window=1024,
            tool_definitions=[{"name": "search", "description": "Search"}],
            skills_metadata=[
                {
                    "name": "doc-skill",
                    "summary": "selected skill metadata only",
                    "instructions": "full instructions " * 200,
                }
            ],
            memory_snippets=["Scoped semantic memory snippet."],
            source_summaries=[
                {
                    "source_type": "kb",
                    "citation": "kb://policy#1",
                    "summary": "KB policy source summary.",
                }
            ],
            tool_result_summaries=[
                {"tool_name": "search", "summary": long_raw_output},
            ],
            artifact_summaries=[
                {"artifact_id": "artifact-1", "summary": "Generated artifact summary."},
            ],
            compaction_summary="Older conversation summarized with URLs preserved.",
        )

        user_content = messages[-1]["content"]
        assert user_content.index("## Historical Conversation Memory") < user_content.index(
            "## Current Structured User Memory"
        )
        assert user_content.index("## Current Structured User Memory") < user_content.index(
            "RAG snippet"
        )
        assert user_content.index("RAG snippet") < user_content.index("## Source Summaries")
        assert user_content.index("## Source Summaries") < user_content.index(
            "## Recent Tool Results"
        )
        assert user_content.index("## Recent Tool Results") < user_content.index(
            "## Recent Artifacts"
        )
        assert user_content.index("## Recent Artifacts") < user_content.index(
            "## Compaction Summary"
        )
        assert user_content.index("## Compaction Summary") < user_content.index(
            "What should be assembled next?"
        )
        assert "full instructions" not in user_content
        assert len(user_content) < len(long_raw_output) + 800

        assert budget_event["context_packet_order"] == [
            "stable_system_policy",
            "current_turn_and_session_state",
            "selected_capability_metadata",
            "scoped_memory_snippets",
            "rag_source_summaries",
            "recent_tool_artifact_summaries",
            "compaction_summary",
            "budget_telemetry",
        ]
        assert budget_event["compaction"]["dropped_history_messages"] > 0
        assert detail["tokens_by_category"]["source_summaries"] > 0
        assert detail["tokens_by_category"]["tool_results"] > 0
        assert detail["tokens_by_category"]["artifacts"] > 0
        assert detail["tokens_by_category"]["compaction"] > 0


# =============================================================================
# Integration Tests
# =============================================================================


class TestContextEngineIntegration:
    """Integration tests for realistic usage scenarios."""

    def test_full_conversation_flow(self):
        """Test a realistic conversation flow with all components."""
        engine = ContextEngine(provider="anthropic")

        # Build context for a realistic scenario
        context = ContextStructure(
            system_prompt="You are a code review assistant. Help users improve their code.",
            tool_definitions=[
                {
                    "name": "analyze_code",
                    "description": "Analyze code for potential issues",
                    "parameters": {"code": "string"},
                }
            ],
            user_preferences="Prefer detailed explanations. Focus on security issues.",
            long_term_memory="User works with Python and JavaScript. Has 5 years of experience.",
            task_state="Reviewing pull request #123 for the auth module.",
            conversation_history=[
                {
                    "role": "user",
                    "content": "Can you review this function?",
                },
                {
                    "role": "assistant",
                    "content": "I'd be happy to review it. Please share the code.",
                },
            ],
            current_context="```python\ndef authenticate(user, password):\n    return True\n```",
            current_query="Is there a security issue with this authentication function?",
        )

        messages = engine.build_messages(context)

        # Verify structure
        assert len(messages) == 4  # system + 2 history + current

        # System message with all layers
        system_content = messages[0]["content"]
        assert "You are a code review assistant" in system_content
        assert "## User Preferences" in system_content
        assert "## Background Knowledge" in system_content
        assert "## Current Task State" in system_content

        # History preserved
        assert messages[1]["content"] == "Can you review this function?"
        assert messages[2]["content"] == "I'd be happy to review it. Please share the code."

        # Current query with context
        assert "```python" in messages[3]["content"]
        assert "Is there a security issue" in messages[3]["content"]

    def test_append_only_history(self):
        """Test that conversation history is truly append-only."""
        engine = ContextEngine(provider="openai")

        # First query
        context1 = ContextStructure(
            system_prompt="Base prompt",
            conversation_history=[],
            current_query="First message",
        )
        messages1 = engine.build_messages(context1)

        # Second query (history extended)
        context2 = ContextStructure(
            system_prompt="Base prompt",
            conversation_history=[
                {"role": "user", "content": "First message"},
                {"role": "assistant", "content": "First response"},
            ],
            current_query="Second message",
        )
        messages2 = engine.build_messages(context2)

        # System message should be identical
        assert messages1[0]["content"] == messages2[0]["content"]

        # History should be extended, not replaced
        assert len(messages2) == 4  # system + 2 history + current
        assert messages2[1]["content"] == "First message"
        assert messages2[2]["content"] == "First response"
        assert messages2[3]["content"] == "Second message"
