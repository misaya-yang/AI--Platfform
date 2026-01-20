"""
Tests for ModelRegistry - Unified LLM provider interface.

Tests model management, provider configuration, and access control.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

# Skip if tiktoken not available (required by assistant module)
pytest.importorskip("tiktoken", reason="tiktoken required for model registry tests")


class TestModelProvider:
    """Tests for ModelProvider enum."""

    def test_provider_values(self):
        """ModelProvider should have expected values."""
        from src.services.assistant.model_registry import ModelProvider

        assert ModelProvider.OPENAI == "openai"
        assert ModelProvider.ANTHROPIC == "anthropic"
        assert ModelProvider.DEEPSEEK == "deepseek"
        assert ModelProvider.DASHSCOPE == "dashscope"
        assert ModelProvider.GOOGLE == "google"


class TestModelAccessLevel:
    """Tests for ModelAccessLevel enum."""

    def test_access_levels(self):
        """ModelAccessLevel should have expected values."""
        from src.services.assistant.model_registry import ModelAccessLevel

        assert ModelAccessLevel.PUBLIC == "public"
        assert ModelAccessLevel.PREMIUM == "premium"
        assert ModelAccessLevel.ADMIN == "admin"


class TestModelInfo:
    """Tests for ModelInfo dataclass."""

    def test_create_model_info(self):
        """Should create ModelInfo with required fields."""
        from src.services.assistant.model_registry import (
            ModelInfo,
            ModelProvider,
            ModelAccessLevel,
        )

        model = ModelInfo(
            id="gpt-4o",
            name="GPT-4o",
            provider=ModelProvider.OPENAI,
        )

        assert model.id == "gpt-4o"
        assert model.name == "GPT-4o"
        assert model.provider == ModelProvider.OPENAI
        assert model.context_window == 128000  # default
        assert model.max_output_tokens == 4096  # default
        assert model.supports_vision is False
        assert model.supports_tools is True
        assert model.access_level == ModelAccessLevel.PUBLIC

    def test_model_info_with_custom_values(self):
        """Should accept custom configuration."""
        from src.services.assistant.model_registry import (
            ModelInfo,
            ModelProvider,
            ModelAccessLevel,
        )

        model = ModelInfo(
            id="claude-3-opus",
            name="Claude 3 Opus",
            provider=ModelProvider.ANTHROPIC,
            context_window=200000,
            max_output_tokens=8192,
            supports_vision=True,
            input_price_per_1k=0.015,
            output_price_per_1k=0.075,
            access_level=ModelAccessLevel.PREMIUM,
        )

        assert model.context_window == 200000
        assert model.max_output_tokens == 8192
        assert model.supports_vision is True
        assert model.input_price_per_1k == 0.015
        assert model.output_price_per_1k == 0.075
        assert model.access_level == ModelAccessLevel.PREMIUM


class TestModelRegistryInit:
    """Tests for ModelRegistry initialization."""

    def test_init_with_default_models(self):
        """Should initialize with default model catalog."""
        from src.services.assistant.model_registry import ModelRegistry

        registry = ModelRegistry(use_default_models=True)

        # Should have models from default catalog
        assert len(registry._models) > 0

    def test_init_without_default_models(self):
        """Should initialize empty when default models disabled."""
        from src.services.assistant.model_registry import ModelRegistry

        registry = ModelRegistry(use_default_models=False)

        assert len(registry._models) == 0


class TestModelRegistryProviderConfig:
    """Tests for provider configuration."""

    def test_configure_provider(self):
        """Should configure provider with API key."""
        from src.services.assistant.model_registry import ModelRegistry, ModelProvider

        registry = ModelRegistry(use_default_models=False)

        registry.configure_provider(
            provider=ModelProvider.OPENAI,
            api_key="sk-test-key",
        )

        assert registry.is_provider_configured(ModelProvider.OPENAI) is True
        assert registry.is_provider_configured(ModelProvider.ANTHROPIC) is False

    def test_configure_provider_with_custom_base_url(self):
        """Should allow custom base URL."""
        from src.services.assistant.model_registry import ModelRegistry, ModelProvider

        registry = ModelRegistry(use_default_models=False)

        registry.configure_provider(
            provider=ModelProvider.OPENAI,
            api_key="sk-test-key",
            base_url="https://custom-api.example.com",
        )

        config = registry._configs.get(ModelProvider.OPENAI)
        assert config is not None
        assert config.base_url == "https://custom-api.example.com"

    def test_unconfigured_provider(self):
        """Unconfigured provider should return False."""
        from src.services.assistant.model_registry import ModelRegistry, ModelProvider

        registry = ModelRegistry(use_default_models=False)

        assert registry.is_provider_configured(ModelProvider.DEEPSEEK) is False


class TestModelRegistryModelManagement:
    """Tests for model management."""

    def test_get_model(self):
        """Should retrieve model by ID."""
        from src.services.assistant.model_registry import (
            ModelRegistry,
            ModelInfo,
            ModelProvider,
        )

        registry = ModelRegistry(use_default_models=False)

        # Add a model
        model = ModelInfo(
            id="test-model",
            name="Test Model",
            provider=ModelProvider.OPENAI,
        )
        registry.add_custom_model(model)

        # Retrieve it
        retrieved = registry.get_model("test-model")
        assert retrieved is not None
        assert retrieved.id == "test-model"
        assert retrieved.name == "Test Model"

    def test_get_nonexistent_model(self):
        """Should return None for nonexistent model."""
        from src.services.assistant.model_registry import ModelRegistry

        registry = ModelRegistry(use_default_models=False)

        result = registry.get_model("nonexistent-model")
        assert result is None

    def test_add_custom_model(self):
        """Should add custom model to registry."""
        from src.services.assistant.model_registry import (
            ModelRegistry,
            ModelInfo,
            ModelProvider,
        )

        registry = ModelRegistry(use_default_models=False)

        model = ModelInfo(
            id="custom-llm",
            name="Custom LLM",
            provider=ModelProvider.OPENAI,
            context_window=32000,
        )
        registry.add_custom_model(model)

        assert "custom-llm" in registry._models
        assert registry._models["custom-llm"].context_window == 32000

    def test_clear_models(self):
        """Should clear all models from registry."""
        from src.services.assistant.model_registry import ModelRegistry

        registry = ModelRegistry(use_default_models=True)
        initial_count = len(registry._models)
        assert initial_count > 0

        registry.clear_models()

        assert len(registry._models) == 0
        assert registry._db_models_loaded is False


class TestModelRegistryAvailableModels:
    """Tests for available models filtering."""

    def test_get_available_models_configured_provider(self):
        """Should return models only from configured providers."""
        from src.services.assistant.model_registry import (
            ModelRegistry,
            ModelInfo,
            ModelProvider,
        )

        registry = ModelRegistry(use_default_models=False)

        # Add models from different providers
        registry.add_custom_model(ModelInfo(
            id="openai-model",
            name="OpenAI Model",
            provider=ModelProvider.OPENAI,
        ))
        registry.add_custom_model(ModelInfo(
            id="anthropic-model",
            name="Anthropic Model",
            provider=ModelProvider.ANTHROPIC,
        ))

        # Only configure OpenAI
        registry.configure_provider(
            provider=ModelProvider.OPENAI,
            api_key="sk-test",
        )

        available = registry.get_available_models()

        # Should only include OpenAI model
        model_ids = [m.id for m in available]
        assert "openai-model" in model_ids
        assert "anthropic-model" not in model_ids

    def test_get_available_models_no_configured_providers(self):
        """Should return empty list if no providers configured."""
        from src.services.assistant.model_registry import ModelRegistry

        registry = ModelRegistry(use_default_models=True)

        # No providers configured
        available = registry.get_available_models()

        assert len(available) == 0


class TestSanitizeUsage:
    """Tests for usage sanitization helper."""

    def test_sanitize_basic_usage(self):
        """Should pass through integer values."""
        from src.services.assistant.model_registry import _sanitize_usage

        raw = {"input_tokens": 100, "output_tokens": 50}
        result = _sanitize_usage(raw)

        assert result["input_tokens"] == 100
        assert result["output_tokens"] == 50

    def test_sanitize_openai_format(self):
        """Should normalize OpenAI-style keys."""
        from src.services.assistant.model_registry import _sanitize_usage

        raw = {"prompt_tokens": 100, "completion_tokens": 50}
        result = _sanitize_usage(raw)

        assert result["input_tokens"] == 100
        assert result["output_tokens"] == 50

    def test_sanitize_filters_nested_dicts(self):
        """Should filter out nested dictionaries."""
        from src.services.assistant.model_registry import _sanitize_usage

        raw = {
            "prompt_tokens": 100,
            "prompt_tokens_details": {"cached_tokens": 0},  # Should be filtered
            "completion_tokens": 50,
        }
        result = _sanitize_usage(raw)

        assert "prompt_tokens_details" not in result
        assert result["input_tokens"] == 100
        assert result["output_tokens"] == 50


class TestStreamDelta:
    """Tests for StreamDelta dataclass."""

    def test_create_stream_delta(self):
        """Should create StreamDelta with defaults."""
        from src.services.assistant.model_registry import StreamDelta

        delta = StreamDelta()

        assert delta.content == ""
        assert delta.tool_calls is None
        assert delta.finish_reason is None
        assert delta.usage is None

    def test_stream_delta_with_content(self):
        """Should create StreamDelta with content."""
        from src.services.assistant.model_registry import StreamDelta

        delta = StreamDelta(
            content="Hello, world!",
            finish_reason="stop",
            usage={"input_tokens": 10, "output_tokens": 5},
        )

        assert delta.content == "Hello, world!"
        assert delta.finish_reason == "stop"
        assert delta.usage["input_tokens"] == 10


class TestChatMessage:
    """Tests for ChatMessage dataclass."""

    def test_create_chat_message(self):
        """Should create ChatMessage with required fields."""
        from src.services.assistant.model_registry import ChatMessage

        message = ChatMessage(role="user", content="Hello")

        assert message.role == "user"
        assert message.content == "Hello"
        assert message.name is None
        assert message.tool_calls is None
        assert message.images is None

    def test_chat_message_with_images(self):
        """Should support vision input."""
        from src.services.assistant.model_registry import ChatMessage

        message = ChatMessage(
            role="user",
            content="Describe this image",
            images=["data:image/png;base64,ABC123"],
        )

        assert len(message.images) == 1

    def test_chat_message_with_tool_calls(self):
        """Should support tool calls."""
        from src.services.assistant.model_registry import ChatMessage

        message = ChatMessage(
            role="assistant",
            content="",
            tool_calls=[{
                "id": "call_123",
                "type": "function",
                "function": {"name": "get_weather", "arguments": "{}"},
            }],
        )

        assert len(message.tool_calls) == 1
        assert message.tool_calls[0]["function"]["name"] == "get_weather"
