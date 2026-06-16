"""Image API adversarial tests — focused on the hostile angles a real
attacker / chaotic production environment would hit.

These are the tests we'd write for a security-or-correctness audit, not
the happy-path coverage tests. Each one targets a specific class of bug
that a casual unit test would miss.

Categories:

  A. **Wallet-critical idempotency under DB pathology** — what if Postgres
     hiccups mid-claim? Does the route silently double-charge?
  B. **Pre-migration data smuggling** — can a stale `reference_artifact_id`
     from before the redesign feed watermarked bytes into next-turn?
  C. **Owner-scope forgery** — can a caller forge `\\x1F` in the headers
     to collide with another tenant's scope?
  D. **CAS-vs-pre-check race** — is `expected_parent_artifact_id` actually
     a guarantee, or does a generation-time race make it advisory?
  E. **Real concurrent CAS** — under asyncio.gather with two simultaneous
     submits, does exactly one win?
  F. **Failed-task replay** — can a caller retry the same client_request_id
     after the original task crashed?
  G. **URL re-sign authority leak** — if a caller forges an artifact_id
     they don't own, does the response leak existence info via timing or
     error message?
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).parent))
from ai_gateway_core.image.image_state import compute_owner_scope
from assistant_service.api.routes import images as images_module
from assistant_service.api.routes.images import (
    AsyncImageGenerationRequest,
    ImageGenerationRequest,
    generate_image,
    get_artifact_download_url,
    get_image_session_view,
    submit_image_generation,
)
from test_image_redesign import (  # noqa: E402
    GEMINI_MODEL,
    PNG_1X1,
    _Harness,
    _make_request,
    _registry_stub,
    _user,
)

# ===========================================================================
# A. Wallet-critical: Postgres hiccup during idempotency claim
# ===========================================================================
#
# Java retries 3x with same client_request_id. Mid-flight, AS's record_idempotent
# call hits a transient asyncpg error (connection reset). Pre-fix: _db_safe
# decorator caught the error → returned False → route fell through to "no
# row found, run anyway" → DOUBLE provider call, double charge. Post-fix:
# real DB errors propagate → 500 → caller retries → no double charge.
# ===========================================================================


@pytest.mark.asyncio
async def test_A1_record_idempotent_propagates_real_db_errors():
    """A real Postgres error during record_idempotent MUST propagate, not
    silently degrade to ``run anyway``. Without this guarantee,
    client_request_id is wallet-unsafe under DB flake."""
    user = _user()
    body = AsyncImageGenerationRequest(
        prompt="x", model_id=GEMINI_MODEL, client_request_id="cri-A1",
    )
    with _Harness() as h:
        # Wire a record_idempotent that raises asyncpg-ish error
        from asyncpg import exceptions as pg_exc

        def _explode(*args, **kwargs):
            raise pg_exc.ConnectionDoesNotExistError("simulated DB outage")

        with (
            patch.object(images_module, "record_idempotent", side_effect=_explode),
            patch.object(images_module, "get_session_manager", return_value=None),
            pytest.raises((HTTPException, pg_exc.ConnectionDoesNotExistError)),
        ):
            await submit_image_generation(
                body=body, request=_make_request(), user=user,
                model_registry=_registry_stub("google"),
            )

    # The provider must NOT have been called — no spawned worker.
    assert len(h.gemini.generate.mock_calls) == 0, (
        "DB outage during idempotency claim spawned worker anyway → wallet-unsafe"
    )


@pytest.mark.asyncio
async def test_A2_inconsistent_state_refuses_run_rather_than_double_spend():
    """If record_idempotent returns False but lookup_idempotent returns None,
    the state is inconsistent (impossible-by-construction in normal ops).
    The route must REFUSE rather than fall through to ``run anyway``."""
    user = _user()
    body = AsyncImageGenerationRequest(
        prompt="x", model_id=GEMINI_MODEL, client_request_id="cri-A2",
    )
    with (
        _Harness() as h,
        patch.object(images_module, "record_idempotent", AsyncMock(return_value=False)),
        patch.object(images_module, "lookup_idempotent", AsyncMock(return_value=None)),
        patch.object(images_module, "get_session_manager", return_value=None),
        pytest.raises(HTTPException) as exc,
    ):
        await submit_image_generation(
            body=body, request=_make_request(), user=user,
            model_registry=_registry_stub("google"),
        )
    assert exc.value.status_code in (500, 503), (
        f"Expected 5xx for inconsistent idempotency state, got {exc.value.status_code}"
    )
    assert len(h.gemini.generate.mock_calls) == 0, "Provider was called on inconsistent state"


# ===========================================================================
# B. Pre-migration watermarked artifact smuggle
# ===========================================================================
#
# The 001 migration's ``ALTER ADD COLUMN variant DEFAULT 'raw'`` mislabels
# pre-existing watermarked rows as variant='raw'. A caller who has an
# OLD artifact_id (from before the deploy) passes it as
# reference_artifact_id. Pre-fix: find_variant treats it as raw, downloads
# bytes (which are watermarked), feeds to Gemini → watermark bleeds into
# next-turn editing.
#
# 002_backfill_watermarked_variant.sql relabels them to 'display'. Then
# find_variant on the orphan display row returns None (no parent_artifact_id
# → returns None per artifact_storage.py logic) → 404, no smuggle.
# ===========================================================================


@pytest.mark.asyncio
async def test_B1_orphan_display_artifact_cannot_be_used_as_raw_reference():
    """A pre-migration watermarked artifact (relabeled to variant='display'
    by the backfill, but with no parent_artifact_id linkage) MUST NOT
    serve as a raw reference for the next turn — 404 instead."""
    user = _user()
    with _Harness() as h:
        # Seed an orphan display artifact (mimics post-backfill pre-mig data)
        orphan = await h.storage.create_artifact(
            session_id="legacy-sess", tenant_id="t1", user_id="u1",
            type="image", format="png", title="legacy", filename="l.png",
            content=PNG_1X1, source="image_generation_watermarked",
            variant="display", parent_artifact_id=None,  # ← the orphan smell
            owner_scope=None,  # legacy NULL owner_scope
            width=1, height=1,
        )
        # Try to use it as reference for editing
        with pytest.raises(HTTPException) as exc:
            await generate_image(
                body=ImageGenerationRequest(
                    prompt="edit this orphan", model_id=GEMINI_MODEL,
                    reference_artifact_id=orphan.artifact_id,
                    add_watermark=False,
                ),
                request=_make_request(), user=user,
                model_registry=_registry_stub("google"),
            )
        assert exc.value.status_code == 404, (
            f"Orphan display must not be smuggled as raw reference; got {exc.value.status_code}"
        )
        # And Gemini was definitely never called with watermarked bytes
        assert len(h.gemini.generate.mock_calls) == 0


@pytest.mark.asyncio
async def test_B2_cross_owner_reference_artifact_bytes_return_404():
    """Any authenticated user who guesses another user's artifact UUID must
    still be unable to load the raw bytes for edit/reference flows."""
    user = _user()
    with _Harness() as h:
        raw = await h.storage.create_artifact(
            session_id="other-sess", tenant_id="t1", user_id="other",
            type="image", format="png", title="private", filename="p.png",
            content=PNG_1X1, source="image_generation",
            variant="raw", parent_artifact_id=None,
            owner_scope="other_user",
            width=1, height=1,
        )

        with pytest.raises(HTTPException) as exc:
            await images_module._load_artifact_bytes_owner_scoped(
                h.storage,
                raw.artifact_id,
                owner_scope=user.user_id,
                user=user,
            )

    assert exc.value.status_code == 404


# ===========================================================================
# C. Owner-scope forgery via control char in app_user_id
# ===========================================================================
#
# owner_scope = JWT_subject + "\x1F" + app_tenant_id + "\x1F" + app_user_id.
# An attacker submits app_user_id="alice\x1Fadmin" to forge collision with
# (jwt=u, tenant=alice, user=admin). compute_owner_scope MUST reject; the
# header parser MUST also reject before the route ever runs.
# ===========================================================================


def test_C1_compute_owner_scope_rejects_unit_separator_in_inputs():
    """Direct test on the helper — ASCII 0x1F in app_* fields raises."""
    with pytest.raises(ValueError):
        compute_owner_scope(
            user_id="jwt_subject",
            app_tenant_id="acme\x1Fattacker",
            app_user_id="bob",
        )
    with pytest.raises(ValueError):
        compute_owner_scope(
            user_id="jwt_subject",
            app_tenant_id="acme",
            app_user_id="bob\x1Fadmin",
        )


@pytest.mark.asyncio
async def test_C2_user_context_header_parser_rejects_control_chars():
    """The HTTP header parser must reject control chars BEFORE the route
    ever sees them — defense in depth."""
    from types import SimpleNamespace

    from assistant_service.auth.user_context import get_user_context

    async def run(headers):
        req = SimpleNamespace(
            headers=headers,
            app=SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(
                app=SimpleNamespace(allow_anonymous=False)
            ))),
            client=SimpleNamespace(host="127.0.0.1"),
        )
        return await get_user_context(req)

    with pytest.raises(HTTPException) as exc:
        await run({
            "X-User-Id": "u",
            "X-Tenant-Id": "t",
            "X-App-User-Id": "alice\x1Fadmin",  # ← attacker payload
        })
    assert exc.value.status_code == 400


# ===========================================================================
# D. CAS-vs-pre-check race window — expected_parent is advisory, not strict
# ===========================================================================
#
# Caller passes expected_parent_artifact_id=X. Submit time: latest=X,
# _check_expected_parent passes. Provider runs (10s). During those 10s,
# another caller advances latest X→Y. CAS at the end fails. Caller gets
# success=True, latest_advanced=False. Charged for an invisible result.
#
# This is a documented limitation. Test verifies the contract: caller
# MUST inspect ``latest_advanced`` and not assume 200=ok.
# ===========================================================================


@pytest.mark.asyncio
async def test_D1_expected_parent_passes_at_submit_but_loses_cas_during_generation():
    """The race scenario: latest moves AFTER expected_parent check,
    BEFORE post-generation CAS. Result must signal latest_advanced=False
    so the caller knows their output is a branch."""
    sid = "race-D1"
    user = _user()
    with _Harness():
        # Turn 1 establishes latest=A
        r1 = await generate_image(
            body=ImageGenerationRequest(
                prompt="seed", model_id=GEMINI_MODEL, session_id=sid,
                add_watermark=False,
            ),
            request=_make_request(), user=user,
            model_registry=_registry_stub("google"),
        )
        a = r1.output_artifact_id

        # Simulate the race: a competing turn lands BEFORE our CAS would.
        # We do this by patching advance_latest_artifact_cas to return False
        # (CAS fail) just for our turn, while leaving the session's latest
        # untouched (mimics another writer winning).
        async def _fake_cas_lose(pool, *, session_id, expected_parent, new_artifact_id):
            # Pretend someone else won — leave latest as-is
            return False

        with patch.object(images_module, "advance_latest_artifact_cas",
                          side_effect=_fake_cas_lose):
            r2 = await generate_image(
                body=ImageGenerationRequest(
                    prompt="add hat", model_id=GEMINI_MODEL, session_id=sid,
                    expected_parent_artifact_id=a,  # passes pre-check
                    add_watermark=False,
                ),
                request=_make_request(), user=user,
                model_registry=_registry_stub("google"),
            )

        # Generation succeeded but CAS lost → latest_advanced MUST be False
        assert r2.success is True
        assert r2.latest_advanced is False, (
            "expected_parent passed pre-check but CAS lost — latest_advanced "
            "MUST be False so the caller can detect the branch outcome"
        )


# ===========================================================================
# E. Real concurrent submits via asyncio.gather
# ===========================================================================
#
# The existing test_19 in test_image_redesign uses sequential awaits.
# This one fires 5 truly concurrent coroutines and verifies exactly one
# wins the idempotency race AND the rest get the same task_id.
# ===========================================================================


@pytest.mark.asyncio
async def test_E1_concurrent_idempotent_submits_serialize_correctly():
    """5 coroutines fired via asyncio.gather all with the same
    client_request_id. Exactly one inserts into image_idempotency; the
    other four must all replay back the winner's task_id."""
    user = _user()
    body = AsyncImageGenerationRequest(
        prompt="concurrent", model_id=GEMINI_MODEL,
        client_request_id="cri-E1",
    )
    with _Harness() as h, patch.object(images_module, "get_session_manager", return_value=None):
        results = await asyncio.gather(*[
            submit_image_generation(
                body=body, request=_make_request(), user=user,
                model_registry=_registry_stub("google"),
            )
            for _ in range(5)
        ], return_exceptions=False)
        # Drain workers
        await asyncio.gather(
            *list(images_module._in_flight_workers),
            return_exceptions=True,
        )

    task_ids = {r.task_id for r in results}
    assert len(task_ids) == 1, f"5 concurrent submits → {len(task_ids)} task_ids (want 1)"
    # Exactly one image_turns row, exactly one idempotency row
    assert len(h.state.idem) == 1
    assert len(h.state.turns) == 1
    # Exactly one provider call across whichever provider was routed to
    total_calls = h.gemini.generate.call_count + h.smart.generate.call_count
    assert total_calls == 1, (
        f"5 concurrent submits → {total_calls} provider calls "
        f"(gemini={h.gemini.generate.call_count}, smart={h.smart.generate.call_count}) — "
        f"want 1, claim-before-work failed"
    )


