"""Fail-closed control-plane contract for trusted Local Node runtimes.

The Web client is never a device authority.  User-facing operations are bound
to the authenticated tenant/user, while device-originated operations also need
an independently verified Local Node channel.  The router intentionally owns
no persistence or execution logic; ``local_node_control_service`` and
``local_node_channel_verifier`` are injected through ``app.state``.

The assistant-service composition root registers this router even before a
durable service and device-channel verifier are configured.  Its dependencies
fail closed with stable 503 responses, so route discovery never implies that a
Local Node is ready or that Web authentication is a device authority.
"""

from __future__ import annotations

import hashlib
import inspect
import ipaddress
import json
import re
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal, NoReturn, Protocol, TypeVar, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from ...auth import UserContext, get_user_context

router = APIRouter(prefix="/local-nodes", tags=["Local Nodes"])

OpaqueId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]
Digest = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        to_lower=True,
        pattern=r"^(?:sha256:)?[0-9a-f]{64}$",
    ),
]
CapabilityName = Literal[
    "file.list",
    "file.read",
    "file.search",
    "file.watch",
    "file.write",
    "file.move",
    "file.delete",
    "process.run",
    "screen.observe",
    "screen.share",
    "app.observe",
    "app.control",
    "app.submit",
    "network.fetch",
    "network.upload",
    "clipboard.read",
    "clipboard.write",
    "credential.use",
]
DeviceState = Literal["online", "offline", "stale", "revoked"]
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

_PUBLIC_FAULT_MESSAGES = {
    400: "Local node request is invalid",
    401: "Local node channel authentication failed",
    403: "Local node request is not authorized",
    404: "Local node resource was not found",
    409: "Local node state conflicts with this request",
    410: "Local node resource has expired",
    422: "Local node request could not be validated",
    429: "Local node request limit was exceeded",
    503: "Local node control plane is unavailable",
}
_ALLOWED_FAULT_STATUSES = frozenset(_PUBLIC_FAULT_MESSAGES)
_DOMAIN_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_SAFE_ERROR_CODE = re.compile(r"^LOCAL_NODE_[A-Z0-9_]{1,68}$")


class _StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _PublicModel(BaseModel):
    # Ignore unexpected service fields so credentials/attestation material can
    # never leak merely because a backend object gained a new property.
    model_config = ConfigDict(extra="ignore", from_attributes=True)


class LocalNodeServiceFault(Exception):
    """Stable, non-sensitive failure raised by a Local Node service seam."""

    def __init__(self, *, status_code: int, code: str) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code


class DeviceChannelPrincipal(_PublicModel):
    """Verified owner/device binding returned by the channel verifier."""

    tenant_id: str = Field(min_length=1, max_length=255)
    user_id: str = Field(min_length=1, max_length=255)
    device_id: OpaqueId | None = None
    channel_id: OpaqueId


class LocalNodeChannelVerifier(Protocol):
    async def verify(
        self,
        *,
        request: Request,
        purpose: str,
        body_digest: str,
        challenge_id: str | None,
        expected_device_id: str | None,
    ) -> DeviceChannelPrincipal | Mapping[str, Any]: ...


class LocalNodeDispatchPrincipal(_PublicModel):
    """ExecutionGateway authority bound to one exact action envelope."""

    tenant_id: str = Field(min_length=1, max_length=255)
    user_id: str = Field(min_length=1, max_length=255)
    device_id: OpaqueId
    authority_id: OpaqueId
    envelope_digest: Digest


class LocalNodeDispatchAuthority(Protocol):
    async def authorize(
        self,
        *,
        request: Request,
        purpose: str,
        tenant_id: str,
        user_id: str,
        device_id: str,
        envelope_digest: str,
    ) -> LocalNodeDispatchPrincipal | Mapping[str, Any]: ...


