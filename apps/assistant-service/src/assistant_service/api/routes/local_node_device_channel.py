"""Device-only outbound Local Node channel.

These endpoints are intentionally outside the browser-safe Gateway proxy.  A
Web session cannot complete pairing or submit device events.  Production
ingress must expose this Assistant endpoint over HTTPS; the Local Node initiates
every request and no host/LAN listener is involved.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, NoReturn, cast

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from ...core.local_node.device_channel import (
    PROTOCOL_VERSION,
    SQLiteDeviceChannelBroker,
)
from .local_nodes import LocalNodeServiceFault

router = APIRouter(prefix="/local-node-device", tags=["Local Node Device Channel"])

Opaque = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]

CapabilityName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=80,
        pattern=r"^[a-z][a-z0-9_.-]*$",
    ),
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DeviceChannelRequest(_Strict):
    protocol_version: str = Field(min_length=1, max_length=64)
    kind: str = Field(min_length=1, max_length=32)
    challenge_id: Opaque | None = None
    user_code: str | None = Field(default=None, min_length=4, max_length=32)
    device_id: Opaque | None = None
    proof_algorithm: str | None = Field(default=None, min_length=1, max_length=32)
    proof_public_key: str | None = Field(default=None, min_length=32, max_length=128)
    device_proof: str | None = Field(default=None, min_length=64, max_length=256)
    display_name: str | None = Field(default=None, min_length=1, max_length=80)
    platform: str | None = Field(default=None, pattern=r"^(macos|windows|linux)$")
    node_version: str | None = Field(default=None, min_length=1, max_length=64)
    capability_claims: list[CapabilityName] | None = Field(default=None, max_length=32)
    permission_snapshot_digest: str | None = Field(
        default=None,
        pattern=r"^(?:sha256:)?[0-9a-f]{64}$",
    )
    doctor: dict[str, Any] | None = Field(default=None, max_length=64)
    receipts: list[dict[str, Any]] | None = Field(default=None, max_length=200)
    sent_at: float | None = Field(default=None, gt=0)

    @field_validator("capability_claims")
    @classmethod
    def _unique_claims(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and len(set(value)) != len(value):
            raise ValueError("capability claims must be unique")
        return value


def _broker(request: Request) -> SQLiteDeviceChannelBroker:
    broker = getattr(request.app.state, "local_node_device_channel_broker", None)
    if broker is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "LOCAL_NODE_DEVICE_CHANNEL_UNAVAILABLE",
                "message": "Local node device channel is unavailable",
            },
        )
    return cast(SQLiteDeviceChannelBroker, broker)


def _fault(exc: LocalNodeServiceFault) -> NoReturn:
    status_code = (
        exc.status_code if exc.status_code in {400, 401, 403, 404, 409, 410, 422, 429, 503} else 503
    )
    raise HTTPException(
        status_code=status_code,
        detail={"code": exc.code, "message": "Local node device request was rejected"},
    )


def _exact_payload(payload: DeviceChannelRequest) -> Mapping[str, Any]:
    value = payload.model_dump(exclude_none=True)
    if payload.protocol_version != PROTOCOL_VERSION:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "LOCAL_NODE_PROTOCOL_INCOMPATIBLE",
                "message": "Local node device request was rejected",
            },
        )
    if payload.kind == "pairing_redeem":
        required = {
            "protocol_version",
            "kind",
            "challenge_id",
            "user_code",
            "device_id",
            "proof_algorithm",
            "proof_public_key",
            "device_proof",
            "display_name",
            "platform",
            "node_version",
            "capability_claims",
            "permission_snapshot_digest",
        }
    elif payload.kind == "heartbeat":
        required = {
            "protocol_version",
            "kind",
            "device_id",
            "doctor",
            "receipts",
            "sent_at",
        }
    else:
        required = set()
    if not required or set(value) != required:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "LOCAL_NODE_DEVICE_REQUEST_SHAPE_INVALID",
                "message": "Local node device request was rejected",
            },
        )
    return value


@router.post("")
async def local_node_device_exchange(
    payload: DeviceChannelRequest,
    request: Request,
) -> dict[str, Any]:
    """Redeem a pairing challenge or heartbeat an authenticated device."""

    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail={
                "code": "LOCAL_NODE_CONTENT_TYPE_REQUIRED",
                "message": "Local node device request was rejected",
            },
        )
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > 1_310_720:
                raise HTTPException(status_code=413, detail="Local node request too large")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid content length") from None
    broker = _broker(request)
    value = _exact_payload(payload)
    try:
        if payload.kind == "pairing_redeem":
            return await broker.redeem_pairing(value)
        return await broker.heartbeat(
            authorization=request.headers.get("authorization"),
            payload=value,
        )
    except LocalNodeServiceFault as exc:
        _fault(exc)
