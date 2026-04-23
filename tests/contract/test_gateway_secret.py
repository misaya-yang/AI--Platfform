"""Phase-5a contract tests for ``X-Gateway-Secret`` HMAC.

Locks the sign/verify contract between gateway and assistant-service:
- round-trip succeeds with matching secrets
- mismatched secret rejected
- timestamp skew rejected
- tampered signature rejected
- replay of the same request_id rejected
- missing / malformed header rejected

Reference: GATE G5a-4 in
``plans/SystemDesign-Assistant-Service-True-Extraction-Phase5-2026-04-23.md``.
"""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from ai_gateway_core.auth.gateway_secret import (
    GatewaySecret,
    InMemoryReplayStore,
    InvalidGatewaySecret,
)


SECRET = "0" * 32  # 32 hex-chars, passes the min-length guard
OTHER = "1" * 32


@pytest.fixture
def signer() -> GatewaySecret:
    return GatewaySecret(secret=SECRET, replay_store=InMemoryReplayStore())


@pytest.fixture
def verifier() -> GatewaySecret:
    return GatewaySecret(secret=SECRET, replay_store=InMemoryReplayStore())


def test_roundtrip(signer: GatewaySecret, verifier: GatewaySecret) -> None:
    header = signer.sign()
    assert verifier.verify(header) is not None


def test_request_id_preserved(signer: GatewaySecret, verifier: GatewaySecret) -> None:
    header = signer.sign(request_id="abc-123")
    assert verifier.verify(header) == "abc-123"


def test_mismatched_secret_rejected() -> None:
    signer = GatewaySecret(secret=SECRET, replay_store=InMemoryReplayStore())
    verifier = GatewaySecret(secret=OTHER, replay_store=InMemoryReplayStore())
    header = signer.sign()
    with pytest.raises(InvalidGatewaySecret, match="signature"):
        verifier.verify(header)


def test_tampered_signature_rejected(signer: GatewaySecret, verifier: GatewaySecret) -> None:
    header = signer.sign()
    parts = header.split(":")
    # Flip one hex nibble in the signature
    bad_sig = parts[3][:-1] + ("0" if parts[3][-1] != "0" else "1")
    tampered = ":".join(parts[:3] + [bad_sig])
    with pytest.raises(InvalidGatewaySecret, match="signature"):
        verifier.verify(tampered)


def test_stale_timestamp_rejected(verifier: GatewaySecret) -> None:
    # Craft a header with ts = now - 120s, well outside the ±60s window.
    old_signer = GatewaySecret(
        secret=SECRET, replay_store=InMemoryReplayStore(), max_skew_ms=60_000
    )
    now_ms = int(time.time() * 1000)
    with patch(
        "ai_gateway_core.auth.gateway_secret._epoch_ms",
        return_value=now_ms - 120_000,
    ):
        header = old_signer.sign()
    with pytest.raises(InvalidGatewaySecret, match="skew"):
        verifier.verify(header)


def test_future_timestamp_rejected(verifier: GatewaySecret) -> None:
    future_signer = GatewaySecret(
        secret=SECRET, replay_store=InMemoryReplayStore(), max_skew_ms=60_000
    )
    now_ms = int(time.time() * 1000)
    with patch(
        "ai_gateway_core.auth.gateway_secret._epoch_ms",
        return_value=now_ms + 120_000,
    ):
        header = future_signer.sign()
    with pytest.raises(InvalidGatewaySecret, match="skew"):
        verifier.verify(header)


def test_missing_header_rejected(verifier: GatewaySecret) -> None:
    with pytest.raises(InvalidGatewaySecret, match="missing"):
        verifier.verify(None)
    with pytest.raises(InvalidGatewaySecret, match="missing"):
        verifier.verify("")


def test_malformed_header_rejected(verifier: GatewaySecret) -> None:
    with pytest.raises(InvalidGatewaySecret, match="malformed"):
        verifier.verify("not-a-valid-header")
    with pytest.raises(InvalidGatewaySecret, match="malformed"):
        verifier.verify("v2:rid:1234:abc")  # wrong version
    with pytest.raises(InvalidGatewaySecret, match="malformed"):
        verifier.verify("v1:only:three")  # missing segment


def test_replay_rejected(signer: GatewaySecret) -> None:
    shared_store = InMemoryReplayStore()
    verifier = GatewaySecret(secret=SECRET, replay_store=shared_store)
    header = signer.sign(request_id="replay-once")
    assert verifier.verify(header) == "replay-once"
    with pytest.raises(InvalidGatewaySecret, match="replay"):
        verifier.verify(header)


def test_replay_store_isolation_across_ids(signer: GatewaySecret) -> None:
    shared_store = InMemoryReplayStore()
    verifier = GatewaySecret(secret=SECRET, replay_store=shared_store)
    assert verifier.verify(signer.sign(request_id="one")) == "one"
    assert verifier.verify(signer.sign(request_id="two")) == "two"


def test_secret_length_guard() -> None:
    with pytest.raises(ValueError, match="16 chars"):
        GatewaySecret(secret="short")
    with pytest.raises(ValueError, match="16 chars"):
        GatewaySecret(secret="")


def test_forged_header_does_not_poison_replay_store() -> None:
    """A signature-invalid header must not record its request_id.

    Otherwise an attacker could pre-burn legitimate request_ids by
    submitting forged headers with the same rid — causing legit
    requests from the real gateway to be rejected as replays.
    """
    shared_store = InMemoryReplayStore()
    verifier = GatewaySecret(secret=SECRET, replay_store=shared_store)

    # Forge a header with a valid ts but a garbage signature
    ts = str(int(time.time() * 1000))
    forged = f"v1:hot-rid:{ts}:{'f' * 64}"
    with pytest.raises(InvalidGatewaySecret):
        verifier.verify(forged)

    # Legit request with the same rid must still succeed
    legit_signer = GatewaySecret(secret=SECRET, replay_store=InMemoryReplayStore())
    legit_header = legit_signer.sign(request_id="hot-rid")
    assert verifier.verify(legit_header) == "hot-rid"