class LocalNodeControlService(Protocol):
    async def create_pairing_challenge(self, **values: Any) -> Any: ...

    async def complete_pairing(self, **values: Any) -> Any: ...

    async def revoke_device(self, **values: Any) -> Any: ...

    async def list_devices(self, **values: Any) -> Any: ...

    async def get_device_status(self, **values: Any) -> Any: ...

    async def get_device_capabilities(self, **values: Any) -> Any: ...

    async def get_permission_doctor(self, **values: Any) -> Any: ...

    async def create_grant(self, **values: Any) -> Any: ...

    async def list_grants(self, **values: Any) -> Any: ...

    async def revoke_grant(self, **values: Any) -> Any: ...

    async def dispatch_action(self, **values: Any) -> Any: ...

    async def get_action(self, **values: Any) -> Any: ...

    async def cancel_action(self, **values: Any) -> Any: ...

    async def record_approval_receipt(self, **values: Any) -> Any: ...

    async def append_events(self, **values: Any) -> Any: ...

    async def list_events(self, **values: Any) -> Any: ...


class PairingChallengeRequest(_StrictRequest):
    display_name_hint: str | None = Field(default=None, min_length=1, max_length=80)
    ttl_seconds: int = Field(default=180, ge=30, le=600)


class PairingChallenge(_PublicModel):
    challenge_id: OpaqueId
    # A human correlation code, not a bearer credential.  Proof of possession
    # is authenticated independently on the device channel.
    user_code: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            min_length=4,
            max_length=32,
            pattern=r"^[A-Za-z0-9-]+$",
        ),
    ]
    expires_at: AwareDatetime


class PairingChallengeResponse(_PublicModel):
    challenge: PairingChallenge


class PairingCompleteRequest(_StrictRequest):
    display_name: str = Field(min_length=1, max_length=80)
    platform: Literal["macos", "windows", "linux"]
    node_version: str = Field(min_length=1, max_length=64)
    protocol_version: str = Field(min_length=1, max_length=32)
    capability_claims: list[CapabilityName] = Field(default_factory=list, max_length=32)
    permission_snapshot_digest: Digest

    @field_validator("capability_claims")
    @classmethod
    def _unique_claims(cls, value: list[CapabilityName]) -> list[CapabilityName]:
        if len(set(value)) != len(value):
            raise ValueError("capability claims must be unique")
        return value


class DeviceSummary(_PublicModel):
    device_id: OpaqueId
    display_name: str = Field(min_length=1, max_length=80)
    platform: Literal["macos", "windows", "linux"]
    node_version: str = Field(min_length=1, max_length=64)
    status: DeviceState
    last_seen_at: AwareDatetime | None = None


class PairingCompleteResponse(_PublicModel):
    device: DeviceSummary


class DeviceListResponse(_PublicModel):
    devices: list[DeviceSummary] = Field(default_factory=list, max_length=100)


class DeviceStatusDetail(_PublicModel):
    device_id: OpaqueId
    status: DeviceState
    last_seen_at: AwareDatetime | None = None
    active_action_id: OpaqueId | None = None
    active_lease_expires_at: AwareDatetime | None = None
    protocol_compatible: bool


class DeviceStatusResponse(_PublicModel):
    device: DeviceStatusDetail


class CapabilityState(_PublicModel):
    name: CapabilityName
    state: HealthState
    reason_code: str | None = Field(default=None, max_length=80)


class DeviceCapabilitiesResponse(_PublicModel):
    device_id: OpaqueId
    revision: int = Field(ge=0)
    capabilities: list[CapabilityState] = Field(default_factory=list, max_length=64)


class PermissionCheck(_PublicModel):
    permission: str = Field(min_length=1, max_length=80)
    state: HealthState
    checked_at: AwareDatetime
    reason_code: str | None = Field(default=None, max_length=80)
    action_hint: str | None = Field(default=None, max_length=240)


class PermissionDoctorResponse(_PublicModel):
    device_id: OpaqueId
    checked_at: AwareDatetime
    permissions: list[PermissionCheck] = Field(default_factory=list, max_length=32)


class RevokeRequest(_StrictRequest):
    reason: str | None = Field(default=None, max_length=240)


class RevocationResponse(_PublicModel):
    revoked: bool
    device_id: OpaqueId
    revoked_at: AwareDatetime


class _GrantRequest(_StrictRequest):
    display_name: str = Field(min_length=1, max_length=160)
    capabilities: list[CapabilityName] = Field(min_length=1, max_length=16)
    session_id: OpaqueId | None = None
    expires_at: AwareDatetime | None = None

    @field_validator("capabilities")
    @classmethod
    def _unique_capabilities(cls, value: list[CapabilityName]) -> list[CapabilityName]:
        if len(set(value)) != len(value):
            raise ValueError("capabilities must be unique")
        return value


