"""Per-domain endpoint-switch resolver for DashScope and Google families.

Both providers expose two interchangeable endpoints per domain
(chat / image / embedding) — a paid/full one and a free-tier alternative.
Operators want to be able to route each domain independently, e.g. keep
embeddings on the paid CN endpoint but flip chat to the Intl free tier.

Env-var pattern (per provider family + per domain):

    # DashScope
    DASHSCOPE_{DOMAIN}_API_KEY            # e.g. DASHSCOPE_CHAT_API_KEY
    DASHSCOPE_{DOMAIN}_BASE_URL           # e.g. DASHSCOPE_CHAT_BASE_URL

    # Google (ai_studio ↔ vertex)
    GOOGLE_{DOMAIN}_BACKEND               # "ai_studio" | "vertex"
    VERTEX_{DOMAIN}_API_KEY               # Express-Mode key (AQ.xxx)

Each domain's override falls back through a chain (first non-empty wins):

    DashScope api_key:  DASHSCOPE_{DOMAIN}_API_KEY
                      → DASHSCOPE_API_KEY                     (general)

    DashScope base_url: DASHSCOPE_{DOMAIN}_BASE_URL
                      → DASHSCOPE_BASE_URL                    (general)
                      → {paid CN default for that domain}

    Google backend:     GOOGLE_{DOMAIN}_BACKEND
                      → GOOGLE_API_BACKEND                    (general)
                      → "ai_studio"

    Google api_key:     when backend == vertex:
                          VERTEX_{DOMAIN}_API_KEY
                        → VERTEX_API_KEY
                        → GEMINI_API_KEY / GOOGLE_API_KEY     (shared fallback)
                        when backend == ai_studio:
                          GEMINI_API_KEY
                        → GOOGLE_API_KEY

Callers should prefer this helper over raw ``os.environ.get`` so the
fallback behaviour stays consistent across chat/image/embedding. The
module is intentionally pure — no I/O, no side effects, no logging.
"""

from __future__ import annotations

import os
from typing import Literal

Domain = Literal["chat", "image", "embedding"]

# DashScope defaults. Chat uses the OpenAI-compat suffix; image/embedding
# use the native dashscope.aliyuncs.com root because the SDK hardcodes
# /api/v1 paths.
DASHSCOPE_DEFAULT_CHAT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode"
DASHSCOPE_DEFAULT_LEGACY_BASE_URL = "https://dashscope.aliyuncs.com"

GOOGLE_AI_STUDIO_BASE_URL = "https://generativelanguage.googleapis.com"
GOOGLE_VERTEX_BASE_URL = "https://aiplatform.googleapis.com"


def _first(*candidates: str) -> str:
    """Return the first non-empty candidate after strip()."""
    for c in candidates:
        if c and c.strip():
            return c.strip()
    return ""


def resolve_dashscope(domain: Domain) -> tuple[str, str]:
    """Resolve DashScope ``(api_key, base_url)`` for the given domain.

    Falls back through the per-domain env, then the general env, then
    the domain-appropriate default base URL. Returns ``("", default_url)``
    if no key is configured — callers decide whether to treat that as
    "not configured".
    """
    up = domain.upper()
    api_key = _first(
        os.environ.get(f"DASHSCOPE_{up}_API_KEY", ""),
        os.environ.get("DASHSCOPE_API_KEY", ""),
    )
    # Chat uses the OpenAI-compat prefix because the model_registry's
    # OpenAI-compatible client appends /v1/chat/completions. Image and
    # embedding speak to the legacy /api/v1/* endpoints, so they want
    # the bare host.
    default_url = (
        DASHSCOPE_DEFAULT_CHAT_BASE_URL
        if domain == "chat"
        else DASHSCOPE_DEFAULT_LEGACY_BASE_URL
    )
    base_url = _first(
        os.environ.get(f"DASHSCOPE_{up}_BASE_URL", ""),
        os.environ.get("DASHSCOPE_BASE_URL", ""),
    ) or default_url
    return api_key, base_url


def resolve_google(domain: Domain) -> tuple[str, str, str]:
    """Resolve Google ``(api_key, base_url, backend)`` for the given domain.

    ``backend`` is always ``"ai_studio"`` or ``"vertex"``. The base URL
    is the default for the selected backend — callers that need a
    non-default host (none exist today) can still override via their
    own logic; this helper only handles the free/paid swap.

    When ``backend == "vertex"`` and no VERTEX key is configured, the
    generic ``GEMINI_API_KEY`` / ``GOOGLE_API_KEY`` is returned as the
    shared-key fallback (the "I know what I'm doing" escape hatch —
    see ``tests/services/assistant/test_google_vertex_switch.py``).
    """
    up = domain.upper()
    backend = _first(
        os.environ.get(f"GOOGLE_{up}_BACKEND", ""),
        os.environ.get("GOOGLE_API_BACKEND", ""),
    ).lower() or "ai_studio"
    if backend not in {"ai_studio", "vertex"}:
        backend = "ai_studio"

    studio_key = _first(
        os.environ.get("GEMINI_API_KEY", ""),
        os.environ.get("GOOGLE_API_KEY", ""),
    )
    if backend == "vertex":
        api_key = _first(
            os.environ.get(f"VERTEX_{up}_API_KEY", ""),
            os.environ.get("VERTEX_API_KEY", ""),
            studio_key,  # shared-key escape hatch
        )
        base_url = GOOGLE_VERTEX_BASE_URL
    else:
        api_key = studio_key
        base_url = GOOGLE_AI_STUDIO_BASE_URL
    return api_key, base_url, backend
