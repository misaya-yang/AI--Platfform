"""Provider configuration and registry lifecycle primitives."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx
from ai_gateway_core.enums import ModelAccessLevel, ModelProvider
from ai_gateway_core.logging import get_logger, record_internal_exception

from .model_catalog import DEFAULT_MODELS, ModelConfig, ModelInfo
from .responses_api import (
    CHAT_COMPLETIONS_WIRE_PROTOCOL,
    RESPONSES_V1_WIRE_PROTOCOL,
    SUPPORTED_WIRE_PROTOCOLS,
)

logger = get_logger(__name__)


class RegistryLifecycleMixin:
    """Own model inventory, provider configuration, and HTTP clients."""

    # Default base URLs for each provider
    DEFAULT_BASE_URLS = {
        ModelProvider.OPENAI: "https://api.openai.com",
        ModelProvider.ANTHROPIC: "https://api.anthropic.com",
        ModelProvider.DEEPSEEK: "https://api.deepseek.com",
        ModelProvider.DASHSCOPE: "https://dashscope.aliyuncs.com/compatible-mode",
        ModelProvider.GOOGLE: "https://generativelanguage.googleapis.com",
        ModelProvider.GOOGLE_VERTEX: "https://aiplatform.googleapis.com",
    }

    #: Base URL used when a Google provider is configured with ``backend="vertex"``.
    #: Separate from ``DEFAULT_BASE_URLS`` because the same ``ModelProvider.GOOGLE``
    #: enum value routes to two completely different hosts depending on backend.
    VERTEX_BASE_URL = "https://aiplatform.googleapis.com"

    def __init__(
        self,
        use_default_models: bool = True,
        *,
        vertex_models: Iterable[str] | None = None,
        vertex_api_key_override: str = "",
        startup_config_frozen: bool = False,
    ):
        self._configs: dict[ModelProvider, ModelConfig] = {}
        self._models: dict[str, ModelInfo] = {}
        self._clients: dict[ModelProvider, httpx.AsyncClient] = {}
        self._db_models_loaded: bool = False
        self._vertex_models = frozenset(vertex_models or ())
        self._vertex_api_key_override = vertex_api_key_override
        self._startup_config_frozen = startup_config_frozen

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

            loaded_count = self.replace_models_from_database_rows(db_models)

            self._db_models_loaded = True
            logger.info(f"Loaded {loaded_count} models from database")
            return loaded_count

        except Exception as exc:
            record_internal_exception(
                __name__, "assistant.core.models.registry_lifecycle.internal_failure", exc
            )
            return 0

    def replace_models_from_database_rows(
        self,
        rows: Iterable[Mapping[str, Any]],
        *,
        default_context_window: int = 128000,
        default_max_output_tokens: int = 4096,
    ) -> int:
        """Replace the catalog with valid, explicitly classified DB rows only.

        A malformed or missing access level is a configuration error, not a
        public model. Skipping the row fails closed for both the regular
        database loader and the assistant-service startup loader.
        """
        self._models.clear()
        loaded_count = 0
        for row in rows:
            try:
                provider = ModelProvider(row.get("provider_id", ""))
                access_level = ModelAccessLevel(row.get("access_level"))
                model_id = row["model_id"]
                model = ModelInfo(
                    id=model_id,
                    name=row.get("display_name") or model_id,
                    provider=provider,
                    context_window=row.get("context_window") or default_context_window,
                    max_output_tokens=row.get("max_output_tokens") or default_max_output_tokens,
                    supports_vision=row.get("supports_vision", False),
                    supports_tools=row.get("supports_tools", True),
                    input_price_per_1k=float(row.get("input_price_per_1k", 0)),
                    output_price_per_1k=float(row.get("output_price_per_1k", 0)),
                    access_level=access_level,
                )
            except Exception as exc:
                record_internal_exception(
                    __name__, "assistant.core.models.registry_lifecycle.internal_failure", exc
                )
                continue

            self._models[model.id] = model
            loaded_count += 1

        return loaded_count

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
        backend: str = "ai_studio",
        wire_protocol: str = CHAT_COMPLETIONS_WIRE_PROTOCOL,
    ) -> None:
        """Configure a provider with API credentials.

        ``backend`` is meaningful only for ``ModelProvider.GOOGLE`` — either
        ``"ai_studio"`` (default, ``generativelanguage.googleapis.com``) or
        ``"vertex"`` (``aiplatform.googleapis.com``). When ``"vertex"`` and
        no explicit ``base_url`` is passed, the Vertex base URL is selected
        automatically so callers don't have to know the host format.
        """
        normalized_wire_protocol = str(wire_protocol or "").strip().lower()
        if normalized_wire_protocol not in SUPPORTED_WIRE_PROTOCOLS:
            raise ValueError(f"Unsupported provider wire protocol: {normalized_wire_protocol}")
        if normalized_wire_protocol == RESPONSES_V1_WIRE_PROTOCOL and provider not in {
            ModelProvider.OPENAI,
            ModelProvider.DASHSCOPE,
        }:
            raise ValueError(
                f"Provider {provider.value} does not support the Responses v1 wire protocol"
            )

        resolved_base = base_url
        if resolved_base is None:
            if provider == ModelProvider.GOOGLE and backend == "vertex":
                resolved_base = self.VERTEX_BASE_URL
            else:
                resolved_base = self.DEFAULT_BASE_URLS.get(provider)

        self._configs[provider] = ModelConfig(
            api_key=api_key,
            base_url=resolved_base,
            timeout=timeout,
            backend=backend if provider == ModelProvider.GOOGLE else "ai_studio",
            wire_protocol=normalized_wire_protocol,
        )
        # Reset client if exists
        if provider in self._clients:
            # Don't close here to avoid async issues
            del self._clients[provider]

    def is_provider_configured(self, provider: ModelProvider) -> bool:
        """Check if a provider is configured."""
        return provider in self._configs and bool(self._configs[provider].api_key)

    def _uses_responses_v1(self, provider: ModelProvider) -> bool:
        config = self._configs.get(provider)
        return bool(config and config.wire_protocol == RESPONSES_V1_WIRE_PROTOCOL)

    def _responses_endpoint(self, provider: ModelProvider) -> str:
        """Return a relative Responses path without duplicating a configured ``/v1``."""

        config = self._configs.get(provider)
        base_path = urlsplit(str(config.base_url or "") if config else "").path.rstrip("/")
        if base_path.endswith("/responses"):
            return "."
        if base_path.endswith("/v1"):
            return "responses"
        return "/v1/responses"

    def _google_backend_for_model(self, model_id: str) -> str:
        """Resolve the effective Google backend for one model.

        Precedence (first match wins):
          1. ``GOOGLE_VERTEX_MODELS`` env — comma-separated list; if
             ``model_id`` is in it, force ``vertex``.
          2. ``ModelConfig.backend`` on the Google provider config — set at
             startup from ``GOOGLE_API_BACKEND`` in ``main.py``.
          3. Default: ``ai_studio``.
        """
        vertex_models = self._vertex_models
        if not self._startup_config_frozen:
            import os

            vertex_models = frozenset(
                item.strip()
                for item in os.environ.get("GOOGLE_VERTEX_MODELS", "").split(",")
                if item.strip()
            )
        if model_id in vertex_models:
            return "vertex"
        cfg = self._configs.get(ModelProvider.GOOGLE)
        return cfg.backend if cfg else "ai_studio"

    def _google_endpoint(self, model_id: str, *, stream: bool) -> str:
        """Build the **absolute** endpoint URL for a Google call.

        Returns a full URL (not a path) so that per-model backend overrides
        via ``GOOGLE_VERTEX_MODELS`` work even when the provider's global
        client has a different base_url — httpx treats absolute URLs as
        overrides, so we can route individual models without recreating
        the HTTP client.

        Supports two endpoints:
          * AI Studio: ``https://generativelanguage.googleapis.com/v1beta/models/{m}:{action}``
          * Vertex Express Mode: ``https://aiplatform.googleapis.com/v1/publishers/google/models/{m}:{action}``

        Authentication is supplied through the ``x-goog-api-key`` request
        header. API keys must never be embedded in the URL because HTTP
        client access logs routinely include the complete request target.
        """
        backend = self._google_backend_for_model(model_id)
        action = "streamGenerateContent" if stream else "generateContent"
        if backend == "vertex":
            base = self.VERTEX_BASE_URL
            path = f"/v1/publishers/google/models/{model_id}:{action}"
        else:
            base = self.DEFAULT_BASE_URLS[ModelProvider.GOOGLE]
            path = f"/v1beta/models/{model_id}:{action}"
        if stream:
            path += "?alt=sse"
        return f"{base}{path}"

    def _vertex_endpoint(self, model_id: str, *, stream: bool) -> str:
        """Build the absolute Vertex Express-Mode endpoint URL.

        Used when a model's ``provider == ModelProvider.GOOGLE_VERTEX``.
        Mirrors the Vertex branch of ``_google_endpoint`` but sources the
        key from the GOOGLE_VERTEX provider config (not VERTEX_API_KEY env)
        because the whole point of making Vertex its own provider is that
        operators configure it through the Service Management UI — the DB
        row is the source of truth.

        ``model_id`` may carry a ``-vertex`` suffix that's a DB-only
        disambiguator used to let the same underlying Gemini model coexist
        under both the ``google`` (AI Studio) provider and ``google-vertex``
        without colliding on the old 2-column PK. The suffix is stripped
        here — Google's Vertex API only knows the bare id (e.g.
        ``gemini-3-flash-preview``). Safe no-op when a row already uses
        a canonical id.
        """
        config = self._configs.get(ModelProvider.GOOGLE_VERTEX)
        base = (config.base_url if config else None) or self.VERTEX_BASE_URL
        upstream_id = model_id[: -len("-vertex")] if model_id.endswith("-vertex") else model_id
        action = "streamGenerateContent" if stream else "generateContent"
        path = f"/v1/publishers/google/models/{upstream_id}:{action}"
        if stream:
            path += "?alt=sse"
        return f"{base}{path}"

    def _google_api_key_for_model(
        self,
        provider: ModelProvider,
        model_id: str | None,
    ) -> str:
        """Return the request key without ever placing it in a URL."""

        config = self._configs.get(provider)
        configured_key = config.api_key if config else ""
        if (
            provider == ModelProvider.GOOGLE
            and model_id
            and self._google_backend_for_model(model_id) == "vertex"
        ):
            if self._startup_config_frozen:
                return self._vertex_api_key_override or configured_key
            import os

            return os.environ.get("VERTEX_API_KEY", "").strip() or configured_key
        return configured_key

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

    async def _get_client(
        self,
        provider: ModelProvider,
        *,
        model_id: str | None = None,
    ) -> httpx.AsyncClient:
        """Get or create HTTP client for provider.

        Google providers (both AI Studio and Vertex) use a **fresh client
        per call**. Our earlier mitigation — caching the client with
        ``max_keepalive_connections=0`` — did NOT eliminate the 30-47s
        tail latency observed on the second streaming request of a
        session. Even with keepalive off, httpx's ``AsyncClient``
        maintains internal HTTP/1 connection state per host; an SSE
        stream that finishes normally appears to leave something in a
        half-closed state that the Google servers don't reply to
        promptly.

        The fix is to make the client fully ephemeral for Google: every
        call builds, uses, and closes its own ``AsyncClient`` (see the
        ``finally: await client.aclose()`` in ``chat_stream`` /
        ``chat``). This costs ~150ms per request for TLS, but guarantees
        no shared state between calls. Non-Google providers keep their
        cached, keepalive-enabled client — they don't exhibit the bug.

        Caller contract:
          * Google / Vertex: treat the returned client as single-use.
            ``async with`` it or explicitly ``await client.aclose()``.
          * Everyone else: do NOT close it; the registry owns the lifetime.
        """
        config = self._configs.get(provider)
        if not config:
            raise ValueError(f"Provider {provider} not configured")

        api_key = (
            self._google_api_key_for_model(provider, model_id)
            if provider in (ModelProvider.GOOGLE, ModelProvider.GOOGLE_VERTEX)
            else config.api_key
        )
        headers = self._build_headers(provider, api_key)
        is_google = provider in (ModelProvider.GOOGLE, ModelProvider.GOOGLE_VERTEX)

        if is_google:
            # Ephemeral — caller closes.
            return httpx.AsyncClient(
                base_url=config.base_url,
                headers=headers,
                timeout=httpx.Timeout(config.timeout),
                limits=httpx.Limits(max_keepalive_connections=0, max_connections=2),
            )

        if provider not in self._clients:
            self._clients[provider] = httpx.AsyncClient(
                base_url=config.base_url,
                headers=headers,
                timeout=httpx.Timeout(config.timeout),
                limits=httpx.Limits(max_keepalive_connections=5, max_connections=20),
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
        elif provider in (ModelProvider.GOOGLE, ModelProvider.GOOGLE_VERTEX):
            return {
                "Content-Type": "application/json",
                "x-goog-api-key": api_key,
            }
        else:
            # OpenAI-compatible (OpenAI, DeepSeek, DashScope)
            return {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
