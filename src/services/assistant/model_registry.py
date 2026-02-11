"""
Model Registry - Unified interface for multiple LLM providers.

Supports:
- OpenAI (GPT-4o, GPT-4o-mini, etc.)
- Anthropic (Claude 3.5 Sonnet, Claude 3 Opus, etc.)
- DeepSeek (deepseek-chat, deepseek-reasoner)
- DashScope/Qwen (qwen-turbo, qwen-plus, qwen-max)
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import Enum
from typing import Any

import httpx

from ...core.observability.logging import get_logger

logger = get_logger(__name__)


class ModelProvider(str, Enum):
    """Supported LLM providers."""

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    DEEPSEEK = "deepseek"
    DASHSCOPE = "dashscope"
    GOOGLE = "google"


class ModelAccessLevel(str, Enum):
    """Model access permission levels."""

    PUBLIC = "public"  # Available to all authenticated users
    PREMIUM = "premium"  # Available to premium/paid users only
    ADMIN = "admin"  # Admin-only models (expensive or experimental)


@dataclass
class ModelInfo:
    """Model metadata."""

    id: str
    name: str
    provider: ModelProvider
    context_window: int = 128000
    max_output_tokens: int = 4096
    supports_vision: bool = False
    supports_tools: bool = True
    input_price_per_1k: float = 0.0  # USD per 1K tokens
    output_price_per_1k: float = 0.0
    access_level: ModelAccessLevel = ModelAccessLevel.PUBLIC  # Permission level required


def _sanitize_usage(raw_usage: dict[str, Any]) -> dict[str, int]:
    """
    Sanitize and normalize usage dict.

    - Only include integer values (filter out nested dicts)
    - Normalize OpenAI keys (prompt_tokens -> input_tokens, completion_tokens -> output_tokens)

    Some providers (e.g., DashScope) return nested dicts like:
    {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 0}}
    """
    result: dict[str, int] = {}
    for k, v in raw_usage.items():
        if not isinstance(v, int):
            continue
        # Normalize OpenAI-style keys to standard format
        if k == "prompt_tokens":
            result["input_tokens"] = v
        elif k == "completion_tokens":
            result["output_tokens"] = v
        else:
            result[k] = v
    return result


@dataclass
class StreamDelta:
    """A single streaming delta from the model."""

    content: str = ""
    tool_calls: list[dict[str, Any]] | None = None
    finish_reason: str | None = None
    usage: dict[str, int] | None = None
    thought_signature: str | None = None  # Gemini 3 thought signature


@dataclass
class ChatMessage:
    """A chat message."""

    role: str  # system, user, assistant, tool
    content: str
    name: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    images: list[str] | None = None  # Base64 or URLs for vision models
    thought_signature: str | None = None  # Gemini 3 thought signature

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChatMessage:
        """Create ChatMessage from dictionary."""
        return cls(
            role=data.get("role", "user"),
            content=data.get("content", ""),
            name=data.get("name"),
            tool_calls=data.get("tool_calls"),
            tool_call_id=data.get("tool_call_id"),
            images=data.get("images"),
            thought_signature=data.get("thought_signature"),
        )


def _normalize_message(msg) -> ChatMessage:
    """Convert message to ChatMessage if it's a dict."""
    if isinstance(msg, ChatMessage):
        return msg
    elif isinstance(msg, dict):
        return ChatMessage.from_dict(msg)
    else:
        raise TypeError(f"Expected ChatMessage or dict, got {type(msg)}")


@dataclass
class ModelConfig:
    """Configuration for a model provider."""

    api_key: str
    base_url: str | None = None
    timeout: float = 300.0
    max_retries: int = 2


