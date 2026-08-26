"""Shared configuration helpers.

Pure environment-read utilities: no I/O, no side effects, safe to call
during module import. See ``endpoints.py`` for the per-domain
free/paid endpoint-switch resolver.
"""

from .endpoints import (
    DASHSCOPE_DEFAULT_CHAT_BASE_URL,
    DASHSCOPE_DEFAULT_NATIVE_BASE_URL,
    GOOGLE_AI_STUDIO_BASE_URL,
    GOOGLE_VERTEX_BASE_URL,
    normalize_dashscope_base,
    resolve_dashscope,
    resolve_google,
)

# Back-compat alias — pre-2026-04-28 default for image/embedding was the
# bare host without ``/api/v1``. Removed because the dashscope SDK 404s
# on it; kept here as an alias to the new value so old import sites
# don't break during the upgrade window.

__all__ = [
    "DASHSCOPE_DEFAULT_CHAT_BASE_URL",
    "DASHSCOPE_DEFAULT_NATIVE_BASE_URL",
    "GOOGLE_AI_STUDIO_BASE_URL",
    "GOOGLE_VERTEX_BASE_URL",
    "normalize_dashscope_base",
    "resolve_dashscope",
    "resolve_google",
]
