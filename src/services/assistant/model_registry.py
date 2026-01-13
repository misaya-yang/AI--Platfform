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
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import httpx

from ...core.observability.logging import get_logger

logger = get_logger(__name__)


class ModelProvider(str, Enum):
    """Supported LLM providers."""
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    DEEPSEEK = "deepseek"
    DASHSCOPE = "dashscope"


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


def _sanitize_usage(raw_usage: Dict[str, Any]) -> Dict[str, int]:
    """
    Sanitize usage dict to only include integer values.

    Some providers (e.g., DashScope) return nested dicts like:
    {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 0}}

    We only keep top-level int fields for compatibility.
    """
    return {k: v for k, v in raw_usage.items() if isinstance(v, int)}


@dataclass
class StreamDelta:
    """A single streaming delta from the model."""
    content: str = ""
    tool_calls: Optional[List[Dict[str, Any]]] = None
    finish_reason: Optional[str] = None
    usage: Optional[Dict[str, int]] = None


@dataclass
class ChatMessage:
    """A chat message."""
    role: str  # system, user, assistant, tool
    content: str
    name: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None
    images: Optional[List[str]] = None  # Base64 or URLs for vision models


@dataclass
class ModelConfig:
    """Configuration for a model provider."""
    api_key: str
    base_url: Optional[str] = None
    timeout: float = 120.0
    max_retries: int = 2


