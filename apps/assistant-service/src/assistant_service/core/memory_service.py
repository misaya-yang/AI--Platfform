"""Memory Service — re-export shim.

Phase 5d moved the canonical implementation to
``ai_gateway_core.memory.MemoryService`` so gateway's DI container can
instantiate the service without a compile-time dep on
``assistant_service``. This shim keeps existing AS-internal imports
(``from ..memory_service import MemoryService``) working; delete once
every AS site migrates.
"""

from __future__ import annotations

from ai_gateway_core.memory import MemoryService

__all__ = ["MemoryService"]
