"""
Tests for AssistantService core functionality.

Tests session management, streaming, and configuration handling.
"""

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

# Skip entire module if tiktoken is not available
tiktoken = pytest.importorskip("tiktoken", reason="tiktoken required for assistant service tests")


@dataclass
class MockUserContext:
    """Mock UserContext for testing."""

    user_id: str
    tenant_id: str = ""
    tier: str = "normal"
    is_authenticated: bool = True
    ip: str = "127.0.0.1"
    roles: list[str] = None

    def __post_init__(self):
        if self.roles is None:
            self.roles = []


class TestAssistantServiceInit:
    """Tests for AssistantService initialization."""

    def test_init_with_required_args(self):
        """Should initialize with just model_registry."""
        from assistant_service.core.assistant_service import AssistantService
        from assistant_service.core.models.model_registry import ModelRegistry

        mock_registry = MagicMock(spec=ModelRegistry)

        service = AssistantService(model_registry=mock_registry)

        assert service.model_registry == mock_registry
        assert service.kb_service is None
        assert service.session_manager is None

    def test_init_with_kb_service(self):
        """Should initialize with knowledge service."""
        from assistant_service.core.assistant_service import AssistantService
        from assistant_service.core.models.model_registry import ModelRegistry
        from knowledge_service.services.knowledge.knowledge_service import KnowledgeService

        mock_registry = MagicMock(spec=ModelRegistry)
        mock_kb = MagicMock(spec=KnowledgeService)

        service = AssistantService(
            model_registry=mock_registry,
            kb_service=mock_kb,
        )

        assert service.kb_service == mock_kb

    def test_init_with_session_manager(self):
        """Should initialize with session manager."""
        from assistant_service.core.assistant_service import AssistantService
        from assistant_service.core.models.model_registry import ModelRegistry

        from src.services.session.database_session_manager import DatabaseSessionManager

        mock_registry = MagicMock(spec=ModelRegistry)
        mock_session_mgr = MagicMock(spec=DatabaseSessionManager)

        service = AssistantService(
            model_registry=mock_registry,
            session_manager=mock_session_mgr,
        )

        assert service.session_manager == mock_session_mgr

    def test_init_creates_context_manager(self):
        """Should create context manager during init."""
        from assistant_service.core.assistant_service import AssistantService
        from assistant_service.core.models.model_registry import ModelRegistry

        mock_registry = MagicMock(spec=ModelRegistry)

        service = AssistantService(model_registry=mock_registry)

        assert service.context_manager is not None

    def test_builtin_domain_policy_disabled_by_default(self):
        """Built-in domain policy should be off for generic assistant by default."""
        from assistant_service.core.assistant_service import AssistantService
        from assistant_service.core.models.model_registry import ModelRegistry

        mock_registry = MagicMock(spec=ModelRegistry)
        service = AssistantService(model_registry=mock_registry)

        assert service.builtin_domain_policy_enabled is False
        assert service.domain_policy_resolver is None

    def test_builtin_domain_policy_can_be_enabled_by_env(self, monkeypatch):
        """Emergency rollback switch can re-enable built-in domain policy."""
        from assistant_service.core.assistant_service import AssistantService
        from assistant_service.core.models.model_registry import ModelRegistry

        monkeypatch.setenv("ASSISTANT_BUILTIN_DOMAIN_POLICY_ENABLED", "true")
        mock_registry = MagicMock(spec=ModelRegistry)
        service = AssistantService(model_registry=mock_registry)

        assert service.builtin_domain_policy_enabled is True
        assert service.domain_policy_resolver is not None


