"""Image-redesign Phase 2 — comprehensive coverage of the 26 spec scenarios.

Covers:

* Multi-turn lineage primitives (parent_artifact_id, session.latest CAS, branching)
* Watermark raw/display split (raw is always the lineage anchor)
* Style-lock first-turn / inherit / override / default-reset
* `download-url` variant fallback (thumbnail → display → raw, display → raw)
* `image-sessions/{id}` listing + pagination + include_urls
* client_request_id idempotency replay + conflict
* expected_parent_artifact_id CAS pass / 409
* Concurrent CAS (one wins, the other doesn't clobber)
* Owner-scope isolation by app_user_id / app_tenant_id
* Legacy clients (no X-App-* headers, base64/URL references)
* No image bytes leak in responses or logs

Mocking strategy
----------------
We unit-test the route handlers (``generate_image``, ``submit_image_generation``,
``get_image_task_status``, ``get_artifact_download_url``,
``get_image_session_view``) by:

  1. Providing a fake **db pool** sentinel object (truthy, but never actually
     `acquire()`'d) and patching the ``image_state`` functions imported into
     ``assistant_service.api.routes.images`` (``get_image_session``,
     ``upsert_image_session``, ``advance_latest_artifact_cas``,
     ``insert_turn``, ``update_turn_status``, ``get_turn_by_task``,
     ``list_turns``, ``lookup_idempotent``, ``record_idempotent``,
     ``set_locked_style`` aliased as ``_set_locked_style``) with an
     in-memory backing store (``_FakeImageStateStore``).
  2. Patching ``_get_artifact_storage`` with a fake ArtifactStorageService
     stub (``_FakeArtifactStorage``) that supports ``create_artifact``,
     ``find_variant``, ``download_artifact``,
     ``get_presigned_download_url``, and
     ``get_presigned_download_url_for_variant``.
  3. Patching ``get_gemini_image_generator`` / ``get_smart_image_generator``
     for the actual provider call.

A 1×1 PNG is the canned image bytes everywhere — small enough for asserts
to inspect cheaply.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from assistant_service.api.routes import images as images_module
from assistant_service.api.routes.images import (
    _image_tasks,
    AsyncImageGenerationRequest,
    ImageGenerationRequest,
    ImageBlobFetchUrlRequest,
    fetch_image_blob_from_url,
    generate_image,
    get_artifact_download_url,
    get_image_session_view,
    get_image_task_status,
    submit_image_generation,
)
from assistant_service.auth import UserContext
from assistant_service.core.tools.gemini_image_tool import GeminiImageResult
from assistant_service.core.tools.smart_image_generator import (
    SmartImageGenerationResult,
)


# ---------------------------------------------------------------------------
# Tiny PNG fixture (1×1 transparent) — small enough to keep base64 short
# but real bytes so anything that tries to decode it succeeds.
# ---------------------------------------------------------------------------

PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)
PNG_B64 = base64.b64encode(PNG_1X1).decode()


# ---------------------------------------------------------------------------
# UserContext + Request stubs
# ---------------------------------------------------------------------------


def _user(
    user_id: str = "u1",
    tenant_id: str = "t1",
    *,
    app_user_id: str | None = None,
    app_tenant_id: str | None = None,
) -> UserContext:
    return UserContext(
        user_id=user_id,
        tenant_id=tenant_id,
        is_authenticated=True,
        app_user_id=app_user_id,
        app_tenant_id=app_tenant_id,
    )


# Sentinel pool — truthy enough that `pool is not None` checks pass, but
# the route never calls `.acquire()` on it because we patch every
# `image_state` function that would.
_POOL_SENTINEL = object()


def _make_request(session_manager=None) -> SimpleNamespace:
    app_state = SimpleNamespace(
        session_manager=session_manager,
        multi_rate_limiter=None,
        redis=None,
    )
    return SimpleNamespace(
        app=SimpleNamespace(state=app_state),
        headers={},
        client=SimpleNamespace(host="127.0.0.1"),
    )


def _registry_stub(provider: str = "google") -> MagicMock:
    reg = MagicMock()
    info = MagicMock()
    info.provider = MagicMock()
    info.provider.value = provider
    reg.get_model.return_value = info
    return reg


# ---------------------------------------------------------------------------
# In-memory ArtifactStorage fake
# ---------------------------------------------------------------------------


class _FakeArtifactStorage:
    """Just enough of ``ArtifactStorageService`` to drive the route layer.

    Stores ArtifactInfo-shaped MagicMocks indexed by artifact_id; tracks
    bytes in a parallel dict; supports owner_scope via the live attribute
    on each artifact entry.

    Variant family: every variant carries the raw artifact_id as its
    ``parent_artifact_id``; raw rows have ``parent_artifact_id=None``.
    ``find_variant`` walks the family from any starting member.
    """

    def __init__(self) -> None:
        self._artifacts: dict[str, MagicMock] = {}
        self._bytes: dict[str, bytes] = {}
        self._counter = 0
        # call counters for assertions
        self.create_calls: list[dict] = []

    def _new_id(self) -> str:
        self._counter += 1
        return f"art_{self._counter:04d}_{uuid.uuid4().hex[:6]}"

    async def create_artifact(self, **kwargs) -> MagicMock:
        aid = self._new_id()
        a = MagicMock()
        a.artifact_id = aid
        a.session_id = kwargs.get("session_id")
        a.tenant_id = kwargs.get("tenant_id")
        a.user_id = kwargs.get("user_id")
        a.type = kwargs.get("type")
        a.format = kwargs.get("format")
        a.title = kwargs.get("title")
        a.filename = kwargs.get("filename")
        a.storage_key = f"key/{aid}"
        a.size_bytes = len(kwargs.get("content") or b"")
        a.mime_type = f"image/{kwargs.get('format', 'png')}"
        a.source = kwargs.get("source", "ai")
        a.message_id = None
        a.metadata = {}
        a.created_at = datetime.now(timezone.utc)
        a.updated_at = a.created_at
        a.variant = kwargs.get("variant", "raw")
        a.parent_artifact_id = kwargs.get("parent_artifact_id")
        a.turn_id = kwargs.get("turn_id")
        a.owner_scope = kwargs.get("owner_scope")
        a.width = kwargs.get("width")
        a.height = kwargs.get("height")
        a.provider = kwargs.get("provider")
        a.model_id = kwargs.get("model_id")
        a.prompt = kwargs.get("prompt")
        self._artifacts[aid] = a
        if "content" in kwargs:
            self._bytes[aid] = kwargs["content"]
        record = dict(kwargs)
        record["_returned_id"] = aid
        self.create_calls.append(record)
        return a

    async def find_variant(self, artifact_id: str, variant: str):
        base = self._artifacts.get(artifact_id)
        if base is None:
            return None
        if base.variant == variant:
            return base
        if base.variant == "raw":
            raw_id = base.artifact_id
        else:
            raw_id = base.parent_artifact_id
            if not raw_id:
                return None
        if variant == "raw":
            return self._artifacts.get(raw_id)
        # find any sibling with parent_artifact_id == raw_id and variant matches
        for a in self._artifacts.values():
            if a.parent_artifact_id == raw_id and a.variant == variant:
                return a
        return None

    async def get_presigned_download_url(self, artifact, expiry_seconds: int = 3600):
        aid = artifact.artifact_id if hasattr(artifact, "artifact_id") else artifact
        return f"https://s3.example.com/{aid}?exp={expiry_seconds}"

    async def get_presigned_download_url_for_variant(
        self,
        artifact_id: str,
        variant: str = "display",
        expiry_seconds: int = 3600,
        *,
        owner_scope: str | None = None,
        tenant_id: str | None = None,
        user_id: str | None = None,
    ):
        order = (
            ("thumbnail", "display", "raw")
            if variant == "thumbnail"
            else ("display", "raw")
            if variant == "display"
            else ("raw",)
        )
        for v in order:
            artifact = await self.find_variant(artifact_id, v)
            if artifact is None:
                continue
            if owner_scope is not None:
                if artifact.owner_scope is not None:
                    if artifact.owner_scope != owner_scope:
                        continue
                elif not (
                    tenant_id
                    and user_id
                    and artifact.tenant_id == tenant_id
                    and artifact.user_id == user_id
                ):
                    continue
            url = await self.get_presigned_download_url(artifact, expiry_seconds)
            return url, v
        return None, None

    async def download_artifact(self, artifact_id: str) -> bytes | None:
        return self._bytes.get(artifact_id)

    async def get_artifact(self, artifact_id: str, *, owner_scope: str | None = None):
        a = self._artifacts.get(artifact_id)
        if a is None:
            return None
        if owner_scope is not None and a.owner_scope is not None:
            if a.owner_scope != owner_scope:
                return None
        return a


class _FakeBlobBackend:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}

    async def upload(self, key: str, content: bytes, content_type: str, metadata=None):
        self.objects[key] = (content, content_type)
        return f"s3://bucket/{key}"

    async def download(self, key: str) -> bytes:
        return self.objects[key][0]

    async def exists(self, key: str) -> bool:
        return key in self.objects

    async def generate_presigned_upload_url(self, key: str, content_type: str, expiry_seconds=900, metadata=None):
        return {"url": f"https://s3.example.com/upload/{key}", "method": "PUT"}


# ---------------------------------------------------------------------------
# In-memory image_state store + stub functions
# ---------------------------------------------------------------------------


class _FakeImageStateStore:
    """Holds image_sessions / image_turns / image_idempotency rows."""

    def __init__(self) -> None:
        # session_id → row dict
        self.sessions: dict[str, dict] = {}
        # turn_id → row dict (insertion-ordered for stable ordering)
        self.turns: dict[str, dict] = {}
        # (owner_scope, client_request_id) → {request_hash, task_id, created_at}
        self.idem: dict[tuple[str, str], dict] = {}
        # set_locked_style call counter (for asserting NO write on failure)
        self.set_locked_style_calls: list[tuple[str, str | None]] = []
        self.advance_calls: list[dict] = []

    # image_sessions
    async def get_image_session(self, pool, session_id: str):
        return deepcopy(self.sessions.get(session_id))

    async def upsert_image_session(
        self,
        pool,
        *,
        session_id: str,
        owner_scope: str,
        app_user_id: str | None,
        app_tenant_id: str | None,
        locked_style: str | None = None,
    ):
        existing = self.sessions.get(session_id)
        now = datetime.now(timezone.utc)
        if existing is None:
            self.sessions[session_id] = {
                "session_id": session_id,
                "owner_scope": owner_scope,
                "app_user_id": app_user_id,
                "app_tenant_id": app_tenant_id,
                "latest_artifact_id": None,
                "locked_style": locked_style,
                "created_at": now,
                "updated_at": now,
            }
        else:
            existing["updated_at"] = now
            if locked_style is not None:
                existing["locked_style"] = locked_style

    async def set_locked_style(self, pool, session_id: str, style: str | None):
        self.set_locked_style_calls.append((session_id, style))
        if session_id in self.sessions:
            self.sessions[session_id]["locked_style"] = style
            self.sessions[session_id]["updated_at"] = datetime.now(timezone.utc)

    async def advance_latest_artifact_cas(
        self,
        pool,
        *,
        session_id: str,
        expected_parent: str | None,
        new_artifact_id: str,
    ) -> bool:
        self.advance_calls.append({
            "session_id": session_id,
            "expected_parent": expected_parent,
            "new_artifact_id": new_artifact_id,
        })
        row = self.sessions.get(session_id)
        if row is None:
            return False
        if row["latest_artifact_id"] != expected_parent:
            return False
        row["latest_artifact_id"] = new_artifact_id
        row["updated_at"] = datetime.now(timezone.utc)
        return True

    # image_turns
    async def insert_turn(
        self,
        pool,
        *,
        turn_id: str,
        session_id: str,
        owner_scope: str,
        task_id: str | None,
        prompt: str | None,
        model_id: str | None,
        style: str | None,
        add_watermark: bool,
        parent_artifact_id: str | None,
        output_artifact_id: str | None,
        status: str,
        error: str | None,
        error_code: str | None,
        client_request_id: str | None,
        request_hash: str | None,
        completed_at=None,
        thought_signature: str | None = None,
        provider_text: str | None = None,
        output_artifact_ids: list[str] | None = None,
        state: str | None = None,
    ):
        existing = self.turns.get(turn_id)
        now = datetime.now(timezone.utc)
        if existing is None:
            self.turns[turn_id] = {
                "turn_id": turn_id,
                "session_id": session_id,
                "owner_scope": owner_scope,
                "task_id": task_id,
                "prompt": prompt,
                "model_id": model_id,
                "style": style,
                "add_watermark": add_watermark,
                "parent_artifact_id": parent_artifact_id,
                "output_artifact_id": output_artifact_id,
                "status": status,
                "error": error,
                "error_code": error_code,
                "client_request_id": client_request_id,
                "request_hash": request_hash,
                "thought_signature": thought_signature,
                "provider_text": provider_text,
                "output_artifact_ids": output_artifact_ids,
                "state": state or status,
                "created_at": now,
                "completed_at": completed_at,
            }
        else:
            existing["status"] = status
            existing["error"] = error
            existing["error_code"] = error_code
            if output_artifact_id is not None:
                existing["output_artifact_id"] = output_artifact_id
            if completed_at is not None:
                existing["completed_at"] = completed_at
            if thought_signature is not None:
                existing["thought_signature"] = thought_signature
            if provider_text is not None:
                existing["provider_text"] = provider_text
            if output_artifact_ids is not None:
                existing["output_artifact_ids"] = output_artifact_ids
            existing["state"] = state or status

    async def update_turn_status(
        self,
        pool,
        *,
        turn_id: str,
        status: str,
        output_artifact_id: str | None = None,
        error: str | None = None,
        error_code: str | None = None,
    ):
        row = self.turns.get(turn_id)
        if row is None:
            return
        row["status"] = status
        if output_artifact_id is not None:
            row["output_artifact_id"] = output_artifact_id
        row["error"] = error
        row["error_code"] = error_code
        if status in ("completed", "failed"):
            row["completed_at"] = datetime.now(timezone.utc)

    async def get_turn_by_task(self, pool, task_id: str):
        candidates = [r for r in self.turns.values() if r.get("task_id") == task_id]
        candidates.sort(key=lambda r: r["created_at"], reverse=True)
        return deepcopy(candidates[0]) if candidates else None

    async def list_turns(
        self,
        pool,
        *,
        session_id: str,
        owner_scope: str,
        limit: int = 50,
        cursor: str | None = None,
    ):
        rows = [
            r for r in self.turns.values()
            if r["session_id"] == session_id and r["owner_scope"] == owner_scope
        ]
        rows.sort(key=lambda r: (r["created_at"], r["turn_id"]), reverse=True)
        if cursor:
            try:
                ts_str, turn_id = cursor.split("|", 1)
                cur_ts = datetime.fromisoformat(ts_str)
                rows = [
                    r for r in rows
                    if (r["created_at"], r["turn_id"]) < (cur_ts, turn_id)
                ]
            except Exception:
                pass
        limit = max(1, min(limit, 200))
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit - 1]
            next_cursor = f"{last['created_at'].isoformat()}|{last['turn_id']}"
            rows = rows[:limit]
        return [deepcopy(r) for r in rows], next_cursor

    # idempotency
    async def lookup_idempotent(self, pool, *, owner_scope: str, client_request_id: str):
        return deepcopy(self.idem.get((owner_scope, client_request_id)))

    async def record_idempotent(
        self,
        pool,
        *,
        owner_scope: str,
        client_request_id: str,
        request_hash: str,
        task_id: str,
    ) -> bool:
        key = (owner_scope, client_request_id)
        if key in self.idem:
            return False
        self.idem[key] = {
            "owner_scope": owner_scope,
            "client_request_id": client_request_id,
            "request_hash": request_hash,
            "task_id": task_id,
            "created_at": datetime.now(timezone.utc),
        }
        return True


# ---------------------------------------------------------------------------
# Patch helper — installs the fakes onto the route module + DB pool sentinel
# ---------------------------------------------------------------------------


class _Harness:
    """Bundles the storage + state-store fakes and the patch context manager.

    Use as a context manager around test bodies.
    """

    def __init__(self, *, gemini_result=None, smart_result=None) -> None:
        self.storage = _FakeArtifactStorage()
        self.state = _FakeImageStateStore()
        self.gemini = MagicMock()
        self.gemini.is_configured = True
        self.gemini.generate = AsyncMock(
            return_value=gemini_result or _gemini_image_result(),
        )
        self.gemini.generate_chat = AsyncMock(
            return_value=gemini_result or _gemini_image_result(),
        )
        self.smart = MagicMock()
        self.smart.generate = AsyncMock(
            return_value=smart_result or _smart_image_result(),
        )
        self._stack: list = []

    def __enter__(self):
        ps = [
            patch.object(images_module, "_get_artifact_storage", return_value=self.storage),
            patch.object(images_module, "_get_db_pool", return_value=_POOL_SENTINEL),
            patch.object(images_module, "get_gemini_image_generator", return_value=self.gemini),
            patch.object(images_module, "get_smart_image_generator", return_value=self.smart),
            # image_state functions
            patch.object(images_module, "get_image_session", side_effect=self.state.get_image_session),
            patch.object(images_module, "upsert_image_session", side_effect=self.state.upsert_image_session),
            patch.object(images_module, "_set_locked_style", side_effect=self.state.set_locked_style),
            patch.object(images_module, "advance_latest_artifact_cas", side_effect=self.state.advance_latest_artifact_cas),
            patch.object(images_module, "insert_turn", side_effect=self.state.insert_turn),
            patch.object(images_module, "update_turn_status", side_effect=self.state.update_turn_status),
            patch.object(images_module, "get_turn_by_task", side_effect=self.state.get_turn_by_task),
            patch.object(images_module, "list_turns", side_effect=self.state.list_turns),
            patch.object(images_module, "lookup_idempotent", side_effect=self.state.lookup_idempotent),
            patch.object(images_module, "record_idempotent", side_effect=self.state.record_idempotent),
        ]
        for p in ps:
            p.start()
            self._stack.append(p)
        return self

    def __exit__(self, *exc):
        for p in reversed(self._stack):
            try:
                p.stop()
            except Exception:
                pass
        self._stack.clear()
        return False


def _gemini_image_result(b64: str = PNG_B64) -> GeminiImageResult:
    return GeminiImageResult(
        success=True,
        images=[{
            "filename": "g.png",
            "content_base64": b64,
            "mime_type": "image/png",
            "size_bytes": len(PNG_1X1),
        }],
        text=None,
        duration_ms=10.0,
    )


def _smart_image_result(b64: str = PNG_B64, provider: str = "google") -> SmartImageGenerationResult:
    return SmartImageGenerationResult(
        success=True,
        provider=provider,
        images=[{
            "filename": "s.png",
            "content_base64": b64,
            "mime_type": "image/png",
            "size_bytes": len(PNG_1X1),
        }],
        duration_ms=12.0,
    )


# ===========================================================================
# Scenarios
# ===========================================================================

GEMINI_MODEL = "gemini-3.1-flash-image-preview"


# ---- 1. Pure text-to-image async happy path -------------------------------

@pytest.mark.asyncio
async def test_01_pure_text_to_image_async_happy_path():
    body = AsyncImageGenerationRequest(
        prompt="a sunset over mountains",
        model_id=GEMINI_MODEL,
        add_watermark=False,
    )
    user = _user()

    with _Harness() as h:
        request = _make_request()
        # Patch get_session_manager which submit_image_generation uses
        with patch.object(images_module, "get_session_manager", return_value=None):
            submit = await submit_image_generation(
                body=body, request=request, user=user,
                model_registry=_registry_stub("google"),
            )
        # Drive the worker tasks to completion
        await asyncio.sleep(0)
        await asyncio.gather(*list(images_module._in_flight_workers), return_exceptions=True)

        # Poll
        status = await get_image_task_status(
            task_id=submit.task_id, request=request, user=user,
        )

    assert status.status == "completed"
    assert status.task_id == submit.task_id
    assert len(status.images) == 1
    assert status.images[0].artifact_id is not None
    assert status.images[0].url and status.images[0].url.startswith("https://")
    assert status.turn_id is not None
    assert status.session_id is None  # text-to-image without session


@pytest.mark.asyncio
async def test_01b_async_caps_provider_over_return_to_requested_n():
    body = AsyncImageGenerationRequest(
        prompt="a red square",
        model_id=GEMINI_MODEL,
        n=1,
        add_watermark=False,
    )
    user = _user()
    multi_result = SmartImageGenerationResult(
        success=True,
        provider="google",
        images=[
            {
                "filename": "s1.png",
                "content_base64": PNG_B64,
                "mime_type": "image/png",
                "size_bytes": len(PNG_1X1),
            },
            {
                "filename": "s2.png",
                "content_base64": PNG_B64,
                "mime_type": "image/png",
                "size_bytes": len(PNG_1X1),
            },
        ],
        duration_ms=12.0,
    )

    with _Harness(smart_result=multi_result) as h:
        request = _make_request()
        with patch.object(images_module, "get_session_manager", return_value=None):
            submit = await submit_image_generation(
                body=body, request=request, user=user,
                model_registry=_registry_stub("google"),
            )
        await asyncio.sleep(0)
        await asyncio.gather(*list(images_module._in_flight_workers), return_exceptions=True)

        status = await get_image_task_status(
            task_id=submit.task_id, request=request, user=user,
        )

    assert status.status == "completed"
    assert len(status.images) == 1
    assert len(h.storage._artifacts) == 1


# ---- 2. Session first turn — image_sessions row gets created --------------

@pytest.mark.asyncio
async def test_02_session_first_turn_creates_session_row_and_locks_style():
    body = ImageGenerationRequest(
        prompt="a cat",
        model_id=GEMINI_MODEL,
        session_id="sess-2",
        style="oil_paint",
        add_watermark=False,
    )

    with _Harness() as h:
        # No reference, no latest_artifact → falls into smart router branch
        # (not legacy session_manager because we don't pass one). That means
        # smart router generates and post-bookkeeping runs.
        resp = await generate_image(
            body=body,
            request=_make_request(session_manager=None),
            user=_user(),
            model_registry=_registry_stub("google"),
        )

    assert resp.success is True
    assert "sess-2" in h.state.sessions, "image_sessions row should be created"
    sess = h.state.sessions["sess-2"]
    assert sess["latest_artifact_id"] is not None, "latest_artifact_id must advance to new raw"
    assert sess["locked_style"] == "oil_paint"


# ---- 3. Session second turn auto-uses RAW (not watermarked display) -------

@pytest.mark.asyncio
async def test_03_session_second_turn_uses_raw_bytes_for_gemini():
    sid = "sess-3"
    user = _user()
    with _Harness() as h:
        # Turn 1 — watermarked, will create raw + display
        body1 = ImageGenerationRequest(
            prompt="a cat", model_id=GEMINI_MODEL,
            session_id=sid, add_watermark=True,
        )
        resp1 = await generate_image(
            body=body1, request=_make_request(),
            user=user, model_registry=_registry_stub("google"),
        )
        assert resp1.success
        raw_anchor = resp1.output_artifact_id
        assert raw_anchor is not None
        # latest_artifact_id should be raw (not display)
        assert h.state.sessions[sid]["latest_artifact_id"] == raw_anchor
        raw_obj = h.storage._artifacts[raw_anchor]
        assert raw_obj.variant == "raw"

        # Turn 2 — same session, no explicit ref → resolved from session.latest
        body2 = ImageGenerationRequest(
            prompt="add a hat", model_id=GEMINI_MODEL,
            session_id=sid, add_watermark=False,
        )
        h.gemini.generate.reset_mock()
        resp2 = await generate_image(
            body=body2, request=_make_request(),
            user=user, model_registry=_registry_stub("google"),
        )
        assert resp2.success

    # Gemini.generate (single-image edit path) was called with reference_image
    # bytes that match the RAW (not the watermarked display)
    h.gemini.generate.assert_called_once()
    ref_b64 = h.gemini.generate.call_args.kwargs["reference_image"]
    raw_bytes = h.storage._bytes[raw_anchor]
    assert ref_b64 == base64.b64encode(raw_bytes).decode()


# ---- 4. parent_artifact_id explicit edit ---------------------------------

@pytest.mark.asyncio
async def test_04_parent_artifact_id_overrides_session_latest():
    user = _user()
    with _Harness() as h:
        # Turn 1
        sid = "sess-4"
        resp1 = await generate_image(
            body=ImageGenerationRequest(
                prompt="a cat", model_id=GEMINI_MODEL,
                session_id=sid, add_watermark=False,
            ),
            request=_make_request(), user=user, model_registry=_registry_stub("google"),
        )
        turn1_raw = resp1.output_artifact_id
        assert turn1_raw is not None
        # Turn 2 with explicit parent_artifact_id pointing at turn1_raw
        h.gemini.generate.reset_mock()
        resp2 = await generate_image(
            body=ImageGenerationRequest(
                prompt="add hat", model_id=GEMINI_MODEL,
                session_id=sid, parent_artifact_id=turn1_raw,
                add_watermark=False,
            ),
            request=_make_request(), user=user, model_registry=_registry_stub("google"),
        )
        assert resp2.success
        assert resp2.parent_artifact_id == turn1_raw

    h.gemini.generate.assert_called_once()
    ref = h.gemini.generate.call_args.kwargs["reference_image"]
    assert ref == base64.b64encode(h.storage._bytes[turn1_raw]).decode()


# ---- 5. add_watermark=True returns display URL but stores RAW for next ----

@pytest.mark.asyncio
async def test_05_watermark_keeps_raw_for_lineage_and_display_for_url():
    sid = "sess-5"
    user = _user()
    with _Harness() as h:
        # Turn 1 watermarked
        resp1 = await generate_image(
            body=ImageGenerationRequest(
                prompt="a cat", model_id=GEMINI_MODEL,
                session_id=sid, add_watermark=True,
            ),
            request=_make_request(), user=user, model_registry=_registry_stub("google"),
        )

        raw_anchor = resp1.output_artifact_id
        # latest in session is the raw anchor (not the display variant)
        assert h.state.sessions[sid]["latest_artifact_id"] == raw_anchor
        raw_obj = h.storage._artifacts[raw_anchor]
        assert raw_obj.variant == "raw"
        # The user-facing url is for the display variant — different artifact id
        public_artifact_id = resp1.images[0].artifact_id
        assert public_artifact_id != raw_anchor
        public_obj = h.storage._artifacts[public_artifact_id]
        assert public_obj.variant == "display"
        assert public_obj.parent_artifact_id == raw_anchor

        # Turn 2 — Gemini gets raw bytes
        h.gemini.generate.reset_mock()
        await generate_image(
            body=ImageGenerationRequest(
                prompt="b", model_id=GEMINI_MODEL,
                session_id=sid, add_watermark=False,
            ),
            request=_make_request(), user=user, model_registry=_registry_stub("google"),
        )
    h.gemini.generate.assert_called_once()
    ref = h.gemini.generate.call_args.kwargs["reference_image"]
    assert ref == base64.b64encode(h.storage._bytes[raw_anchor]).decode()


# ---- 6. Style lock — first locks, inherit, override, default reset --------

@pytest.mark.asyncio
async def test_06_style_lock_lifecycle():
    sid = "sess-6"
    user = _user()
    with _Harness() as h:
        # Turn 1 — explicit photography → lock
        await generate_image(
            body=ImageGenerationRequest(
                prompt="t1", model_id=GEMINI_MODEL,
                session_id=sid, style="photography", add_watermark=False,
            ),
            request=_make_request(), user=user, model_registry=_registry_stub("google"),
        )
        assert h.state.sessions[sid]["locked_style"] == "realistic"  # alias

        # Turn 2 — default → inherit (no lock change)
        before = h.state.sessions[sid]["locked_style"]
        await generate_image(
            body=ImageGenerationRequest(
                prompt="t2", model_id=GEMINI_MODEL,
                session_id=sid, add_watermark=False,  # style defaults to DEFAULT
            ),
            request=_make_request(), user=user, model_registry=_registry_stub("google"),
        )
        assert h.state.sessions[sid]["locked_style"] == before, "default style must inherit lock"

        # Turn 3 — explicit anime → lock updates
        await generate_image(
            body=ImageGenerationRequest(
                prompt="t3", model_id=GEMINI_MODEL,
                session_id=sid, style="anime", add_watermark=False,
            ),
            request=_make_request(), user=user, model_registry=_registry_stub("google"),
        )
        assert h.state.sessions[sid]["locked_style"] == "anime"

        # Turn 4 — explicit "default" → caller is *resetting* the lock back
        # to default. Detected via Pydantic's `model_fields_set` (the caller
        # named the field), so the route distinguishes this from "didn't
        # touch style" → inherit.
        await generate_image(
            body=ImageGenerationRequest(
                prompt="t4", model_id=GEMINI_MODEL,
                session_id=sid, style="default", add_watermark=False,
            ),
            request=_make_request(), user=user, model_registry=_registry_stub("google"),
        )
        # explicit `default` clears the lock.
        assert h.state.sessions[sid]["locked_style"] is None


# ---- 7-9. download-url variant resolution --------------------------------

async def _seed_artifact_family(harness: _Harness, *, with_display: bool, with_thumb: bool, owner_scope: str) -> str:
    raw = await harness.storage.create_artifact(
        session_id="seed", tenant_id="t1", user_id="u1",
        type="image", format="png", title="t", filename="r.png",
        content=PNG_1X1, source="image_generation",
        variant="raw", parent_artifact_id=None, owner_scope=owner_scope,
        width=1, height=1,
    )
    if with_display:
        await harness.storage.create_artifact(
            session_id="seed", tenant_id="t1", user_id="u1",
            type="image", format="png", title="d", filename="d.png",
            content=PNG_1X1, source="image_generation_watermarked",
            variant="display", parent_artifact_id=raw.artifact_id,
            owner_scope=owner_scope, width=1, height=1,
        )
    if with_thumb:
        await harness.storage.create_artifact(
            session_id="seed", tenant_id="t1", user_id="u1",
            type="image", format="png", title="thu", filename="t.png",
            content=PNG_1X1, source="image_generation_thumbnail",
            variant="thumbnail", parent_artifact_id=raw.artifact_id,
            owner_scope=owner_scope, width=1, height=1,
        )
    return raw.artifact_id


@pytest.mark.asyncio
async def test_07_download_url_display_happy_path():
    user = _user()
    owner_scope = user.user_id
    with _Harness() as h:
        raw_id = await _seed_artifact_family(
            h, with_display=True, with_thumb=False, owner_scope=owner_scope,
        )
        resp = await get_artifact_download_url(
            artifact_id=raw_id, request=_make_request(),
            variant="display", expires_in=600, user=user,
        )
        assert resp.variant == "display"
        assert resp.url.startswith("https://s3.example.com/")


@pytest.mark.asyncio
async def test_08_download_url_raw_happy_path():
    user = _user()
    with _Harness() as h:
        raw_id = await _seed_artifact_family(
            h, with_display=True, with_thumb=False, owner_scope=user.user_id,
        )
        resp = await get_artifact_download_url(
            artifact_id=raw_id, request=_make_request(),
            variant="raw", expires_in=600, user=user,
        )
        assert resp.variant == "raw"


@pytest.mark.asyncio
async def test_09_download_url_display_falls_back_to_raw_when_no_sibling():
    """Audit: display→raw fallback is documented behavior."""
    user = _user()
    with _Harness() as h:
        raw_id = await _seed_artifact_family(
            h, with_display=False, with_thumb=False, owner_scope=user.user_id,
        )
        resp = await get_artifact_download_url(
            artifact_id=raw_id, request=_make_request(),
            variant="display", expires_in=600, user=user,
        )
        assert resp.variant == "raw"


# ---- 10. download-url 404 for missing artifact ---------------------------

@pytest.mark.asyncio
async def test_10_download_url_missing_returns_404():
    from fastapi import HTTPException
    with _Harness() as h:
        with pytest.raises(HTTPException) as exc:
            await get_artifact_download_url(
                artifact_id="art_nonexistent", request=_make_request(),
                variant="display", expires_in=600, user=_user(),
            )
    assert exc.value.status_code == 404


# ---- 11. Cross-owner artifact returns 404 (IDOR-safe) --------------------

@pytest.mark.asyncio
async def test_11_download_url_cross_owner_returns_404():
    with _Harness() as h:
        raw_id = await _seed_artifact_family(
            h, with_display=True, with_thumb=False, owner_scope="other_user",
        )
        with pytest.raises(HTTPException) as exc:
            await get_artifact_download_url(
                artifact_id=raw_id, request=_make_request(),
                variant="display", expires_in=600, user=_user("u1", "t1"),
            )
    assert exc.value.status_code == 404


# ---- 12. /image-sessions — happy path -----------------------------------

@pytest.mark.asyncio
async def test_12_image_sessions_listing_no_urls():
    sid = "sess-12"
    user = _user()
    with _Harness() as h:
        # Submit 3 turns
        for i in range(3):
            await generate_image(
                body=ImageGenerationRequest(
                    prompt=f"t{i}", model_id=GEMINI_MODEL,
                    session_id=sid, add_watermark=False,
                ),
                request=_make_request(), user=user,
                model_registry=_registry_stub("google"),
            )
            # Force monotonically-increasing created_at — _FakeStore writes
            # `now` directly so we may need a tiny separator on fast machines.
            await asyncio.sleep(0.001)

        resp = await get_image_session_view(
            session_id=sid, request=_make_request(),
            limit=50, cursor=None, include_urls=False, user=user,
        )

    assert len(resp.turns) == 3
    # DESC ordering by created_at
    times = [t.created_at for t in resp.turns]
    assert times == sorted(times, reverse=True)
    # No URLs
    assert all(t.output_url is None for t in resp.turns)
    # Session state echoed
    assert resp.latest_artifact_id is not None


# ---- 13. /image-sessions — pagination -----------------------------------

@pytest.mark.asyncio
async def test_13_image_sessions_pagination():
    sid = "sess-13"
    user = _user()
    with _Harness() as h:
        for i in range(5):
            await generate_image(
                body=ImageGenerationRequest(
                    prompt=f"t{i}", model_id=GEMINI_MODEL,
                    session_id=sid, add_watermark=False,
                ),
                request=_make_request(), user=user,
                model_registry=_registry_stub("google"),
            )
            await asyncio.sleep(0.001)

        # Walk pages with limit=2
        seen: list[str] = []
        cursor = None
        for _ in range(5):  # safety bound
            resp = await get_image_session_view(
                session_id=sid, request=_make_request(),
                limit=2, cursor=cursor, include_urls=False, user=user,
            )
            seen.extend(t.turn_id for t in resp.turns)
            if not resp.next_cursor:
                break
            cursor = resp.next_cursor

    assert len(seen) == 5
    assert len(set(seen)) == 5


# ---- 14. /image-sessions with include_urls=true -------------------------

@pytest.mark.asyncio
async def test_14_image_sessions_with_include_urls():
    sid = "sess-14"
    user = _user()
    with _Harness() as h:
        await generate_image(
            body=ImageGenerationRequest(
                prompt="t", model_id=GEMINI_MODEL,
                session_id=sid, add_watermark=False,
            ),
            request=_make_request(), user=user,
            model_registry=_registry_stub("google"),
        )
        resp = await get_image_session_view(
            session_id=sid, request=_make_request(),
            limit=50, cursor=None, include_urls=True, user=user,
        )

    assert len(resp.turns) == 1
    assert resp.turns[0].output_url is not None
    assert resp.turns[0].output_url.startswith("https://")


# ---- 15. client_request_id idempotency replay ----------------------------

@pytest.mark.asyncio
async def test_15_idempotency_replay_returns_same_task():
    user = _user()
    body = AsyncImageGenerationRequest(
        prompt="x", model_id=GEMINI_MODEL,
        client_request_id="cri-15",
    )
    with _Harness() as h:
        with patch.object(images_module, "get_session_manager", return_value=None):
            r1 = await submit_image_generation(
                body=body, request=_make_request(), user=user,
                model_registry=_registry_stub("google"),
            )
            await asyncio.gather(*list(images_module._in_flight_workers), return_exceptions=True)
            r2 = await submit_image_generation(
                body=body, request=_make_request(), user=user,
                model_registry=_registry_stub("google"),
            )
    assert r2.task_id == r1.task_id
    assert "replay" in r2.message.lower() or "idempotent" in r2.message.lower()


# ---- 16. client_request_id with different payload → 409 -----------------

@pytest.mark.asyncio
async def test_16_idempotency_conflict_on_different_payload():
    from fastapi import HTTPException
    user = _user()
    body1 = AsyncImageGenerationRequest(
        prompt="first", model_id=GEMINI_MODEL,
        client_request_id="cri-16",
    )
    body2 = AsyncImageGenerationRequest(
        prompt="second-different", model_id=GEMINI_MODEL,
        client_request_id="cri-16",
    )
    with _Harness() as h:
        with patch.object(images_module, "get_session_manager", return_value=None):
            await submit_image_generation(
                body=body1, request=_make_request(), user=user,
                model_registry=_registry_stub("google"),
            )
            await asyncio.gather(*list(images_module._in_flight_workers), return_exceptions=True)
            with pytest.raises(HTTPException) as exc:
                await submit_image_generation(
                    body=body2, request=_make_request(), user=user,
                    model_registry=_registry_stub("google"),
                )
    assert exc.value.status_code == 409
    detail = exc.value.detail
    assert isinstance(detail, dict)
    assert detail["error_code"] == "idempotency_conflict"


# ---- 17. expected_parent matches → 200 ----------------------------------

@pytest.mark.asyncio
async def test_17_expected_parent_match_succeeds():
    sid = "sess-17"
    user = _user()
    with _Harness() as h:
        r1 = await generate_image(
            body=ImageGenerationRequest(
                prompt="t1", model_id=GEMINI_MODEL,
                session_id=sid, add_watermark=False,
            ),
            request=_make_request(), user=user, model_registry=_registry_stub("google"),
        )
        latest = r1.output_artifact_id
        r2 = await generate_image(
            body=ImageGenerationRequest(
                prompt="t2", model_id=GEMINI_MODEL,
                session_id=sid, add_watermark=False,
                expected_parent_artifact_id=latest,
            ),
            request=_make_request(), user=user, model_registry=_registry_stub("google"),
        )
    assert r2.success is True


# ---- 18. expected_parent mismatches → 409 -------------------------------

@pytest.mark.asyncio
async def test_18_expected_parent_mismatch_409():
    from fastapi import HTTPException
    sid = "sess-18"
    user = _user()
    with _Harness() as h:
        await generate_image(
            body=ImageGenerationRequest(
                prompt="t1", model_id=GEMINI_MODEL,
                session_id=sid, add_watermark=False,
            ),
            request=_make_request(), user=user, model_registry=_registry_stub("google"),
        )
        with pytest.raises(HTTPException) as exc:
            await generate_image(
                body=ImageGenerationRequest(
                    prompt="t2", model_id=GEMINI_MODEL,
                    session_id=sid, add_watermark=False,
                    expected_parent_artifact_id="art_does_not_match",
                ),
                request=_make_request(), user=user,
                model_registry=_registry_stub("google"),
            )
    assert exc.value.status_code == 409
    detail = exc.value.detail
    assert isinstance(detail, dict) and detail["error_code"] == "latest_artifact_conflict"


# ---- 19. Concurrent submits — only one wins CAS ------------------------

@pytest.mark.asyncio
async def test_19_concurrent_submits_one_wins_cas():
    sid = "sess-19"
    user = _user()
    with _Harness() as h:
        # Seed one turn so both concurrent requests share the same parent
        r0 = await generate_image(
            body=ImageGenerationRequest(
                prompt="t0", model_id=GEMINI_MODEL,
                session_id=sid, add_watermark=False,
            ),
            request=_make_request(), user=user, model_registry=_registry_stub("google"),
        )
        parent = r0.output_artifact_id

        async def _submit(prompt: str):
            return await generate_image(
                body=ImageGenerationRequest(
                    prompt=prompt, model_id=GEMINI_MODEL,
                    session_id=sid, parent_artifact_id=parent,
                    add_watermark=False,
                ),
                request=_make_request(), user=user,
                model_registry=_registry_stub("google"),
            )

        a, b = await asyncio.gather(_submit("a"), _submit("b"))

    # Both succeeded
    assert a.success and b.success
    a_id, b_id = a.output_artifact_id, b.output_artifact_id
    assert a_id != b_id
    final_latest = h.state.sessions[sid]["latest_artifact_id"]
    # Whichever won the CAS must equal latest, and the other must NOT
    assert final_latest in {a_id, b_id}
    # Both turn rows present
    assert len([
        t for t in h.state.turns.values() if t["session_id"] == sid
    ]) == 3  # t0 + a + b


# ---- 20. Owner isolation by app_user_id --------------------------------

@pytest.mark.asyncio
async def test_20_cross_app_user_cannot_read_session():
    user_a = _user("u1", "t1", app_user_id="end-A", app_tenant_id="appT")
    user_b = _user("u1", "t1", app_user_id="end-B", app_tenant_id="appT")
    sid = "sess-20"

    with _Harness() as h:
        # User A creates a session
        await generate_image(
            body=ImageGenerationRequest(
                prompt="A image", model_id=GEMINI_MODEL,
                session_id=sid, add_watermark=False,
            ),
            request=_make_request(), user=user_a,
            model_registry=_registry_stub("google"),
        )
        with pytest.raises(HTTPException) as exc:
            await get_image_session_view(
                session_id=sid, request=_make_request(),
                limit=50, cursor=None, include_urls=False, user=user_b,
            )
        assert exc.value.status_code == 404


# ---- 21. Owner isolation by app_tenant_id ------------------------------

@pytest.mark.asyncio
async def test_21_cross_app_tenant_cannot_read_session():
    user_a = _user("u1", "t1", app_user_id="endU", app_tenant_id="tenantA")
    user_b = _user("u1", "t1", app_user_id="endU", app_tenant_id="tenantB")
    sid = "sess-21"

    with _Harness() as h:
        await generate_image(
            body=ImageGenerationRequest(
                prompt="x", model_id=GEMINI_MODEL,
                session_id=sid, add_watermark=False,
            ),
            request=_make_request(), user=user_a,
            model_registry=_registry_stub("google"),
        )
        with pytest.raises(HTTPException) as exc:
            await get_image_session_view(
                session_id=sid, request=_make_request(),
                limit=50, cursor=None, include_urls=False, user=user_b,
            )
        assert exc.value.status_code == 404


# ---- 22. Legacy client (no X-App-* headers) still works ----------------

@pytest.mark.asyncio
async def test_22_legacy_client_without_app_headers_works():
    user = _user()  # No app_user_id / app_tenant_id
    with _Harness() as h:
        resp = await generate_image(
            body=ImageGenerationRequest(
                prompt="hello", model_id=GEMINI_MODEL,
                add_watermark=False,
            ),
            request=_make_request(), user=user,
            model_registry=_registry_stub("google"),
        )
    assert resp.success is True
    # owner_scope falls back to user_id alone
    raw_id = resp.output_artifact_id
    if raw_id is not None:
        raw_obj = h.storage._artifacts[raw_id]
        assert raw_obj.owner_scope == "u1"


# ---- 23. reference_image_url legacy path -------------------------------

@pytest.mark.asyncio
async def test_23_reference_image_url_legacy_works():
    user = _user()
    with _Harness() as h:
        with patch.object(
            images_module, "safe_fetch",
            new=AsyncMock(return_value=PNG_1X1),
        ) as fetcher:
            resp = await generate_image(
                body=ImageGenerationRequest(
                    prompt="edit it", model_id=GEMINI_MODEL,
                    reference_image_url="https://example.com/prior.png",
                    add_watermark=False,
                ),
                request=_make_request(), user=user,
                model_registry=_registry_stub("google"),
            )
            fetcher.assert_awaited_once()
    assert resp.success is True
    h.gemini.generate.assert_called_once()


# ---- 24. reference_image (base64) legacy path -------------------------

@pytest.mark.asyncio
async def test_24_reference_image_base64_legacy_works():
    user = _user()
    with _Harness() as h:
        resp = await generate_image(
            body=ImageGenerationRequest(
                prompt="edit it", model_id=GEMINI_MODEL,
                reference_image=PNG_B64,
                add_watermark=False,
            ),
            request=_make_request(), user=user,
            model_registry=_registry_stub("google"),
        )
    assert resp.success is True
    h.gemini.generate.assert_called_once()
    assert h.gemini.generate.call_args.kwargs["reference_image"] == PNG_B64


@pytest.mark.asyncio
async def test_p0_rejects_multiple_explicit_reference_sources():
    user = _user()
    with _Harness() as h:
        with pytest.raises(Exception) as exc:
            await generate_image(
                body=ImageGenerationRequest(
                    prompt="edit it",
                    model_id=GEMINI_MODEL,
                    parent_artifact_id="art_parent",
                    reference_image=PNG_B64,
                ),
                request=_make_request(),
                user=user,
                model_registry=_registry_stub("google"),
            )

    assert getattr(exc.value, "status_code", None) == 422
    assert exc.value.detail["error_code"] == "multiple_reference_sources"
    h.gemini.generate.assert_not_called()


@pytest.mark.asyncio
async def test_p0_task_status_db_fallback_uses_public_display_artifact_id():
    task_id = "task_db_cold"
    owner_scope = "u1"
    with _Harness() as h:
        raw = await h.storage.create_artifact(
            session_id="s",
            tenant_id="t1",
            user_id="u1",
            type="image",
            format="png",
            title="raw",
            filename="raw.png",
            content=PNG_1X1,
            source="image_generation",
            variant="raw",
            parent_artifact_id=None,
            owner_scope=owner_scope,
        )
        display = await h.storage.create_artifact(
            session_id="s",
            tenant_id="t1",
            user_id="u1",
            type="image",
            format="png",
            title="display",
            filename="display.png",
            content=PNG_1X1,
            source="image_generation_watermarked",
            variant="display",
            parent_artifact_id=raw.artifact_id,
            owner_scope=owner_scope,
        )
        await h.state.insert_turn(
            _POOL_SENTINEL,
            turn_id="itn_cold",
            session_id="s",
            owner_scope=owner_scope,
            task_id=task_id,
            prompt="t",
            model_id=GEMINI_MODEL,
            style="default",
            add_watermark=True,
            parent_artifact_id=None,
            output_artifact_id=raw.artifact_id,
            status="completed",
            error=None,
            error_code=None,
            client_request_id=None,
            request_hash=None,
            completed_at=datetime.now(timezone.utc),
        )
        _image_tasks.pop(task_id, None)
        status = await get_image_task_status(
            task_id=task_id,
            request=_make_request(),
            user=_user(),
        )

    assert status.output_artifact_id == raw.artifact_id
    assert status.images[0].artifact_id == display.artifact_id
    assert status.images[0].url.startswith("https://")


@pytest.mark.asyncio
async def test_p0_sync_short_wait_returns_202_when_task_still_running():
    async def slow_generate(*args, **kwargs):
        await asyncio.sleep(0.05)
        return _gemini_image_result()

    with _Harness() as h:
        h.gemini.generate = AsyncMock(side_effect=slow_generate)
        request = _make_request()
        with patch.object(images_module, "_SYNC_WAIT_SECONDS", 0.001):
            resp = await generate_image(
                body=ImageGenerationRequest(
                    prompt="slow edit",
                    model_id=GEMINI_MODEL,
                    reference_image=PNG_B64,
                    add_watermark=False,
                ),
                request=request,
                user=_user(),
                model_registry=_registry_stub("google"),
            )
        assert getattr(resp, "status_code", None) == 202
        payload = json.loads(resp.body)
        assert payload["task_id"]
        await asyncio.gather(*list(images_module._in_flight_workers), return_exceptions=True)


@pytest.mark.asyncio
async def test_p0_fetch_url_creates_ready_blob_without_data_url_response():
    with _Harness() as h:
        h.storage._backend = _FakeBlobBackend()
        with patch.object(images_module, "safe_fetch", new=AsyncMock(return_value=PNG_1X1)):
            resp = await fetch_image_blob_from_url(
                body=ImageBlobFetchUrlRequest(url="https://example.com/ref.png"),
                request=_make_request(),
                user=_user(),
            )

    assert resp.status == "ready"
    assert resp.byte_size == len(PNG_1X1)
    assert resp.content_sha256 is not None
    assert resp.storage_key.startswith("image-blobs/")


# ---- 25. Response payloads contain no base64 image data ----------------

_BASE64_IMAGE_HEADERS = ("data:image", "iVBORw", "/9j/", "R0lGOD")


def _scan_for_base64(obj, path: str = "") -> list[str]:
    """Walk a nested obj and report any string fields that look like image
    base64 (data URLs or known PNG/JPEG/GIF magic prefixes)."""
    hits: list[str] = []
    if isinstance(obj, str):
        for header in _BASE64_IMAGE_HEADERS:
            if header in obj:
                hits.append(f"{path}: matched {header!r} in str (len={len(obj)})")
    elif isinstance(obj, dict):
        for k, v in obj.items():
            hits.extend(_scan_for_base64(v, f"{path}.{k}" if path else k))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            hits.extend(_scan_for_base64(v, f"{path}[{i}]"))
    return hits


@pytest.mark.asyncio
async def test_25_no_base64_image_data_in_any_response_payload():
    user = _user()
    sid = "sess-25"
    with _Harness() as h:
        # 1. /generate-image
        gen_resp = await generate_image(
            body=ImageGenerationRequest(
                prompt="hi", model_id=GEMINI_MODEL,
                session_id=sid, add_watermark=True,
            ),
            request=_make_request(), user=user,
            model_registry=_registry_stub("google"),
        )
        gen_dump = gen_resp.model_dump()
        hits = _scan_for_base64(gen_dump, "generate-image")
        assert not hits, f"base64 image leaked: {hits}"

        # 2. /generate-image-async + 3. /image-task/{id}
        with patch.object(images_module, "get_session_manager", return_value=None):
            submit_resp = await submit_image_generation(
                body=AsyncImageGenerationRequest(
                    prompt="hi-async", model_id=GEMINI_MODEL,
                    add_watermark=False,
                ),
                request=_make_request(), user=user,
                model_registry=_registry_stub("google"),
            )
            await asyncio.gather(*list(images_module._in_flight_workers), return_exceptions=True)
            status_resp = await get_image_task_status(
                task_id=submit_resp.task_id,
                request=_make_request(), user=user,
            )
        hits2 = _scan_for_base64(submit_resp.model_dump(), "generate-image-async")
        hits3 = _scan_for_base64(status_resp.model_dump(), "image-task")
        assert not hits2 and not hits3

        # 4. /artifacts/{id}/download-url
        dl_resp = await get_artifact_download_url(
            artifact_id=gen_resp.output_artifact_id,
            request=_make_request(), variant="display",
            expires_in=600, user=user,
        )
        hits4 = _scan_for_base64(dl_resp.model_dump(), "download-url")
        assert not hits4

        # 5. /image-sessions/{id}
        sess_resp = await get_image_session_view(
            session_id=sid, request=_make_request(),
            limit=50, cursor=None, include_urls=True, user=user,
        )
        hits5 = _scan_for_base64(sess_resp.model_dump(), "image-sessions")
        assert not hits5


# ---- 26. No image bytes in logs ---------------------------------------

@pytest.mark.asyncio
async def test_26_no_image_bytes_in_logs(caplog):
    """Capture all logs across one full generation; assert no record carries
    image base64 or large binary blobs."""
    caplog.set_level(logging.DEBUG, logger="assistant_service.api.routes.images")
    caplog.set_level(logging.DEBUG, logger="ai_gateway_core")

    user = _user()
    with _Harness() as h:
        await generate_image(
            body=ImageGenerationRequest(
                prompt="log-test", model_id=GEMINI_MODEL,
                add_watermark=True,
            ),
            request=_make_request(), user=user,
            model_registry=_registry_stub("google"),
        )

    for rec in caplog.records:
        # The full formatted message
        msg = rec.getMessage()
        for hdr in _BASE64_IMAGE_HEADERS:
            assert hdr not in msg, f"log leak: {hdr!r} in {msg[:120]!r}"
        # Args may be raw bytes; check none are >8KB strings/bytes
        for arg in (rec.args or ()):
            if isinstance(arg, (str, bytes)) and len(arg) > 8 * 1024:
                # The log shouldn't carry whole-image payloads
                raise AssertionError(
                    f"log leak: arg of len {len(arg)} (>8KB) on record {rec.name}",
                )