# ===========================================================================
# F. Failed-task replay — same client_request_id after task failed
# ===========================================================================
#
# This documents the contract: client_request_id is single-use. If the
# original task FAILED, retrying with the same key returns the failed
# task_id (poll → status=failed). Caller cannot recover without a new
# client_request_id. This is intentional but worth proving so the
# behavior doesn't drift later.
# ===========================================================================


@pytest.mark.asyncio
async def test_F1_failed_task_is_not_replayed_with_new_attempt():
    """Submit fails → idempotency row points to failed task. Retry with
    same key returns the SAME failed task_id (no fresh attempt). Caller
    must mint a new client_request_id to recover."""
    user = _user()
    body = AsyncImageGenerationRequest(
        prompt="will fail", model_id=GEMINI_MODEL,
        client_request_id="cri-F1",
    )
    with _Harness() as h:
        # Make the provider fail
        from assistant_service.core.tools.gemini_image_tool import GeminiImageResult
        h.gemini.generate = AsyncMock(return_value=GeminiImageResult(
            success=False, images=[], text=None, duration_ms=1.0,
            error="simulated provider outage",
        ))
        with patch.object(images_module, "get_session_manager", return_value=None):
            r1 = await submit_image_generation(
                body=body, request=_make_request(), user=user,
                model_registry=_registry_stub("google"),
            )
            await asyncio.gather(
                *list(images_module._in_flight_workers),
                return_exceptions=True,
            )

            r2 = await submit_image_generation(
                body=body, request=_make_request(), user=user,
                model_registry=_registry_stub("google"),
            )

    assert r2.task_id == r1.task_id, (
        "Same client_request_id must return same task_id even after failure — "
        "key is single-use"
    )
    # And the provider was NOT called a second time (no fresh attempt) across
    # either possible provider seam
    total_calls = h.gemini.generate.call_count + h.smart.generate.call_count
    assert total_calls == 1, (
        f"Failed-task replay re-ran provider; total_calls={total_calls} (want 1)"
    )


