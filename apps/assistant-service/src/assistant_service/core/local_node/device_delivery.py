"""Durable, device-authenticated delivery for bounded read-only file actions.

This is an explicit development/test adapter.  It persists the exact signed
claim until a paired outbound device reports a terminal result.  It never
opens a listener, discovers credentials, or enables itself at import time.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import secrets
import sqlite3
import stat
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol, runtime_checkable

from .control_plane import canonical_digest
from .provider_adapter import validate_file_result

_FILE_OPERATIONS: dict[str, tuple[str, str]] = {
    "file.list": ("file.list", "local_file_list"),
    "file.read": ("file.read", "local_file_read"),
    "file.hash": ("file.read", "local_file_hash"),
    "file.search": ("file.search", "local_file_search"),
    "file.watch": ("file.watch", "local_file_watch"),
}
_TERMINAL = frozenset({"succeeded", "failed", "cancelled", "interrupted", "unknown"})
_MAX_RESULT_BYTES = 1_200_000
_MAX_COMMAND_BYTES = 256_000


@runtime_checkable
class LocalNodeControlCommandSigner(Protocol):
    """Trusted platform signer injected independently from device messages."""

    @property
    def key_id(self) -> str: ...

    def sign(self, payload: bytes) -> str: ...


@dataclass(frozen=True, slots=True)
class PreparedFileResult:
    action_id: str
    result: dict[str, Any]
    result_digest: str


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _plain_digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _required_string(value: Any, *, maximum: int = 4096) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(ord(character) < 0x20 for character in value)
    ):
        raise ValueError("Local Node delivery value is invalid")
    return value


def _safe_relative(value: Any, *, allow_root: bool) -> str:
    path = _required_string(value, maximum=2048)
    parts = PurePosixPath(path).parts
    if path == "." and allow_root:
        return path
    if (
        path.startswith(("/", "\\"))
        or "\\" in path
        or not parts
        or ":" in parts[0]
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise ValueError("Local Node delivery path is invalid")
    return path


def _bounded_integer(
    value: Any,
    *,
    minimum: int,
    maximum: int,
    default: int,
) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError("Local Node delivery budget is invalid")
    return value


def _validate_arguments(operation: str, raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError("Local Node delivery arguments are invalid")
    arguments = dict(raw)
    allowed: dict[str, frozenset[str]] = {
        "file.list": frozenset({"grant_id", "path", "limit"}),
        "file.read": frozenset({"grant_id", "path", "max_bytes"}),
        "file.hash": frozenset({"grant_id", "path", "max_bytes"}),
        "file.search": frozenset({"grant_id", "path", "query", "limit"}),
        "file.watch": frozenset({"grant_id", "path", "after_revision", "timeout_ms"}),
    }
    if operation not in allowed or not {"grant_id", "path"}.issubset(arguments):
        raise ValueError("Local Node delivery arguments are invalid")
    if not set(arguments).issubset(allowed[operation]):
        raise ValueError("Local Node delivery arguments are invalid")
    _required_string(arguments["grant_id"], maximum=200)
    _safe_relative(
        arguments["path"],
        allow_root=operation in {"file.list", "file.search", "file.watch"},
    )
    if operation == "file.list":
        _bounded_integer(arguments.get("limit"), minimum=1, maximum=500, default=500)
    elif operation in {"file.read", "file.hash"}:
        _bounded_integer(
            arguments.get("max_bytes"),
            minimum=1,
            maximum=8 * 1024 * 1024,
            default=8 * 1024 * 1024,
        )
    elif operation == "file.search":
        query = _required_string(arguments.get("query"), maximum=500)
        if "\x00" in query:
            raise ValueError("Local Node search query is invalid")
        _bounded_integer(arguments.get("limit"), minimum=1, maximum=200, default=200)
    else:
        after = arguments.get("after_revision")
        if after is not None:
            _required_string(after, maximum=200)
        _bounded_integer(
            arguments.get("timeout_ms"),
            minimum=1,
            maximum=30_000,
            default=1_000,
        )
    return arguments


class SQLiteDeviceDelivery:
    """Private SQLite command/result queue for explicit local/test wiring."""

    idempotent_enqueue = True
    supported_environments = frozenset({"development", "test"})

    def __init__(
        self,
        path: str | Path,
        *,
        purpose: Literal["development", "test"],
        control_signer: LocalNodeControlCommandSigner | None = None,
        now: Any = time.time,
    ) -> None:
        if purpose not in self.supported_environments:
            raise ValueError("SQLite device delivery is restricted to development/test")
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            raise ValueError("SQLite device delivery path must be absolute")
        candidate.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        parent = candidate.parent.resolve(strict=True)
        self.path = parent / candidate.name
        if self.path.exists() and (self.path.is_symlink() or not self.path.is_file()):
            raise ValueError("SQLite device delivery path must be a regular file")
        if not self.path.exists():
            descriptor = os.open(
                self.path,
                os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            os.close(descriptor)
        if stat.S_IMODE(os.stat(self.path, follow_symlinks=False).st_mode) & 0o077:
            raise PermissionError("SQLite device delivery must be private")
        if control_signer is not None and not isinstance(
            control_signer, LocalNodeControlCommandSigner
        ):
            raise TypeError("trusted Local Node control signer is invalid")
        self.control_signer = control_signer
        self._now = now
        self._lock = asyncio.Lock()
        self._closed = False
        self._db = sqlite3.connect(
            str(self.path),
            check_same_thread=False,
            isolation_level=None,
        )
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA secure_delete=ON")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS device_deliveries (
              delivery_id TEXT PRIMARY KEY,
              tenant_id TEXT NOT NULL,
              user_id TEXT NOT NULL,
              device_id TEXT NOT NULL,
              action_id TEXT UNIQUE NOT NULL,
              idempotency_key TEXT NOT NULL,
              envelope_digest TEXT NOT NULL,
              capability TEXT NOT NULL,
              operation TEXT NOT NULL,
              tool_name TEXT NOT NULL,
              grant_id TEXT NOT NULL,
              arguments_json TEXT NOT NULL,
              command_json TEXT NOT NULL,
              command_digest TEXT NOT NULL,
              action_expires_at REAL NOT NULL,
              state TEXT NOT NULL CHECK(state IN
                ('queued','delivered','succeeded','failed','cancelled','interrupted','unknown')),
              created_at REAL NOT NULL,
              delivered_at REAL,
              terminal_at REAL,
              result_json TEXT,
              result_digest TEXT,
              consumed_at REAL,
              UNIQUE(tenant_id,user_id,device_id,idempotency_key)
            );
            CREATE INDEX IF NOT EXISTS device_delivery_poll
              ON device_deliveries(tenant_id,user_id,device_id,state,created_at);
            CREATE TABLE IF NOT EXISTS device_control_commands (
              command_id TEXT PRIMARY KEY,
              tenant_id TEXT NOT NULL,
              user_id TEXT NOT NULL,
              device_id TEXT NOT NULL,
              action_id TEXT,
              command_json TEXT NOT NULL,
              command_digest TEXT NOT NULL,
              expires_at REAL NOT NULL,
              state TEXT NOT NULL CHECK(state IN ('queued','delivered','done')),
              created_at REAL NOT NULL
            );
            """
        )

    @staticmethod
    def _command_from_envelope(
        *,
        tenant_id: str,
        user_id: str,
        device_id: str,
        action_id: str,
        idempotency_key: str,
        envelope_digest: str,
        envelope: Mapping[str, Any],
        command_id: str,
    ) -> tuple[dict[str, Any], str, str, str, str, float]:
        if canonical_digest(envelope) != envelope_digest:
            raise ValueError("Local Node delivery envelope digest changed")
        signed = envelope.get("signed_action")
        server_arguments = envelope.get("normalized_arguments")
        if not isinstance(signed, Mapping) or not isinstance(server_arguments, Mapping):
            raise ValueError("Local Node signed delivery is incomplete")
        if set(server_arguments) != {"grant_id", "model_arguments"}:
            raise ValueError("Local Node server arguments are invalid")
        operation = _required_string(signed.get("operation"), maximum=80)
        expected = _FILE_OPERATIONS.get(operation)
        if expected is None:
            raise PermissionError("device delivery accepts read-only file actions only")
        capability, tool_name = expected
        grant_id = _required_string(server_arguments.get("grant_id"), maximum=200)
        arguments = _validate_arguments(operation, server_arguments.get("model_arguments"))
        if arguments.get("grant_id") != grant_id:
            raise PermissionError("Local Node grant binding changed")
        required_values = {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "device_id": device_id,
            "action_id": action_id,
            "idempotency_key": idempotency_key,
            "capability": capability,
            "tool_name": tool_name,
            "operation": operation,
            "capability_lease_id": grant_id,
            "arguments_digest": _plain_digest(arguments),
        }
        if any(signed.get(name) != value for name, value in required_values.items()):
            raise PermissionError("Local Node signed delivery binding changed")
        if not isinstance(signed.get("agent_version"), str) or not signed.get("agent_version"):
            raise PermissionError("Local Node agent version is not signed")
        if not isinstance(signed.get("platform_signature"), str) or not signed.get(
            "platform_signature"
        ):
            raise PermissionError("Local Node platform signature is absent")
        resources = signed.get("resource_refs")
        if not isinstance(resources, list) or len(resources) != 2:
            raise PermissionError("Local Node resource binding is invalid")
        if resources != [grant_id, arguments["path"]]:
            raise PermissionError("Local Node resource binding changed")
        expires_at = signed.get("expires_at")
        if (
            isinstance(expires_at, bool)
            or not isinstance(expires_at, (int, float))
            or not math.isfinite(float(expires_at))
            or float(expires_at) <= time.time()
        ):
            raise PermissionError("Local Node signed delivery is expired")
        command = {
            "kind": "claim",
            "command_id": command_id,
            "action": dict(signed),
            "normalized_arguments": arguments,
        }
        encoded = _canonical_json(command).encode("utf-8")
        if len(encoded) > _MAX_COMMAND_BYTES:
            raise ValueError("Local Node delivery command is too large")
        return command, capability, operation, tool_name, grant_id, float(expires_at)

    async def enqueue_action(
        self,
        *,
        tenant_id: str,
        user_id: str,
        device_id: str,
        action_id: str,
        idempotency_key: str,
        envelope_digest: str,
        envelope: Mapping[str, Any],
    ) -> str:
        values = tuple(
            _required_string(item, maximum=512)
            for item in (tenant_id, user_id, device_id, action_id, idempotency_key)
        )
        tenant_id, user_id, device_id, action_id, idempotency_key = values
        command_id = "cmd_" + secrets.token_urlsafe(24)
        command, capability, operation, tool_name, grant_id, expires_at = (
            self._command_from_envelope(
                tenant_id=tenant_id,
                user_id=user_id,
                device_id=device_id,
                action_id=action_id,
                idempotency_key=idempotency_key,
                envelope_digest=envelope_digest,
                envelope=envelope,
                command_id=command_id,
            )
        )
        arguments = dict(envelope["normalized_arguments"]["model_arguments"])
        command_json = _canonical_json(command)
        command_digest = _plain_digest(command)
        now = float(self._now())
        async with self._lock:
            self._require_open()
            existing = self._db.execute(
                "SELECT delivery_id,envelope_digest FROM device_deliveries WHERE action_id=?",
                (action_id,),
            ).fetchone()
            if existing is not None:
                if existing[1] != envelope_digest:
                    raise ValueError("Local Node delivery idempotency conflict")
                return str(existing[0])
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute(
                    "INSERT INTO device_deliveries VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                    "'queued',?,NULL,NULL,NULL,NULL,NULL)",
                    (
                        command_id,
                        tenant_id,
                        user_id,
                        device_id,
                        action_id,
                        idempotency_key,
                        envelope_digest,
                        capability,
                        operation,
                        tool_name,
                        grant_id,
                        _canonical_json(arguments),
                        command_json,
                        command_digest,
                        expires_at,
                        now,
                    ),
                )
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        return command_id

    async def claim_commands(
        self,
        *,
        tenant_id: str,
        user_id: str,
        device_id: str,
        limit: int = 100,
    ) -> tuple[dict[str, Any], ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("Local Node command batch limit is invalid")
        now = float(self._now())
        async with self._lock:
            self._require_open()
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute(
                    "UPDATE device_deliveries SET state='failed',terminal_at=? "
                    "WHERE tenant_id=? AND user_id=? AND device_id=? "
                    "AND state IN ('queued','delivered') AND action_expires_at<=?",
                    (now, tenant_id, user_id, device_id, now),
                )
                rows = self._db.execute(
                    "SELECT delivery_id,command_json,command_digest FROM device_deliveries "
                    "WHERE tenant_id=? AND user_id=? AND device_id=? "
                    "AND state IN ('queued','delivered') AND action_expires_at>? "
                    "ORDER BY created_at,delivery_id LIMIT ?",
                    (tenant_id, user_id, device_id, now, limit),
                ).fetchall()
                remaining = limit - len(rows)
                controls = []
                if remaining > 0:
                    controls = self._db.execute(
                        "SELECT command_id,command_json,command_digest "
                        "FROM device_control_commands WHERE tenant_id=? AND user_id=? "
                        "AND device_id=? AND state IN ('queued','delivered') AND expires_at>? "
                        "ORDER BY created_at,command_id LIMIT ?",
                        (tenant_id, user_id, device_id, now, remaining),
                    ).fetchall()
                ids = [str(row[0]) for row in rows]
                control_ids = [str(row[0]) for row in controls]
                if ids:
                    self._db.executemany(
                        "UPDATE device_deliveries SET state='delivered',"
                        "delivered_at=COALESCE(delivered_at,?) WHERE delivery_id=?",
                        [(now, item) for item in ids],
                    )
                if control_ids:
                    self._db.executemany(
                        "UPDATE device_control_commands SET state='delivered' WHERE command_id=?",
                        [(item,) for item in control_ids],
                    )
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        commands: list[dict[str, Any]] = []
        for _, raw, expected_digest in [*rows, *controls]:
            decoded = json.loads(str(raw))
            if not isinstance(decoded, dict) or _plain_digest(decoded) != expected_digest:
                raise RuntimeError("Local Node delivery command integrity failed")
            commands.append(decoded)
        return tuple(commands)

    async def prepare_result_receipts(
        self,
        *,
        tenant_id: str,
        user_id: str,
        device_id: str,
        receipts: list[Mapping[str, Any]],
    ) -> tuple[PreparedFileResult, ...]:
        prepared: list[PreparedFileResult] = []
        async with self._lock:
            self._require_open()
            for receipt in receipts:
                raw_result = receipt.get("result")
                if raw_result is None:
                    continue
                if (
                    receipt.get("event_type") != "action.succeeded"
                    or receipt.get("status") != "succeeded"
                ):
                    raise ValueError("Local Node result requires a succeeded terminal receipt")
                action_id = _required_string(receipt.get("action_id"), maximum=512)
                row = self._db.execute(
                    "SELECT operation,arguments_json,state FROM device_deliveries "
                    "WHERE tenant_id=? AND user_id=? AND device_id=? AND action_id=?",
                    (tenant_id, user_id, device_id, action_id),
                ).fetchone()
                if row is None or row[2] not in {"queued", "delivered", "succeeded"}:
                    raise PermissionError("Local Node result has no matching delivery")
                if not isinstance(raw_result, Mapping):
                    raise ValueError("Local Node result is invalid")
                arguments = json.loads(str(row[1]))
                normalized = validate_file_result(
                    operation=str(row[0]),
                    arguments=arguments,
                    value=raw_result,
                )
                encoded = _canonical_json(normalized).encode("utf-8")
                if len(encoded) > _MAX_RESULT_BYTES:
                    raise ValueError("Local Node result exceeds the channel budget")
                expected_digest = _plain_digest(normalized)
                supplied_digest = receipt.get("result_digest")
                if supplied_digest != expected_digest:
                    raise ValueError("Local Node result digest mismatch")
                prepared.append(PreparedFileResult(action_id, normalized, expected_digest))
        return tuple(prepared)

    async def accept_prepared_results(
        self,
        *,
        tenant_id: str,
        user_id: str,
        device_id: str,
        results: tuple[PreparedFileResult, ...],
    ) -> None:
        if not results:
            return
        now = float(self._now())
        async with self._lock:
            self._require_open()
            self._db.execute("BEGIN IMMEDIATE")
            try:
                for item in results:
                    existing = self._db.execute(
                        "SELECT state,result_digest,result_json FROM device_deliveries "
                        "WHERE tenant_id=? AND user_id=? AND device_id=? AND action_id=?",
                        (tenant_id, user_id, device_id, item.action_id),
                    ).fetchone()
                    if existing is None:
                        raise PermissionError("Local Node result delivery disappeared")
                    result_json = _canonical_json(item.result)
                    if existing[0] == "succeeded":
                        if existing[1] != item.result_digest or existing[2] != result_json:
                            raise ValueError("Local Node result replay conflict")
                        continue
                    if existing[0] not in {"queued", "delivered"}:
                        raise ValueError("Local Node result conflicts with terminal delivery")
                    self._db.execute(
                        "UPDATE device_deliveries SET state='succeeded',terminal_at=?,"
                        "result_json=?,result_digest=? WHERE action_id=?",
                        (now, result_json, item.result_digest, item.action_id),
                    )
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    async def await_result(
        self,
        *,
        tenant_id: str,
        user_id: str,
        device_id: str,
        action_id: str,
        timeout_seconds: float,
    ) -> Mapping[str, Any] | None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or not 0 < timeout_seconds <= 60
        ):
            raise ValueError("Local Node result wait budget is invalid")
        deadline = time.monotonic() + float(timeout_seconds)
        while True:
            async with self._lock:
                self._require_open()
                row = self._db.execute(
                    "SELECT state,result_json,result_digest,consumed_at FROM device_deliveries "
                    "WHERE tenant_id=? AND user_id=? AND device_id=? AND action_id=?",
                    (tenant_id, user_id, device_id, action_id),
                ).fetchone()
                if row is None:
                    raise PermissionError("Local Node result action is not bound to this owner")
                if row[0] == "succeeded":
                    if row[3] is not None or row[1] is None:
                        return None
                    value = json.loads(str(row[1]))
                    if not isinstance(value, dict) or _plain_digest(value) != row[2]:
                        raise RuntimeError("Local Node result integrity failed")
                    self._db.execute(
                        "UPDATE device_deliveries SET consumed_at=COALESCE(consumed_at,?),"
                        "result_json=NULL "
                        "WHERE action_id=?",
                        (float(self._now()), action_id),
                    )
                    self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    return value
                if row[0] in _TERMINAL:
                    return None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            await asyncio.sleep(min(0.05, remaining))

    async def cancel_action(
        self,
        *,
        tenant_id: str,
        user_id: str,
        device_id: str,
        action_id: str,
    ) -> None:
        now = float(self._now())
        async with self._lock:
            self._require_open()
            row = self._db.execute(
                "SELECT state FROM device_deliveries WHERE tenant_id=? AND user_id=? "
                "AND device_id=? AND action_id=?",
                (tenant_id, user_id, device_id, action_id),
            ).fetchone()
            if row is None:
                raise PermissionError("Local Node cancellation has no matching delivery")
            if row[0] == "queued":
                self._db.execute(
                    "UPDATE device_deliveries SET state='cancelled',terminal_at=? "
                    "WHERE action_id=?",
                    (now, action_id),
                )
                return
            if row[0] in _TERMINAL:
                return
            command = self._signed_control("cancel", device_id=device_id, action_id=action_id)
            self._insert_control(
                tenant_id=tenant_id,
                user_id=user_id,
                device_id=device_id,
                action_id=action_id,
                command=command,
            )

    async def enqueue_emergency_stop(
        self,
        *,
        tenant_id: str,
        user_id: str,
        device_id: str,
    ) -> str:
        command = self._signed_control("emergency_stop", device_id=device_id)
        async with self._lock:
            self._require_open()
            return self._insert_control(
                tenant_id=tenant_id,
                user_id=user_id,
                device_id=device_id,
                action_id=None,
                command=command,
            )

    def _signed_control(
        self,
        kind: str,
        *,
        device_id: str,
        action_id: str | None = None,
    ) -> dict[str, Any]:
        signer = self.control_signer
        if signer is None:
            raise RuntimeError("trusted Local Node control signer is unavailable")
        issued_at = float(self._now())
        unsigned: dict[str, Any] = {
            "kind": kind,
            "command_id": "ctl_" + secrets.token_urlsafe(24),
            "device_id": device_id,
            "nonce": secrets.token_urlsafe(24),
            "issued_at": issued_at,
            "expires_at": issued_at + 60,
            "platform_key_id": signer.key_id,
        }
        if action_id is not None:
            unsigned["action_id"] = action_id
        signature = signer.sign(_canonical_json(unsigned).encode("utf-8"))
        if not isinstance(signature, str) or not signature:
            raise RuntimeError("trusted Local Node control signer returned no signature")
        return {**unsigned, "platform_signature": signature}

    def _insert_control(
        self,
        *,
        tenant_id: str,
        user_id: str,
        device_id: str,
        action_id: str | None,
        command: Mapping[str, Any],
    ) -> str:
        command_id = str(command["command_id"])
        self._db.execute(
            "INSERT OR IGNORE INTO device_control_commands("
            "command_id,tenant_id,user_id,device_id,action_id,command_json,"
            "command_digest,expires_at,state,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,'queued',?)",
            (
                command_id,
                tenant_id,
                user_id,
                device_id,
                action_id,
                _canonical_json(command),
                _plain_digest(command),
                float(command["expires_at"]),
                float(self._now()),
            ),
        )
        return command_id

    async def revoke_device(
        self,
        *,
        tenant_id: str,
        user_id: str,
        device_id: str,
    ) -> None:
        now = float(self._now())
        async with self._lock:
            self._require_open()
            self._db.execute(
                "UPDATE device_deliveries SET state='interrupted',terminal_at=? "
                "WHERE tenant_id=? AND user_id=? AND device_id=? "
                "AND state IN ('queued','delivered')",
                (now, tenant_id, user_id, device_id),
            )
            self._db.execute(
                "UPDATE device_control_commands SET state='done' WHERE tenant_id=? "
                "AND user_id=? AND device_id=? AND state!='done'",
                (tenant_id, user_id, device_id),
            )

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("SQLite device delivery is closed")

    def secret_canary_absent(self, *values: str) -> bool:
        self._db.execute("PRAGMA wal_checkpoint(FULL)")
        material = self.path.read_bytes()
        wal = Path(str(self.path) + "-wal")
        if wal.exists():
            material += wal.read_bytes()
        return all(value.encode("utf-8") not in material for value in values)

    def close(self) -> None:
        self._closed = True
        self._db.close()


__all__ = [
    "LocalNodeControlCommandSigner",
    "PreparedFileResult",
    "SQLiteDeviceDelivery",
]
