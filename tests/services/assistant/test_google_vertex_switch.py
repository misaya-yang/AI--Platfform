"""Regression tests for the Google backend switch (AI Studio ↔ Vertex).

Covers the three-level precedence established in ``_google_backend_for_model``:

  1. ``GOOGLE_VERTEX_MODELS`` env wins on a per-model basis.
  2. ``ModelConfig.backend`` (set from ``GOOGLE_API_BACKEND`` at startup)
     is the fallback.
  3. Default is ``ai_studio``.

And that ``_google_endpoint`` emits the right absolute URL + path for each
backend, so ``httpx`` will route to the correct host even when the client's
global ``base_url`` points elsewhere (which happens when one model is
overridden per-request).
"""

from __future__ import annotations

import os

import pytest

from src.services.assistant.models.model_registry import (
    ModelConfig,
    ModelProvider,
    ModelRegistry,
)


@pytest.fixture
def registry() -> ModelRegistry:
    return ModelRegistry(use_default_models=False)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_BACKEND", raising=False)
    monkeypatch.delenv("GOOGLE_VERTEX_MODELS", raising=False)
    monkeypatch.delenv("VERTEX_API_KEY", raising=False)


# ---------------------------------------------------------------------------
# configure_provider backend plumbing


def test_configure_google_default_backend_is_ai_studio(registry: ModelRegistry):
    registry.configure_provider(ModelProvider.GOOGLE, api_key="AIzaSy_fake")
    cfg = registry._configs[ModelProvider.GOOGLE]
    assert cfg.backend == "ai_studio"
    assert cfg.base_url == "https://generativelanguage.googleapis.com"


def test_configure_google_backend_vertex_picks_vertex_base(registry: ModelRegistry):
    registry.configure_provider(
        ModelProvider.GOOGLE, api_key="AQ.xxx", backend="vertex"
    )
    cfg = registry._configs[ModelProvider.GOOGLE]
    assert cfg.backend == "vertex"
    assert cfg.base_url == "https://aiplatform.googleapis.com"


def test_configure_non_google_backend_field_is_ignored(registry: ModelRegistry):
    """Backend field is Google-only. Passing it to another provider should
    silently become ``"ai_studio"`` (no-op) — we don't want rogue backends
    leaking onto unrelated providers."""
    registry.configure_provider(
        ModelProvider.OPENAI, api_key="sk-fake", backend="vertex",
    )
    cfg = registry._configs[ModelProvider.OPENAI]
    assert cfg.backend == "ai_studio"


# ---------------------------------------------------------------------------
# per-model override precedence


def test_backend_for_model_defaults_to_ai_studio_without_config(registry: ModelRegistry):
    assert registry._google_backend_for_model("gemini-2.5-flash") == "ai_studio"


def test_backend_for_model_follows_config_backend(registry: ModelRegistry):
    registry.configure_provider(
        ModelProvider.GOOGLE, api_key="AQ.xxx", backend="vertex"
    )
    assert registry._google_backend_for_model("gemini-2.5-flash") == "vertex"


def test_vertex_models_env_overrides_ai_studio_default(
    registry: ModelRegistry, monkeypatch,
):
    """``GOOGLE_VERTEX_MODELS`` is the escape hatch for A/B testing one
    model on Vertex while leaving the rest on AI Studio."""
    registry.configure_provider(ModelProvider.GOOGLE, api_key="AIzaSy_fake")
    monkeypatch.setenv("GOOGLE_VERTEX_MODELS", "gemini-3-flash-preview")
    assert registry._google_backend_for_model("gemini-3-flash-preview") == "vertex"
    assert registry._google_backend_for_model("gemini-2.5-flash") == "ai_studio"


def test_vertex_models_env_handles_whitespace_and_empty_items(
    registry: ModelRegistry, monkeypatch,
):
    registry.configure_provider(ModelProvider.GOOGLE, api_key="AIzaSy_fake")
    monkeypatch.setenv("GOOGLE_VERTEX_MODELS", "  gemini-3-flash-preview , , gemini-2.5-pro  ")
    assert registry._google_backend_for_model("gemini-3-flash-preview") == "vertex"
    assert registry._google_backend_for_model("gemini-2.5-pro") == "vertex"
    assert registry._google_backend_for_model("gemini-2.5-flash") == "ai_studio"


# ---------------------------------------------------------------------------
# endpoint URL construction


