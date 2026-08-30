"""Compatibility shim — implementation moved to ``ai_gateway_contracts``.

ARC-04 first batch (2026-08-29): the event-bus error taxonomy now lives in
``ai_gateway_contracts.event_errors`` because the envelope parser
(``parse_envelope``) raises ``EventDeserializationError`` as part of the
wire contract.  This module only re-exports it so existing import paths keep
working.

Consumers still importing through this path
(reports/inventory/core-import-inventory.json, ``shim_consumers``):

- core-internal: ``ai_gateway_core.events.{__init__,bus,consumer,envelope}``
- tests: ``tests/events/test_bus_publish.py`` (direct
  ``ai_gateway_core.events.errors`` import)

Removal conditions (PRD §ARC-04 goal 5): delete this shim once every
consumer imports ``ai_gateway_contracts.event_errors`` directly and
``scripts/core_boundary/check_core_boundary.py`` reports zero shim consumers
for it.  The shim identity test
``packages/ai-gateway-contracts/tests/test_shim_identity.py`` must be removed
together with it.
"""

from __future__ import annotations

from ai_gateway_contracts.event_errors import (  # noqa: F401
    EventBusError,
    EventDeserializationError,
    EventHandlerError,
)

__all__ = [
    "EventBusError",
    "EventDeserializationError",
    "EventHandlerError",
]
