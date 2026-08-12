"""Durable authentication for the outbound Local Node device channel.

This module is deliberately independent from browser authentication.  The Web
owner creates a short-lived challenge through the existing API; a device then
redeems it with the exact human code and an Ed25519 proof.  Only public key
material and SHA-256 credential/code digests are stored.  The device credential
is opaque, scoped, expiring, revocable, and accepted only in the ``Device``
Authorization scheme.

No listener is created here.  The FastAPI route is the SaaS endpoint contacted
by a Local Node's outbound HTTPS client.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import math
import os
import secrets
import sqlite3
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from ...api.routes.local_nodes import LocalNodeServiceFault
from .protocol import LOCAL_NODE_PROTOCOL_VERSION

PROTOCOL_VERSION = LOCAL_NODE_PROTOCOL_VERSION
MAX_DEVICE_CREDENTIAL_TTL_SECONDS = 3600
_MAX_CLOCK_SKEW_SECONDS = 30


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _b64url_decode(value: str, *, expected_length: int, field: str) -> bytes:
    if not value or len(value) > 256 or any(ord(character) < 0x21 for character in value):
        raise LocalNodeServiceFault(status_code=422, code=f"LOCAL_NODE_{field}_INVALID")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError) as exc:
        raise LocalNodeServiceFault(
            status_code=422,
            code=f"LOCAL_NODE_{field}_INVALID",
        ) from exc
    if len(decoded) != expected_length:
        raise LocalNodeServiceFault(status_code=422, code=f"LOCAL_NODE_{field}_INVALID")
    return decoded


def pairing_redemption_digest(
    *,
    challenge_id: str,
    user_code: str,
    device_id: str,
    proof_algorithm: str,
    proof_public_key: str,
    display_name: str,
    platform: str,
    node_version: str,
    capability_claims: tuple[str, ...],
    permission_snapshot_digest: str,
) -> str:
    preproof = {
        "protocol_version": PROTOCOL_VERSION,
        "kind": "pairing_redeem",
        "challenge_id": challenge_id,
        "user_code_digest": _sha256_text(user_code),
        "device_id": device_id,
        "proof_algorithm": proof_algorithm,
        "proof_public_key": proof_public_key,
        "display_name": display_name,
        "platform": platform,
        "node_version": node_version,
        "capability_claims": sorted(capability_claims),
        "permission_snapshot_digest": permission_snapshot_digest,
    }
    return "sha256:" + hashlib.sha256(_canonical_json(preproof).encode("utf-8")).hexdigest()


def pairing_proof_payload(redemption_digest: str) -> bytes:
    return _canonical_json({"redemption_digest": redemption_digest}).encode("utf-8")


@dataclass(frozen=True, slots=True)
class DeviceChannelPrincipal:
    tenant_id: str
    user_id: str
    device_id: str
    channel_id: str


class PairingControlService(Protocol):
    async def complete_pairing(self, **values: Any) -> Any: ...

    async def get_device_status(self, **values: Any) -> Any: ...

    async def record_capability_snapshot(self, **values: Any) -> Any: ...

    async def record_permission_snapshot(self, **values: Any) -> Any: ...

    async def append_events(self, **values: Any) -> Any: ...


class DeviceDeliveryBroker(Protocol):
    async def prepare_result_receipts(self, **values: Any) -> Any: ...

    async def accept_prepared_results(self, **values: Any) -> None: ...

    async def claim_commands(self, **values: Any) -> tuple[dict[str, Any], ...]: ...


class SQLiteDeviceChannelBroker:
    """SQLite credential fence for one explicit Assistant composition root.

    The database can persist challenges and credential revocation across
    process restarts.  It is not auto-created by application startup: callers
    must supply an explicit state path and wire this broker into ``app.state``.
    """

    def __init__(
        self,
        path: Path,
        *,
        control_service: PairingControlService,
        device_delivery: DeviceDeliveryBroker | None = None,
        credential_ttl_seconds: int = MAX_DEVICE_CREDENTIAL_TTL_SECONDS,
        now: Any = time.time,
    ) -> None:
        if (
            credential_ttl_seconds <= 0
            or credential_ttl_seconds > MAX_DEVICE_CREDENTIAL_TTL_SECONDS
        ):
            raise ValueError("device credential TTL must be between 1 and 3600 seconds")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = path
        self.control_service = control_service
        self.device_delivery = device_delivery
        self.credential_ttl_seconds = credential_ttl_seconds
        self._now = now
        self._lock = asyncio.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        os.chmod(path, 0o600)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS device_pairing_challenges (
              challenge_id TEXT PRIMARY KEY,
              tenant_id TEXT NOT NULL,
              user_id TEXT NOT NULL,
              user_code_digest TEXT NOT NULL,
              expires_at REAL NOT NULL,
              attempted_at REAL,
              completed_at REAL
            );
            CREATE TABLE IF NOT EXISTS device_channel_credentials (
              credential_digest TEXT PRIMARY KEY,
              tenant_id TEXT NOT NULL,
              user_id TEXT NOT NULL,
              device_id TEXT NOT NULL,
              channel_id TEXT UNIQUE NOT NULL,
              proof_algorithm TEXT NOT NULL,
              proof_public_key TEXT NOT NULL,
              protocol_version TEXT NOT NULL,
              issued_at REAL NOT NULL,
              expires_at REAL NOT NULL,
              status TEXT NOT NULL CHECK(status IN ('pending','active','revoked')),
              last_seen_at REAL,
              accepted_through_sequence INTEGER NOT NULL DEFAULT 0
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_device_credential
              ON device_channel_credentials(device_id)
              WHERE status IN ('pending','active');
            """
        )

    async def register_challenge(
        self,
        *,
        tenant_id: str,
        user_id: str,
        challenge: Mapping[str, Any],
    ) -> None:
        challenge_id = str(challenge.get("challenge_id", ""))
        user_code = str(challenge.get("user_code", ""))
        expires_at = challenge.get("expires_at")
        if not challenge_id or not user_code or not isinstance(expires_at, datetime):
            raise LocalNodeServiceFault(
                status_code=503,
                code="LOCAL_NODE_PAIRING_CHALLENGE_INVALID",
            )
        if expires_at.tzinfo is None or expires_at.utcoffset() is None:
            raise LocalNodeServiceFault(
                status_code=503,
                code="LOCAL_NODE_PAIRING_CHALLENGE_INVALID",
            )
        async with self._lock:
            try:
                self._db.execute(
                    "INSERT INTO device_pairing_challenges VALUES(?,?,?,?,?,NULL,NULL)",
                    (
                        challenge_id,
                        tenant_id,
                        user_id,
                        _sha256_text(user_code),
                        expires_at.timestamp(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise LocalNodeServiceFault(
                    status_code=409,
                    code="LOCAL_NODE_PAIRING_REPLAYED",
                ) from exc

    async def redeem_pairing(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Consume a challenge, verify exact Ed25519 proof, and issue a credential.

        A syntactically valid redemption attempt is consumed before checking
        the human code or signature.  This prevents online guessing by retrying
        the same challenge.  Malformed HTTP bodies are rejected at the route's
        bounded parser and never reach a credential operation.
        """

        challenge_id = str(payload["challenge_id"])
        now = float(self._now())
        async with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute(
                    "SELECT tenant_id,user_id,user_code_digest,expires_at,attempted_at "
                    "FROM device_pairing_challenges WHERE challenge_id=?",
                    (challenge_id,),
                ).fetchone()
                if row is None:
                    raise LocalNodeServiceFault(
                        status_code=404,
                        code="LOCAL_NODE_PAIRING_NOT_FOUND",
                    )
                if row[4] is not None:
                    raise LocalNodeServiceFault(
                        status_code=409,
                        code="LOCAL_NODE_PAIRING_REPLAYED",
                    )
                self._db.execute(
                    "UPDATE device_pairing_challenges SET attempted_at=? WHERE challenge_id=?",
                    (now, challenge_id),
                )
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        tenant_id, user_id, expected_code_digest, expires_at, _ = row
        if now >= float(expires_at):
            raise LocalNodeServiceFault(
                status_code=410,
                code="LOCAL_NODE_PAIRING_EXPIRED",
            )
        user_code = str(payload["user_code"])
        if not hmac.compare_digest(expected_code_digest, _sha256_text(user_code)):
            raise LocalNodeServiceFault(
                status_code=403,
                code="LOCAL_NODE_PAIRING_PROOF_INVALID",
            )
        if payload["protocol_version"] != PROTOCOL_VERSION:
            raise LocalNodeServiceFault(
                status_code=422,
                code="LOCAL_NODE_PROTOCOL_INCOMPATIBLE",
            )
        if payload["proof_algorithm"] != "ed25519":
            raise LocalNodeServiceFault(
                status_code=422,
                code="LOCAL_NODE_PAIRING_ALGORITHM_UNSUPPORTED",
            )
        public_key_bytes = _b64url_decode(
            str(payload["proof_public_key"]),
            expected_length=32,
            field="PAIRING_PUBLIC_KEY",
        )
        signature = _b64url_decode(
            str(payload["device_proof"]),
            expected_length=64,
            field="PAIRING_PROOF",
        )
        redemption_digest = pairing_redemption_digest(
            challenge_id=challenge_id,
            user_code=user_code,
            device_id=str(payload["device_id"]),
            proof_algorithm=str(payload["proof_algorithm"]),
            proof_public_key=str(payload["proof_public_key"]),
            display_name=str(payload["display_name"]),
            platform=str(payload["platform"]),
            node_version=str(payload["node_version"]),
            capability_claims=tuple(payload["capability_claims"]),
            permission_snapshot_digest=str(payload["permission_snapshot_digest"]),
        )
        try:
            Ed25519PublicKey.from_public_bytes(public_key_bytes).verify(
                signature,
                pairing_proof_payload(redemption_digest),
            )
        except (InvalidSignature, ValueError) as exc:
            raise LocalNodeServiceFault(
                status_code=403,
                code="LOCAL_NODE_PAIRING_PROOF_INVALID",
            ) from exc

        device_id = str(payload["device_id"])
        credential = "devcred_" + secrets.token_urlsafe(48)
        credential_digest = _sha256_text(credential)
        channel_id = "channel_" + secrets.token_urlsafe(24)
        credential_expires_at = now + self.credential_ttl_seconds
        async with self._lock:
            try:
                self._db.execute(
                    "INSERT INTO device_channel_credentials VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,0)",
                    (
                        credential_digest,
                        tenant_id,
                        user_id,
                        device_id,
                        channel_id,
                        "ed25519",
                        str(payload["proof_public_key"]),
                        PROTOCOL_VERSION,
                        now,
                        credential_expires_at,
                        "pending",
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise LocalNodeServiceFault(
                    status_code=409,
                    code="LOCAL_NODE_DEVICE_ALREADY_PAIRED",
                ) from exc

        principal = DeviceChannelPrincipal(tenant_id, user_id, device_id, channel_id)
        try:
            await self.control_service.complete_pairing(
                tenant_id=tenant_id,
                user_id=user_id,
                challenge_id=challenge_id,
                channel=principal,
                display_name=str(payload["display_name"]),
                platform=str(payload["platform"]),
                node_version=str(payload["node_version"]),
                protocol_version=PROTOCOL_VERSION,
                capability_claims=list(payload["capability_claims"]),
                permission_snapshot_digest=str(payload["permission_snapshot_digest"]),
            )
        except Exception:
            async with self._lock:
                self._db.execute(
                    "UPDATE device_channel_credentials SET status='revoked' "
                    "WHERE credential_digest=?",
                    (credential_digest,),
                )
            raise
        async with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute(
                    "UPDATE device_channel_credentials SET status='active' "
                    "WHERE credential_digest=? AND status='pending'",
                    (credential_digest,),
                )
                self._db.execute(
                    "UPDATE device_pairing_challenges SET completed_at=? WHERE challenge_id=?",
                    (float(self._now()), challenge_id),
                )
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        return {
            "protocol_version": PROTOCOL_VERSION,
            "device_id": device_id,
            "credential": credential,
            "expires_at": credential_expires_at,
        }

    async def authenticate(
        self,
        *,
        authorization: str | None,
        expected_device_id: str,
    ) -> DeviceChannelPrincipal:
        if authorization is None or not authorization.startswith("Device "):
            raise LocalNodeServiceFault(
                status_code=401,
                code="LOCAL_NODE_CHANNEL_AUTH_REQUIRED",
            )
        credential = authorization[7:]
        if not credential or len(credential) > 512 or any(ord(item) < 0x21 for item in credential):
            raise LocalNodeServiceFault(
                status_code=401,
                code="LOCAL_NODE_CHANNEL_AUTH_FAILED",
            )
        credential_digest = _sha256_text(credential)
        now = float(self._now())
        async with self._lock:
            row = self._db.execute(
                "SELECT tenant_id,user_id,device_id,channel_id,expires_at,status "
                "FROM device_channel_credentials WHERE credential_digest=?",
                (credential_digest,),
            ).fetchone()
            if (
                row is None
                or row[5] != "active"
                or now >= float(row[4])
                or not hmac.compare_digest(str(row[2]), expected_device_id)
            ):
                raise LocalNodeServiceFault(
                    status_code=401,
                    code="LOCAL_NODE_CHANNEL_AUTH_FAILED",
                )
            self._db.execute(
                "UPDATE device_channel_credentials SET last_seen_at=? WHERE credential_digest=?",
                (now, credential_digest),
            )
        principal = DeviceChannelPrincipal(str(row[0]), str(row[1]), str(row[2]), str(row[3]))
        try:
            status = await self.control_service.get_device_status(
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                device_id=principal.device_id,
            )
        except Exception:
            await self.revoke_device(
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                device_id=principal.device_id,
            )
            raise LocalNodeServiceFault(
                status_code=401,
                code="LOCAL_NODE_CHANNEL_AUTH_FAILED",
            )
        if status.get("device", {}).get("status") == "revoked":
            await self.revoke_device(
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                device_id=principal.device_id,
            )
            raise LocalNodeServiceFault(
                status_code=401,
                code="LOCAL_NODE_CHANNEL_AUTH_FAILED",
            )
        return principal

    async def heartbeat(
        self,
        *,
        authorization: str | None,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        device_id = str(payload["device_id"])
        principal = await self.authenticate(
            authorization=authorization,
            expected_device_id=device_id,
        )
        doctor = payload["doctor"]
        capabilities = doctor.get("capabilities", {})
        revision = doctor.get("capability_revision")
        if capabilities or revision is not None:
            if (
                not isinstance(revision, int)
                or revision <= 0
                or not isinstance(capabilities, Mapping)
            ):
                raise LocalNodeServiceFault(
                    status_code=422,
                    code="LOCAL_NODE_DOCTOR_INVALID",
                )
            await self.control_service.record_capability_snapshot(
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                device_id=device_id,
                channel=principal,
                revision=revision,
                capabilities=dict(capabilities),
            )
        permissions = doctor.get("permissions")
        if permissions is not None:
            if not isinstance(permissions, list) or len(permissions) > 32:
                raise LocalNodeServiceFault(
                    status_code=422,
                    code="LOCAL_NODE_DOCTOR_INVALID",
                )
            await self.control_service.record_permission_snapshot(
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                device_id=device_id,
                channel=principal,
                permissions=permissions,
            )
        receipts = list(payload["receipts"])
        delivery = self.device_delivery
        has_results = any(item.get("result") is not None for item in receipts)
        if has_results and delivery is None:
            raise LocalNodeServiceFault(
                status_code=503,
                code="LOCAL_NODE_RESULT_CHANNEL_UNAVAILABLE",
            )
        prepared_results = ()
        if receipts and delivery is not None:
            try:
                prepared_results = await delivery.prepare_result_receipts(
                    tenant_id=principal.tenant_id,
                    user_id=principal.user_id,
                    device_id=device_id,
                    receipts=receipts,
                )
            except (PermissionError, ValueError) as exc:
                raise LocalNodeServiceFault(
                    status_code=422,
                    code="LOCAL_NODE_RESULT_RECEIPT_INVALID",
                ) from exc
        accepted = 0
        if receipts:
            events = [_receipt_to_event(item) for item in receipts]
            appended = await self.control_service.append_events(
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                device_id=device_id,
                channel=principal,
                events=events,
            )
            accepted = int(appended["accepted_through_sequence"])
        else:
            async with self._lock:
                row = self._db.execute(
                    "SELECT accepted_through_sequence FROM device_channel_credentials "
                    "WHERE device_id=? AND status='active'",
                    (device_id,),
                ).fetchone()
                accepted = 0 if row is None else int(row[0])
        if receipts:
            async with self._lock:
                self._db.execute(
                    "UPDATE device_channel_credentials SET accepted_through_sequence=? "
                    "WHERE device_id=? AND status='active'",
                    (accepted, device_id),
                )
        if prepared_results and delivery is not None:
            await delivery.accept_prepared_results(
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                device_id=device_id,
                results=prepared_results,
            )
        commands: tuple[dict[str, Any], ...] = ()
        if delivery is not None:
            commands = await delivery.claim_commands(
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                device_id=device_id,
            )
        return {
            "protocol_version": PROTOCOL_VERSION,
            "accepted_through_sequence": accepted,
            "commands": list(commands),
        }

    async def revoke_device(self, *, tenant_id: str, user_id: str, device_id: str) -> None:
        async with self._lock:
            self._db.execute(
                "UPDATE device_channel_credentials SET status='revoked' "
                "WHERE tenant_id=? AND user_id=? AND device_id=?",
                (tenant_id, user_id, device_id),
            )

    def secret_canary_absent(self, *values: str) -> bool:
        """Test/audit helper: raw codes and credentials must not be in SQLite."""
        self._db.execute("PRAGMA wal_checkpoint(FULL)")
        material = self.path.read_bytes()
        wal_path = Path(str(self.path) + "-wal")
        if wal_path.exists():
            material += wal_path.read_bytes()
        return all(value.encode("utf-8") not in material for value in values)

    def close(self) -> None:
        self._db.close()


def _receipt_to_event(value: Mapping[str, Any]) -> dict[str, Any]:
    occurred_at = value.get("occurred_at")
    if isinstance(occurred_at, bool) or not isinstance(occurred_at, (int, float)):
        raise LocalNodeServiceFault(status_code=422, code="LOCAL_NODE_EVENT_TIME_INVALID")
    timestamp = float(occurred_at)
    if not math.isfinite(timestamp) or timestamp <= 0:
        raise LocalNodeServiceFault(status_code=422, code="LOCAL_NODE_EVENT_TIME_INVALID")
    digest = value.get("result_digest")
    if digest is not None and isinstance(digest, str) and not digest.startswith("sha256:"):
        digest = "sha256:" + digest
    return {
        "event_id": value.get("event_id"),
        "sequence": value.get("sequence"),
        "event_type": value.get("event_type"),
        "occurred_at": datetime.fromtimestamp(timestamp, timezone.utc),
        "action_id": value.get("action_id"),
        "status": value.get("status"),
        "summary": value.get("summary"),
        "result_digest": digest,
        "artifact_refs": [],
        "error_code": value.get("error_code"),
    }
