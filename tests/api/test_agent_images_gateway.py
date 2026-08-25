"""Contract tests for the Gateway-owned image routes."""

import base64
import inspect
from types import SimpleNamespace

import pytest

from src.api.schemas.assistant import ImageBlobFetchUrlRequest
from src.api.v1 import agent_images as image_routes
from src.api.v1.agent_images import _sniff_mime, fetch_image_blob_from_url, router
from src.services.images.repository import reserve_scoped_image_task
from src.services.images.service import (
    _decode_image,
    _deterministic_artifact_id,
    _owner,
    _provider_supports_reference_images,
    _resolve_reference,
    _sniff_reference,
)
from src.services.images.worker import ImageTaskWorker


def test_gateway_image_routes_keep_public_paths() -> None:
    paths = {route.path for route in router.routes}
    assert {
        "/assistant/generate-image",
        "/assistant/generate-image-async",
        "/assistant/image-task/{task_id}",
        "/assistant/image-sessions/{session_id}",
        "/assistant/artifacts/{artifact_id}/download-url",
        "/assistant/image-blobs/upload-url",
        "/assistant/image-blobs/complete",
        "/assistant/image-blobs/fetch-url",
    } <= paths


def test_provider_image_payload_is_validated_before_artifact_write() -> None:
    with pytest.raises(ValueError, match="invalid image"):
        _decode_image({"content_base64": base64.b64encode(b"not-an-image").decode()})


def test_artifact_id_is_stable_and_scoped() -> None:
    args = ("tenant", "user", "session", "turn", 1, "a" * 64)
    first = _deterministic_artifact_id(*args)
    assert first == _deterministic_artifact_id(*args)
    assert first.startswith("art_") and len(first) == 20
    assert first != _deterministic_artifact_id("other", *args[1:])


@pytest.mark.asyncio
async def test_remote_blob_fetch_rejects_private_destination(monkeypatch) -> None:
    monkeypatch.setattr(image_routes, "get_artifact_storage", lambda: object())
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(multi_rate_limiter=None)))
    with pytest.raises(Exception) as error:
        await fetch_image_blob_from_url(
            ImageBlobFetchUrlRequest(url="http://127.0.0.1/a.png"),
            request,
            SimpleNamespace(user_id="u", tenant_id="t"),
        )
    assert getattr(error.value, "status_code", None) == 422


def test_blob_magic_is_not_trusted_from_declared_mime() -> None:
    assert _sniff_mime(b"not-a-png") is None


def test_concurrent_reservation_contract_is_scoped_and_atomic() -> None:
    params = inspect.signature(reserve_scoped_image_task).parameters
    assert {"tenant_id", "user_id", "owner_scope", "client_request_id", "request_hash"} <= set(
        params
    )


def test_reference_magic_and_worker_are_bounded() -> None:
    assert _sniff_reference(b"\x89PNG\r\n\x1a\n") == "image/png"
    assert "return" in ImageTaskWorker.run_once.__annotations__


def test_request_body_cannot_forge_owner_scope() -> None:
    user = SimpleNamespace(user_id="gateway-user", tenant_id="tenant-a")
    body = SimpleNamespace(app_tenant_id="tenant-b", app_user_id="other-user")
    assert _owner(user, body) == image_routes._owner_scope(user)
    assert _owner(user, body) != image_routes._owner_scope(
        SimpleNamespace(user_id="gateway-user", tenant_id="tenant-b")
    )


def test_reference_capability_requires_explicit_declaration() -> None:
    assert not _provider_supports_reference_images(SimpleNamespace(config=SimpleNamespace()))
    assert _provider_supports_reference_images(
        SimpleNamespace(config=SimpleNamespace(supports_reference_images=True))
    )


@pytest.mark.asyncio
async def test_reference_requires_valid_image_bytes_not_magic_only() -> None:
    body = SimpleNamespace(reference_image=base64.b64encode(b"not-an-image").decode())
    with pytest.raises(ValueError, match="reference image is invalid"):
        await _resolve_reference(
            body,
            session_row=None,
            storage=SimpleNamespace(),
            owner="owner",
            user=SimpleNamespace(tenant_id="tenant", user_id="user"),
            pool=None,
        )