# Default model catalog
DEFAULT_MODELS: dict[ModelProvider, list[ModelInfo]] = {
    ModelProvider.OPENAI: [
        ModelInfo(
            id="gpt-4o",
            name="GPT-4o",
            provider=ModelProvider.OPENAI,
            context_window=128000,
            max_output_tokens=16384,
            supports_vision=True,
            input_price_per_1k=0.0025,
            output_price_per_1k=0.01,
        ),
        ModelInfo(
            id="gpt-4o-mini",
            name="GPT-4o Mini",
            provider=ModelProvider.OPENAI,
            context_window=128000,
            max_output_tokens=16384,
            supports_vision=True,
            input_price_per_1k=0.00015,
            output_price_per_1k=0.0006,
        ),
        ModelInfo(
            id="o1",
            name="O1",
            provider=ModelProvider.OPENAI,
            context_window=200000,
            max_output_tokens=100000,
            supports_vision=True,
            input_price_per_1k=0.015,
            output_price_per_1k=0.06,
        ),
        ModelInfo(
            id="o1-mini",
            name="O1 Mini",
            provider=ModelProvider.OPENAI,
            context_window=128000,
            max_output_tokens=65536,
            supports_vision=False,
            input_price_per_1k=0.0011,
            output_price_per_1k=0.0044,
        ),
    ],
    ModelProvider.ANTHROPIC: [
        ModelInfo(
            id="claude-sonnet-4-20250514",
            name="Claude Sonnet 4",
            provider=ModelProvider.ANTHROPIC,
            context_window=200000,
            max_output_tokens=64000,
            supports_vision=True,
            input_price_per_1k=0.003,
            output_price_per_1k=0.015,
        ),
        ModelInfo(
            id="claude-3-5-sonnet-20241022",
            name="Claude 3.5 Sonnet",
            provider=ModelProvider.ANTHROPIC,
            context_window=200000,
            max_output_tokens=8192,
            supports_vision=True,
            input_price_per_1k=0.003,
            output_price_per_1k=0.015,
        ),
        ModelInfo(
            id="claude-3-5-haiku-20241022",
            name="Claude 3.5 Haiku",
            provider=ModelProvider.ANTHROPIC,
            context_window=200000,
            max_output_tokens=8192,
            supports_vision=True,
            input_price_per_1k=0.0008,
            output_price_per_1k=0.004,
        ),
    ],
    ModelProvider.DEEPSEEK: [
        ModelInfo(
            id="deepseek-chat",
            name="DeepSeek Chat",
            provider=ModelProvider.DEEPSEEK,
            context_window=64000,
            max_output_tokens=8192,
            supports_vision=False,
            input_price_per_1k=0.00028,
            output_price_per_1k=0.00042,
        ),
        ModelInfo(
            id="deepseek-reasoner",
            name="DeepSeek Reasoner (R1)",
            provider=ModelProvider.DEEPSEEK,
            context_window=64000,
            max_output_tokens=8192,
            supports_vision=False,
            input_price_per_1k=0.00028,
            output_price_per_1k=0.00042,
        ),
    ],
    ModelProvider.DASHSCOPE: [
        ModelInfo(
            id="qwen-turbo",
            name="Qwen Turbo",
            provider=ModelProvider.DASHSCOPE,
            context_window=131072,
            max_output_tokens=8192,
            supports_vision=False,
            input_price_per_1k=0.0003,
            output_price_per_1k=0.0006,
        ),
        ModelInfo(
            id="qwen-plus",
            name="Qwen Plus",
            provider=ModelProvider.DASHSCOPE,
            context_window=131072,
            max_output_tokens=8192,
            supports_vision=False,
            input_price_per_1k=0.0004,
            output_price_per_1k=0.0012,
        ),
        ModelInfo(
            id="qwen-max",
            name="Qwen Max",
            provider=ModelProvider.DASHSCOPE,
            context_window=32768,
            max_output_tokens=8192,
            supports_vision=False,
            input_price_per_1k=0.0012,
            output_price_per_1k=0.006,
        ),
        ModelInfo(
            id="qwen-vl-max",
            name="Qwen VL Max",
            provider=ModelProvider.DASHSCOPE,
            context_window=32768,
            max_output_tokens=8192,
            supports_vision=True,
            input_price_per_1k=0.00023,
            output_price_per_1k=0.000574,
        ),
    ],
    ModelProvider.GOOGLE: [
        # Gemini 3.0 系列 (2025年1月发布) - 高端模型，仅限管理员
        ModelInfo(
            id="gemini-3-pro-preview",
            name="Gemini 3 Pro",
            provider=ModelProvider.GOOGLE,
            context_window=1000000,
            max_output_tokens=65536,
            supports_vision=True,
            input_price_per_1k=0.002,  # $2 per 1M = $0.002 per 1K
            output_price_per_1k=0.012,  # $12 per 1M = $0.012 per 1K
            access_level=ModelAccessLevel.ADMIN,  # 高价模型，仅管理员可用
        ),
        ModelInfo(
            id="gemini-3-flash-preview",
            name="Gemini 3 Flash",
            provider=ModelProvider.GOOGLE,
            context_window=1000000,
            max_output_tokens=65536,
            supports_vision=True,
            input_price_per_1k=0.0005,  # $0.50 per 1M
            output_price_per_1k=0.003,  # $3 per 1M
            access_level=ModelAccessLevel.ADMIN,  # 管理员可用
        ),
        # Gemini 2.5 系列 (稳定版)
        ModelInfo(
            id="gemini-2.5-pro",
            name="Gemini 2.5 Pro",
            provider=ModelProvider.GOOGLE,
            context_window=1000000,
            max_output_tokens=65536,
            supports_vision=True,
            input_price_per_1k=0.00125,  # $1.25 per 1M
            output_price_per_1k=0.01,  # $10 per 1M
            access_level=ModelAccessLevel.ADMIN,  # 高价模型
        ),
        ModelInfo(
            id="gemini-2.5-flash",
            name="Gemini 2.5 Flash",
            provider=ModelProvider.GOOGLE,
            context_window=1000000,
            max_output_tokens=65536,
            supports_vision=True,
            input_price_per_1k=0.0003,  # $0.30 per 1M
            output_price_per_1k=0.0025,  # $2.50 per 1M
            access_level=ModelAccessLevel.PREMIUM,  # 性价比较高，付费用户可用
        ),
        ModelInfo(
            id="gemini-2.5-flash-lite",
            name="Gemini 2.5 Flash Lite",
            provider=ModelProvider.GOOGLE,
            context_window=1000000,
            max_output_tokens=65536,
            supports_vision=True,
            input_price_per_1k=0.0001,
            output_price_per_1k=0.0004,
            access_level=ModelAccessLevel.PUBLIC,  # 最便宜的Google模型，普通用户可用
        ),
    ],
}


