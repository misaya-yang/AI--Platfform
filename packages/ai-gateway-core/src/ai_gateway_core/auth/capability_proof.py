"""Compatibility shim — implementation moved to ``ai_gateway_contracts``.

ARC-04 first batch (2026-08-29): the capability-proof protocol now lives in
``ai_gateway_contracts.capability_proof``.  This module only re-exports it so
existing import paths keep working.

Consumers still importing through this path
(reports/inventory/core-import-inventory.json, ``shim_consumers``):

- gateway: ``src/api/internal/{agent,attachment,confluence,image}_capabilities.py``,
  ``src/api/internal/office_artifacts.py``, ``src/api/internal_mcp_broker.py``,
  ``src/api/v1/agent_local_nodes.py`` (plus ``ai_gateway_core.auth`` re-export)
- knowledge: ``apps/knowledge-service/src/knowledge_service/api/routes/capability_plane.py``
- tests: ``tests/packages/ai_gateway_core/test_capability_proof.py``,
  ``tests/api/test_{internal_agent,confluence_capability,image_capability}_*.py``,
  ``tests/api/test_internal_mcp_broker.py``,
  ``tests/services/knowledge/test_capability_plane.py``

Removal conditions (PRD §ARC-04 goal 5): delete this shim once every
consumer above imports ``ai_gateway_contracts.capability_proof`` directly and
``scripts/core_boundary/check_core_boundary.py`` reports zero shim consumers
for it.  The shim identity test
``packages/ai-gateway-contracts/tests/test_shim_identity.py`` must be removed
together with it.
"""

from __future__ import annotations

from ai_gateway_contracts.capability_proof import (  # noqa: F401
    HEADER_NAME,
    MAX_TTL_SECONDS,
    SCHEMA_VERSION,
    CapabilityProof,
    CapabilityProofError,
    canonical_body_hash,
    canonical_json,
    sign_capability_proof,
    verify_capability_proof,
)

__all__ = [
    "CapabilityProof",
    "CapabilityProofError",
    "HEADER_NAME",
    "MAX_TTL_SECONDS",
    "SCHEMA_VERSION",
    "canonical_body_hash",
    "canonical_json",
    "sign_capability_proof",
    "verify_capability_proof",
]