# ===========================================================================
# G. URL re-sign on cross-owner artifact
# ===========================================================================
#
# Attacker authed as user A submits download-url for artifact_id owned
# by user B. MUST 404 (not 403) so the existence of B's artifact isn't
# leaked. Variant fallback must NOT bypass the owner check on any path.
# ===========================================================================


@pytest.mark.asyncio
async def test_G1_cross_owner_download_url_returns_404():
    user_a = _user(user_id="alice", tenant_id="acme",
                   app_user_id="alice", app_tenant_id="acme")
    user_b = _user(user_id="bob", tenant_id="acme",
                   app_user_id="bob", app_tenant_id="acme")
    with _Harness():
        # User A creates an image
        ra = await generate_image(
            body=ImageGenerationRequest(
                prompt="alice's secret", model_id=GEMINI_MODEL,
                add_watermark=True,
            ),
            request=_make_request(), user=user_a,
            model_registry=_registry_stub("google"),
        )
        a_artifact = ra.output_artifact_id

        for variant in ("display", "raw"):
            with pytest.raises(HTTPException) as exc:
                await get_artifact_download_url(
                    artifact_id=a_artifact, request=_make_request(),
                    user=user_b, variant=variant, expires_in=3600,
                )
            assert exc.value.status_code == 404

        with pytest.raises(HTTPException) as exc:
            await get_artifact_download_url(
                artifact_id=a_artifact, request=_make_request(),
                user=user_b, variant="thumbnail", expires_in=3600,
            )
        assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_G2_image_sessions_view_cross_user_returns_404():
    sid = "legacy-G2"
    user_a = _user(user_id="alice", tenant_id="t1")
    user_b = _user(user_id="bob", tenant_id="t1")
    with _Harness():
        # User A creates the session
        await generate_image(
            body=ImageGenerationRequest(
                prompt="seed", model_id=GEMINI_MODEL, session_id=sid,
                add_watermark=False,
            ),
            request=_make_request(), user=user_a,
            model_registry=_registry_stub("google"),
        )

        with pytest.raises(HTTPException) as exc:
            await get_image_session_view(
                session_id=sid, request=_make_request(), user=user_b,
                limit=10, cursor=None, include_urls=False,
            )
        assert exc.value.status_code == 404