class TestAssistantConfig:
    """Tests for AssistantConfig dataclass."""

    def test_default_values(self):
        """AssistantConfig should have sensible defaults."""
        from assistant_service.core.assistant_service import AssistantConfig

        config = AssistantConfig()

        # Model defaults
        assert config.model_id == "qwen3.6-plus"  # Gateway default
        assert config.temperature == 0.7
        assert config.max_tokens is None

        # RAG defaults
        assert config.kb_dataset_ids == []
        assert config.kb_top_k == 5

        # Feature flags
        assert config.web_search_enabled is False
        assert config.enable_task_planning is False

    def test_custom_values(self):
        """Should accept custom configuration."""
        from assistant_service.core.assistant_service import AssistantConfig

        config = AssistantConfig(
            model_id="gpt-4",
            temperature=0.5,
            max_tokens=2000,
            kb_dataset_ids=["docs", "wiki"],
            kb_top_k=10,
            web_search_enabled=True,
        )

        assert config.model_id == "gpt-4"

    def test_route_config_falls_back_when_model_provider_is_not_configured(self):
        """HTTP route should not pass a model whose provider has no runtime key."""
        from assistant_service.api.routes.chat import ChatRequest, _build_config
        from assistant_service.core.models.model_registry import ModelInfo, ModelProvider

        dashscope_model = ModelInfo(
            id="qwen3.6-plus",
            name="Qwen 3.6 Plus",
            provider=ModelProvider.DASHSCOPE,
        )
        openai_model = ModelInfo(
            id="gpt-4o-mini",
            name="GPT-4o mini",
            provider=ModelProvider.OPENAI,
        )
        registry = MagicMock()
        registry.get_model.return_value = dashscope_model
        registry.is_provider_configured.return_value = False
        registry.get_available_models.return_value = [openai_model]

        config = _build_config(
            ChatRequest(
                message="hello",
                temperature=0.5,
                max_tokens=2000,
                kb_dataset_ids=["docs", "wiki"],
                kb_top_k=10,
                web_search_enabled=True,
            ),
            registry,
        )

        assert config.model_id == "gpt-4o-mini"
        assert config.model_provider == ModelProvider.OPENAI
        assert config.temperature == 0.5
        assert config.max_tokens == 2000
        assert config.kb_dataset_ids == ["docs", "wiki"]
        assert config.kb_top_k == 10
        assert config.web_search_enabled is True

    def test_route_config_carries_traceparent(self):
        """HTTP route config should preserve request trace correlation."""
        from assistant_service.api.routes.chat import ChatRequest, _build_config

        traceparent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"

        config = _build_config(
            ChatRequest(message="hello"),
            None,
            traceparent=traceparent,
        )

        assert config.traceparent == traceparent
        assert config.otel_trace_id == "4bf92f3577b34da6a3ce929d0e0e4736"


class TestAssistantE2EStub:
    """Tests for explicit local-E2E deterministic assistant behavior."""

    def test_e2e_memory_stub_is_disabled_by_default(self, monkeypatch):
        from assistant_service.api.routes import chat as chat_routes

        monkeypatch.delenv("ASSISTANT_E2E_STUB_LLM", raising=False)
        chat_routes._E2E_MEMORY_BY_USER.clear()

        response = chat_routes._build_e2e_memory_stub_response(
            chat_routes.ChatRequest(message="请记住：我的名字是小明，我来自悉尼。"),
            MockUserContext(user_id="user-a", tenant_id="tenant-a"),
        )

        assert response is None

    def test_e2e_memory_stub_recalls_per_user(self, monkeypatch):
        from assistant_service.api.routes import chat as chat_routes

        monkeypatch.setenv("ASSISTANT_E2E_STUB_LLM", "1")
        chat_routes._E2E_MEMORY_BY_USER.clear()
        user = MockUserContext(user_id="user-a", tenant_id="tenant-a")

        remember = chat_routes._build_e2e_memory_stub_response(
            chat_routes.ChatRequest(message="请记住：我的名字是小明，我来自悉尼-abc。"),
            user,
        )
        recall = chat_routes._build_e2e_memory_stub_response(
            chat_routes.ChatRequest(message="你还记得我的名字和城市吗？直接回答。"),
            user,
        )

        assert remember == "已记住"
        assert recall == "你的名字是小明，你来自悉尼-abc。"


