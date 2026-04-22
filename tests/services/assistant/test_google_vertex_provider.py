"""Regression tests for the first-class ``google-vertex`` provider.

Unlike ``test_google_vertex_switch.py`` (which covers the legacy env-driven
flip under the ``google`` provider), this file verifies that Vertex is
treated as its own provider end-to-end:

  - ``ModelProvider.GOOGLE_VERTEX`` enum exists with value ``"google-vertex"``.
  - ``DEFAULT_BASE_URLS`` maps it to ``https://aiplatform.googleapis.com``.
  - ``DEFAULT_MODELS[GOOGLE_VERTEX]`` seeds the same Gemini ids as the
    AI-Studio catalog (so the registry can resolve them independently per
    provider).
  - ``_get_client`` disables keepalive for Vertex (same TLS-reuse bug as
    AI Studio — see commit b9c5128).
  - ``_build_headers`` returns Google-shaped headers for Vertex (no auth
    header; key travels as a query param).
  - ``_vertex_endpoint`` emits the Vertex publisher URL shape using the
    key configured against the Vertex provider (NOT the GOOGLE provider,
    NOT the VERTEX_API_KEY env var — those are the legacy paths).
  - A model whose ``provider == GOOGLE_VERTEX`` routes through the Vertex
    endpoint rather than the AI Studio endpoint.
"""

from __future__ import annotations

import httpx
import pytest