class ModelRegistry:
    """
    Registry for managing multiple LLM providers.

    Provides a unified interface for:
    - Model discovery and metadata
    - Chat completions (streaming and non-streaming)
    - Provider-specific API handling
    """

    # Default base URLs for each provider
    DEFAULT_BASE_URLS = {
        ModelProvider.OPENAI: "https://api.openai.com",
        ModelProvider.ANTHROPIC: "https://api.anthropic.com",
        ModelProvider.DEEPSEEK: "https://api.deepseek.com",
        ModelProvider.DASHSCOPE: "https://dashscope.aliyuncs.com/compatible-mode",
        ModelProvider.GOOGLE: "https://generativelanguage.googleapis.com",
    }

    def __init__(self, use_default_models: bool = True):
        self._configs: dict[ModelProvider, ModelConfig] = {}
        self._models: dict[str, ModelInfo] = {}
        self._clients: dict[ModelProvider, httpx.AsyncClient] = {}
        self._db_models_loaded: bool = False

        # Initialize default model catalog (fallback)
        if use_default_models:
            for _provider, models in DEFAULT_MODELS.items():
                for model in models:
                    self._models[model.id] = model

    async def load_models_from_database(self, model_service, tenant_id: str = "default") -> int:
        """
        Load models from database, replacing default models.

        Args:
            model_service: ModelService instance
            tenant_id: Tenant ID to load models for

        Returns:
            Number of models loaded
        """
        try:
            db_models = await model_service.list_models(
                tenant_id=tenant_id,
                include_disabled=False,
            )

            if not db_models:
                logger.info("No models in database, keeping default catalog")
                return 0

            # Clear existing models and load from database
            self._models.clear()
            loaded_count = 0
            for row in db_models:
                try:
                    # Map provider_id to ModelProvider enum
                    provider_id = row.get("provider_id", "")
                    try:
                        provider = ModelProvider(provider_id)
                    except ValueError:
                        # Unknown provider, skip
                        logger.warning(
                            f"Unknown provider {provider_id} for model {row.get('model_id')}"
                        )
                        continue

                    # Map access_level string to enum
                    access_str = row.get("access_level", "public")
                    try:
                        access_level = ModelAccessLevel(access_str)
                    except ValueError:
                        access_level = ModelAccessLevel.PUBLIC

                    model = ModelInfo(
                        id=row["model_id"],
                        name=row["display_name"],
                        provider=provider,
                        context_window=row.get("context_window", 128000),
                        max_output_tokens=row.get("max_output_tokens", 4096),
                        supports_vision=row.get("supports_vision", False),
                        supports_tools=row.get("supports_tools", True),
                        input_price_per_1k=float(row.get("input_price_per_1k", 0)),
                        output_price_per_1k=float(row.get("output_price_per_1k", 0)),
                        access_level=access_level,
                    )
                    self._models[model.id] = model
                    loaded_count += 1
                except Exception as e:
                    logger.warning(f"Failed to load model {row.get('model_id')}: {e}")

            self._db_models_loaded = True
            logger.info(f"Loaded {loaded_count} models from database")
            return loaded_count

        except Exception as e:
            logger.warning(f"Failed to load models from database: {e}")
            return 0

    def clear_models(self) -> None:
        """Clear all models from registry."""
        self._models.clear()
        self._db_models_loaded = False

    def configure_provider(
        self,
        provider: ModelProvider,
        api_key: str,
        base_url: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        """Configure a provider with API credentials."""
        self._configs[provider] = ModelConfig(
            api_key=api_key,
            base_url=base_url or self.DEFAULT_BASE_URLS.get(provider),
            timeout=timeout,
        )
        # Reset client if exists
        if provider in self._clients:
            # Don't close here to avoid async issues
            del self._clients[provider]

    def is_provider_configured(self, provider: ModelProvider) -> bool:
        """Check if a provider is configured."""
        return provider in self._configs and bool(self._configs[provider].api_key)

    def get_available_models(self) -> list[ModelInfo]:
        """Get all models from configured providers."""
        available = []
        for model in self._models.values():
            if self.is_provider_configured(model.provider):
                available.append(model)
        return available

    def get_model(self, model_id: str) -> ModelInfo | None:
        """Get model info by ID."""
        return self._models.get(model_id)

    def add_custom_model(self, model: ModelInfo) -> None:
        """Add a custom model to the registry."""
        self._models[model.id] = model

    async def _get_client(self, provider: ModelProvider) -> httpx.AsyncClient:
        """Get or create HTTP client for provider."""
        if provider not in self._clients:
            config = self._configs.get(provider)
            if not config:
                raise ValueError(f"Provider {provider} not configured")

            headers = self._build_headers(provider, config.api_key)
            self._clients[provider] = httpx.AsyncClient(
                base_url=config.base_url,
                headers=headers,
                timeout=httpx.Timeout(config.timeout),
            )
        return self._clients[provider]

    def _build_headers(self, provider: ModelProvider, api_key: str) -> dict[str, str]:
        """Build headers for API requests."""
        if provider == ModelProvider.ANTHROPIC:
            return {
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
        elif provider == ModelProvider.GOOGLE:
            # Google uses API key in URL, not header
            return {
                "Content-Type": "application/json",
            }
        else:
            # OpenAI-compatible (OpenAI, DeepSeek, DashScope)
            return {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }

    def _build_request_body(
        self,
        provider: ModelProvider,
        model_id: str,
        messages: list[ChatMessage],
        temperature: float = 0.7,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        thinking_level: str | None = None,
        tool_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build request body for the provider's API."""
        if provider == ModelProvider.ANTHROPIC:
            return self._build_anthropic_body(
                model_id, messages, temperature, max_tokens, tools, stream
            )
        elif provider == ModelProvider.GOOGLE:
            return self._build_google_body(
                model_id,
                messages,
                temperature,
                max_tokens,
                tools,
                stream,
                thinking_level,
                tool_config,
            )
        else:
            return self._build_openai_body(
                model_id, messages, temperature, max_tokens, tools, stream
            )

    def _build_openai_body(
        self,
        model_id: str,
        messages: list[ChatMessage],
        temperature: float,
        max_tokens: int | None,
        tools: list[dict[str, Any]] | None,
        stream: bool,
    ) -> dict[str, Any]:
        """Build OpenAI-compatible request body."""
        formatted_messages = []
        for raw_msg in messages:
            msg = _normalize_message(raw_msg)
            m: dict[str, Any] = {"role": msg.role, "content": msg.content}
            if msg.name:
                m["name"] = msg.name
            if msg.tool_calls:
                m["tool_calls"] = msg.tool_calls
            if msg.tool_call_id:
                m["tool_call_id"] = msg.tool_call_id
            # Handle vision content
            if msg.images and msg.role == "user":
                content_parts = [{"type": "text", "text": msg.content}]
                for img in msg.images:
                    if img.startswith("http") or img.startswith("data:"):
                        # URL or data URL - use as-is
                        content_parts.append({"type": "image_url", "image_url": {"url": img}})
                    else:
                        # Raw base64 - assume jpeg
                        content_parts.append(
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/jpeg;base64,{img}"},
                            }
                        )
                m["content"] = content_parts
            formatted_messages.append(m)

        body: dict[str, Any] = {
            "model": model_id,
            "messages": formatted_messages,
            "temperature": temperature,
            "stream": stream,
        }
        if max_tokens:
            body["max_tokens"] = max_tokens
        if tools:
            body["tools"] = tools
        if stream:
            body["stream_options"] = {"include_usage": True}
        return body

    def _build_anthropic_body(
        self,
        model_id: str,
        messages: list[ChatMessage],
        temperature: float,
        max_tokens: int | None,
        tools: list[dict[str, Any]] | None,
        stream: bool,
    ) -> dict[str, Any]:
        """Build Anthropic-specific request body."""
        system_prompt = None
        formatted_messages = []

        for raw_msg in messages:
            msg = _normalize_message(raw_msg)
            if msg.role == "system":
                system_prompt = msg.content
                continue

            m: dict[str, Any] = {"role": msg.role}

            # Handle vision content
            if msg.images and msg.role == "user":
                content_parts = []
                for img in msg.images:
                    if img.startswith("http"):
                        # Anthropic supports URL source
                        content_parts.append(
                            {
                                "type": "image",
                                "source": {
                                    "type": "url",
                                    "url": img,
                                },
                            }
                        )
                    elif img.startswith("data:"):
                        # Parse data URL: data:{mime_type};base64,{base64_data}
                        try:
                            header, base64_data = img.split(",", 1)
                            media_type = header.split(":")[1].split(";")[0]
                        except (ValueError, IndexError):
                            media_type = "image/jpeg"
                            base64_data = img
                        content_parts.append(
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": media_type,
                                    "data": base64_data,
                                },
                            }
                        )
                    else:
                        # Raw base64 - assume jpeg
                        content_parts.append(
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/jpeg",
                                    "data": img,
                                },
                            }
                        )
                content_parts.append({"type": "text", "text": msg.content})
                m["content"] = content_parts
            else:
                m["content"] = msg.content

            formatted_messages.append(m)

        body: dict[str, Any] = {
            "model": model_id,
            "messages": formatted_messages,
            "temperature": temperature,
            "max_tokens": max_tokens or 4096,
            "stream": stream,
        }
        if system_prompt:
            body["system"] = system_prompt
        if tools:
            # Convert OpenAI tool format to Anthropic format
            anthropic_tools = []
            for tool in tools:
                if tool.get("type") == "function":
                    func = tool["function"]
                    anthropic_tools.append(
                        {
                            "name": func["name"],
                            "description": func.get("description", ""),
                            "input_schema": func.get(
                                "parameters", {"type": "object", "properties": {}}
                            ),
                        }
                    )
            if anthropic_tools:
                body["tools"] = anthropic_tools
        return body

    def _build_google_body(
        self,
        model_id: str,
        messages: list[ChatMessage],
        temperature: float,
        max_tokens: int | None,
        tools: list[dict[str, Any]] | None,
        stream: bool,
        thinking_level: str | None = None,
        tool_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build Google Gemini API request body."""
        contents = []
        system_instruction = None

        for raw_msg in messages:
            msg = _normalize_message(raw_msg)
            if msg.role == "system":
                system_instruction = msg.content
                continue

            # Handle tool result messages (functionResponse)
            if msg.role == "tool" and msg.tool_call_id:
                # Parse function name from tool_call_id (format: "call_<name>")
                func_name = msg.name or msg.tool_call_id
                if func_name.startswith("call_"):
                    func_name = func_name[5:]

                # Try to parse content as JSON, otherwise wrap as object
                try:
                    response_data = json.loads(msg.content) if msg.content else {}
                except json.JSONDecodeError:
                    response_data = {"result": msg.content}

                contents.append(
                    {
                        "role": "user",  # Google uses "user" role for function responses
                        "parts": [
                            {"functionResponse": {"name": func_name, "response": response_data}}
                        ],
                    }
                )
                continue

            role = "user" if msg.role == "user" else "model"
            parts = []

            # Handle assistant messages with tool_calls (functionCall)
            if msg.role == "assistant":
                # Add text content first if present
                if msg.content:
                    text_part = {"text": msg.content}
                    # Attach thoughtSignature to text part if present
                    if msg.thought_signature:
                        text_part["thoughtSignature"] = msg.thought_signature
                    parts.append(text_part)

                # Add function calls with thoughtSignature (CRITICAL for Gemini 3)
                if msg.tool_calls:
                    for tc in msg.tool_calls:
                        func = tc.get("function", {})
                        func_name = func.get("name", "")

                        # Parse arguments
                        try:
                            args = json.loads(func.get("arguments", "{}"))
                        except json.JSONDecodeError:
                            args = {}

                        func_call_part: dict[str, Any] = {
                            "functionCall": {"name": func_name, "args": args}
                        }

                        # CRITICAL: Include thoughtSignature if present (required for Gemini 3)
                        if "thoughtSignature" in tc:
                            func_call_part["thoughtSignature"] = tc["thoughtSignature"]
                            logger.debug(f"[GEMINI3] Including thoughtSignature for {func_name}")

                        parts.append(func_call_part)

                # If only thoughtSignature is present without content or tool calls (unlikely but possible)
                if not msg.content and not msg.tool_calls and msg.thought_signature:
                    parts.append({"text": "", "thoughtSignature": msg.thought_signature})

                if parts:
                    contents.append({"role": role, "parts": parts})
                continue

            # Handle vision content
            if msg.images and msg.role == "user":
                for img in msg.images:
                    if img.startswith("http"):
                        # Infer mime type from URL extension
                        mime_type = "image/jpeg"
                        if ".png" in img.lower():
                            mime_type = "image/png"
                        elif ".gif" in img.lower():
                            mime_type = "image/gif"
                        elif ".webp" in img.lower():
                            mime_type = "image/webp"
                        parts.append({"fileData": {"fileUri": img, "mimeType": mime_type}})
                    elif img.startswith("data:"):
                        # Parse data URL: data:{mime_type};base64,{base64_data}
                        try:
                            header, base64_data = img.split(",", 1)
                            mime_type = header.split(":")[1].split(";")[0]
                        except (ValueError, IndexError):
                            mime_type = "image/jpeg"
                            base64_data = img
                        # Gemini REST API uses camelCase
                        parts.append({"inlineData": {"mimeType": mime_type, "data": base64_data}})
                    else:
                        # Raw base64 data, assume jpeg
                        parts.append({"inlineData": {"mimeType": "image/jpeg", "data": img}})
                parts.append({"text": msg.content})
            else:
                parts.append({"text": msg.content})

            contents.append({"role": role, "parts": parts})

        body: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens or 8192,
            },
        }

        if thinking_level:
            body["generationConfig"]["thinkingConfig"] = {"thinkingLevel": thinking_level}

        if system_instruction:
            body["systemInstruction"] = {"parts": [{"text": system_instruction}]}

        if tools:
            # Convert OpenAI tool format to Google format
            google_tools = []
            function_declarations = []
            for tool in tools:
                if tool.get("type") == "function":
                    func = tool["function"]
                    function_declarations.append(
                        {
                            "name": func["name"],
                            "description": func.get("description", ""),
                            "parameters": func.get(
                                "parameters", {"type": "object", "properties": {}}
                            ),
                        }
                    )
            if function_declarations:
                google_tools.append({"functionDeclarations": function_declarations})
                body["tools"] = google_tools

        # Apply tool_config if provided
        if tool_config:
            body["toolConfig"] = tool_config

        return body

    async def chat(
        self,
        model_id: str,
        messages: list[ChatMessage],
        temperature: float = 0.7,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        thinking_level: str | None = None,
    ) -> tuple[str, dict[str, int]]:
        """
        Non-streaming chat completion.

        Returns:
            Tuple of (response_content, usage_dict)
        """
        model = self.get_model(model_id)
        if not model:
            raise ValueError(f"Unknown model: {model_id}")

        client = await self._get_client(model.provider)
        body = self._build_request_body(
            model.provider,
            model_id,
            messages,
            temperature,
            max_tokens,
            tools,
            stream=False,
            thinking_level=thinking_level,
        )

        if model.provider == ModelProvider.GOOGLE:
            # Google API uses different endpoint format
            config = self._configs.get(model.provider)
            endpoint = f"/v1beta/models/{model_id}:generateContent?key={config.api_key}"
        elif model.provider == ModelProvider.ANTHROPIC:
            endpoint = "/v1/messages"
        else:
            endpoint = "/v1/chat/completions"

        response = await client.post(endpoint, json=body)
        response.raise_for_status()
        data = response.json()

        if model.provider == ModelProvider.GOOGLE:
            # Parse Google Gemini response
            content = ""
            candidates = data.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                for part in parts:
                    if "text" in part:
                        content += part["text"]
            usage_meta = data.get("usageMetadata", {})
            usage = {
                "input_tokens": usage_meta.get("promptTokenCount", 0),
                "output_tokens": usage_meta.get("candidatesTokenCount", 0),
            }
        elif model.provider == ModelProvider.ANTHROPIC:
            content = ""
            for block in data.get("content", []):
                if block.get("type") == "text":
                    content += block.get("text", "")
            usage = {
                "input_tokens": data.get("usage", {}).get("input_tokens", 0),
                "output_tokens": data.get("usage", {}).get("output_tokens", 0),
            }
        else:
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            usage = _sanitize_usage(data.get("usage", {}))

        return content, usage

    async def chat_stream(
        self,
        model_id: str,
        messages: list[ChatMessage],
        temperature: float = 0.7,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        thinking_level: str | None = None,
        tool_config: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamDelta]:
        """
        Streaming chat completion.

        Yields StreamDelta objects with incremental content.
        """
        model = self.get_model(model_id)
        if not model:
            raise ValueError(f"Unknown model: {model_id}")

        client = await self._get_client(model.provider)
        body = self._build_request_body(
            model.provider,
            model_id,
            messages,
            temperature,
            max_tokens,
            tools,
            stream=True,
            thinking_level=thinking_level,
            tool_config=tool_config,
        )

        if model.provider == ModelProvider.GOOGLE:
            config = self._configs.get(model.provider)
            endpoint = (
                f"/v1beta/models/{model_id}:streamGenerateContent?key={config.api_key}&alt=sse"
            )
            async for delta in self._stream_google(client, endpoint, body):
                yield delta
        elif model.provider == ModelProvider.ANTHROPIC:
            endpoint = "/v1/messages"
            async for delta in self._stream_anthropic(client, endpoint, body):
                yield delta
        else:
            endpoint = "/v1/chat/completions"
            async for delta in self._stream_openai(client, endpoint, body):
                yield delta

    async def _stream_openai(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
        body: dict[str, Any],
    ) -> AsyncIterator[StreamDelta]:
        """Stream from OpenAI-compatible API."""
        async with client.stream("POST", endpoint, json=body) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    evt = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                # Handle usage - can appear in final chunk alongside choices
                usage_data = None
                if isinstance(evt.get("usage"), dict):
                    usage_data = _sanitize_usage(evt["usage"])
                    logger.debug(f"[USAGE] Received usage data: {usage_data}")

                # Safely get choices - may be empty list or missing
                choices = evt.get("choices", [])
                if not choices:
                    # No choices in this event, only yield if we have usage data
                    if usage_data:
                        yield StreamDelta(usage=usage_data)
                    continue

                choice = choices[0]
                delta = choice.get("delta", {})
                finish_reason = choice.get("finish_reason")

                # Yield delta with all available info (content, tool_calls, finish_reason, usage)
                yield StreamDelta(
                    content=delta.get("content", ""),
                    tool_calls=delta.get("tool_calls"),
                    finish_reason=finish_reason,
                    usage=usage_data,
                )

    async def _stream_anthropic(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
        body: dict[str, Any],
    ) -> AsyncIterator[StreamDelta]:
        """Stream from Anthropic API."""
        async with client.stream("POST", endpoint, json=body) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                try:
                    evt = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                evt_type = evt.get("type")

                if evt_type == "content_block_delta":
                    delta = evt.get("delta", {})
                    if delta.get("type") == "text_delta":
                        yield StreamDelta(content=delta.get("text", ""))
                    elif delta.get("type") == "input_json_delta":
                        # Tool call arguments
                        pass

                elif evt_type == "message_delta":
                    usage = evt.get("usage", {})
                    yield StreamDelta(
                        finish_reason=evt.get("delta", {}).get("stop_reason"),
                        usage={
                            "output_tokens": usage.get("output_tokens", 0),
                        },
                    )

                elif evt_type == "message_start":
                    usage = evt.get("message", {}).get("usage", {})
                    if usage.get("input_tokens"):
                        yield StreamDelta(usage={"input_tokens": usage["input_tokens"]})

    async def _stream_google(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
        body: dict[str, Any],
    ) -> AsyncIterator[StreamDelta]:
        """Stream from Google Gemini API."""
        # Debug: Log request body for troubleshooting
        import json as json_module

        tool_names = []
        for t in body.get("tools", []):
            for fd in t.get("functionDeclarations", []):
                tool_names.append(fd.get("name", "unknown"))
        logger.info(f"[GEMINI] Tools in request: {tool_names}")
        logger.debug(
            f"[GEMINI] Request body: {json_module.dumps(body, ensure_ascii=False, default=str)[:2000]}"
        )

        async with client.stream("POST", endpoint, json=body) as response:
            if response.status_code != 200:
                # Read error response body for debugging
                error_body = await response.aread()
                logger.error(
                    f"[GEMINI] Error response ({response.status_code}): {error_body.decode('utf-8', errors='replace')}"
                )
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if not data_str:
                    continue
                try:
                    evt = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                # Parse Google Gemini streaming response
                candidates = evt.get("candidates", [])

                # Check for promptFeedback (Safety blocking)
                prompt_feedback = evt.get("promptFeedback")
                if prompt_feedback and prompt_feedback.get("blockReason"):
                    block_reason = prompt_feedback.get("blockReason")
                    logger.warning(f"[GEMINI] Response blocked: {block_reason}")
                    yield StreamDelta(
                        finish_reason="safety",
                        content=f"\n\n[System: Response blocked due to safety reason: {block_reason}]",
                    )
                    continue

                if candidates:
                    candidate = candidates[0]
                    content = candidate.get("content", {})
                    parts = content.get("parts", [])

                    # Check for finishReason in candidate even if parts are empty
                    finish_reason = candidate.get("finishReason")

                    if not parts and finish_reason:
                        # Handle case where only finishReason is sent (e.g. SAFETY, STOP)
                        yield StreamDelta(finish_reason=finish_reason.lower())

                    tool_calls_batch = []
                    for part in parts:
                        if "text" in part:
                            yield StreamDelta(content=part["text"])
                        elif "functionCall" in part:
                            # Gemini 3 function call with optional thoughtSignature
                            fc = part["functionCall"]
                            # IMPORTANT: tool_call ids must be unique per call for the assistant UI.
                            # Gemini streaming does not provide a stable unique call id, so we generate one.
                            import uuid

                            tool_call: dict[str, Any] = {
                                "id": f"call_{fc.get('name', 'unknown')}_{uuid.uuid4().hex[:10]}",
                                "type": "function",
                                "function": {
                                    "name": fc.get("name"),
                                    "arguments": json.dumps(fc.get("args", {})),
                                },
                            }
                            # CRITICAL: Preserve thoughtSignature for Gemini 3
                            # This must be passed back in subsequent requests
                            if "thoughtSignature" in part:
                                tool_call["thoughtSignature"] = part["thoughtSignature"]
                                logger.debug(
                                    f"[GEMINI3] Captured thoughtSignature for {fc.get('name')}"
                                )
                            tool_calls_batch.append(tool_call)

                        # Capture standalone thoughtSignature if present (rare but possible)
                        elif "thoughtSignature" in part and "functionCall" not in part:
                            logger.debug("[GEMINI3] Captured standalone thoughtSignature")
                            ts = part["thoughtSignature"]
                            # If we have text content in the same part (which shouldn't happen based on API structure, but to be safe)
                            # Or if we want to yield it attached to text
                            yield StreamDelta(thought_signature=ts)

                    # Yield all tool calls together
                    if tool_calls_batch:
                        yield StreamDelta(tool_calls=tool_calls_batch)

                    # Check finish reason
                    if finish_reason:
                        yield StreamDelta(finish_reason=finish_reason.lower())

                else:
                    # No candidates - could be usage metadata only or keep-alive
                    pass

                # Handle usage metadata
                usage_meta = evt.get("usageMetadata", {})
                if usage_meta:
                    usage = {}
                    if "promptTokenCount" in usage_meta:
                        usage["input_tokens"] = usage_meta["promptTokenCount"]
                    if "candidatesTokenCount" in usage_meta:
                        usage["output_tokens"] = usage_meta["candidatesTokenCount"]
                    if usage:
                        yield StreamDelta(usage=usage)

    async def close(self) -> None:
        """Close all HTTP clients."""
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()