class WorkspaceGrantRequest(_GrantRequest):
    # Opaque handle minted after an OS-native directory selection.  Raw paths
    # are intentionally rejected by the identifier grammar.
    resource_ref: OpaqueId

    @field_validator("capabilities")
    @classmethod
    def _workspace_capabilities(cls, value: list[CapabilityName]) -> list[CapabilityName]:
        allowed = {
            "file.list",
            "file.read",
            "file.search",
            "file.watch",
            "file.write",
            "file.move",
            "file.delete",
            "process.run",
        }
        if any(capability not in allowed for capability in value):
            raise ValueError("workspace grant contains an unrelated capability")
        return value


class AppGrantRequest(_GrantRequest):
    app_ref: OpaqueId

    @field_validator("capabilities")
    @classmethod
    def _app_capabilities(cls, value: list[CapabilityName]) -> list[CapabilityName]:
        allowed = {
            "app.observe",
            "app.control",
            "app.submit",
            "screen.observe",
            "screen.share",
            "clipboard.read",
            "clipboard.write",
        }
        if any(capability not in allowed for capability in value):
            raise ValueError("app grant contains an unrelated capability")
        return value


class DomainGrantRequest(_GrantRequest):
    domain: str = Field(min_length=1, max_length=253)

    @field_validator("domain")
    @classmethod
    def _canonical_domain(cls, value: str) -> str:
        candidate = value.strip().lower().rstrip(".")
        if "://" in candidate or any(character in candidate for character in "/?#@:*\\"):
            raise ValueError("domain must be a hostname without scheme, path, port, or wildcard")
        try:
            ipaddress.ip_address(candidate)
        except ValueError:
            pass
        else:
            raise ValueError("IP literals require a separate network policy")
        try:
            ascii_domain = candidate.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ValueError("domain is invalid") from exc
        labels = ascii_domain.split(".")
        if len(labels) < 2 or any(not _DOMAIN_LABEL.fullmatch(label) for label in labels):
            raise ValueError("domain must be a fully qualified hostname")
        return ascii_domain

    @field_validator("capabilities")
    @classmethod
    def _domain_capabilities(cls, value: list[CapabilityName]) -> list[CapabilityName]:
        if any(capability not in {"network.fetch", "network.upload"} for capability in value):
            raise ValueError("domain grant contains an unrelated capability")
        return value


class GrantView(_PublicModel):
    grant_id: OpaqueId
    device_id: OpaqueId
    kind: Literal["workspace", "app", "domain"]
    display_name: str = Field(min_length=1, max_length=160)
    resource_ref: OpaqueId | None = None
    domain: str | None = Field(default=None, max_length=253)
    capabilities: list[CapabilityName] = Field(min_length=1, max_length=16)
    session_id: OpaqueId | None = None
    status: Literal["active", "pending", "expired", "revoked"]
    created_at: AwareDatetime
    expires_at: AwareDatetime | None = None


class GrantResponse(_PublicModel):
    grant: GrantView


class GrantListResponse(_PublicModel):
    grants: list[GrantView] = Field(default_factory=list, max_length=200)


class GrantRevocationResponse(_PublicModel):
    revoked: bool
    device_id: OpaqueId
    grant_id: OpaqueId
    revoked_at: AwareDatetime