def test_ai_studio_endpoint_non_stream(registry: ModelRegistry):
    registry.configure_provider(ModelProvider.GOOGLE, api_key="AIzaSy_fake")
    url = registry._google_endpoint("gemini-2.5-flash", stream=False)
    assert url.startswith("https://generativelanguage.googleapis.com/v1beta/models/")
    assert "gemini-2.5-flash:generateContent" in url
    assert "key=AIzaSy_fake" in url
    assert "alt=sse" not in url


def test_ai_studio_endpoint_stream_appends_alt_sse(registry: ModelRegistry):
    registry.configure_provider(ModelProvider.GOOGLE, api_key="AIzaSy_fake")
    url = registry._google_endpoint("gemini-2.5-flash", stream=True)
    assert "streamGenerateContent" in url
    assert url.endswith("&alt=sse")


def test_vertex_endpoint_has_publishers_google_prefix(registry: ModelRegistry):
    registry.configure_provider(
        ModelProvider.GOOGLE, api_key="AQ.xxx", backend="vertex",
    )
    url = registry._google_endpoint("gemini-2.5-flash-lite", stream=False)
    assert url.startswith("https://aiplatform.googleapis.com/v1/publishers/google/models/")
    assert "gemini-2.5-flash-lite:generateContent" in url
    assert "key=AQ.xxx" in url


def test_per_model_override_flips_only_that_model_url(
    registry: ModelRegistry, monkeypatch,
):
    """Config says ai_studio for the provider; env routes ONE model to
    Vertex. The two endpoints should land on different hosts — this is
    the whole point of returning an **absolute** URL from
    ``_google_endpoint``: httpx sees the absolute URL and ignores the
    client's global ``base_url``."""
    registry.configure_provider(ModelProvider.GOOGLE, api_key="AIzaSy_fake")
    monkeypatch.setenv("GOOGLE_VERTEX_MODELS", "gemini-3-flash-preview")

    vertex_url = registry._google_endpoint("gemini-3-flash-preview", stream=False)
    ai_studio_url = registry._google_endpoint("gemini-2.5-flash", stream=False)

    assert "aiplatform.googleapis.com" in vertex_url
    assert "publishers/google/models" in vertex_url
    assert "generativelanguage.googleapis.com" in ai_studio_url
    assert "/v1beta/models" in ai_studio_url


def test_missing_google_config_still_produces_usable_url(registry: ModelRegistry):
    """If configure_provider was never called, we still get a URL — just
    without an API key. Lets the error surface at the HTTP layer (401)
    instead of crashing during URL construction."""
    url = registry._google_endpoint("gemini-2.5-flash", stream=False)
    assert url.startswith("https://generativelanguage.googleapis.com/")
    assert url.endswith("key=")


def test_per_model_vertex_override_uses_vertex_api_key(
    registry: ModelRegistry, monkeypatch,
):
    """The global provider is configured for AI Studio with an
    ``AIzaSy...`` key. When one model flips to Vertex via env, the URL
    must carry ``VERTEX_API_KEY`` (``AQ.xxx``) instead — otherwise the
    AI-Studio-shaped key hits the Vertex endpoint and gets 401."""
    registry.configure_provider(ModelProvider.GOOGLE, api_key="AIzaSy_ai_studio_key")
    monkeypatch.setenv("GOOGLE_VERTEX_MODELS", "gemini-3-flash-preview")
    monkeypatch.setenv("VERTEX_API_KEY", "AQ.vertex_express_mode_key")

    url = registry._google_endpoint("gemini-3-flash-preview", stream=False)
    assert "aiplatform.googleapis.com" in url
    assert "key=AQ.vertex_express_mode_key" in url
    assert "AIzaSy_ai_studio_key" not in url

    # The non-overridden model should still use the AI Studio key.
    other = registry._google_endpoint("gemini-2.5-flash", stream=False)
    assert "key=AIzaSy_ai_studio_key" in other


def test_vertex_override_falls_back_to_config_key_when_no_vertex_env(
    registry: ModelRegistry, monkeypatch,
):
    """If someone sets ``GOOGLE_VERTEX_MODELS`` without also setting
    ``VERTEX_API_KEY`` we still try the call — but with the config's
    key. This is the "I know what I'm doing" escape hatch for a shared
    key that actually works for both backends (rare, but tests should
    allow it). Should not crash, should not log secrets."""
    registry.configure_provider(ModelProvider.GOOGLE, api_key="AIzaSy_shared")
    monkeypatch.setenv("GOOGLE_VERTEX_MODELS", "gemini-3-flash-preview")
    monkeypatch.delenv("VERTEX_API_KEY", raising=False)

    url = registry._google_endpoint("gemini-3-flash-preview", stream=False)
    assert "aiplatform.googleapis.com" in url
    assert "key=AIzaSy_shared" in url