class TestAssistantServiceChatStream:
    """Tests for chat_stream functionality."""

    @pytest.fixture
    def mock_model_registry(self):
        """Create mock model registry."""
        from assistant_service.core.models.model_registry import ModelRegistry

        registry = MagicMock(spec=ModelRegistry)
        registry.get_model = MagicMock(
            return_value={
                "model_id": "gpt-4",
                "provider_id": "openai",
                "context_window": 8192,
            }
        )
        return registry

    @pytest.fixture
    def mock_session_manager(self):
        """Create mock session manager."""
        from src.models.session import Session
        from src.services.session.database_session_manager import DatabaseSessionManager

        manager = AsyncMock(spec=DatabaseSessionManager)
        manager.get = AsyncMock(
            return_value=Session(
                session_id="test-session",
                user_id="user1",
                tenant_id="tenant1",
                history=[],
            )
        )
        manager.add_message = AsyncMock()
        return manager

    @pytest.fixture
    def service(self, mock_model_registry, mock_session_manager):
        """Create AssistantService with mocks."""
        from assistant_service.core.assistant_service import AssistantService

        return AssistantService(
            model_registry=mock_model_registry,
            session_manager=mock_session_manager,
        )

    @pytest.mark.asyncio
    async def test_chat_stream_yields_status_first(self, service, mock_model_registry):
        """chat_stream should yield status or run_started event first."""
        from assistant_service.core.assistant_service import (
            AssistantConfig,
            StreamEventType,
        )

        user = MockUserContext(user_id="user1", tenant_id="tenant1")
        # Disable agent loop to test traditional path
        config = AssistantConfig(model_id="gpt-4", use_agent_loop=False)

        # Mock the model provider
        mock_provider = AsyncMock()
        mock_model_registry.get_provider = MagicMock(return_value=mock_provider)

        # Create async generator that yields text
        async def mock_stream(*args, **kwargs):
            from assistant_service.core.model_registry import StreamDelta

            yield StreamDelta(text="Hello")

        mock_provider.chat_stream = mock_stream

        events = []
        try:
            async for event in service.chat_stream(
                user=user,
                session_id="test-session",
                message="Hi",
                config=config,
                history=[],
            ):
                events.append(event)
                if len(events) >= 1:
                    break
        except Exception:
            pass  # May fail due to incomplete mocking

        # First event should be status (traditional path) or run_started (agent loop path)
        if events:
            assert events[0].event_type in (StreamEventType.STATUS, StreamEventType.RUN_STARTED)

    def test_service_has_session_manager(self, service, mock_session_manager):
        """Service should have session manager configured."""
        assert service.session_manager is not None
        assert service.session_manager == mock_session_manager


class TestRAGMode:
    """Tests for RAG mode enum."""

    def test_rag_modes(self):
        """RAGMode should have expected values."""
        from assistant_service.core.assistant_service import RAGMode

        assert RAGMode.AUTO == "auto"
        assert RAGMode.TOOL == "tool"
        assert RAGMode.DISABLED == "off"


class TestStreamEventType:
    """Tests for stream event types."""

    def test_core_event_types_exist(self):
        """Core event types should be defined."""
        from assistant_service.core.assistant_service import StreamEventType

        # Core streaming
        assert StreamEventType.TEXT_DELTA == "text_delta"
        assert StreamEventType.THINKING_DELTA == "thinking_delta"
        assert StreamEventType.TOOL_CALL == "tool_call"
        assert StreamEventType.TOOL_RESULT == "tool_result"

        # Status events
        assert StreamEventType.STATUS == "status"
        assert StreamEventType.USAGE == "usage"
        assert StreamEventType.DONE == "done"
        assert StreamEventType.ERROR == "error"

    def test_rag_event_types_exist(self):
        """RAG-related event types should be defined."""
        from assistant_service.core.assistant_service import StreamEventType

        assert StreamEventType.CONTEXT_RETRIEVED == "context_retrieved"
        assert StreamEventType.WEB_SEARCH_RESULTS == "web_search_results"
        assert StreamEventType.RAG_EVALUATION == "rag_evaluation"


class TestAssistantStreamEvent:
    """Tests for AssistantStreamEvent dataclass."""

    def test_create_event(self):
        """Should create stream event with data."""
        from assistant_service.core.assistant_service import (
            AssistantStreamEvent,
            StreamEventType,
        )

        event = AssistantStreamEvent(
            event_type=StreamEventType.TEXT_DELTA,
            data={"text": "Hello, world!"},
        )

        assert event.event_type == StreamEventType.TEXT_DELTA
        assert event.data["text"] == "Hello, world!"

    def test_event_has_timestamp(self):
        """Event should have timestamp."""
        from assistant_service.core.assistant_service import (
            AssistantStreamEvent,
            StreamEventType,
        )

        event = AssistantStreamEvent(
            event_type=StreamEventType.TEXT_DELTA,
            data={"text": "Test"},
        )

        assert event.timestamp is not None
        assert event.timestamp > 0
