"""Process-local delegation identity, idempotency, lineage, and concurrency guards.

This module deliberately does not implement background execution.  It owns only
bounded in-process state used by the synchronous streaming consumer.  A process
restart loses completed-result reuse, so callers must not treat this registry as
a durable workflow store.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from ai_gateway_core.logging import get_logger, log_internal_exception

logger = get_logger(__name__)

DEFAULT_MAX_SUBAGENT_DEPTH = 1
OPERATOR_MAX_SUBAGENT_DEPTH = 2
DEFAULT_TENANT_SUBAGENT_CONCURRENCY = 10
DEFAULT_SESSION_SUBAGENT_CONCURRENCY = 5
DEFAULT_DISPATCH_MAX_RECORDS = 512
DEFAULT_DISPATCH_INFLIGHT_TTL_SECONDS = 300.0
DEFAULT_DISPATCH_UNCERTAIN_TTL_SECONDS = 900.0
DEFAULT_DISPATCH_COMPLETED_TTL_SECONDS = 3600.0

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class SubAgentDispatchError(RuntimeError):
    """Base class for fail-closed dispatch errors."""


class SubAgentDispatchConflict(SubAgentDispatchError):
    """A stable delegation id was reused with a different request."""


class SubAgentDispatchInFlight(SubAgentDispatchError):
    """The exact logical delegation is already executing."""


class SubAgentDispatchUncertain(SubAgentDispatchError):
    """A prior attempt ended without a complete terminal receipt."""


class SubAgentDispatchCapacityExceeded(SubAgentDispatchError):
    """The bounded process-local dispatch ledger has no safe eviction candidate."""


class SubAgentDepthExceeded(SubAgentDispatchError):
    """A child would exceed the operator-owned delegation depth."""


class SubAgentCycleDetected(SubAgentDispatchError):
    """A logical task already exists in its parent lineage."""


class SubAgentConcurrencyExceeded(SubAgentDispatchError):
    """A tenant or session has no remaining process-local child slots."""


def canonical_sha256(value: Any) -> str:
    """Return a deterministic digest for a JSON-compatible dispatch value."""

    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def stable_identifier(explicit: Any, *, prefix: str, payload: Any) -> str:
    """Validate an explicit id or derive one solely from canonical task data."""

    if explicit is not None:
        value = str(explicit).strip()
        if not _IDENTIFIER_RE.fullmatch(value):
            raise ValueError(f"{prefix}_id must match {_IDENTIFIER_RE.pattern}")
        return value
    return f"{prefix}_{canonical_sha256(payload)[:20]}"


@dataclass(frozen=True)
class DispatchScope:
    tenant_id: str
    session_id: str
    run_id: str = "unknown"

    @classmethod
    def from_parent(cls, parent: Any | None, fallback_tenant_id: str = "") -> DispatchScope:
        return cls(
            tenant_id=str(getattr(parent, "tenant_id", None) or fallback_tenant_id or "unknown"),
            session_id=str(getattr(parent, "session_id", None) or "unknown"),
            run_id=str(getattr(parent, "run_id", None) or "unknown"),
        )


@dataclass(frozen=True)
class DispatchDecision:
    action: Literal["start", "reuse"]
    receipt: dict[str, Any] | None = None


@dataclass
class _DispatchRecord:
    request_sha256: str
    state: Literal["inflight", "completed", "uncertain"]
    created_at: float
    updated_at: float
    receipt: dict[str, Any] | None = None
    abort_reason: str | None = None
    side_effect_unknown: bool = False


class SubAgentDispatchRegistry:
    """Bounded-process idempotency registry scoped by tenant and session."""

    def __init__(
        self,
        *,
        max_completed: int = 256,
        max_records: int = DEFAULT_DISPATCH_MAX_RECORDS,
        inflight_ttl_seconds: float = DEFAULT_DISPATCH_INFLIGHT_TTL_SECONDS,
        uncertain_ttl_seconds: float = DEFAULT_DISPATCH_UNCERTAIN_TTL_SECONDS,
        completed_ttl_seconds: float = DEFAULT_DISPATCH_COMPLETED_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            isinstance(max_completed, bool)
            or not isinstance(max_completed, int)
            or isinstance(max_records, bool)
            or not isinstance(max_records, int)
            or max_completed <= 0
            or max_records <= 0
            or max_completed > max_records
        ):
            raise ValueError("dispatch registry capacities are invalid")
        for name, value in (
            ("inflight_ttl_seconds", inflight_ttl_seconds),
            ("uncertain_ttl_seconds", uncertain_ttl_seconds),
            ("completed_ttl_seconds", completed_ttl_seconds),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} must be positive")
        self.max_completed = max_completed
        self.max_records = max_records
        self.inflight_ttl_seconds = float(inflight_ttl_seconds)
        self.uncertain_ttl_seconds = float(uncertain_ttl_seconds)
        self.completed_ttl_seconds = float(completed_ttl_seconds)
        self._clock = clock
        self._records: dict[tuple[str, str, str, str], _DispatchRecord] = {}
        self._completed_order: list[tuple[str, str, str, str]] = []
        self._lock = threading.Lock()

    @staticmethod
    def _key(scope: DispatchScope, delegation_id: str) -> tuple[str, str, str, str]:
        # A deterministic delegation id is idempotent only inside one parent
        # run.  Reusing it across later turns in the same session would return
        # stale legal/financial/research work from an unrelated run.
        return (scope.tenant_id, scope.session_id, scope.run_id, delegation_id)

    def _prune_locked(self, now: float) -> None:
        for key, record in list(self._records.items()):
            age = max(0.0, now - record.updated_at)
            if record.state == "inflight" and age >= self.inflight_ttl_seconds:
                # A missing coordinator terminal is never equivalent to a safe
                # retry. Quarantine it first; a later uncertain TTL only bounds
                # this explicitly non-durable process ledger.
                record.state = "uncertain"
                record.updated_at = now
                record.receipt = None
                record.abort_reason = "inflight_ttl_expired"
                record.side_effect_unknown = True
            elif (record.state == "uncertain" and age >= self.uncertain_ttl_seconds) or (
                record.state == "completed" and age >= self.completed_ttl_seconds
            ):
                self._records.pop(key, None)
        self._completed_order = [
            key
            for key in self._completed_order
            if (record := self._records.get(key)) is not None and record.state == "completed"
        ]

    def _make_capacity_locked(self) -> None:
        while len(self._records) >= self.max_records and self._completed_order:
            evicted = self._completed_order.pop(0)
            record = self._records.get(evicted)
            if record is not None and record.state == "completed":
                self._records.pop(evicted, None)
        if len(self._records) >= self.max_records:
            raise SubAgentDispatchCapacityExceeded(
                "dispatch ledger is full of active or quarantined records"
            )

    def begin(
        self,
        scope: DispatchScope,
        *,
        delegation_id: str,
        request_sha256: str,
    ) -> DispatchDecision:
        key = self._key(scope, delegation_id)
        with self._lock:
            now = self._clock()
            self._prune_locked(now)
            existing = self._records.get(key)
            if existing is None:
                self._make_capacity_locked()
                self._records[key] = _DispatchRecord(
                    request_sha256=request_sha256,
                    state="inflight",
                    created_at=now,
                    updated_at=now,
                )
                return DispatchDecision(action="start")
            if existing.request_sha256 != request_sha256:
                raise SubAgentDispatchConflict("delegation_id payload conflict")
            if existing.state == "inflight":
                raise SubAgentDispatchInFlight("delegation is already in flight")
            if existing.state == "uncertain":
                raise SubAgentDispatchUncertain(
                    "delegation outcome is uncertain; automatic replay is unsafe"
                )
            if existing.side_effect_unknown:
                raise SubAgentDispatchUncertain(
                    "delegation has unknown side effects; cached reuse is unsafe"
                )
            return DispatchDecision(
                action="reuse",
                receipt=copy.deepcopy(existing.receipt or {}),
            )

    def touch(
        self,
        scope: DispatchScope,
        *,
        delegation_id: str,
        request_sha256: str,
    ) -> None:
        """Heartbeat an actively consumed dispatch without changing its outcome."""

        key = self._key(scope, delegation_id)
        with self._lock:
            existing = self._records.get(key)
            if (
                existing is not None
                and existing.request_sha256 == request_sha256
                and existing.state == "inflight"
            ):
                existing.updated_at = self._clock()

    def complete(
        self,
        scope: DispatchScope,
        *,
        delegation_id: str,
        request_sha256: str,
        receipt: dict[str, Any],
    ) -> None:
        if not isinstance(receipt, dict) or not receipt:
            raise ValueError("dispatch completion requires a terminal receipt")
        key = self._key(scope, delegation_id)
        with self._lock:
            existing = self._records.get(key)
            if existing is None or existing.request_sha256 != request_sha256:
                raise SubAgentDispatchConflict("delegation registry completion conflict")
            if existing.state == "uncertain" or existing.side_effect_unknown:
                raise SubAgentDispatchUncertain(
                    "an uncertain delegation cannot be promoted to reusable"
                )
            if existing.state != "inflight":
                raise SubAgentDispatchConflict("delegation is already terminal")
            existing.state = "completed"
            existing.receipt = copy.deepcopy(receipt)
            existing.updated_at = self._clock()
            existing.abort_reason = None
            if key not in self._completed_order:
                self._completed_order.append(key)
            while len(self._completed_order) > self.max_completed:
                evicted = self._completed_order.pop(0)
                self._records.pop(evicted, None)

    def abort(
        self,
        scope: DispatchScope,
        *,
        delegation_id: str,
        request_sha256: str,
        reason: str,
        side_effect_unknown: bool = True,
    ) -> None:
        """Terminally quarantine an unfinished claim; safe to call repeatedly."""

        key = self._key(scope, delegation_id)
        with self._lock:
            existing = self._records.get(key)
            if existing is None or existing.request_sha256 != request_sha256:
                return
            if existing.state == "completed":
                return
            existing.state = "uncertain"
            existing.receipt = None
            existing.updated_at = self._clock()
            existing.abort_reason = reason[:160]
            existing.side_effect_unknown = existing.side_effect_unknown or side_effect_unknown

    def mark_uncertain(
        self,
        scope: DispatchScope,
        *,
        delegation_id: str,
        request_sha256: str,
    ) -> None:
        self.abort(
            scope,
            delegation_id=delegation_id,
            request_sha256=request_sha256,
            reason="dispatch_terminal_uncertain",
            side_effect_unknown=True,
        )


@dataclass
class SubAgentDispatchCoordinator:
    """Own one synchronous dispatch claim from begin through terminal state."""

    registry: SubAgentDispatchRegistry
    scope: DispatchScope
    delegation_id: str
    request_sha256: str
    decision: DispatchDecision
    _open: bool

    @classmethod
    def begin(
        cls,
        registry: SubAgentDispatchRegistry,
        scope: DispatchScope,
        *,
        delegation_id: str,
        request_sha256: str,
    ) -> SubAgentDispatchCoordinator:
        decision = registry.begin(
            scope,
            delegation_id=delegation_id,
            request_sha256=request_sha256,
        )
        return cls(
            registry=registry,
            scope=scope,
            delegation_id=delegation_id,
            request_sha256=request_sha256,
            decision=decision,
            _open=decision.action == "start",
        )

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def _identity(self) -> dict[str, str]:
        return {
            "delegation_id": self.delegation_id,
            "request_sha256": self.request_sha256,
        }

    def touch(self) -> None:
        if self._open:
            self.registry.touch(self.scope, **self._identity)

    def finish(self, *, receipt: dict[str, Any], reusable: bool) -> None:
        if not self._open:
            return
        if reusable:
            self.registry.complete(self.scope, receipt=receipt, **self._identity)
        else:
            self.registry.mark_uncertain(self.scope, **self._identity)
        self._open = False

    def abort(self, reason: str) -> None:
        if not self._open:
            return
        self.registry.abort(
            self.scope,
            reason=reason,
            side_effect_unknown=True,
            **self._identity,
        )
        self._open = False


@dataclass(frozen=True)
class SubAgentConcurrencyLease:
    limiter: Any
    scope: DispatchScope
    count: int

    def release(self) -> None:
        self.limiter.release(self)


class SubAgentConcurrencyLimiter:
    """Atomic tenant/session active-child slot accounting with optional Redis distributed backend."""

    def __init__(
        self,
        *,
        tenant_limit: int = DEFAULT_TENANT_SUBAGENT_CONCURRENCY,
        session_limit: int = DEFAULT_SESSION_SUBAGENT_CONCURRENCY,
        redis_client: Any | None = None,
        ttl_seconds: int = 300,
    ) -> None:
        if tenant_limit <= 0 or session_limit <= 0 or session_limit > tenant_limit:
            raise ValueError("invalid tenant/session sub-agent concurrency limits")
        self.tenant_limit = tenant_limit
        self.session_limit = session_limit
        self.redis_client = redis_client
        self.ttl_seconds = ttl_seconds
        self._tenant_active: dict[str, int] = {}
        self._session_active: dict[tuple[str, str], int] = {}
        self._lock = threading.Lock()

    def configure_redis(self, redis_client: Any | None, ttl_seconds: int = 300) -> None:
        """Attach or update the Redis distributed client for multi-worker deployments."""
        self.redis_client = redis_client
        self.ttl_seconds = ttl_seconds

    def acquire(self, scope: DispatchScope, count: int) -> SubAgentConcurrencyLease:
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise ValueError("concurrency lease count must be a positive integer")
        session_key = (scope.tenant_id, scope.session_id)

        # 1. Process-local atomic check
        with self._lock:
            tenant_used = self._tenant_active.get(scope.tenant_id, 0)
            session_used = self._session_active.get(session_key, 0)
            if tenant_used + count > self.tenant_limit:
                raise SubAgentConcurrencyExceeded("tenant sub-agent concurrency exhausted")
            if session_used + count > self.session_limit:
                raise SubAgentConcurrencyExceeded("session sub-agent concurrency exhausted")
            self._tenant_active[scope.tenant_id] = tenant_used + count
            self._session_active[session_key] = session_used + count

        # 2. Redis distributed accounting if client is attached
        if self.redis_client is not None:
            tenant_redis_key = f"subagent:concurrency:tenant:{scope.tenant_id}"
            session_redis_key = f"subagent:concurrency:session:{scope.tenant_id}:{scope.session_id}"
            try:
                if hasattr(self.redis_client, "incrby"):
                    # Synchronous redis client
                    cur_tenant = self.redis_client.incrby(tenant_redis_key, count)
                    if hasattr(self.redis_client, "expire"):
                        self.redis_client.expire(tenant_redis_key, self.ttl_seconds)
                    cur_session = self.redis_client.incrby(session_redis_key, count)
                    if hasattr(self.redis_client, "expire"):
                        self.redis_client.expire(session_redis_key, self.ttl_seconds)

                    if cur_tenant > self.tenant_limit or cur_session > self.session_limit:
                        # Roll back
                        self.redis_client.decrby(tenant_redis_key, count)
                        self.redis_client.decrby(session_redis_key, count)
                        # Roll back local lock
                        with self._lock:
                            self._tenant_active[scope.tenant_id] = max(
                                0, self._tenant_active.get(scope.tenant_id, 0) - count
                            )
                            self._session_active[session_key] = max(
                                0, self._session_active.get(session_key, 0) - count
                            )
                        if cur_tenant > self.tenant_limit:
                            raise SubAgentConcurrencyExceeded("distributed tenant sub-agent concurrency exhausted")
                        raise SubAgentConcurrencyExceeded("distributed session sub-agent concurrency exhausted")
            except SubAgentConcurrencyExceeded:
                raise
            except Exception as exc:
                # Log warning and preserve local in-memory lease if Redis is momentarily unreachable
                log_internal_exception(
                    logger,
                    "assistant.subagent_concurrency.redis_acquire_failed",
                    exc,
                    level=logging.WARNING,
                )

        return SubAgentConcurrencyLease(self, scope, count)

    def release(self, lease: SubAgentConcurrencyLease) -> None:
        session_key = (lease.scope.tenant_id, lease.scope.session_id)
        with self._lock:
            tenant_remaining = max(
                0,
                self._tenant_active.get(lease.scope.tenant_id, 0) - lease.count,
            )
            session_remaining = max(
                0,
                self._session_active.get(session_key, 0) - lease.count,
            )
            if tenant_remaining:
                self._tenant_active[lease.scope.tenant_id] = tenant_remaining
            else:
                self._tenant_active.pop(lease.scope.tenant_id, None)
            if session_remaining:
                self._session_active[session_key] = session_remaining
            else:
                self._session_active.pop(session_key, None)

        if self.redis_client is not None:
            tenant_redis_key = f"subagent:concurrency:tenant:{lease.scope.tenant_id}"
            session_redis_key = f"subagent:concurrency:session:{lease.scope.tenant_id}:{lease.scope.session_id}"
            try:
                if hasattr(self.redis_client, "decrby"):
                    rem_t = self.redis_client.decrby(tenant_redis_key, lease.count)
                    if rem_t <= 0 and hasattr(self.redis_client, "delete"):
                        self.redis_client.delete(tenant_redis_key)
                    rem_s = self.redis_client.decrby(session_redis_key, lease.count)
                    if rem_s <= 0 and hasattr(self.redis_client, "delete"):
                        self.redis_client.delete(session_redis_key)
            except Exception as exc:
                log_internal_exception(
                    logger,
                    "assistant.subagent_concurrency.redis_release_failed",
                    exc,
                    level=logging.WARNING,
                )


# These registries are intentionally process-local. They enforce hard bounds and
# exact-repeat semantics across AgentLoop instances in one worker, but are not a
# durable resume mechanism across worker restarts.
GLOBAL_SUBAGENT_DISPATCH_REGISTRY = SubAgentDispatchRegistry()
GLOBAL_SUBAGENT_CONCURRENCY_LIMITER = SubAgentConcurrencyLimiter()
