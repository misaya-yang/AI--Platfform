"""Explicit durable SQLite repository for a local Local Node control plane.

This module is never auto-discovered by service startup.  Callers must provide
an explicit file path and ``development``/``test`` purpose, then inject the
repository through ``wire_local_node_control_plane``.  The repository uses a
single versioned snapshot inside a SQLite transaction; that is an internal
local-state format, not an application database migration.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import sqlite3
import stat
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, NoReturn, cast

from ...api.routes.local_nodes import LocalNodeServiceFault
from .control_plane import (
    ActionState,
    HealthState,
    LocalNodeState,
    _Action,
    _Device,
    _Event,
    _Grant,
    _PairingChallenge,
    canonical_digest,
)

_CODEC_VERSION = 1
_SCHEMA_VERSION = 1
_SINGLETON_ID = 1
_ALLOWED_ACTION_STATES = frozenset(
    {
        "proposed",
        "policy_check",
        "awaiting_approval",
        "dispatched",
        "running",
        "observed",
        "succeeded",
        "failed",
        "cancelled",
        "interrupted",
        "unknown",
    }
)
_ALLOWED_HEALTH_STATES = frozenset({"ready", "denied", "needs_action", "unsupported", "unknown"})
_ALLOWED_DEVICE_STATES = frozenset({"online", "offline", "stale", "revoked"})


def _repository_fault(code: str, cause: BaseException | None = None) -> NoReturn:
    fault = LocalNodeServiceFault(status_code=503, code=code)
    if cause is None:
        raise fault
    raise fault from cause


def _timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Local Node state contains a naive datetime")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: Any, *, optional: bool = False) -> datetime | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError("Local Node timestamp is invalid")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    result = datetime.fromisoformat(normalized)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Local Node timestamp is not timezone-aware")
    return result.astimezone(timezone.utc)


def _pack(value: Any) -> list[Any]:
    """Encode finite JSON-like values without ambiguous sentinel objects."""

    if value is None:
        return ["null"]
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, int):
        return ["int", value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Local Node state contains a non-finite number")
        return ["float", value]
    if isinstance(value, str):
        return ["str", value]
    if isinstance(value, datetime):
        return ["datetime", _timestamp(value)]
    if isinstance(value, Mapping):
        items: list[list[Any]] = []
        for key in sorted(value):
            if not isinstance(key, str):
                raise ValueError("Local Node state mappings require string keys")
            items.append([key, _pack(value[key])])
        return ["map", items]
    if isinstance(value, list):
        return ["list", [_pack(item) for item in value]]
    if isinstance(value, tuple):
        return ["tuple", [_pack(item) for item in value]]
    raise ValueError(f"unsupported Local Node state value: {type(value).__name__}")


def _unpack(value: Any) -> Any:
    if not isinstance(value, list) or not value or not isinstance(value[0], str):
        raise ValueError("Local Node tagged value is invalid")
    tag = value[0]
    if tag == "null" and len(value) == 1:
        return None
    if tag == "bool" and len(value) == 2 and isinstance(value[1], bool):
        return value[1]
    if (
        tag == "int"
        and len(value) == 2
        and isinstance(value[1], int)
        and not isinstance(value[1], bool)
    ):
        return value[1]
    if tag == "float" and len(value) == 2 and isinstance(value[1], (int, float)):
        float_result = float(value[1])
        if not math.isfinite(float_result):
            raise ValueError("Local Node tagged float is invalid")
        return float_result
    if tag == "str" and len(value) == 2 and isinstance(value[1], str):
        return value[1]
    if tag == "datetime" and len(value) == 2:
        return _parse_timestamp(value[1])
    if tag in {"list", "tuple"} and len(value) == 2 and isinstance(value[1], list):
        sequence_result = [_unpack(item) for item in value[1]]
        return tuple(sequence_result) if tag == "tuple" else sequence_result
    if tag == "map" and len(value) == 2 and isinstance(value[1], list):
        mapping_result: dict[str, Any] = {}
        for entry in value[1]:
            if (
                not isinstance(entry, list)
                or len(entry) != 2
                or not isinstance(entry[0], str)
                or entry[0] in mapping_result
            ):
                raise ValueError("Local Node tagged mapping is invalid")
            mapping_result[entry[0]] = _unpack(entry[1])
        return mapping_result
    raise ValueError("Local Node tagged value uses an unsupported shape")


def _record_common(record: Any) -> dict[str, Any]:
    return {
        "tenant_id": record.tenant_id,
        "user_id": record.user_id,
    }


def _state_document(state: LocalNodeState) -> dict[str, Any]:
    challenges = [
        {
            **_record_common(item),
            "challenge_id": item.challenge_id,
            "user_code_digest": item.user_code_digest,
            "expires_at": item.expires_at,
            "consumed_at": item.consumed_at,
        }
        for item in sorted(state.challenges.values(), key=lambda value: value.challenge_id)
    ]
    devices = [
        {
            **_record_common(item),
            "device_id": item.device_id,
            "display_name": item.display_name,
            "platform": item.platform,
            "node_version": item.node_version,
            "protocol_version": item.protocol_version,
            "channel_ref_digest": item.channel_ref_digest,
            "capability_ceiling": sorted(item.capability_ceiling),
            "capabilities": dict(sorted(item.capabilities.items())),
            "capability_revision": item.capability_revision,
            "created_at": item.created_at,
            "last_seen_at": item.last_seen_at,
            "status": item.status,
            "revoked_at": item.revoked_at,
            "permission_checked_at": item.permission_checked_at,
            "permissions": item.permissions,
        }
        for item in sorted(state.devices.values(), key=lambda value: value.device_id)
    ]
    grants = [
        {
            **_record_common(item),
            "grant_id": item.grant_id,
            "device_id": item.device_id,
            "kind": item.kind,
            "display_name": item.display_name,
            "capabilities": item.capabilities,
            "created_at": item.created_at,
            "resource_ref": item.resource_ref,
            "domain": item.domain,
            "session_id": item.session_id,
            "expires_at": item.expires_at,
            "revoked_at": item.revoked_at,
        }
        for item in sorted(state.grants.values(), key=lambda value: value.grant_id)
    ]
    actions = [
        {
            **_record_common(item),
            "action_id": item.action_id,
            "device_id": item.device_id,
            "session_id": item.session_id,
            "run_id": item.run_id,
            "call_id": item.call_id,
            "capability": item.capability,
            "status": item.status,
            "sequence": item.sequence,
            "created_at": item.created_at,
            "updated_at": item.updated_at,
            # Exact envelope is the durable outbox payload.  It is protected by
            # 0600 file permissions, never returned by public views, and never
            # included in exceptions or logs from this repository.
            "envelope": item.envelope,
            "envelope_digest": item.envelope_digest,
            "approved_envelope_digest": item.approved_envelope_digest,
            "authority_ref_digest": item.authority_ref_digest,
            "idempotency_key": item.idempotency_key,
            "grant_id": item.grant_id,
            "delivery_ref_digest": item.delivery_ref_digest,
            "terminal_event_id": item.terminal_event_id,
            "error_code": item.error_code,
            "observation_ref": item.observation_ref,
            "artifact_refs": item.artifact_refs,
            "approval_id": item.approval_id,
        }
        for item in sorted(state.actions.values(), key=lambda value: value.action_id)
    ]
    events = [
        {
            **_record_common(item),
            "event_id": item.event_id,
            "device_id": item.device_id,
            "sequence": item.sequence,
            "event_type": item.event_type,
            "occurred_at": item.occurred_at,
            "action_id": item.action_id,
            "status": item.status,
            "summary": item.summary,
            "result_digest": item.result_digest,
            "artifact_refs": item.artifact_refs,
            "error_code": item.error_code,
            "fingerprint": item.fingerprint,
        }
        for device_events in state.events.values()
        for item in device_events
    ]
    events.sort(key=lambda item: (item["device_id"], item["sequence"]))
    return {
        "codec_version": _CODEC_VERSION,
        "challenges": challenges,
        "devices": devices,
        "grants": grants,
        "actions": actions,
        "events": events,
        "approval_nonces": sorted([list(item) for item in state.approval_nonces]),
    }


def _encode_state(state: LocalNodeState) -> str:
    return json.dumps(
        _pack(_state_document(state)),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"Local Node {name} is invalid")
    return cast(dict[str, Any], value)


def _list(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"Local Node {name} is invalid")
    return value


def _required_str(record: Mapping[str, Any], name: str) -> str:
    value = record.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Local Node {name} is invalid")
    return value


def _optional_str(record: Mapping[str, Any], name: str) -> str | None:
    value = record.get(name)
    if value is not None and not isinstance(value, str):
        raise ValueError(f"Local Node {name} is invalid")
    return value


def _owner_values(record: Mapping[str, Any]) -> tuple[str, str]:
    return _required_str(record, "tenant_id"), _required_str(record, "user_id")


def _decode_state(payload: str) -> LocalNodeState:
    raw = json.loads(payload)
    document = _mapping(_unpack(raw), "document")
    if document.get("codec_version") != _CODEC_VERSION:
        raise ValueError("unsupported Local Node state codec")
    state = LocalNodeState()

    for raw_record in _list(document.get("challenges"), "challenges"):
        record = _mapping(raw_record, "challenge")
        tenant_id, user_id = _owner_values(record)
        challenge_id = _required_str(record, "challenge_id")
        if challenge_id in state.challenges:
            raise ValueError("duplicate Local Node pairing challenge")
        state.challenges[challenge_id] = _PairingChallenge(
            challenge_id=challenge_id,
            tenant_id=tenant_id,
            user_id=user_id,
            user_code_digest=_required_str(record, "user_code_digest"),
            expires_at=cast(datetime, record["expires_at"]),
            consumed_at=cast(datetime | None, record.get("consumed_at")),
        )

    for raw_record in _list(document.get("devices"), "devices"):
        record = _mapping(raw_record, "device")
        tenant_id, user_id = _owner_values(record)
        device_id = _required_str(record, "device_id")
        status = _required_str(record, "status")
        if status not in _ALLOWED_DEVICE_STATES or device_id in state.devices:
            raise ValueError("invalid or duplicate Local Node device")
        raw_capabilities = _mapping(record.get("capabilities"), "capabilities")
        if any(value not in _ALLOWED_HEALTH_STATES for value in raw_capabilities.values()):
            raise ValueError("invalid Local Node health state")
        ceiling = frozenset(
            _required_str({"value": item}, "value")
            for item in _list(record.get("capability_ceiling"), "capability ceiling")
        )
        if not set(raw_capabilities).issubset(ceiling):
            raise ValueError("Local Node health exceeds immutable capability ceiling")
        state.devices[device_id] = _Device(
            device_id=device_id,
            tenant_id=tenant_id,
            user_id=user_id,
            display_name=_required_str(record, "display_name"),
            platform=_required_str(record, "platform"),
            node_version=_required_str(record, "node_version"),
            protocol_version=_required_str(record, "protocol_version"),
            channel_ref_digest=_required_str(record, "channel_ref_digest"),
            capability_ceiling=ceiling,
            capabilities=cast(dict[str, HealthState], raw_capabilities),
            capability_revision=int(record["capability_revision"]),
            created_at=cast(datetime, record["created_at"]),
            last_seen_at=cast(datetime, record["last_seen_at"]),
            status=cast(Any, status),
            revoked_at=cast(datetime | None, record.get("revoked_at")),
            permission_checked_at=cast(datetime | None, record.get("permission_checked_at")),
            permissions=cast(list[dict[str, Any]], record.get("permissions", [])),
        )

    for raw_record in _list(document.get("grants"), "grants"):
        record = _mapping(raw_record, "grant")
        tenant_id, user_id = _owner_values(record)
        grant_id = _required_str(record, "grant_id")
        device_id = _required_str(record, "device_id")
        if grant_id in state.grants or device_id not in state.devices:
            raise ValueError("invalid or duplicate Local Node grant")
        state.grants[grant_id] = _Grant(
            grant_id=grant_id,
            tenant_id=tenant_id,
            user_id=user_id,
            device_id=device_id,
            kind=_required_str(record, "kind"),
            display_name=_required_str(record, "display_name"),
            capabilities=tuple(
                _required_str({"value": item}, "value")
                for item in cast(tuple[Any, ...], record.get("capabilities", ()))
            ),
            created_at=cast(datetime, record["created_at"]),
            resource_ref=_optional_str(record, "resource_ref"),
            domain=_optional_str(record, "domain"),
            session_id=_optional_str(record, "session_id"),
            expires_at=cast(datetime | None, record.get("expires_at")),
            revoked_at=cast(datetime | None, record.get("revoked_at")),
        )

    for raw_record in _list(document.get("actions"), "actions"):
        record = _mapping(raw_record, "action")
        tenant_id, user_id = _owner_values(record)
        action_id = _required_str(record, "action_id")
        device_id = _required_str(record, "device_id")
        grant_id = _required_str(record, "grant_id")
        status = _required_str(record, "status")
        envelope = _mapping(record.get("envelope"), "action envelope")
        envelope_digest = _required_str(record, "envelope_digest")
        approved_envelope_digest = _optional_str(record, "approved_envelope_digest")
        persisted_envelope_digest = canonical_digest(envelope)
        if (
            action_id in state.actions
            or device_id not in state.devices
            or grant_id not in state.grants
            or status not in _ALLOWED_ACTION_STATES
            or (approved_envelope_digest is None and persisted_envelope_digest != envelope_digest)
            or (
                approved_envelope_digest is not None
                and persisted_envelope_digest != approved_envelope_digest
            )
        ):
            raise ValueError("invalid or duplicate Local Node action fence")
        action = _Action(
            action_id=action_id,
            tenant_id=tenant_id,
            user_id=user_id,
            device_id=device_id,
            session_id=_required_str(record, "session_id"),
            run_id=_required_str(record, "run_id"),
            call_id=_required_str(record, "call_id"),
            capability=_required_str(record, "capability"),
            status=cast(ActionState, status),
            sequence=int(record["sequence"]),
            created_at=cast(datetime, record["created_at"]),
            updated_at=cast(datetime, record["updated_at"]),
            envelope=envelope,
            envelope_digest=envelope_digest,
            approved_envelope_digest=approved_envelope_digest,
            authority_ref_digest=_required_str(record, "authority_ref_digest"),
            idempotency_key=_required_str(record, "idempotency_key"),
            grant_id=grant_id,
            delivery_ref_digest=_optional_str(record, "delivery_ref_digest"),
            terminal_event_id=_optional_str(record, "terminal_event_id"),
            error_code=_optional_str(record, "error_code"),
            observation_ref=_optional_str(record, "observation_ref"),
            artifact_refs=tuple(cast(tuple[str, ...], record.get("artifact_refs", ()))),
            approval_id=_optional_str(record, "approval_id"),
        )
        index = (tenant_id, user_id, device_id, action.idempotency_key)
        if index in state.idempotency:
            raise ValueError("duplicate Local Node idempotency fence")
        state.actions[action_id] = action
        state.idempotency[index] = (action_id, envelope_digest)

    for raw_record in _list(document.get("events"), "events"):
        record = _mapping(raw_record, "event")
        tenant_id, user_id = _owner_values(record)
        event_id = _required_str(record, "event_id")
        device_id = _required_str(record, "device_id")
        sequence = int(record["sequence"])
        key = (device_id, event_id)
        device_events = state.events.setdefault(device_id, [])
        if key in state.event_ids or sequence != len(device_events) + 1:
            raise ValueError("invalid Local Node event ordering")
        raw_status = record.get("status")
        if raw_status is not None and raw_status not in _ALLOWED_ACTION_STATES:
            raise ValueError("invalid Local Node event status")
        event = _Event(
            event_id=event_id,
            tenant_id=tenant_id,
            user_id=user_id,
            device_id=device_id,
            sequence=sequence,
            event_type=_required_str(record, "event_type"),
            occurred_at=cast(datetime, record["occurred_at"]),
            action_id=_optional_str(record, "action_id"),
            status=cast(ActionState | None, raw_status),
            summary=_optional_str(record, "summary"),
            result_digest=_optional_str(record, "result_digest"),
            artifact_refs=tuple(cast(tuple[str, ...], record.get("artifact_refs", ()))),
            error_code=_optional_str(record, "error_code"),
            fingerprint=_required_str(record, "fingerprint"),
        )
        device_events.append(event)
        state.event_ids[key] = event

    for raw_nonce in _list(document.get("approval_nonces"), "approval nonces"):
        if (
            not isinstance(raw_nonce, list)
            or len(raw_nonce) != 4
            or any(not isinstance(value, str) or not value for value in raw_nonce)
        ):
            raise ValueError("invalid Local Node approval nonce")
        nonce = cast(tuple[str, str, str, str], tuple(raw_nonce))
        if nonce in state.approval_nonces:
            raise ValueError("duplicate Local Node approval nonce")
        state.approval_nonces.add(nonce)
    return state


class SQLiteLocalNodeRepository:
    """Durable local repository with a committed pre-delivery action fence.

    The class is intentionally restricted to explicit development/test use. It
    is durable across process restart, but is not advertised as a distributed
    or production HA store.
    """

    supported_environments = frozenset({"development", "test"})

    def __init__(
        self,
        path: str | Path,
        *,
        purpose: Literal["development", "test"],
        busy_timeout_ms: int = 5_000,
    ) -> None:
        if purpose not in self.supported_environments:
            raise ValueError("SQLite Local Node state is restricted to development/test")
        if busy_timeout_ms < 1 or busy_timeout_ms > 60_000:
            raise ValueError("SQLite Local Node busy timeout is invalid")
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            raise ValueError("SQLite Local Node path must be absolute")
        parent = candidate.parent.resolve(strict=True)
        resolved = parent / candidate.name
        if resolved.exists() and (resolved.is_symlink() or not resolved.is_file()):
            raise ValueError("SQLite Local Node path must be a regular file")
        self.path = resolved
        self.purpose = purpose
        self._lock = asyncio.Lock()
        self._closed = False
        self.durable_dispatch_fence = False
        self._prepare_private_file()
        try:
            self._connection = sqlite3.connect(
                str(self.path),
                isolation_level=None,
                timeout=busy_timeout_ms / 1000,
            )
            self._connection.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._initialize_schema()
        except BaseException:
            with suppress(Exception):
                self._connection.close()
            raise
        self.durable_dispatch_fence = True

    def _prepare_private_file(self) -> None:
        if not self.path.exists():
            descriptor = os.open(
                self.path,
                os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            os.close(descriptor)
        mode = stat.S_IMODE(os.stat(self.path, follow_symlinks=False).st_mode)
        if mode & 0o077:
            raise PermissionError("SQLite Local Node state file must not be group/world accessible")

    def _initialize_schema(self) -> None:
        connection = self._connection
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version not in {0, _SCHEMA_VERSION}:
            raise RuntimeError("unsupported SQLite Local Node schema version")
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS local_node_state (
                    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
                    codec_version INTEGER NOT NULL,
                    generation INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    checksum TEXT NOT NULL
                )
                """
            )
            empty = _encode_state(LocalNodeState())
            checksum = hashlib.sha256(empty.encode("utf-8")).hexdigest()
            connection.execute(
                """
                INSERT OR IGNORE INTO local_node_state
                    (singleton_id, codec_version, generation, payload, checksum)
                VALUES (?, ?, ?, ?, ?)
                """,
                (_SINGLETON_ID, _CODEC_VERSION, 0, empty, checksum),
            )
            connection.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
            connection.execute("COMMIT")
        except BaseException:
            with suppress(sqlite3.Error):
                connection.execute("ROLLBACK")
            raise

    def _load(self) -> tuple[int, str, LocalNodeState]:
        row = self._connection.execute(
            """
            SELECT codec_version, generation, payload, checksum
            FROM local_node_state
            WHERE singleton_id = ?
            """,
            (_SINGLETON_ID,),
        ).fetchone()
        if row is None or int(row[0]) != _CODEC_VERSION:
            raise ValueError("SQLite Local Node state row is invalid")
        generation = int(row[1])
        payload = str(row[2])
        expected = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        if not secrets_compare(expected, str(row[3])):
            raise ValueError("SQLite Local Node state checksum mismatch")
        return generation, payload, _decode_state(payload)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[LocalNodeState]:
        if self._closed or not self.durable_dispatch_fence:
            _repository_fault("LOCAL_NODE_REPOSITORY_UNAVAILABLE")
        async with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                generation, original_payload, state = self._load()
            except (sqlite3.Error, ValueError, TypeError, KeyError) as exc:
                with suppress(sqlite3.Error):
                    self._connection.execute("ROLLBACK")
                _repository_fault("LOCAL_NODE_REPOSITORY_CORRUPT", exc)
            try:
                yield state
            except BaseException:
                with suppress(sqlite3.Error):
                    self._connection.execute("ROLLBACK")
                raise
            try:
                payload = _encode_state(state)
                if payload != original_payload:
                    checksum = hashlib.sha256(payload.encode("utf-8")).hexdigest()
                    cursor = self._connection.execute(
                        """
                        UPDATE local_node_state
                        SET generation = ?, payload = ?, checksum = ?
                        WHERE singleton_id = ? AND generation = ?
                        """,
                        (
                            generation + 1,
                            payload,
                            checksum,
                            _SINGLETON_ID,
                            generation,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise sqlite3.OperationalError("Local Node generation conflict")
                self._connection.execute("COMMIT")
            except (sqlite3.Error, ValueError, TypeError, KeyError) as exc:
                with suppress(sqlite3.Error):
                    self._connection.execute("ROLLBACK")
                _repository_fault("LOCAL_NODE_REPOSITORY_UNAVAILABLE", exc)

    def close(self) -> None:
        if self._closed:
            return
        self.durable_dispatch_fence = False
        self._closed = True
        with suppress(sqlite3.Error):
            self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self._connection.close()

    def __enter__(self) -> SQLiteLocalNodeRepository:
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()


def secrets_compare(left: str, right: str) -> bool:
    """Constant-time checksum comparison without exposing stored payloads."""

    import hmac

    return hmac.compare_digest(left, right)


__all__ = ["SQLiteLocalNodeRepository"]
