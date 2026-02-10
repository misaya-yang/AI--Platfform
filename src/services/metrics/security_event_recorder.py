"""
Security Event Recorder - Persistent aggregates for auth failures and rate limits.

Records events into daily aggregate table for historical dashboards.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ...persistence.database import DatabaseStorage

logger = logging.getLogger(__name__)

_security_event_recorder: Optional["SecurityEventRecorder"] = None


class SecurityEventRecorder:
    def __init__(self, database: Optional["DatabaseStorage"] = None):
        self.database = database

    def set_database(self, database: "DatabaseStorage") -> None:
        self.database = database

    async def record_event(
        self,
        tenant_id: str,
        user_id: Optional[str],
        service_id: Optional[str],
        event_type: str,
        event_date: Optional[date] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not self.database or not self.database._pool:
            return
        try:
            await self.database.record_security_event(
                tenant_id=tenant_id,
                user_id=user_id,
                service_id=service_id,
                event_type=event_type,
                event_date=event_date,
                metadata=metadata,
            )
        except Exception as exc:
            logger.debug(f"Failed to record security event: {exc}")


def get_security_event_recorder() -> SecurityEventRecorder:
    global _security_event_recorder
    if _security_event_recorder is None:
        _security_event_recorder = SecurityEventRecorder()
    return _security_event_recorder


def init_security_event_recorder(database: "DatabaseStorage") -> SecurityEventRecorder:
    global _security_event_recorder
    if _security_event_recorder is None:
        _security_event_recorder = SecurityEventRecorder(database)
    else:
        _security_event_recorder.set_database(database)
    return _security_event_recorder