class ActionDispatchRequest(_StrictRequest):
    idempotency_key: OpaqueId
    session_id: OpaqueId
    run_id: OpaqueId
    call_id: OpaqueId
    capability: CapabilityName
    normalized_arguments: dict[str, Any] = Field(default_factory=dict, max_length=64)
    arguments_digest: Digest
    target_snapshot_digest: Digest
    policy_snapshot_digest: Digest
    approval_id: OpaqueId | None = None
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    trace_context: dict[str, str] = Field(default_factory=dict, max_length=16)

    @field_validator("normalized_arguments")
    @classmethod
    def _bounded_arguments(cls, value: dict[str, Any]) -> dict[str, Any]:
        try:
            encoded = json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("normalized arguments must be finite JSON") from exc
        if len(encoded) > 65_536:
            raise ValueError("normalized arguments exceed 64 KiB")
        return value

    @field_validator("trace_context")
    @classmethod
    def _bounded_trace_context(cls, value: dict[str, str]) -> dict[str, str]:
        if any(
            not key
            or len(key) > 64
            or len(item) > 512
            or any(ord(character) < 0x20 for character in key + item)
            for key, item in value.items()
        ):
            raise ValueError("trace context is invalid")
        return value

    @model_validator(mode="after")
    def _short_lived_envelope(self) -> ActionDispatchRequest:
        if self.expires_at <= self.issued_at:
            raise ValueError("action expiry must be later than issue time")
        if self.expires_at - self.issued_at > timedelta(minutes=10):
            raise ValueError("action envelope lifetime exceeds ten minutes")
        return self


class ActionView(_PublicModel):
    action_id: OpaqueId
    device_id: OpaqueId
    session_id: OpaqueId
    run_id: OpaqueId
    call_id: OpaqueId
    capability: CapabilityName
    status: ActionState
    sequence: int = Field(ge=0)
    created_at: AwareDatetime
    updated_at: AwareDatetime
    error_code: str | None = Field(default=None, max_length=80)
    observation_ref: OpaqueId | None = None
    artifact_refs: list[OpaqueId] = Field(default_factory=list, max_length=32)


class ActionResponse(_PublicModel):
    action: ActionView


class CancelActionRequest(_StrictRequest):
    reason: str | None = Field(default=None, max_length=240)


class LocalApprovalReceiptRequest(_StrictRequest):
    approval_id: OpaqueId
    approved: bool
    arguments_digest: Digest
    target_snapshot_digest: Digest
    policy_snapshot_digest: Digest
    decision_nonce: OpaqueId
    decided_at: AwareDatetime
    expires_at: AwareDatetime
    reason_code: str | None = Field(default=None, max_length=80)

    @model_validator(mode="after")
    def _valid_receipt_window(self) -> LocalApprovalReceiptRequest:
        if self.expires_at <= self.decided_at:
            raise ValueError("approval receipt expiry must be after the decision")
        if self.expires_at - self.decided_at > timedelta(minutes=10):
            raise ValueError("approval receipt lifetime exceeds ten minutes")
        return self


class ApprovalReceiptResponse(_PublicModel):
    action_id: OpaqueId
    approval_id: OpaqueId
    recorded: bool
    action_status: ActionState


class NodeEventInput(_StrictRequest):
    event_id: OpaqueId
    sequence: int = Field(ge=1)
    event_type: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            min_length=1,
            max_length=80,
            pattern=r"^[a-z][a-z0-9_.-]*$",
        ),
    ]
    occurred_at: AwareDatetime
    action_id: OpaqueId | None = None
    status: ActionState | None = None
    summary: str | None = Field(default=None, max_length=500)
    result_digest: Digest | None = None
    artifact_refs: list[OpaqueId] = Field(default_factory=list, max_length=32)
    error_code: str | None = Field(default=None, max_length=80)


class EventBatchRequest(_StrictRequest):
    events: list[NodeEventInput] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def _strictly_contiguous(self) -> EventBatchRequest:
        sequences = [event.sequence for event in self.events]
        expected = list(range(sequences[0], sequences[0] + len(sequences)))
        if sequences != expected:
            raise ValueError("event sequences must be strictly ordered and contiguous")
        if len({event.event_id for event in self.events}) != len(self.events):
            raise ValueError("event IDs must be unique within a batch")
        return self


class EventAppendResponse(_PublicModel):
    device_id: OpaqueId
    accepted_through_sequence: int = Field(ge=0)
    next_expected_sequence: int = Field(ge=1)
    duplicate_count: int = Field(default=0, ge=0)


class EventView(_PublicModel):
    event_id: OpaqueId
    device_id: OpaqueId
    sequence: int = Field(ge=1)
    event_type: str = Field(min_length=1, max_length=80)
    occurred_at: AwareDatetime
    action_id: OpaqueId | None = None
    status: ActionState | None = None
    summary: str | None = Field(default=None, max_length=500)
    result_digest: Digest | None = None
    artifact_refs: list[OpaqueId] = Field(default_factory=list, max_length=32)
    error_code: str | None = Field(default=None, max_length=80)


