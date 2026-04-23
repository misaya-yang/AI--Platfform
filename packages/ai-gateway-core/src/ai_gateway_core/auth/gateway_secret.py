"""Gateway → microservice HMAC signing (``X-Gateway-Secret``).

Design reference: ``plans/SystemDesign-Assistant-Service-True-Extraction-Phase5-2026-04-23.md`` §4.5.

Trust model — only the **middle hop** is secured by this module; the outer
(browser→gateway) is JWT/API-key, the inner (as→DB) is DSN-level.

Header format
-------------
One header, one value::

    X-Gateway-Secret: v1:<request_id>:<epoch_ms>:<hmac_hex>

where ``hmac_hex = HMAC_SHA256(secret, f"{request_id}:{epoch_ms}")``.

Verification rules
------------------
1. Shape: 4 colon-separated segments with ``v1`` prefix.
2. Signature must match a constant-time ``hmac.compare_digest`` of the
   computed HMAC over ``f"{request_id}:{epoch_ms}"``.
3. Timestamp must be within ``max_skew_ms`` of the verifier's clock
   (default ±60s).
4. Replay protection: ``request_id`` is remembered in a seen-ids store
   with a TTL slightly longer than ``max_skew_ms`` so a freshly signed
   request can't be replayed within its validity window.

Replay store
------------
``InMemoryReplayStore`` is process-local. For multi-worker / multi-instance
deployments, swap in a Redis-backed impl (same ``ReplayStore`` protocol).
"""
from __future__ import annotations

import hmac
import logging
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol

logger = logging.getLogger(__name__)

_SIG_PREFIX = "v1"
_DEFAULT_MAX_SKEW_MS = 60_000
_DEFAULT_REPLAY_TTL_MS = 120_000  # 2× max skew — covers worst-case window


class InvalidGatewaySecret(Exception):
    """Raised when ``X-Gateway-Secret`` fails verification.

    Middleware translates this to ``HTTP 401`` so clients never see the
    underlying reason (prevents oracle-style probing of the secret).
    """


class ReplayStore(Protocol):
    """Contract for a seen-request-id store with TTL."""

    def seen_or_record(self, request_id: str, ttl_ms: int) -> bool:
        """Record ``request_id``. Return True if it was already present."""
        ...


class InMemoryReplayStore:
    """LRU-bounded seen-ids store.

    Safe for concurrent access within a single process. Entries expire
    after their TTL; capacity is bounded to prevent unbounded growth.
    """

    __slots__ = ("_seen", "_lock", "_capacity")

    def __init__(self, capacity: int = 10_000) -> None:
        self._seen: OrderedDict[str, int] = OrderedDict()
        self._lock = threading.Lock()
        self._capacity = capacity

    def seen_or_record(self, request_id: str, ttl_ms: int) -> bool:
        now_ms = _epoch_ms()
        with self._lock:
            self._evict_expired(now_ms)
            existing = self._seen.get(request_id)
            if existing is not None and existing > now_ms:
                return True
            self._seen[request_id] = now_ms + ttl_ms
            self._seen.move_to_end(request_id)
            while len(self._seen) > self._capacity:
                self._seen.popitem(last=False)
            return False

    def _evict_expired(self, now_ms: int) -> None:
        stale: list[str] = []
        for rid, expires in self._seen.items():
            if expires <= now_ms:
                stale.append(rid)
            else:
                break
        for rid in stale:
            self._seen.pop(rid, None)


@dataclass
class GatewaySecret:
    """Bi-directional HMAC ``X-Gateway-Secret`` handler.

    Both gateway (signer) and assistant-service (verifier) construct
    this with the same ``secret`` env var (``GATEWAY_ASSISTANT_SHARED_SECRET``).
    """

    secret: str
    max_skew_ms: int = _DEFAULT_MAX_SKEW_MS
    replay_ttl_ms: int = _DEFAULT_REPLAY_TTL_MS
    replay_store: ReplayStore | None = None
    header_name: str = "X-Gateway-Secret"

    def __post_init__(self) -> None:
        if not self.secret or len(self.secret) < 16:
            raise ValueError(
                "GATEWAY_ASSISTANT_SHARED_SECRET must be at least 16 chars"
            )
        if self.replay_store is None:
            self.replay_store = InMemoryReplayStore()

    # ----- sign -----

    def sign(self, request_id: str | None = None) -> str:
        """Produce a fresh header value. ``request_id`` auto-generated if omitted."""
        rid = request_id or secrets.token_hex(16)
        ts = str(_epoch_ms())
        sig = self._hmac(rid, ts)
        return f"{_SIG_PREFIX}:{rid}:{ts}:{sig}"

    # ----- verify -----

    def verify(self, header_value: str | None) -> str:
        """Verify ``header_value``. Returns the ``request_id`` on success.

        Raises ``InvalidGatewaySecret`` for any failure mode. The middleware
        translates that to ``401 Unauthorized``.
        """
        if not header_value:
            raise InvalidGatewaySecret("missing header")

        parts = header_value.split(":")
        if len(parts) != 4 or parts[0] != _SIG_PREFIX:
            raise InvalidGatewaySecret("malformed header")

        _, rid, ts_str, sig = parts
        try:
            ts = int(ts_str)
        except ValueError as e:
            raise InvalidGatewaySecret("bad timestamp") from e

        now = _epoch_ms()
        if abs(now - ts) > self.max_skew_ms:
            raise InvalidGatewaySecret("timestamp skew")

        expected = self._hmac(rid, ts_str)
        if not hmac.compare_digest(expected, sig):
            raise InvalidGatewaySecret("signature mismatch")

        # Replay protection — must happen AFTER signature validation so
        # a forged header can't populate the seen set and DoS legit
        # request_ids.
        assert self.replay_store is not None  # post_init ensures this
        if self.replay_store.seen_or_record(rid, self.replay_ttl_ms):
            raise InvalidGatewaySecret("replay detected")

        return rid

    # ----- internals -----

    def _hmac(self, request_id: str, ts: str) -> str:
        mac = hmac.new(
            self.secret.encode("utf-8"),
            f"{request_id}:{ts}".encode("utf-8"),
            sha256,
        )
        return mac.hexdigest()


def _epoch_ms() -> int:
    return int(time.time() * 1000)


__all__ = [
    "GatewaySecret",
    "InvalidGatewaySecret",
    "InMemoryReplayStore",
    "ReplayStore",
]
