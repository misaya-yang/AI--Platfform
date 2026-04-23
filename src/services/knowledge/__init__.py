"""Gateway-side Knowledge Base shim.

KB business logic now lives in ``apps/knowledge-service`` and is reached
over HTTP via ``KBProxyClient``. The gateway side keeps:

- ``kb_proxy_client.py`` — thin HTTP client to the KB microservice (:8092).
- ``embedding.py`` / ``vlm_service.py`` — kept as **shared utilities** until
  K5c reconciles the Confluence integration. ``confluence/`` calls
  ``..embedding.create_embedding`` and ``..vlm_service.DashScopeVLMService``
  at runtime; deleting them here would break Confluence sync. Both files
  are byte-identical with their kb-service counterparts.
- ``confluence/`` — out of scope for K5b, deferred to K5c.

For pure utilities like ``is_multimodal_embedding_model`` see
``ai_gateway_core.knowledge.utils``.
"""

from __future__ import annotations

from .kb_proxy_client import KBProxyClient, ProxyRetrieveResult

__all__ = [
    "KBProxyClient",
    "ProxyRetrieveResult",
]