class EventListResponse(_PublicModel):
    device_id: OpaqueId
    after_sequence: int = Field(ge=0)
    next_sequence: int = Field(ge=1)
    events: list[EventView] = Field(default_factory=list, max_length=200)


TModel = TypeVar("TModel", bound=BaseModel)


def get_local_node_control_service(request: Request) -> LocalNodeControlService:
    service = getattr(request.app.state, "local_node_control_service", None)
    if service is None:
        _error(request, 503, "LOCAL_NODE_CONTROL_UNAVAILABLE")
    return cast(LocalNodeControlService, service)


def get_local_node_channel_verifier(request: Request) -> LocalNodeChannelVerifier:
    verifier = getattr(request.app.state, "local_node_channel_verifier", None)
    if verifier is None:
        _error(request, 503, "LOCAL_NODE_CHANNEL_UNAVAILABLE")
    return cast(LocalNodeChannelVerifier, verifier)


def get_local_node_dispatch_authority(request: Request) -> LocalNodeDispatchAuthority:
    """Resolve an internal ExecutionGateway authority, never a Web credential."""

    authority = getattr(request.app.state, "local_node_dispatch_authority", None)
    if authority is None:
        _error(request, 503, "LOCAL_NODE_DISPATCH_AUTHORITY_UNAVAILABLE")
    return cast(LocalNodeDispatchAuthority, authority)


def _request_id(request: Request) -> str:
    return str(
        getattr(request.state, "request_id", "")
        or getattr(request.state, "trace_id", "")
        or "local-node-request"
    )


def _error(request: Request, status_code: int, code: str) -> NoReturn:
    public_status = status_code if status_code in _ALLOWED_FAULT_STATUSES else 503
    public_code = (
        code
        if public_status == status_code and _SAFE_ERROR_CODE.fullmatch(code)
        else "LOCAL_NODE_CONTROL_UNAVAILABLE"
    )
    raise HTTPException(
        status_code=public_status,
        detail={
            "code": public_code,
            "message": _PUBLIC_FAULT_MESSAGES[public_status],
            "request_id": _request_id(request),
        },
    )


async def _resolve(value: Any, request: Request) -> Any:
    try:
        return await value if inspect.isawaitable(value) else value
    except LocalNodeServiceFault as exc:
        _error(request, exc.status_code, exc.code)


def _public(model: type[TModel], value: Any, request: Request) -> TModel:
    try:
        return model.model_validate(value)
    except ValidationError:
        _error(request, 503, "LOCAL_NODE_SERVICE_RESPONSE_INVALID")


