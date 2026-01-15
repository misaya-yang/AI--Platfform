"""
Data Status Computation

Provides utilities for computing data freshness status for usage analytics.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional, Tuple


def compute_data_status(
    last_ingested_at: Optional[datetime],
    total_requests: Optional[int] = None,
) -> Tuple[str, Optional[int]]:
    """
    Compute the data freshness status based on the last ingested timestamp.

    Args:
        last_ingested_at: Timestamp of the last ingested data.
        total_requests: Total number of requests (if 0 or None, may indicate no data).

    Returns:
        Tuple of (status, freshness_minutes):
        - status: One of "live", "delayed", "stale", "no_data"
        - freshness_minutes: Minutes since last ingestion (or None if no data)
    """
    if last_ingested_at is None:
        return ("no_data", None)

    if total_requests is not None and total_requests == 0:
        return ("no_data", None)

    now = datetime.utcnow()
    delta = now - last_ingested_at
    freshness_minutes = int(delta.total_seconds() / 60)

    # Status thresholds
    if freshness_minutes <= 5:
        status = "live"
    elif freshness_minutes <= 60:
        status = "delayed"
    else:
        status = "stale"

    return (status, freshness_minutes)
