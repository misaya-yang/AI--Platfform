"""Compatibility shim — implementation moved to ``ai_gateway_contracts``.

ARC-04 first batch (2026-08-29): the runtime model-lease protocol now lives
in ``ai_gateway_contracts.agent_runtime_lease``.  This module only re-exports
it so existing import paths keep working.

Consumers still importing through this path
(reports/inventory/core-import-inventory.json, ``shim_consumers``):

- gateway: ``src/services/agent_runtime/{control_plane,model_plane}.py``,
  ``src/main.py`` — all via the ``ai_gateway_core.agents`` package re-export
- tests: ``tests/api/test_codex_runtime_lease.py``,
  ``tests/services/agent_runtime/{test_control_plane,test_model_plane,
  test_timing}.py``

Removal conditions (PRD §ARC-04 goal 5): delete this shim once every
consumer imports ``ai_gateway_contracts.agent_runtime_lease`` directly and
``scripts/core_boundary/check_core_boundary.py`` reports zero shim consumers
for it.  The shim identity test
``packages/ai-gateway-contracts/tests/test_shim_identity.py`` must be removed
together with it.
"""

from __future__ import annotations

from ai_gateway_contracts.agent_runtime_lease import (  # noqa: F401
    RUNTIME_MODEL_LEASE_SCHEMA_VERSION,
    RuntimeModelLeaseClaims,
    RuntimeModelLeaseError,
    RuntimeModelLeaseSigner,
)

__all__ = [
    "RUNTIME_MODEL_LEASE_SCHEMA_VERSION",
    "RuntimeModelLeaseClaims",
    "RuntimeModelLeaseError",
    "RuntimeModelLeaseSigner",
]