def _body_digest(payload: BaseModel) -> str:
    encoded = json.dumps(
        payload.model_dump(mode="json"),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


async def _verified_channel(
    *,
    request: Request,
    verifier: LocalNodeChannelVerifier,
    user: UserContext,
    purpose: str,
    body: BaseModel,
    expected_device_id: str | None = None,
    challenge_id: str | None = None,
) -> DeviceChannelPrincipal:
    raw_principal = await _resolve(
        verifier.verify(
            request=request,
            purpose=purpose,
            body_digest=_body_digest(body),
            challenge_id=challenge_id,
            expected_device_id=expected_device_id,
        ),
        request,
    )
    principal = _public(DeviceChannelPrincipal, raw_principal, request)
    if principal.tenant_id != user.tenant_id or principal.user_id != user.user_id:
        _error(request, 403, "LOCAL_NODE_CHANNEL_OWNER_MISMATCH")
    if expected_device_id is not None and principal.device_id != expected_device_id:
        _error(request, 403, "LOCAL_NODE_CHANNEL_DEVICE_MISMATCH")
    return principal


def _owner(user: UserContext) -> dict[str, str]:
    return {"tenant_id": user.tenant_id, "user_id": user.user_id}


async def _verified_dispatch_authority(
    *,
    request: Request,
    authority: LocalNodeDispatchAuthority,
    user: UserContext,
    device_id: str,
    payload: ActionDispatchRequest,
) -> LocalNodeDispatchPrincipal:
    envelope_digest = _body_digest(payload)
    raw_principal = await _resolve(
        authority.authorize(
            request=request,
            purpose="action.dispatch",
            **_owner(user),
            device_id=device_id,
            envelope_digest=envelope_digest,
        ),
        request,
    )
    principal = _public(LocalNodeDispatchPrincipal, raw_principal, request)
    if (
        principal.tenant_id != user.tenant_id
        or principal.user_id != user.user_id
        or principal.device_id != device_id
        or principal.envelope_digest != envelope_digest
    ):
        _error(request, 403, "LOCAL_NODE_DISPATCH_AUTHORITY_MISMATCH")
    return principal


@router.post(
    "/pairing/challenges",
    response_model=PairingChallengeResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_pairing_challenge(
    payload: PairingChallengeRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
    service: LocalNodeControlService = Depends(get_local_node_control_service),
) -> PairingChallengeResponse:
    result = await _resolve(
        service.create_pairing_challenge(**_owner(user), **payload.model_dump()),
        request,
    )
    return _public(PairingChallengeResponse, result, request)


@router.post(
    "/pairing/challenges/{challenge_id}/complete",
    response_model=PairingCompleteResponse,
    status_code=status.HTTP_201_CREATED,
)
async def complete_pairing(
    challenge_id: OpaqueId,
    payload: PairingCompleteRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
    service: LocalNodeControlService = Depends(get_local_node_control_service),
    verifier: LocalNodeChannelVerifier = Depends(get_local_node_channel_verifier),
) -> PairingCompleteResponse:
    channel = await _verified_channel(
        request=request,
        verifier=verifier,
        user=user,
        purpose="pairing.complete",
        body=payload,
        challenge_id=challenge_id,
    )
    result = await _resolve(
        service.complete_pairing(
            **_owner(user),
            challenge_id=challenge_id,
            channel=channel,
            **payload.model_dump(),
        ),
        request,
    )
    return _public(PairingCompleteResponse, result, request)


@router.get("", response_model=DeviceListResponse)
async def list_devices(
    request: Request,
    user: UserContext = Depends(get_user_context),
    service: LocalNodeControlService = Depends(get_local_node_control_service),
) -> DeviceListResponse:
    result = await _resolve(service.list_devices(**_owner(user)), request)
    return _public(DeviceListResponse, result, request)


@router.post("/{device_id}/revoke", response_model=RevocationResponse)
async def revoke_device(
    device_id: OpaqueId,
    payload: RevokeRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
    service: LocalNodeControlService = Depends(get_local_node_control_service),
) -> RevocationResponse:
    result = await _resolve(
        service.revoke_device(
            **_owner(user),
            device_id=device_id,
            reason=payload.reason,
        ),
        request,
    )
    return _public(RevocationResponse, result, request)


@router.get("/{device_id}/status", response_model=DeviceStatusResponse)
async def get_device_status(
    device_id: OpaqueId,
    request: Request,
    user: UserContext = Depends(get_user_context),
    service: LocalNodeControlService = Depends(get_local_node_control_service),
) -> DeviceStatusResponse:
    result = await _resolve(
        service.get_device_status(**_owner(user), device_id=device_id),
        request,
    )
    return _public(DeviceStatusResponse, result, request)


@router.get("/{device_id}/capabilities", response_model=DeviceCapabilitiesResponse)
async def get_device_capabilities(
    device_id: OpaqueId,
    request: Request,
    user: UserContext = Depends(get_user_context),
    service: LocalNodeControlService = Depends(get_local_node_control_service),
) -> DeviceCapabilitiesResponse:
    result = await _resolve(
        service.get_device_capabilities(**_owner(user), device_id=device_id),
        request,
    )
    return _public(DeviceCapabilitiesResponse, result, request)


@router.get("/{device_id}/doctor", response_model=PermissionDoctorResponse)
async def get_permission_doctor(
    device_id: OpaqueId,
    request: Request,
    user: UserContext = Depends(get_user_context),
    service: LocalNodeControlService = Depends(get_local_node_control_service),
) -> PermissionDoctorResponse:
    result = await _resolve(
        service.get_permission_doctor(**_owner(user), device_id=device_id),
        request,
    )
    return _public(PermissionDoctorResponse, result, request)


async def _create_trusted_grant(
    *,
    kind: Literal["workspace", "app", "domain"],
    device_id: str,
    payload: _GrantRequest,
    request: Request,
    user: UserContext,
    service: LocalNodeControlService,
    verifier: LocalNodeChannelVerifier,
) -> GrantResponse:
    channel = await _verified_channel(
        request=request,
        verifier=verifier,
        user=user,
        purpose=f"grant.{kind}.create",
        body=payload,
        expected_device_id=device_id,
    )
    result = await _resolve(
        service.create_grant(
            **_owner(user),
            device_id=device_id,
            kind=kind,
            channel=channel,
            grant=payload.model_dump(),
        ),
        request,
    )
    return _public(GrantResponse, result, request)


@router.post(
    "/{device_id}/grants/workspaces",
    response_model=GrantResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_workspace_grant(
    device_id: OpaqueId,
    payload: WorkspaceGrantRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
    service: LocalNodeControlService = Depends(get_local_node_control_service),
    verifier: LocalNodeChannelVerifier = Depends(get_local_node_channel_verifier),
) -> GrantResponse:
    return await _create_trusted_grant(
        kind="workspace",
        device_id=device_id,
        payload=payload,
        request=request,
        user=user,
        service=service,
        verifier=verifier,
    )


@router.post(
    "/{device_id}/grants/apps",
    response_model=GrantResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_app_grant(
    device_id: OpaqueId,
    payload: AppGrantRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
    service: LocalNodeControlService = Depends(get_local_node_control_service),
    verifier: LocalNodeChannelVerifier = Depends(get_local_node_channel_verifier),
) -> GrantResponse:
    return await _create_trusted_grant(
        kind="app",
        device_id=device_id,
        payload=payload,
        request=request,
        user=user,
        service=service,
        verifier=verifier,
    )


@router.post(
    "/{device_id}/grants/domains",
    response_model=GrantResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_domain_grant(
    device_id: OpaqueId,
    payload: DomainGrantRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
    service: LocalNodeControlService = Depends(get_local_node_control_service),
    verifier: LocalNodeChannelVerifier = Depends(get_local_node_channel_verifier),
) -> GrantResponse:
    return await _create_trusted_grant(
        kind="domain",
        device_id=device_id,
        payload=payload,
        request=request,
        user=user,
        service=service,
        verifier=verifier,
    )


@router.get("/{device_id}/grants", response_model=GrantListResponse)
async def list_grants(
    device_id: OpaqueId,
    request: Request,
    user: UserContext = Depends(get_user_context),
    service: LocalNodeControlService = Depends(get_local_node_control_service),
) -> GrantListResponse:
    result = await _resolve(
        service.list_grants(**_owner(user), device_id=device_id),
        request,
    )
    return _public(GrantListResponse, result, request)


@router.delete("/{device_id}/grants/{grant_id}", response_model=GrantRevocationResponse)
async def revoke_grant(
    device_id: OpaqueId,
    grant_id: OpaqueId,
    request: Request,
    user: UserContext = Depends(get_user_context),
    service: LocalNodeControlService = Depends(get_local_node_control_service),
) -> GrantRevocationResponse:
    result = await _resolve(
        service.revoke_grant(
            **_owner(user),
            device_id=device_id,
            grant_id=grant_id,
        ),
        request,
    )
    return _public(GrantRevocationResponse, result, request)


@router.post(
    "/{device_id}/actions",
    response_model=ActionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def dispatch_action(
    device_id: OpaqueId,
    payload: ActionDispatchRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
    service: LocalNodeControlService = Depends(get_local_node_control_service),
    authority: LocalNodeDispatchAuthority = Depends(get_local_node_dispatch_authority),
) -> ActionResponse:
    now = datetime.now(timezone.utc)
    if payload.expires_at <= now:
        _error(request, 422, "LOCAL_NODE_ACTION_EXPIRED")
    if payload.issued_at > now + timedelta(minutes=2):
        _error(request, 422, "LOCAL_NODE_ACTION_ISSUED_IN_FUTURE")
    dispatch_authority = await _verified_dispatch_authority(
        request=request,
        authority=authority,
        user=user,
        device_id=device_id,
        payload=payload,
    )
    result = await _resolve(
        service.dispatch_action(
            **_owner(user),
            device_id=device_id,
            dispatch_authority=dispatch_authority,
            envelope=payload.model_dump(),
        ),
        request,
    )
    return _public(ActionResponse, result, request)


@router.get("/{device_id}/actions/{action_id}", response_model=ActionResponse)
async def get_action(
    device_id: OpaqueId,
    action_id: OpaqueId,
    request: Request,
    user: UserContext = Depends(get_user_context),
    service: LocalNodeControlService = Depends(get_local_node_control_service),
) -> ActionResponse:
    result = await _resolve(
        service.get_action(
            **_owner(user),
            device_id=device_id,
            action_id=action_id,
        ),
        request,
    )
    return _public(ActionResponse, result, request)


@router.post(
    "/{device_id}/actions/{action_id}/cancel",
    response_model=ActionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def cancel_action(
    device_id: OpaqueId,
    action_id: OpaqueId,
    payload: CancelActionRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
    service: LocalNodeControlService = Depends(get_local_node_control_service),
) -> ActionResponse:
    result = await _resolve(
        service.cancel_action(
            **_owner(user),
            device_id=device_id,
            action_id=action_id,
            reason=payload.reason,
        ),
        request,
    )
    return _public(ActionResponse, result, request)


@router.post(
    "/{device_id}/actions/{action_id}/approval-receipts",
    response_model=ApprovalReceiptResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def record_local_approval_receipt(
    device_id: OpaqueId,
    action_id: OpaqueId,
    payload: LocalApprovalReceiptRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
    service: LocalNodeControlService = Depends(get_local_node_control_service),
    verifier: LocalNodeChannelVerifier = Depends(get_local_node_channel_verifier),
) -> ApprovalReceiptResponse:
    channel = await _verified_channel(
        request=request,
        verifier=verifier,
        user=user,
        purpose="action.approval_receipt",
        body=payload,
        expected_device_id=device_id,
    )
    result = await _resolve(
        service.record_approval_receipt(
            **_owner(user),
            device_id=device_id,
            action_id=action_id,
            channel=channel,
            receipt=payload.model_dump(),
        ),
        request,
    )
    return _public(ApprovalReceiptResponse, result, request)


@router.post(
    "/{device_id}/events",
    response_model=EventAppendResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def append_events(
    device_id: OpaqueId,
    payload: EventBatchRequest,
    request: Request,
    user: UserContext = Depends(get_user_context),
    service: LocalNodeControlService = Depends(get_local_node_control_service),
    verifier: LocalNodeChannelVerifier = Depends(get_local_node_channel_verifier),
) -> EventAppendResponse:
    channel = await _verified_channel(
        request=request,
        verifier=verifier,
        user=user,
        purpose="events.append",
        body=payload,
        expected_device_id=device_id,
    )
    result = await _resolve(
        service.append_events(
            **_owner(user),
            device_id=device_id,
            channel=channel,
            events=[event.model_dump() for event in payload.events],
        ),
        request,
    )
    return _public(EventAppendResponse, result, request)


@router.get("/{device_id}/events", response_model=EventListResponse)
async def list_events(
    device_id: OpaqueId,
    request: Request,
    after_sequence: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=200),
    user: UserContext = Depends(get_user_context),
    service: LocalNodeControlService = Depends(get_local_node_control_service),
) -> EventListResponse:
    result = await _resolve(
        service.list_events(
            **_owner(user),
            device_id=device_id,
            after_sequence=after_sequence,
            limit=limit,
        ),
        request,
    )
    response = _public(EventListResponse, result, request)
    sequences = [event.sequence for event in response.events]
    if any(sequence <= after_sequence for sequence in sequences) or sequences != sorted(
        set(sequences)
    ):
        _error(request, 503, "LOCAL_NODE_EVENT_ORDER_INVALID")
    return response


__all__ = [
    "LocalNodeChannelVerifier",
    "LocalNodeControlService",
    "LocalNodeDispatchAuthority",
    "LocalNodeServiceFault",
    "get_local_node_channel_verifier",
    "get_local_node_control_service",
    "get_local_node_dispatch_authority",
    "router",
]
