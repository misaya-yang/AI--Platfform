"""Back-compat shim — KBProxyClient moved to ai_gateway_core in Phase 5f Batch C.

This file is intentionally a thin re-export so existing import sites in
gateway src/ keep working while the canonical implementation lives in
``ai_gateway_core.knowledge.proxy_client``. New code should import from
``ai_gateway_core.knowledge`` directly.
"""

from __future__ import annotations

from ai_gateway_core.knowledge import (
    KB_SERVICE_URL,
    KBProxyClient,
    ProxyRetrieveResult,
)

__all__ = ["KB_SERVICE_URL", "KBProxyClient", "ProxyRetrieveResult"]
