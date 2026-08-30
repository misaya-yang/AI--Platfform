"""Compatibility shim — implementation moved to ``ai_gateway_contracts``.

ARC-04 first batch (2026-08-29): the Agent Runtime snapshot/envelope protocol
now lives in ``ai_gateway_contracts.agent_runtime`` (replay-store protocol in
``ai_gateway_contracts.replay``).  This module only re-exports it so existing
import paths keep working.

Consumers still importing through this path
(reports/inventory/core-import-inventory.json, ``shim_consumers``):

- gateway: ``src/services/agent_runtime/{control_plane,model_plane,
  runtime_configuration}.py``, ``src/api/v1/{agent_public,agent_runtime,
  agents}.py``, ``src/services/agent_runtime_cleanup.py``, ``src/main.py``
  — all via the ``ai_gateway_core.agents`` package re-export
- tests: ``tests/api/test_agent_runtime_envelope.py``,
  ``tests/services/agent_runtime/*``, ``tests/api/test_agent_publish_api.py``
  and the other ``ai_gateway_core.agents`` test consumers

Removal conditions (PRD §ARC-04 goal 5): delete this shim once every
consumer imports ``ai_gateway_contracts.agent_runtime`` directly and
``scripts/core_boundary/check_core_boundary.py`` reports zero shim consumers
for it.  The shim identity test
``packages/ai-gateway-contracts/tests/test_shim_identity.py`` must be removed
together with it.
"""

from __future__ import annotations

from ai_gateway_contracts.agent_runtime import (  # noqa: F401
    AGENT_RUNTIME_ENVELOPE_SCHEMA_VERSION,
    AGENT_RUNTIME_SCHEMA_VERSION,
    AgentRuntimeEnvelopeError,
    AgentRuntimeSigner,
    VerifiedAgentRuntime,
    agent_memory_principal,
    canonical_runtime_json,
    runtime_sha256,
)
from ai_gateway_contracts.replay import InMemoryReplayStore, ReplayStore  # noqa: F401

__all__ = [
    "AGENT_RUNTIME_ENVELOPE_SCHEMA_VERSION",
    "AGENT_RUNTIME_SCHEMA_VERSION",
    "AgentRuntimeEnvelopeError",
    "AgentRuntimeSigner",
    "InMemoryReplayStore",
    "ReplayStore",
    "VerifiedAgentRuntime",
    "agent_memory_principal",
    "canonical_runtime_json",
    "runtime_sha256",
]