# Default model catalog
DEFAULT_MODELS: Dict[ModelProvider, List[ModelInfo]] = {
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
            input_price_per_1k=0.003,
            output_price_per_1k=0.012,
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
            input_price_per_1k=0.00014,
            output_price_per_1k=0.00028,
        ),
        ModelInfo(
            id="deepseek-reasoner",
            name="DeepSeek Reasoner (R1)",
            provider=ModelProvider.DEEPSEEK,
            context_window=64000,
            max_output_tokens=8192,
            supports_vision=False,
            input_price_per_1k=0.00055,
            output_price_per_1k=0.00219,
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
            input_price_per_1k=0.004,
            output_price_per_1k=0.012,
        ),
        ModelInfo(
            id="qwen-vl-max",
            name="Qwen VL Max",
            provider=ModelProvider.DASHSCOPE,
            context_window=32768,
            max_output_tokens=8192,
            supports_vision=True,
            input_price_per_1k=0.003,
            output_price_per_1k=0.009,
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
    }

    def __init__(self):
        self._configs: Dict[ModelProvider, ModelConfig] = {}
        self._models: Dict[str, ModelInfo] = {}
        self._clients: Dict[ModelProvider, httpx.AsyncClient] = {}

        # Initialize default model catalog
        for provider, models in DEFAULT_MODELS.items():
            for model in models:
                self._models[model.id] = model

    def configure_provider(
        self,
        provider: ModelProvider,
        api_key: str,
        base_url: Optional[str] = None,
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

    def get_available_models(self) -> List[ModelInfo]:
        """Get all models from configured providers."""
        available = []
        for model in self._models.values():
            if self.is_provider_configured(model.provider):
                available.append(model)
        return available

    def get_model(self, model_id: str) -> Optional[ModelInfo]:
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

    def _build_headers(self, provider: ModelProvider, api_key: str) -> Dict[str, str]:
        """Build headers for API requests."""
        if provider == ModelProvider.ANTHROPIC:
            return {
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
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
        messages: List[ChatMessage],
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        stream: bool = False,
    ) -> Dict[str, Any]:
        """Build request body for the provider's API."""
        if provider == ModelProvider.ANTHROPIC:
            return self._build_anthropic_body(
                model_id, messages, temperature, max_tokens, tools, stream
            )
        else:
            return self._build_openai_body(
                model_id, messages, temperature, max_tokens, tools, stream
            )

    def _build_openai_body(
        self,
        model_id: str,
        messages: List[ChatMessage],
        temperature: float,
        max_tokens: Optional[int],
        tools: Optional[List[Dict[str, Any]]],
        stream: bool,
    ) -> Dict[str, Any]:
        """Build OpenAI-compatible request body."""
        formatted_messages = []
        for msg in messages:
            m: Dict[str, Any] = {"role": msg.role, "content": msg.content}
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
                    if img.startswith("http"):
                        content_parts.append({
                            "type": "image_url",
                            "image_url": {"url": img}
                        })
                    else:
                        content_parts.append({
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{img}"}
                        })
                m["content"] = content_parts
            formatted_messages.append(m)

        body: Dict[str, Any] = {
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
        messages: List[ChatMessage],
        temperature: float,
        max_tokens: Optional[int],
        tools: Optional[List[Dict[str, Any]]],
        stream: bool,
    ) -> Dict[str, Any]:
        """Build Anthropic-specific request body."""
        system_prompt = None
        formatted_messages = []

        for msg in messages:
            if msg.role == "system":
                system_prompt = msg.content
                continue

            m: Dict[str, Any] = {"role": msg.role}

            # Handle vision content
            if msg.images and msg.role == "user":
                content_parts = []
                for img in msg.images:
                    if img.startswith("http"):
                        # Anthropic requires base64 for images
                        # For URLs, we'd need to fetch and convert
                        content_parts.append({
                            "type": "image",
                            "source": {
                                "type": "url",
                                "url": img,
                            }
                        })
                    else:
                        content_parts.append({
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": img,
                            }
                        })
                content_parts.append({"type": "text", "text": msg.content})
                m["content"] = content_parts
            else:
                m["content"] = msg.content

            formatted_messages.append(m)

        body: Dict[str, Any] = {
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
                    anthropic_tools.append({
                        "name": func["name"],
                        "description": func.get("description", ""),
                        "input_schema": func.get("parameters", {"type": "object", "properties": {}}),
                    })
            if anthropic_tools:
                body["tools"] = anthropic_tools
        return body

    async def chat(
        self,
        model_id: str,
        messages: List[ChatMessage],
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[str, Dict[str, int]]:
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
            model.provider, model_id, messages, temperature, max_tokens, tools, stream=False
        )

        endpoint = "/v1/messages" if model.provider == ModelProvider.ANTHROPIC else "/v1/chat/completions"

        response = await client.post(endpoint, json=body)
        response.raise_for_status()
        data = response.json()

        if model.provider == ModelProvider.ANTHROPIC:
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
        messages: List[ChatMessage],
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
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
            model.provider, model_id, messages, temperature, max_tokens, tools, stream=True
        )

        endpoint = "/v1/messages" if model.provider == ModelProvider.ANTHROPIC else "/v1/chat/completions"

        if model.provider == ModelProvider.ANTHROPIC:
            async for delta in self._stream_anthropic(client, endpoint, body):
                yield delta
        else:
            async for delta in self._stream_openai(client, endpoint, body):
                yield delta

    async def _stream_openai(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
        body: Dict[str, Any],
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

                # Handle usage in final chunk
                if isinstance(evt.get("usage"), dict):
                    yield StreamDelta(usage=_sanitize_usage(evt["usage"]))
                    continue

                choice = evt.get("choices", [{}])[0]
                delta = choice.get("delta", {})

                yield StreamDelta(
                    content=delta.get("content", ""),
                    tool_calls=delta.get("tool_calls"),
                    finish_reason=choice.get("finish_reason"),
                )

    async def _stream_anthropic(
        self,
        client: httpx.AsyncClient,
        endpoint: str,
        body: Dict[str, Any],
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
                        }
                    )

                elif evt_type == "message_start":
                    usage = evt.get("message", {}).get("usage", {})
                    if usage.get("input_tokens"):
                        yield StreamDelta(usage={"input_tokens": usage["input_tokens"]})

    async def close(self) -> None:
        """Close all HTTP clients."""
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()
