"""Fail-closed Local Node control-plane state machine.

The service owns authorization state, ordering, and idempotency.  It does not
open a listener, execute host operations, or trust browser authentication as a
device or ExecutionGateway authority.  Production durability and transport are
injected; the bundled in-memory repository is deliberately restricted to
explicit development/test use.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import secrets
from collections.abc import AsyncIterator, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Protocol

from ...api.routes.local_nodes import LocalNodeServiceFault
from .protocol import LOCAL_NODE_PROTOCOL_VERSION

Owner = tuple[str, str]
HealthState = Literal["ready", "denied", "needs_action", "unsupported", "unknown"]
ActionState = Literal[
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
]

_TERMINAL_ACTION_STATES = frozenset({"succeeded", "failed", "cancelled", "interrupted", "unknown"})
_ACTION_TRANSITIONS: dict[str, frozenset[str]] = {
    "proposed": frozenset({"policy_check", "awaiting_approval", "dispatched"}),
    "policy_check": frozenset({"awaiting_approval", "dispatched"}),
    "awaiting_approval": frozenset({"dispatched", "cancelled", "failed"}),
    "dispatched": frozenset(
        {"running", "observed", "succeeded", "failed", "cancelled", "interrupted", "unknown"}
    ),
    "running": frozenset(
        {"observed", "succeeded", "failed", "cancelled", "interrupted", "unknown"}
    ),
    "observed": frozenset(
        {"running", "succeeded", "failed", "cancelled", "interrupted", "unknown"}
    ),
}
_ACTION_EVENT_STATUSES: dict[str, ActionState] = {
    "action.running": "running",
    "action.observed": "observed",
    "action.succeeded": "succeeded",
    "action.failed": "failed",
    "action.cancelled": "cancelled",
    "action.interrupted": "interrupted",
    "action.unknown": "unknown",
}
_GRANT_CAPABILITY_FAMILIES: dict[str, frozenset[str]] = {
    "workspace": frozenset(
        {
            "file.list",
            "file.read",
            "file.search",
            "file.watch",
            "file.write",
            "file.move",
            "file.delete",
            "process.run",
        }
    ),
    "app": frozenset(
        {
            "app.observe",
            "app.control",
            "app.submit",
            "screen.observe",
            "screen.share",
            "clipboard.read",
            "clipboard.write",
        }
    ),
    "domain": frozenset({"network.fetch", "network.upload"}),
}
_OPAQUE_ARTIFACT_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def canonical_digest(value: Any) -> str:
    """Return the canonical sha256 digest used for exact envelope binding."""

    encoded = json.dumps(
        _json_value(value),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def derive_action_id(
    *,
    tenant_id: str,
    user_id: str,
    device_id: str,
    idempotency_key: str,
) -> str:
    """Derive the stable action fence ID shared with trusted providers."""

    values = (tenant_id, user_id, device_id, idempotency_key)
    if any(not value for value in values):
        raise ValueError("action ID derivation requires complete scope and idempotency key")
    return "act_" + hashlib.sha256("\x1f".join(values).encode("utf-8")).hexdigest()[:32]


def _opaque_digest(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _fault(status_code: int, code: str) -> LocalNodeServiceFault:
    return LocalNodeServiceFault(status_code=status_code, code=code)


def _get(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _required_grant_kind(capabilities: frozenset[str]) -> str | None:
    matches = [
        kind for kind, family in _GRANT_CAPABILITY_FAMILIES.items() if capabilities.issubset(family)
    ]
    return matches[0] if len(matches) == 1 else None


def _approval_arguments_digest(envelope: Mapping[str, Any]) -> Any:
    return envelope.get("device_arguments_digest", envelope.get("arguments_digest"))


def _validate_signed_action_proposal(
    proposal: Any,
    *,
    action_id: str,
    owner: Owner,
    device_id: str,
    envelope: Mapping[str, Any],
) -> Mapping[str, Any]:
    if not isinstance(proposal, Mapping):
        raise _fault(422, "LOCAL_NODE_SIGNED_ACTION_REQUIRED")
    required_values = {
        "action_id": action_id,
        "idempotency_key": envelope.get("idempotency_key"),
        "tenant_id": owner[0],
        "user_id": owner[1],
        "session_id": envelope.get("session_id"),
        "run_id": envelope.get("run_id"),
        "agent_id": envelope.get("agent_id"),
        "agent_version": envelope.get("agent_version"),
        "call_id": envelope.get("call_id"),
        "device_id": device_id,
        "capability": envelope.get("capability"),
        "tool_name": envelope.get("tool_name"),
        "operation": envelope.get("action_operation"),
        "arguments_digest": _approval_arguments_digest(envelope),
        "target_snapshot_digest": envelope.get("target_snapshot_digest"),
        "policy_snapshot_digest": envelope.get("policy_snapshot_digest"),
    }
    if any(proposal.get(name) != value for name, value in required_values.items()):
        raise _fault(403, "LOCAL_NODE_SIGNED_ACTION_INTENT_MISMATCH")
    if proposal.get("approval") is not None:
        raise _fault(403, "LOCAL_NODE_SIGNED_ACTION_PREAPPROVED")
    signature = proposal.get("platform_signature")
    if not isinstance(signature, str) or not signature:
        raise _fault(422, "LOCAL_NODE_SIGNED_ACTION_SIGNATURE_MISSING")
    return proposal


def _validate_finalized_signed_action(
    finalized: Any,
    *,
    proposal: Mapping[str, Any],
    receipt: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(finalized, Mapping) or set(finalized) != set(proposal):
        raise _fault(422, "LOCAL_NODE_APPROVED_ACTION_SHAPE_INVALID")
    for name, value in proposal.items():
        if name not in {"approval", "platform_signature"} and finalized.get(name) != value:
            raise _fault(403, "LOCAL_NODE_APPROVED_ACTION_INTENT_MISMATCH")
    signature = finalized.get("platform_signature")
    if not isinstance(signature, str) or not signature:
        raise _fault(422, "LOCAL_NODE_APPROVED_ACTION_SIGNATURE_MISSING")
    approval = finalized.get("approval")
    if not isinstance(approval, Mapping):
        raise _fault(422, "LOCAL_NODE_APPROVED_ACTION_PROOF_MISSING")
    expected_approval = {
        "approval_id": receipt.get("approval_id"),
        "action_id": proposal.get("action_id"),
        "device_id": proposal.get("device_id"),
        "arguments_digest": receipt.get(
            "device_arguments_digest",
            receipt.get("arguments_digest"),
        ),
        "target_snapshot_digest": receipt.get("target_snapshot_digest"),
        "policy_snapshot_digest": receipt.get("policy_snapshot_digest"),
        "nonce": receipt.get("decision_nonce"),
        "local_signature": receipt.get("local_signature"),
    }
    if any(approval.get(name) != value for name, value in expected_approval.items()):
        raise _fault(403, "LOCAL_NODE_APPROVED_ACTION_PROOF_MISMATCH")
    expires_at = receipt.get("expires_at")
    expected_expires = expires_at.timestamp() if isinstance(expires_at, datetime) else expires_at
    if approval.get("expires_at") != expected_expires:
        raise _fault(403, "LOCAL_NODE_APPROVED_ACTION_PROOF_MISMATCH")
    if set(approval) != {*expected_approval, "expires_at"}:
        raise _fault(422, "LOCAL_NODE_APPROVED_ACTION_PROOF_SHAPE_INVALID")
    return copy.deepcopy(dict(finalized))


@dataclass(slots=True)
class _PairingChallenge:
    challenge_id: str
    tenant_id: str
    user_id: str
    user_code_digest: str = field(repr=False)
    expires_at: datetime
    consumed_at: datetime | None = None


@dataclass(slots=True)
class _Device:
    device_id: str
    tenant_id: str
    user_id: str
    display_name: str
    platform: str
    node_version: str
    protocol_version: str
    channel_ref_digest: str = field(repr=False)
    capability_ceiling: frozenset[str]
    capabilities: dict[str, HealthState]
    capability_revision: int
    created_at: datetime
    last_seen_at: datetime
    status: Literal["online", "offline", "stale", "revoked"] = "online"
    revoked_at: datetime | None = None
    permission_checked_at: datetime | None = None
    permissions: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class _Grant:
    grant_id: str
    tenant_id: str
    user_id: str
    device_id: str
    kind: str
    display_name: str
    capabilities: tuple[str, ...]
    created_at: datetime
    resource_ref: str | None = None
    domain: str | None = None
    session_id: str | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None


@dataclass(slots=True)
class _Action:
    action_id: str
    tenant_id: str
    user_id: str
    device_id: str
    session_id: str
    run_id: str
    call_id: str
    capability: str
    status: ActionState
    sequence: int
    created_at: datetime
    updated_at: datetime
    envelope: dict[str, Any] = field(repr=False)
    envelope_digest: str
    authority_ref_digest: str = field(repr=False)
    idempotency_key: str
    grant_id: str
    approved_envelope_digest: str | None = field(default=None, repr=False)
    delivery_ref_digest: str | None = field(default=None, repr=False)
    terminal_event_id: str | None = None
    error_code: str | None = None
    observation_ref: str | None = None
    artifact_refs: tuple[str, ...] = ()
    approval_id: str | None = None


@dataclass(slots=True)
class _Event:
    event_id: str
    tenant_id: str
    user_id: str
    device_id: str
    sequence: int
    event_type: str
    occurred_at: datetime
    action_id: str | None
    status: ActionState | None
    summary: str | None
    result_digest: str | None
    artifact_refs: tuple[str, ...]
    error_code: str | None
    fingerprint: str


@dataclass(slots=True)
class LocalNodeState:
    challenges: dict[str, _PairingChallenge] = field(default_factory=dict)
    devices: dict[str, _Device] = field(default_factory=dict)
    grants: dict[str, _Grant] = field(default_factory=dict)
    actions: dict[str, _Action] = field(default_factory=dict)
    events: dict[str, list[_Event]] = field(default_factory=dict)
    event_ids: dict[tuple[str, str], _Event] = field(default_factory=dict)
    idempotency: dict[tuple[str, str, str, str], tuple[str, str]] = field(default_factory=dict)
    approval_nonces: set[tuple[str, str, str, str]] = field(default_factory=set)


class LocalNodeRepository(Protocol):
    """Atomic state repository seam for an injected production implementation.

    Production implementations must set ``durable_dispatch_fence`` only when
    action/idempotency records survive process and host failure before an
    external enqueue is attempted.
    """

    durable_dispatch_fence: bool

    def transaction(self) -> AbstractAsyncContextManager[LocalNodeState]: ...


class InMemoryLocalNodeRepository:
    """Copy-on-write repository restricted to explicit development/test use."""

    durable_dispatch_fence = False

    def __init__(self, *, purpose: Literal["development", "test"]) -> None:
        if purpose not in {"development", "test"}:
            raise ValueError("in-memory Local Node state is restricted to development/test")
        self.purpose = purpose
        self._state = LocalNodeState()
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[LocalNodeState]:
        async with self._lock:
            candidate = copy.deepcopy(self._state)
            yield candidate
            self._state = candidate


class LocalNodeActionDeliveryProvider(Protocol):
    """Idempotent, authenticated server-to-device delivery seam.

    ``idempotent_enqueue`` is a required behavioral contract: repeated calls
    with the same action ID and envelope digest must produce one logical device
    delivery and the same opaque delivery reference.  Reuse with a different
    digest must be rejected by the provider.
    """

    idempotent_enqueue: bool

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
    ) -> str: ...

    async def cancel_action(
        self,
        *,
        tenant_id: str,
        user_id: str,
        device_id: str,
        action_id: str,
    ) -> None: ...


class LocalNodeControlPlaneService:
    """Tenant-bound Local Node state machine implementing the API protocol.

    Pairing proof is verified before this seam: the injected device-channel
    verifier must bind challenge ID, user-code/proof of possession, body digest,
    owner, and the channel credential into its principal.  This service then
    consumes the challenge exactly once and stores only opaque digests for the
    human correlation code and channel reference.
    """

    def __init__(
        self,
        *,
        repository: LocalNodeRepository,
        action_provider: LocalNodeActionDeliveryProvider,
        now: Any = _utcnow,
        id_factory: Any | None = None,
        user_code_factory: Any | None = None,
    ) -> None:
        if getattr(action_provider, "idempotent_enqueue", False) is not True:
            raise ValueError("Local Node action provider must guarantee idempotent enqueue")
        self._repository = repository
        self._action_provider = action_provider
        self._now = now
        self._id_factory = id_factory or (
            lambda prefix: f"{prefix}_{secrets.token_urlsafe(18).replace('-', '').replace('_', '')}"
        )
        self._user_code_factory = user_code_factory or (
            lambda: f"{secrets.randbelow(1_000_000):06d}"
        )
        self._pairing_challenge_observer: Any | None = None

    def set_pairing_challenge_observer(self, observer: Any | None) -> None:
        """Attach an explicit device-channel challenge persistence hook.

        The service still owns the owner-bound one-use challenge.  A wired
        device-channel broker persists only its digest and uses it to verify an
        outbound device redemption.  No observer is installed by default.
        """

        self._pairing_challenge_observer = observer

    @staticmethod
    def _owner(tenant_id: str, user_id: str) -> Owner:
        if not tenant_id or not user_id:
            raise _fault(403, "LOCAL_NODE_OWNER_INVALID")
        return tenant_id, user_id

    @staticmethod
    def _assert_owner(record: Any, owner: Owner) -> None:
        if (record.tenant_id, record.user_id) != owner:
            raise _fault(404, "LOCAL_NODE_NOT_FOUND")

    def _device(
        self,
        state: LocalNodeState,
        *,
        owner: Owner,
        device_id: str,
        active: bool = True,
    ) -> _Device:
        device = state.devices.get(device_id)
        if device is None:
            raise _fault(404, "LOCAL_NODE_NOT_FOUND")
        self._assert_owner(device, owner)
        if active and device.status == "revoked":
            raise _fault(410, "LOCAL_NODE_DEVICE_REVOKED")
        return device

    @staticmethod
    def _assert_channel(device: _Device, channel: Any, owner: Owner) -> None:
        if (
            _get(channel, "tenant_id") != owner[0]
            or _get(channel, "user_id") != owner[1]
            or _get(channel, "device_id") != device.device_id
        ):
            raise _fault(403, "LOCAL_NODE_CHANNEL_OWNER_MISMATCH")
        channel_id = _get(channel, "channel_id")
        if (
            not isinstance(channel_id, str)
            or _opaque_digest(channel_id) != device.channel_ref_digest
        ):
            raise _fault(403, "LOCAL_NODE_CHANNEL_CREDENTIAL_MISMATCH")

    @staticmethod
    def _grant_status(grant: _Grant, now: datetime) -> str:
        if grant.revoked_at is not None:
            return "revoked"
        if grant.expires_at is not None and grant.expires_at <= now:
            return "expired"
        return "active"

    @staticmethod
    def _device_view(device: _Device) -> dict[str, Any]:
        return {
            "device_id": device.device_id,
            "display_name": device.display_name,
            "platform": device.platform,
            "node_version": device.node_version,
            "status": device.status,
            "last_seen_at": device.last_seen_at,
        }

    def _grant_view(self, grant: _Grant, now: datetime) -> dict[str, Any]:
        return {
            "grant_id": grant.grant_id,
            "device_id": grant.device_id,
            "kind": grant.kind,
            "display_name": grant.display_name,
            "resource_ref": grant.resource_ref,
            "domain": grant.domain,
            "capabilities": list(grant.capabilities),
            "session_id": grant.session_id,
            "status": self._grant_status(grant, now),
            "created_at": grant.created_at,
            "expires_at": grant.expires_at,
        }

    @staticmethod
    def _action_view(action: _Action) -> dict[str, Any]:
        return {
            "action_id": action.action_id,
            "device_id": action.device_id,
            "session_id": action.session_id,
            "run_id": action.run_id,
            "call_id": action.call_id,
            "capability": action.capability,
            "status": action.status,
            "sequence": action.sequence,
            "created_at": action.created_at,
            "updated_at": action.updated_at,
            "error_code": action.error_code,
            "observation_ref": action.observation_ref,
            "artifact_refs": list(action.artifact_refs),
        }

    async def create_pairing_challenge(
        self,
        *,
        tenant_id: str,
        user_id: str,
        ttl_seconds: int,
        display_name_hint: str | None = None,
    ) -> dict[str, Any]:
        del display_name_hint
        owner = self._owner(tenant_id, user_id)
        now = self._now()
        challenge_id = self._id_factory("pair")
        user_code = self._user_code_factory()
        challenge = _PairingChallenge(
            challenge_id=challenge_id,
            tenant_id=owner[0],
            user_id=owner[1],
            user_code_digest=_opaque_digest(user_code),
            expires_at=now.replace(microsecond=0) + timedelta(seconds=ttl_seconds),
        )
        async with self._repository.transaction() as state:
            if challenge_id in state.challenges:
                raise _fault(409, "LOCAL_NODE_ID_COLLISION")
            state.challenges[challenge_id] = challenge
        result = {
            "challenge": {
                "challenge_id": challenge_id,
                "user_code": user_code,
                "expires_at": challenge.expires_at,
            }
        }
        if self._pairing_challenge_observer is not None:
            try:
                await self._pairing_challenge_observer(
                    tenant_id=owner[0],
                    user_id=owner[1],
                    challenge=result["challenge"],
                )
            except Exception as exc:
                async with self._repository.transaction() as state:
                    state.challenges.pop(challenge_id, None)
                raise _fault(503, "LOCAL_NODE_PAIRING_CHANNEL_UNAVAILABLE") from exc
        return result

    async def complete_pairing(
        self,
        *,
        tenant_id: str,
        user_id: str,
        challenge_id: str,
        channel: Any,
        display_name: str,
        platform: str,
        node_version: str,
        protocol_version: str,
        capability_claims: list[str],
        permission_snapshot_digest: str,
    ) -> dict[str, Any]:
        del permission_snapshot_digest
        if protocol_version != LOCAL_NODE_PROTOCOL_VERSION:
            raise _fault(422, "LOCAL_NODE_PROTOCOL_INCOMPATIBLE")
        owner = self._owner(tenant_id, user_id)
        now = self._now()
        async with self._repository.transaction() as state:
            challenge = state.challenges.get(challenge_id)
            if challenge is None:
                raise _fault(404, "LOCAL_NODE_PAIRING_NOT_FOUND")
            self._assert_owner(challenge, owner)
            if challenge.consumed_at is not None:
                raise _fault(409, "LOCAL_NODE_PAIRING_REPLAYED")
            if challenge.expires_at <= now:
                raise _fault(410, "LOCAL_NODE_PAIRING_EXPIRED")
            if _get(channel, "tenant_id") != owner[0] or _get(channel, "user_id") != owner[1]:
                raise _fault(403, "LOCAL_NODE_CHANNEL_OWNER_MISMATCH")
            channel_id = _get(channel, "channel_id")
            if not isinstance(channel_id, str) or not channel_id:
                raise _fault(403, "LOCAL_NODE_CHANNEL_CREDENTIAL_MISSING")
            challenge.consumed_at = now
            proposed_device_id = _get(channel, "device_id")
            device_id = (
                proposed_device_id
                if isinstance(proposed_device_id, str) and proposed_device_id
                else self._id_factory("node")
            )
            if device_id in state.devices:
                raise _fault(409, "LOCAL_NODE_DEVICE_ALREADY_PAIRED")
            claims = frozenset(capability_claims)
            device = _Device(
                device_id=device_id,
                tenant_id=owner[0],
                user_id=owner[1],
                display_name=display_name,
                platform=platform,
                node_version=node_version,
                protocol_version=protocol_version,
                channel_ref_digest=_opaque_digest(channel_id),
                capability_ceiling=claims,
                capabilities=dict.fromkeys(claims, "unknown"),
                capability_revision=0,
                created_at=now,
                last_seen_at=now,
            )
            state.devices[device_id] = device
        return {"device": self._device_view(device)}

    async def list_devices(self, *, tenant_id: str, user_id: str) -> dict[str, Any]:
        owner = self._owner(tenant_id, user_id)
        async with self._repository.transaction() as state:
            devices = [
                self._device_view(device)
                for device in state.devices.values()
                if (device.tenant_id, device.user_id) == owner
            ]
        devices.sort(key=lambda item: item["device_id"])
        return {"devices": devices[:100]}

    async def revoke_device(
        self,
        *,
        tenant_id: str,
        user_id: str,
        device_id: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        del reason
        owner = self._owner(tenant_id, user_id)
        now = self._now()
        async with self._repository.transaction() as state:
            device = self._device(state, owner=owner, device_id=device_id, active=False)
            if device.revoked_at is None:
                device.revoked_at = now
                device.status = "revoked"
                for grant in state.grants.values():
                    if grant.device_id == device_id and grant.revoked_at is None:
                        grant.revoked_at = now
                for action in state.actions.values():
                    if (
                        action.device_id == device_id
                        and action.status not in _TERMINAL_ACTION_STATES
                    ):
                        action.status = "interrupted"
                        action.updated_at = now
                        action.terminal_event_id = "control-plane-device-revocation"
            revoked_at = device.revoked_at
        return {"revoked": True, "device_id": device_id, "revoked_at": revoked_at}

    async def get_device_status(
        self, *, tenant_id: str, user_id: str, device_id: str
    ) -> dict[str, Any]:
        owner = self._owner(tenant_id, user_id)
        async with self._repository.transaction() as state:
            device = self._device(state, owner=owner, device_id=device_id, active=False)
            active = next(
                (
                    action.action_id
                    for action in state.actions.values()
                    if action.device_id == device_id
                    and action.status not in _TERMINAL_ACTION_STATES
                ),
                None,
            )
            return {
                "device": {
                    "device_id": device_id,
                    "status": device.status,
                    "last_seen_at": device.last_seen_at,
                    "active_action_id": active,
                    "active_lease_expires_at": None,
                    "protocol_compatible": (
                        device.protocol_version == LOCAL_NODE_PROTOCOL_VERSION
                    ),
                }
            }

    async def get_device_capabilities(
        self, *, tenant_id: str, user_id: str, device_id: str
    ) -> dict[str, Any]:
        owner = self._owner(tenant_id, user_id)
        async with self._repository.transaction() as state:
            device = self._device(state, owner=owner, device_id=device_id, active=False)
            capabilities = [
                {"name": name, "state": health, "reason_code": None}
                for name, health in sorted(device.capabilities.items())
            ]
            return {
                "device_id": device_id,
                "revision": device.capability_revision,
                "capabilities": capabilities,
            }

    async def record_capability_snapshot(
        self,
        *,
        tenant_id: str,
        user_id: str,
        device_id: str,
        channel: Any,
        revision: int,
        capabilities: Mapping[str, HealthState],
    ) -> None:
        """Replace realtime health within the immutable pairing claim ceiling.

        Health can recover from ``denied``/missing to ``ready`` on a newer
        signed revision.  The immutable ``capability_ceiling`` and current
        grants remain the independent authority boundaries.
        """

        owner = self._owner(tenant_id, user_id)
        now = self._now()
        async with self._repository.transaction() as state:
            device = self._device(state, owner=owner, device_id=device_id)
            self._assert_channel(device, channel, owner)
            if revision <= device.capability_revision:
                raise _fault(409, "LOCAL_NODE_CAPABILITY_REVISION_STALE")
            names = set(capabilities)
            if not names.issubset(device.capability_ceiling):
                raise _fault(403, "LOCAL_NODE_CAPABILITY_EXPANSION_DENIED")
            device.capabilities = dict(capabilities)
            device.capability_revision = revision
            device.last_seen_at = now
            device.status = "online"

    async def get_permission_doctor(
        self, *, tenant_id: str, user_id: str, device_id: str
    ) -> dict[str, Any]:
        owner = self._owner(tenant_id, user_id)
        async with self._repository.transaction() as state:
            device = self._device(state, owner=owner, device_id=device_id, active=False)
            checked_at = device.permission_checked_at or device.last_seen_at
            return {
                "device_id": device_id,
                "checked_at": checked_at,
                "permissions": copy.deepcopy(device.permissions),
            }

    async def record_permission_snapshot(
        self,
        *,
        tenant_id: str,
        user_id: str,
        device_id: str,
        channel: Any,
        permissions: list[Mapping[str, Any]],
    ) -> None:
        owner = self._owner(tenant_id, user_id)
        now = self._now()
        allowed_states = {"ready", "denied", "needs_action", "unsupported", "unknown"}
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in permissions:
            if not isinstance(raw, Mapping):
                raise _fault(422, "LOCAL_NODE_PERMISSION_SNAPSHOT_INVALID")
            permission = raw.get("permission")
            state_value = raw.get("state")
            if (
                not isinstance(permission, str)
                or not permission
                or len(permission) > 80
                or permission in seen
                or state_value not in allowed_states
                or (
                    raw.get("reason_code") is not None
                    and (
                        not isinstance(raw.get("reason_code"), str)
                        or len(str(raw.get("reason_code"))) > 80
                    )
                )
                or (
                    raw.get("action_hint") is not None
                    and (
                        not isinstance(raw.get("action_hint"), str)
                        or len(str(raw.get("action_hint"))) > 240
                    )
                )
            ):
                raise _fault(422, "LOCAL_NODE_PERMISSION_SNAPSHOT_INVALID")
            seen.add(permission)
            normalized.append(
                {
                    "permission": permission,
                    "state": state_value,
                    "checked_at": now,
                    "reason_code": raw.get("reason_code"),
                    "action_hint": raw.get("action_hint"),
                }
            )
        async with self._repository.transaction() as state:
            device = self._device(state, owner=owner, device_id=device_id)
            self._assert_channel(device, channel, owner)
            device.permissions = normalized
            device.permission_checked_at = now
            device.last_seen_at = now

    async def create_grant(
        self,
        *,
        tenant_id: str,
        user_id: str,
        device_id: str,
        kind: str,
        channel: Any,
        grant: Mapping[str, Any],
    ) -> dict[str, Any]:
        owner = self._owner(tenant_id, user_id)
        now = self._now()
        capabilities = tuple(grant.get("capabilities", ()))
        async with self._repository.transaction() as state:
            device = self._device(state, owner=owner, device_id=device_id)
            self._assert_channel(device, channel, owner)
            if not set(capabilities).issubset(device.capabilities):
                raise _fault(403, "LOCAL_NODE_GRANT_CAPABILITY_DENIED")
            expires_at = grant.get("expires_at")
            if expires_at is not None and expires_at <= now:
                raise _fault(410, "LOCAL_NODE_GRANT_EXPIRED")
            grant_id = self._id_factory("grant")
            if grant_id in state.grants:
                raise _fault(409, "LOCAL_NODE_ID_COLLISION")
            resource_ref = grant.get("resource_ref") or grant.get("app_ref")
            record = _Grant(
                grant_id=grant_id,
                tenant_id=owner[0],
                user_id=owner[1],
                device_id=device_id,
                kind=kind,
                display_name=str(grant["display_name"]),
                capabilities=capabilities,
                created_at=now,
                resource_ref=str(resource_ref) if resource_ref is not None else None,
                domain=str(grant["domain"]) if grant.get("domain") is not None else None,
                session_id=grant.get("session_id"),
                expires_at=expires_at,
            )
            state.grants[grant_id] = record
            device.last_seen_at = now
        return {"grant": self._grant_view(record, now)}

    async def list_grants(self, *, tenant_id: str, user_id: str, device_id: str) -> dict[str, Any]:
        owner = self._owner(tenant_id, user_id)
        now = self._now()
        async with self._repository.transaction() as state:
            self._device(state, owner=owner, device_id=device_id, active=False)
            grants = [
                self._grant_view(grant, now)
                for grant in state.grants.values()
                if grant.device_id == device_id and (grant.tenant_id, grant.user_id) == owner
            ]
        grants.sort(key=lambda item: item["grant_id"])
        return {"grants": grants}

    async def revoke_grant(
        self,
        *,
        tenant_id: str,
        user_id: str,
        device_id: str,
        grant_id: str,
    ) -> dict[str, Any]:
        owner = self._owner(tenant_id, user_id)
        now = self._now()
        async with self._repository.transaction() as state:
            self._device(state, owner=owner, device_id=device_id, active=False)
            grant = state.grants.get(grant_id)
            if grant is None or grant.device_id != device_id:
                raise _fault(404, "LOCAL_NODE_GRANT_NOT_FOUND")
            self._assert_owner(grant, owner)
            if grant.revoked_at is None:
                grant.revoked_at = now
            revoked_at = grant.revoked_at
        return {
            "revoked": True,
            "device_id": device_id,
            "grant_id": grant_id,
            "revoked_at": revoked_at,
        }

    @staticmethod
    def _dispatch_principal(
        dispatch_authority: Any,
        *,
        owner: Owner,
        device_id: str,
        envelope_digest: str,
    ) -> str:
        if (
            _get(dispatch_authority, "tenant_id") != owner[0]
            or _get(dispatch_authority, "user_id") != owner[1]
            or _get(dispatch_authority, "device_id") != device_id
            or _get(dispatch_authority, "envelope_digest") != envelope_digest
        ):
            raise _fault(403, "LOCAL_NODE_DISPATCH_AUTHORITY_MISMATCH")
        authority_id = _get(dispatch_authority, "authority_id")
        if not isinstance(authority_id, str) or not authority_id:
            raise _fault(403, "LOCAL_NODE_DISPATCH_AUTHORITY_MISSING")
        return authority_id

    async def _deliver_fenced_action(
        self,
        *,
        owner: Owner,
        device_id: str,
        action_id: str,
        envelope_digest: str,
    ) -> dict[str, Any]:
        """Deliver one approved/low-risk fence through the idempotent provider."""

        now = self._now()
        async with self._repository.transaction() as state:
            device = self._device(state, owner=owner, device_id=device_id)
            action = state.actions.get(action_id)
            if action is None or action.device_id != device_id:
                raise _fault(404, "LOCAL_NODE_ACTION_NOT_FOUND")
            self._assert_owner(action, owner)
            if action.envelope_digest != envelope_digest:
                raise _fault(409, "LOCAL_NODE_ACTION_FENCE_CONFLICT")
            if action.delivery_ref_digest is not None:
                return {"action": self._action_view(action)}
            if action.status == "awaiting_approval":
                raise _fault(409, "LOCAL_NODE_APPROVAL_REQUIRED")
            if action.status in _TERMINAL_ACTION_STATES:
                return {"action": self._action_view(action)}
            if action.status != "policy_check":
                raise _fault(409, "LOCAL_NODE_ACTION_STATE_CONFLICT")

            expires_at = action.envelope.get("expires_at")
            if not isinstance(expires_at, datetime) or expires_at <= now:
                raise _fault(410, "LOCAL_NODE_ACTION_EXPIRED")
            grant = state.grants.get(action.grant_id)
            if grant is None or grant.device_id != device_id:
                raise _fault(403, "LOCAL_NODE_ACTION_GRANT_DENIED")
            self._assert_owner(grant, owner)
            if self._grant_status(grant, now) != "active":
                raise _fault(410, "LOCAL_NODE_ACTION_GRANT_INACTIVE")
            raw_required = action.envelope.get("required_capabilities")
            if not isinstance(raw_required, list) or not raw_required:
                raise _fault(503, "LOCAL_NODE_ACTION_FENCE_INVALID")
            required_capabilities = frozenset(raw_required)
            expected_grant_kind = _required_grant_kind(required_capabilities)
            if (
                expected_grant_kind is None
                or grant.kind != expected_grant_kind
                or action.capability not in required_capabilities
                or not required_capabilities.issubset(grant.capabilities)
            ):
                raise _fault(403, "LOCAL_NODE_ACTION_CAPABILITY_DENIED")
            if device.capabilities.get(action.capability) != "ready":
                raise _fault(403, "LOCAL_NODE_ACTION_CAPABILITY_UNAVAILABLE")
            if grant.session_id is not None and grant.session_id != action.session_id:
                raise _fault(403, "LOCAL_NODE_ACTION_SESSION_MISMATCH")
            envelope = copy.deepcopy(action.envelope)
            idempotency_key = action.idempotency_key

        try:
            delivery_ref = await self._action_provider.enqueue_action(
                tenant_id=owner[0],
                user_id=owner[1],
                device_id=device_id,
                action_id=action_id,
                idempotency_key=idempotency_key,
                envelope_digest=envelope_digest,
                envelope=envelope,
            )
        except Exception as exc:
            raise _fault(503, "LOCAL_NODE_ACTION_DELIVERY_UNAVAILABLE") from exc
        if not isinstance(delivery_ref, str) or not delivery_ref:
            raise _fault(503, "LOCAL_NODE_ACTION_DELIVERY_INVALID")
        delivery_ref_digest = _opaque_digest(delivery_ref)

        async with self._repository.transaction() as state:
            persisted = state.actions.get(action_id)
            if persisted is None or persisted.envelope_digest != envelope_digest:
                raise _fault(503, "LOCAL_NODE_ACTION_FENCE_LOST")
            if (
                persisted.delivery_ref_digest is not None
                and persisted.delivery_ref_digest != delivery_ref_digest
            ):
                raise _fault(503, "LOCAL_NODE_ACTION_DELIVERY_CONFLICT")
            if persisted.status not in _TERMINAL_ACTION_STATES:
                persisted.delivery_ref_digest = delivery_ref_digest
                persisted.status = "dispatched"
                persisted.updated_at = self._now()
            return {"action": self._action_view(persisted)}

    async def dispatch_action(
        self,
        *,
        tenant_id: str,
        user_id: str,
        device_id: str,
        envelope: Mapping[str, Any],
        dispatch_authority: Any | None = None,
    ) -> dict[str, Any]:
        owner = self._owner(tenant_id, user_id)
        now = self._now()
        envelope_copy = copy.deepcopy(dict(envelope))
        issued_at = envelope_copy.get("issued_at")
        expires_at = envelope_copy.get("expires_at")
        if (
            not isinstance(issued_at, datetime)
            or issued_at.tzinfo is None
            or issued_at.utcoffset() is None
            or not isinstance(expires_at, datetime)
            or expires_at.tzinfo is None
            or expires_at.utcoffset() is None
        ):
            raise _fault(422, "LOCAL_NODE_ACTION_TIME_INVALID")
        if expires_at <= issued_at or expires_at - issued_at > timedelta(minutes=10):
            raise _fault(422, "LOCAL_NODE_ACTION_TIME_WINDOW_INVALID")
        if expires_at <= now:
            raise _fault(422, "LOCAL_NODE_ACTION_EXPIRED")
        if issued_at > now + timedelta(minutes=2):
            raise _fault(422, "LOCAL_NODE_ACTION_ISSUED_IN_FUTURE")
        envelope_digest = canonical_digest(envelope_copy)
        if dispatch_authority is None:
            raise _fault(403, "LOCAL_NODE_DISPATCH_AUTHORITY_MISSING")
        authority_id = self._dispatch_principal(
            dispatch_authority,
            owner=owner,
            device_id=device_id,
            envelope_digest=envelope_digest,
        )
        arguments = envelope_copy.get("normalized_arguments")
        if not isinstance(arguments, Mapping):
            raise _fault(422, "LOCAL_NODE_ACTION_ARGUMENTS_INVALID")
        if canonical_digest(arguments) != envelope_copy.get("arguments_digest"):
            raise _fault(422, "LOCAL_NODE_ACTION_ARGUMENT_DIGEST_MISMATCH")
        raw_required = envelope_copy.get("required_capabilities")
        if (
            not isinstance(raw_required, list)
            or not raw_required
            or any(not isinstance(value, str) or not value for value in raw_required)
            or len(raw_required) != len(set(raw_required))
        ):
            raise _fault(422, "LOCAL_NODE_REQUIRED_CAPABILITIES_INVALID")
        required_capabilities = frozenset(raw_required)
        expected_grant_kind = _required_grant_kind(required_capabilities)
        if expected_grant_kind is None:
            raise _fault(403, "LOCAL_NODE_ACTION_GRANT_KIND_DENIED")
        grant_id = arguments.get("grant_id")
        # App/screen Computer Use, processes, and files all use this same
        # explicit grant handle.  There is no computer.* or grant-free bypass.
        if not isinstance(grant_id, str) or not grant_id:
            raise _fault(403, "LOCAL_NODE_ACTION_GRANT_REQUIRED")
        idempotency_key = str(envelope_copy.get("idempotency_key", ""))
        if not idempotency_key:
            raise _fault(422, "LOCAL_NODE_IDEMPOTENCY_REQUIRED")
        action_id = derive_action_id(
            tenant_id=owner[0],
            user_id=owner[1],
            device_id=device_id,
            idempotency_key=idempotency_key,
        )
        approval_id = envelope_copy.get("approval_id")
        if approval_id is not None and (not isinstance(approval_id, str) or not approval_id):
            raise _fault(422, "LOCAL_NODE_APPROVAL_ID_INVALID")
        if approval_id is not None:
            _validate_signed_action_proposal(
                envelope_copy.get("signed_action"),
                action_id=action_id,
                owner=owner,
                device_id=device_id,
                envelope=envelope_copy,
            )
        async with self._repository.transaction() as state:
            device = self._device(state, owner=owner, device_id=device_id)
            grant = state.grants.get(grant_id)
            if grant is None or grant.device_id != device_id:
                raise _fault(403, "LOCAL_NODE_ACTION_GRANT_DENIED")
            self._assert_owner(grant, owner)
            if self._grant_status(grant, now) != "active":
                raise _fault(410, "LOCAL_NODE_ACTION_GRANT_INACTIVE")
            capability = str(envelope_copy.get("capability", ""))
            if (
                grant.kind != expected_grant_kind
                or capability not in required_capabilities
                or not required_capabilities.issubset(grant.capabilities)
            ):
                raise _fault(403, "LOCAL_NODE_ACTION_CAPABILITY_DENIED")
            if device.capabilities.get(capability) != "ready":
                raise _fault(403, "LOCAL_NODE_ACTION_CAPABILITY_UNAVAILABLE")
            session_id = str(envelope_copy.get("session_id", ""))
            if grant.session_id is not None and grant.session_id != session_id:
                raise _fault(403, "LOCAL_NODE_ACTION_SESSION_MISMATCH")
            idempotency_index = (*owner, device_id, idempotency_key)
            existing = state.idempotency.get(idempotency_index)
            if existing is not None:
                existing_id, existing_digest = existing
                if existing_digest != envelope_digest:
                    raise _fault(409, "LOCAL_NODE_IDEMPOTENCY_CONFLICT")
                existing_action = state.actions[existing_id]
                if existing_action.delivery_ref_digest is not None:
                    return {"action": self._action_view(existing_action)}
                action = existing_action
            else:
                # Commit this stable fence before any external side effect.  A
                # durable repository makes a retry reuse the exact action ID;
                # the provider contract deduplicates a crash after acceptance.
                action = _Action(
                    action_id=action_id,
                    tenant_id=owner[0],
                    user_id=owner[1],
                    device_id=device_id,
                    session_id=session_id,
                    run_id=str(envelope_copy.get("run_id", "")),
                    call_id=str(envelope_copy.get("call_id", "")),
                    capability=capability,
                    status=("awaiting_approval" if approval_id is not None else "policy_check"),
                    sequence=0,
                    created_at=now,
                    updated_at=now,
                    envelope=envelope_copy,
                    envelope_digest=envelope_digest,
                    authority_ref_digest=_opaque_digest(authority_id),
                    idempotency_key=idempotency_key,
                    grant_id=grant_id,
                    approval_id=approval_id,
                )
                state.actions[action_id] = action
                state.idempotency[idempotency_index] = (action_id, envelope_digest)
            if action.status != "policy_check":
                return {"action": self._action_view(action)}

        return await self._deliver_fenced_action(
            owner=owner,
            device_id=device_id,
            action_id=action_id,
            envelope_digest=envelope_digest,
        )

    async def get_action(
        self,
        *,
        tenant_id: str,
        user_id: str,
        device_id: str,
        action_id: str,
    ) -> dict[str, Any]:
        owner = self._owner(tenant_id, user_id)
        async with self._repository.transaction() as state:
            self._device(state, owner=owner, device_id=device_id, active=False)
            action = state.actions.get(action_id)
            if action is None or action.device_id != device_id:
                raise _fault(404, "LOCAL_NODE_ACTION_NOT_FOUND")
            self._assert_owner(action, owner)
            return {"action": self._action_view(action)}

    async def cancel_action(
        self,
        *,
        tenant_id: str,
        user_id: str,
        device_id: str,
        action_id: str,
        reason: str | None = None,
    ) -> dict[str, Any]:
        del reason
        owner = self._owner(tenant_id, user_id)
        now = self._now()
        async with self._repository.transaction() as state:
            self._device(state, owner=owner, device_id=device_id, active=False)
            action = state.actions.get(action_id)
            if action is None or action.device_id != device_id:
                raise _fault(404, "LOCAL_NODE_ACTION_NOT_FOUND")
            self._assert_owner(action, owner)
            if action.status not in _TERMINAL_ACTION_STATES:
                if action.delivery_ref_digest is not None:
                    try:
                        await self._action_provider.cancel_action(
                            tenant_id=owner[0],
                            user_id=owner[1],
                            device_id=device_id,
                            action_id=action_id,
                        )
                    except Exception as exc:
                        raise _fault(503, "LOCAL_NODE_ACTION_CANCEL_UNAVAILABLE") from exc
                action.status = "cancelled"
                action.updated_at = now
                action.terminal_event_id = "control-plane-cancel"
            return {"action": self._action_view(action)}

    async def record_approval_receipt(
        self,
        *,
        tenant_id: str,
        user_id: str,
        device_id: str,
        action_id: str,
        channel: Any,
        receipt: Mapping[str, Any],
    ) -> dict[str, Any]:
        owner = self._owner(tenant_id, user_id)
        now = self._now()
        approved = False
        envelope_digest = ""
        async with self._repository.transaction() as state:
            device = self._device(state, owner=owner, device_id=device_id)
            self._assert_channel(device, channel, owner)
            action = state.actions.get(action_id)
            if action is None or action.device_id != device_id:
                raise _fault(404, "LOCAL_NODE_ACTION_NOT_FOUND")
            self._assert_owner(action, owner)
            if action.approval_id is None:
                raise _fault(409, "LOCAL_NODE_APPROVAL_NOT_REQUIRED")
            if action.status != "awaiting_approval":
                raise _fault(409, "LOCAL_NODE_APPROVAL_REPLAYED")
            approval_id = str(receipt.get("approval_id", ""))
            if action.approval_id != approval_id:
                raise _fault(403, "LOCAL_NODE_APPROVAL_ID_MISMATCH")
            for name in (
                "arguments_digest",
                "target_snapshot_digest",
                "policy_snapshot_digest",
            ):
                if receipt.get(name) != action.envelope.get(name):
                    raise _fault(403, "LOCAL_NODE_APPROVAL_DIGEST_MISMATCH")
            device_arguments_digest = action.envelope.get("device_arguments_digest")
            if (
                device_arguments_digest is not None
                and receipt.get("device_arguments_digest") != device_arguments_digest
            ):
                raise _fault(403, "LOCAL_NODE_APPROVAL_DIGEST_MISMATCH")
            nonce = str(receipt.get("decision_nonce", ""))
            nonce_key = (*owner, device_id, nonce)
            if not nonce or nonce_key in state.approval_nonces:
                raise _fault(409, "LOCAL_NODE_APPROVAL_REPLAYED")
            decided_at = receipt.get("decided_at")
            receipt_expires_at = receipt.get("expires_at")
            if (
                not isinstance(decided_at, datetime)
                or decided_at.tzinfo is None
                or decided_at.utcoffset() is None
                or not isinstance(receipt_expires_at, datetime)
                or receipt_expires_at.tzinfo is None
                or receipt_expires_at.utcoffset() is None
                or receipt_expires_at <= decided_at
                or receipt_expires_at - decided_at > timedelta(minutes=10)
            ):
                raise _fault(422, "LOCAL_NODE_APPROVAL_TIME_INVALID")
            if receipt_expires_at <= now:
                raise _fault(410, "LOCAL_NODE_APPROVAL_EXPIRED")
            action_expires_at = action.envelope.get("expires_at")
            if not isinstance(action_expires_at, datetime) or action_expires_at <= now:
                raise _fault(410, "LOCAL_NODE_ACTION_EXPIRED")
            proposal = _validate_signed_action_proposal(
                action.envelope.get("signed_action"),
                action_id=action.action_id,
                owner=owner,
                device_id=device_id,
                envelope=action.envelope,
            )
            approved = bool(receipt.get("approved"))
            finalized_signed_action: dict[str, Any] | None = None
            if approved:
                finalized_signed_action = _validate_finalized_signed_action(
                    receipt.get("finalized_signed_action"),
                    proposal=proposal,
                    receipt=receipt,
                )
            state.approval_nonces.add(nonce_key)
            if not approved:
                action.status = "cancelled"
                action.updated_at = now
                action.terminal_event_id = "local-approval-denied"
            else:
                # Persist device-local approval before external delivery.
                assert finalized_signed_action is not None
                action.envelope["signed_action"] = finalized_signed_action
                action.approved_envelope_digest = canonical_digest(action.envelope)
                action.status = "policy_check"
                action.updated_at = now
            device.last_seen_at = now
            envelope_digest = action.envelope_digest

        if approved:
            delivered = await self._deliver_fenced_action(
                owner=owner,
                device_id=device_id,
                action_id=action_id,
                envelope_digest=envelope_digest,
            )
            action_status = delivered["action"]["status"]
        else:
            action_status = "cancelled"
        return {
            "action_id": action_id,
            "approval_id": approval_id,
            "recorded": True,
            "action_status": action_status,
        }

    @staticmethod
    def _event_view(event: _Event) -> dict[str, Any]:
        return {
            "event_id": event.event_id,
            "device_id": event.device_id,
            "sequence": event.sequence,
            "event_type": event.event_type,
            "occurred_at": event.occurred_at,
            "action_id": event.action_id,
            "status": event.status,
            "summary": event.summary,
            "result_digest": event.result_digest,
            "artifact_refs": list(event.artifact_refs),
            "error_code": event.error_code,
        }

    async def append_events(
        self,
        *,
        tenant_id: str,
        user_id: str,
        device_id: str,
        channel: Any,
        events: list[Mapping[str, Any]],
    ) -> dict[str, Any]:
        owner = self._owner(tenant_id, user_id)
        now = self._now()
        async with self._repository.transaction() as state:
            device = self._device(state, owner=owner, device_id=device_id)
            self._assert_channel(device, channel, owner)
            stored = state.events.setdefault(device_id, [])
            next_sequence = len(stored) + 1
            duplicate_count = 0
            pending: list[_Event] = []
            for raw in events:
                sequence = int(raw["sequence"])
                event_id = str(raw["event_id"])
                event_type = str(raw["event_type"])
                raw_artifact_refs = raw.get("artifact_refs", ())
                if (
                    not isinstance(raw_artifact_refs, (list, tuple))
                    or len(raw_artifact_refs) > 32
                    or any(
                        not isinstance(reference, str)
                        or _OPAQUE_ARTIFACT_REF.fullmatch(reference) is None
                        for reference in raw_artifact_refs
                    )
                ):
                    raise _fault(422, "LOCAL_NODE_ARTIFACT_REF_INVALID")
                artifact_refs = tuple(raw_artifact_refs)
                expected_status = _ACTION_EVENT_STATUSES.get(event_type)
                if expected_status is None:
                    raise _fault(422, "LOCAL_NODE_EVENT_TYPE_DENIED")
                if raw.get("status") != expected_status or raw.get("action_id") is None:
                    raise _fault(422, "LOCAL_NODE_EVENT_SHAPE_INVALID")
                fingerprint = canonical_digest(raw)
                duplicate = state.event_ids.get((device_id, event_id))
                if duplicate is not None:
                    if duplicate.sequence != sequence or duplicate.fingerprint != fingerprint:
                        raise _fault(409, "LOCAL_NODE_EVENT_REPLAY_CONFLICT")
                    duplicate_count += 1
                    continue
                if sequence != next_sequence + len(pending):
                    raise _fault(409, "LOCAL_NODE_EVENT_SEQUENCE_CONFLICT")
                action_id = raw.get("action_id")
                status = raw.get("status")
                action: _Action | None = None
                if action_id is not None:
                    action = state.actions.get(str(action_id))
                    if action is None or action.device_id != device_id:
                        raise _fault(404, "LOCAL_NODE_ACTION_NOT_FOUND")
                    self._assert_owner(action, owner)
                if status is not None:
                    if action is None:
                        raise _fault(422, "LOCAL_NODE_EVENT_ACTION_REQUIRED")
                    if action.status in _TERMINAL_ACTION_STATES:
                        raise _fault(409, "LOCAL_NODE_ACTION_TERMINAL_CONFLICT")
                    allowed = _ACTION_TRANSITIONS.get(action.status, frozenset())
                    if status not in allowed:
                        raise _fault(409, "LOCAL_NODE_ACTION_TRANSITION_INVALID")
                    action.status = status
                    action.sequence = sequence
                    action.updated_at = raw["occurred_at"]
                    action.error_code = raw.get("error_code")
                    refs = artifact_refs
                    if refs:
                        action.artifact_refs = refs
                    if status == "observed" and refs:
                        action.observation_ref = refs[0]
                    if status in _TERMINAL_ACTION_STATES:
                        if action.terminal_event_id is not None:
                            raise _fault(409, "LOCAL_NODE_ACTION_TERMINAL_CONFLICT")
                        action.terminal_event_id = event_id
                pending.append(
                    _Event(
                        event_id=event_id,
                        tenant_id=owner[0],
                        user_id=owner[1],
                        device_id=device_id,
                        sequence=sequence,
                        event_type=event_type,
                        occurred_at=raw["occurred_at"],
                        action_id=str(action_id) if action_id is not None else None,
                        status=status,
                        summary=raw.get("summary"),
                        result_digest=raw.get("result_digest"),
                        artifact_refs=artifact_refs,
                        error_code=raw.get("error_code"),
                        fingerprint=fingerprint,
                    )
                )
            for event in pending:
                stored.append(event)
                state.event_ids[(device_id, event.event_id)] = event
            device.last_seen_at = now
            accepted = len(stored)
        return {
            "device_id": device_id,
            "accepted_through_sequence": accepted,
            "next_expected_sequence": accepted + 1,
            "duplicate_count": duplicate_count,
        }

    async def list_events(
        self,
        *,
        tenant_id: str,
        user_id: str,
        device_id: str,
        after_sequence: int,
        limit: int,
    ) -> dict[str, Any]:
        owner = self._owner(tenant_id, user_id)
        async with self._repository.transaction() as state:
            self._device(state, owner=owner, device_id=device_id, active=False)
            selected = [
                self._event_view(event)
                for event in state.events.get(device_id, [])
                if event.sequence > after_sequence
            ][:limit]
            next_sequence = selected[-1]["sequence"] + 1 if selected else after_sequence + 1
            return {
                "device_id": device_id,
                "after_sequence": after_sequence,
                "next_sequence": next_sequence,
                "events": selected,
            }
