"""Embedding provider management and configuration resolution."""
from __future__ import annotations

from typing import Any

from ...core.exceptions import ValidationFailedError
from ...core.observability.logging import get_logger
from .common import ensure_dict as _ensure_dict
from .embedding import (
    BaseEmbedding,
    EmbeddingConfig,
    create_embedding,
)

logger = get_logger(__name__)


class EmbeddingManager:
    """Creates and configures text/multimodal embedders from dataset settings."""

    def __init__(self, settings: Any):
        self.settings = settings

    def is_multimodal_dataset(self, dataset: dict[str, Any]) -> bool:
        """Check if dataset is configured for multimodal (unified embedding space).

        A dataset is considered multimodal if:
        1. embedding_provider is 'unified_multimodal', 'unified', or 'cross_modal'
        2. OR embedding_model is a known multimodal model
        3. OR index_config explicitly enables multimodal mode

        Returns:
            True if the dataset should use unified multimodal embedding
        """
        provider = str(dataset.get("embedding_provider") or "").lower()
        model = str(dataset.get("embedding_model") or "")

        # Check provider
        multimodal_providers = {
            "unified_multimodal",
            "unified",
            "cross_modal",
            "dashscope_multimodal",
            "multimodal",
        }
        if provider in multimodal_providers:
            return True

        # Check model
        from .embedding import MULTIMODAL_EMBEDDING_MODELS

        if model in MULTIMODAL_EMBEDDING_MODELS:
            return True

        # Check index_config
        index_config = _ensure_dict(dataset.get("index_config"))
        return bool(index_config.get("multimodal_enabled") or index_config.get("enable_multimodal"))

    def get_unified_multimodal_embedder(
        self,
        dataset: dict[str, Any],
        embedding_config: dict[str, Any] | None = None,
    ) -> UnifiedMultimodalEmbedding:
        """Create UnifiedMultimodalEmbedding for multimodal datasets.

        This ensures text and images are embedded in the same vector space,
        enabling true cross-modal retrieval.

        Uses settings.knowledge.multimodal_embedding_* configuration.
        """
        from .embedding import UnifiedMultimodalEmbedding

        # Resolve API key from dataset config or gateway settings
        ec = embedding_config or _ensure_dict(dataset.get("embedding_config"))
        api_key = str(ec.get("api_key") or "").strip()
        if not api_key:
            raise ValidationFailedError(
                "Multimodal embedding api_key is required in dataset embedding_config"
            )

        # Use model from dataset or fall back to settings
        model = dataset.get("embedding_model") or self.settings.knowledge.multimodal_embedding_model
        max_concurrent = (
            ec.get("max_concurrent") or self.settings.knowledge.multimodal_embedding_max_concurrent
        )

        return UnifiedMultimodalEmbedding(
            api_key=api_key,
            model=model,
            base_url=ec.get("base_url"),
            max_concurrent=max_concurrent,
        )

    def get_text_embedder(
        self,
        dataset: dict[str, Any],
        embedding_config: dict[str, Any] | None = None,
    ) -> BaseEmbedding:
        """Create embedder for text-only datasets using dataset-level config."""
        ec = (
            _ensure_dict(embedding_config)
            if embedding_config is not None
            else _ensure_dict(dataset.get("embedding_config"))
        )
        provider = str(dataset.get("embedding_provider") or "").lower() or "local"
        model = str(dataset.get("embedding_model") or "")
        dimension = int(dataset.get("embedding_dimension") or 0) or None
        default_model = {
            "gemini": "gemini-embedding-001",
            "google": "gemini-embedding-001",
            "dashscope": "text-embedding-v3",
            "aliyun": "text-embedding-v3",
            "siliconflow": "BAAI/bge-m3",
            "silicon": "BAAI/bge-m3",
            "sf": "BAAI/bge-m3",
        }.get(provider, "hash-384")
        resolved_model = model or default_model

        econf = self.resolve_embedding_config(
            provider=provider,
            model=resolved_model,
            embedding_config=ec,
        )
        resolved_dimension = dimension
        if resolved_dimension is None and provider not in {"local", "builtin", "hash"}:
            resolved_dimension = self.settings.knowledge.text_embedding_dimension
        return create_embedding(econf, dimension=resolved_dimension)

    def resolve_embedding_config(
        self, provider: str, model: str, embedding_config: dict[str, Any]
    ) -> EmbeddingConfig:
        provider_key = (provider or "").lower()
        api_key = str(embedding_config.get("api_key") or "").strip()
        base_url = str(embedding_config.get("base_url") or "").strip() or None

        if provider_key in {"local", "builtin", "hash"}:
            return EmbeddingConfig(
                provider="local",
                model=model or "hash-384",
                api_key=None,
                base_url=None,
                timeout_seconds=5.0,
                extra=embedding_config or {},
            )
        if provider_key in {"gemini", "google"}:
            if not api_key:
                api_key = str(self.settings.knowledge.gemini.api_key or "").strip()
            # Env-level fallback: DASHSCOPE_EMBEDDING_API_KEY /
            # VERTEX_EMBEDDING_API_KEY are resolved inline here (mirrors
            # ai_gateway_core.config.endpoints; duplicated intentionally
            # because knowledge-service ships as a standalone image that
            # does not depend on ai-gateway-core).
            if not api_key:
                import os as _os
                api_key = (
                    _os.environ.get("VERTEX_EMBEDDING_API_KEY", "").strip()
                    if _os.environ.get("GOOGLE_EMBEDDING_BACKEND", "").strip().lower() == "vertex"
                    else ""
                ) or _os.environ.get("GEMINI_API_KEY", "").strip() or _os.environ.get("GOOGLE_API_KEY", "").strip()
            if not api_key:
                raise ValidationFailedError(
                    "Gemini api_key is required in dataset embedding_config"
                )
        elif provider_key in {"dashscope", "aliyun"}:
            if not api_key:
                api_key = str(self.settings.knowledge.dashscope.api_key or "").strip()
            # Inline equivalent of resolve_dashscope("embedding") — see
            # note on Gemini branch above. ``base_url`` resolution must
            # NOT be nested under ``if not api_key`` — when settings
            # already provide the key, the env-supplied SDK base URL is
            # still required to keep the dashscope SDK off its CN
            # default (which would route to a CN-side account whose
            # billing is independent of the Intl key).
            if not api_key:
                import os as _os
                api_key = (
                    _os.environ.get("DASHSCOPE_EMBEDDING_API_KEY", "").strip()
                    or _os.environ.get("DASHSCOPE_API_KEY", "").strip()
                )
            if not base_url:
                import os as _os
                base_url = (
                    _os.environ.get("DASHSCOPE_EMBEDDING_BASE_URL", "").strip()
                    or _os.environ.get("DASHSCOPE_BASE_URL", "").strip()
                    or None
                )
            # The dashscope SDK's ``base_http_api_url`` already INCLUDES
            # the ``/api/v1`` segment by default
            # (``https://dashscope.aliyuncs.com/api/v1``); the SDK appends
            # ``/services/embeddings/...`` to it. Two common env-supplied
            # values need normalising before they're handed to the SDK:
            #
            #   * ``…/compatible-mode``  → OpenAI-HTTP chat path; not a
            #     dashscope SDK base. Replace with ``/api/v1``.
            #   * bare host (``https://dashscope-intl.aliyuncs.com``) →
            #     missing ``/api/v1``; SDK then 404s on every call.
            #     Append ``/api/v1``.
            #
            # Incident 2026-04-28 — CN account arrearage when SDK fell
            # back to its CN default; then 404 when the env-supplied base
            # was the bare host.
            if base_url:
                if base_url.endswith("/compatible-mode"):
                    base_url = base_url[: -len("/compatible-mode")] + "/api/v1"
                elif not base_url.rstrip("/").endswith("/api/v1"):
                    base_url = base_url.rstrip("/") + "/api/v1"
            if not api_key:
                raise ValidationFailedError(
                    "DashScope api_key is required in dataset embedding_config"
                )
        elif provider_key in {"siliconflow", "silicon", "sf"}:
            if not api_key:
                api_key = str(self.settings.knowledge.siliconflow.api_key or "").strip()
            if not api_key:
                raise ValidationFailedError(
                    "SiliconFlow api_key is required in dataset embedding_config"
                )
            endpoint = base_url or (
                str(self.settings.knowledge.siliconflow.base_url or "").strip()
                or "https://api.siliconflow.cn/v1"
            )
            if endpoint.rstrip("/").endswith("/embeddings"):
                base_url = endpoint
            else:
                base_url = f"{endpoint.rstrip('/')}/embeddings"
        else:
            raise ValidationFailedError(f"Unsupported embedding provider: {provider}")

        return EmbeddingConfig(
            provider=provider_key,
            model=model,
            api_key=api_key or None,
            base_url=base_url,
            timeout_seconds=30.0,
            extra={
                k: v
                for k, v in (embedding_config or {}).items()
                if k not in {"api_key", "base_url"}
            },
        )
