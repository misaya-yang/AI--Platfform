"""Compatibility shim — implementation moved to ``ai_gateway_contracts``.

ARC-04 first batch (2026-08-29): the event envelope protocol now lives in
``ai_gateway_contracts.event_envelope`` (error taxonomy in
``ai_gateway_contracts.event_errors``).  This module only re-exports it so
existing import paths keep working.  ``_PAYLOAD_MODELS`` is re-exported too
because the schema-evolution contract test reads it.

Consumers still importing through this path
(reports/inventory/core-import-inventory.json, ``shim_consumers``):

- gateway: ``src/main.py``, ``src/services/test usage recorder`` — via the
  ``ai_gateway_core.events`` package re-export
- core-internal: ``ai_gateway_core.events.{__init__,bus,consumer}``
- tests: ``tests/contract/test_event_schema_evolution.py`` (direct
  ``ai_gateway_core.events.envelope`` import), ``tests/events/*``,
  ``tests/services/test_usage_recorder_event_publish.py``

Removal conditions (PRD §ARC-04 goal 5): delete this shim once every
consumer imports ``ai_gateway_contracts.event_envelope`` directly and
``scripts/core_boundary/check_core_boundary.py`` reports zero shim consumers
for it.  The shim identity test
``packages/ai-gateway-contracts/tests/test_shim_identity.py`` must be removed
together with it.
"""

from __future__ import annotations

from ai_gateway_contracts.event_envelope import (  # noqa: F401
    _PAYLOAD_MODELS,
    EventEnvelope,
    PayloadT,
    UsageRecordedV1,
    parse_envelope,
    payload_for,
)

__all__ = [
    "EventEnvelope",
    "UsageRecordedV1",
    "parse_envelope",
    "payload_for",
]