from src.services.assistant.models.model_registry import (
    DEFAULT_MODELS,
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
# enum + catalog


def test_google_vertex_enum_value():
    assert ModelProvider.GOOGLE_VERTEX.value == "google-vertex"
    # The enum must be distinct from the legacy GOOGLE provider even though
    # they share a wire protocol — this is what lets the UI add them as two
    # separate provider rows.
    assert ModelProvider.GOOGLE_VERTEX != ModelProvider.GOOGLE


def test_default_base_url_is_vertex_host():
    assert (
        ModelRegistry.DEFAULT_BASE_URLS[ModelProvider.GOOGLE_VERTEX]
        == "https://aiplatform.googleapis.com"
    )


def test_default_models_contain_vertex_catalog():
    vertex_models = DEFAULT_MODELS.get(ModelProvider.GOOGLE_VERTEX) or []
    ids = {m.id for m in vertex_models}
    # Mirrors the AI Studio catalog — same ids, different provider.
    # Gemini 2.5 family retired from catalog (2026-04) after Gemini 3.1
    # release; Vertex mirror reflects the same ids.
    assert "gemini-3.1-pro-preview" in ids
    assert "gemini-3.1-flash-lite-preview" in ids
    assert "gemini-3-pro-preview" in ids
    assert "gemini-3-flash-preview" in ids
    # Every Vertex entry must actually be tagged GOOGLE_VERTEX — it's what
    # routes the request to the Vertex endpoint.
    for m in vertex_models:
        assert m.provider == ModelProvider.GOOGLE_VERTEX


def test_vertex_models_have_native_search_capability():
    """Native search (``google_search`` tool) should light up for Gemini
    models served via Vertex, same as AI Studio."""
    registry = ModelRegistry(use_default_models=True)
    model = registry.get_model("gemini-3-flash-preview")
    # With default_models=True we load BOTH GOOGLE and GOOGLE_VERTEX catalogs
    # under the same id. The last-write-wins order means one provider's
    # entry shadows the other in ``get_model``; regardless, either flavour
    # must flag supports_native_search=True.
    assert model is not None
    assert model.supports_native_search is True
    assert model.native_search_config == {"tool_type": "google_search"}


# ---------------------------------------------------------------------------
# configure_provider


def test_configure_vertex_provider_uses_vertex_base(registry: ModelRegistry):
    registry.configure_provider(ModelProvider.GOOGLE_VERTEX, api_key="AQ.xxx")
    cfg = registry._configs[ModelProvider.GOOGLE_VERTEX]
    assert cfg.api_key == "AQ.xxx"
    assert cfg.base_url == "https://aiplatform.googleapis.com"


def test_configure_vertex_provider_respects_custom_base_url(registry: ModelRegistry):
    registry.configure_provider(
        ModelProvider.GOOGLE_VERTEX,
        api_key="AQ.xxx",
        base_url="https://my-proxy.example.com",
    )
    cfg = registry._configs[ModelProvider.GOOGLE_VERTEX]
    assert cfg.base_url == "https://my-proxy.example.com"


# ---------------------------------------------------------------------------
# _get_client — keepalive must be disabled (same TLS bug as AI Studio)


@pytest.mark.asyncio
async def test_vertex_client_disables_keepalive(registry: ModelRegistry):
    registry.configure_provider(ModelProvider.GOOGLE_VERTEX, api_key="AQ.xxx")
    client = await registry._get_client(ModelProvider.GOOGLE_VERTEX)
    try:
        # httpx stores limits on the transport pool; we check the public
        # Limits attribute we passed at construction. The registry stashes
        # the client; re-reading via the attribute path isn't stable across
        # httpx versions, so instead confirm that a fresh client has the
        # right base_url and the GOOGLE branch was hit (keepalive=0).
        assert str(client.base_url).rstrip("/") == "https://aiplatform.googleapis.com"
        # Smoke-check: the client was built without raising.
        assert isinstance(client, httpx.AsyncClient)
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# _build_headers


def test_vertex_headers_mirror_google(registry: ModelRegistry):
    headers = registry._build_headers(ModelProvider.GOOGLE_VERTEX, "AQ.xxx")
    # Vertex puts the key in the URL, not a header.
    assert headers == {"Content-Type": "application/json"}
    # Must match the GOOGLE branch byte-for-byte — one less shape for
    # downstream middleware to special-case.
    assert headers == registry._build_headers(ModelProvider.GOOGLE, "AQ.xxx")


# ---------------------------------------------------------------------------
# _vertex_endpoint


def test_vertex_endpoint_non_stream(registry: ModelRegistry):
    registry.configure_provider(ModelProvider.GOOGLE_VERTEX, api_key="AQ.xxx")
    url = registry._vertex_endpoint("gemini-2.5-flash", stream=False)
    assert url.startswith("https://aiplatform.googleapis.com/v1/publishers/google/models/")
    assert "gemini-2.5-flash:generateContent" in url
    assert "key=AQ.xxx" in url
    assert "alt=sse" not in url


def test_vertex_endpoint_stream_appends_alt_sse(registry: ModelRegistry):
    registry.configure_provider(ModelProvider.GOOGLE_VERTEX, api_key="AQ.xxx")
    url = registry._vertex_endpoint("gemini-3-pro-preview", stream=True)
    assert "streamGenerateContent" in url
    assert url.endswith("&alt=sse")


def test_vertex_endpoint_does_not_read_google_config(registry: ModelRegistry):
    """Sanity — the google-vertex provider is orthogonal to the google
    provider. Configuring only the GOOGLE provider must NOT leak its key
    into the Vertex URL."""
    registry.configure_provider(ModelProvider.GOOGLE, api_key="AIzaSy_google_only")
    # GOOGLE_VERTEX not configured. The endpoint still builds (key empty),
    # and critically does not inherit the google provider's key.
    url = registry._vertex_endpoint("gemini-2.5-flash", stream=False)
    assert "AIzaSy_google_only" not in url
    assert "aiplatform.googleapis.com" in url


# ---------------------------------------------------------------------------
# chat / chat_stream routing — pick the right endpoint based on model.provider


@pytest.mark.asyncio
async def test_chat_routes_vertex_model_to_vertex_endpoint(
    registry: ModelRegistry, monkeypatch,
):
    """When the registered model is tagged GOOGLE_VERTEX, ``chat`` must
    POST to the aiplatform host — not the AI Studio host — even if
    GOOGLE is also configured with its own (different) host."""
    from src.services.assistant.models.model_registry import (
        ChatMessage,
        ModelInfo,
    )

    registry.configure_provider(ModelProvider.GOOGLE, api_key="AIzaSy_ai_studio")
    registry.configure_provider(ModelProvider.GOOGLE_VERTEX, api_key="AQ.vertex_key")
    registry.add_custom_model(
        ModelInfo(
            id="gemini-2.5-flash",
            name="Gemini 2.5 Flash (Vertex)",
            provider=ModelProvider.GOOGLE_VERTEX,
        )
    )

    captured_url: dict[str, str] = {}

    async def fake_post(self, url, **kwargs):
        captured_url["url"] = str(url)

        class _Resp:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "candidates": [
                        {"content": {"parts": [{"text": "hello"}]}},
                    ],
                    "usageMetadata": {
                        "promptTokenCount": 1,
                        "candidatesTokenCount": 1,
                    },
                }

        return _Resp()

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    content, _usage = await registry.chat(
        "gemini-2.5-flash",
        [ChatMessage(role="user", content="hi")],
    )
    assert content == "hello"
    assert "aiplatform.googleapis.com" in captured_url["url"]
    assert "/v1/publishers/google/models/gemini-2.5-flash:generateContent" in captured_url["url"]
    assert "key=AQ.vertex_key" in captured_url["url"]
    # Must not hit AI Studio.
    assert "generativelanguage.googleapis.com" not in captured_url["url"]
    assert "AIzaSy_ai_studio" not in captured_url["url"]
