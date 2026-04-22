"""HMAC-signed URL helpers.

The plan calls for per-tenant pre-signed URLs. S3/OSS/R2 all have native
signers. For the local store we roll a tiny HMAC-SHA256 signer so the
API surface is identical across backends.

Secret is read from ``DOCGEN_ARTIFACT_SIGN_KEY`` (falls back to a
per-process random — rotates on restart, which is fine for local).
"""

from __future__ import annotations

import hmac
import os
import secrets
import time
from hashlib import sha256
from urllib.parse import urlencode, urlparse, urlunparse

_SECRET = os.environ.get("DOCGEN_ARTIFACT_SIGN_KEY") or secrets.token_urlsafe(32)


def _signature(message: str) -> str:
    return hmac.new(_SECRET.encode("utf-8"), message.encode("utf-8"), sha256).hexdigest()


def _normalise(url: str) -> str:
    """Round-trip through urlparse so sign + verify see identical bytes.

    Without this, ``"file:/a/b"`` and ``"file:///a/b"`` parse to the same
    ParseResult but unparse to a canonical form with three slashes —
    which breaks signature equality if the caller hands the non-canonical
    form in.
    """
    u = urlparse(url)
    return urlunparse(u._replace(query=""))


def sign_url(base_url: str, *, ttl_seconds: int, tenant_id: str) -> str:
    expires_at = int(time.time()) + ttl_seconds
    normalised = _normalise(base_url)
    payload = f"{normalised}|{tenant_id}|{expires_at}"
    sig = _signature(payload)
    u = urlparse(base_url)
    query = urlencode({"tenant": tenant_id, "exp": expires_at, "sig": sig})
    sep = "&" if u.query else ""
    return urlunparse(u._replace(query=(u.query + sep + query)))


def verify_url(url: str) -> bool:
    u = urlparse(url)
    params = dict(p.split("=", 1) for p in u.query.split("&") if "=" in p)
    tenant = params.get("tenant")
    exp = params.get("exp")
    sig = params.get("sig")
    if not (tenant and exp and sig):
        return False
    if int(exp) < int(time.time()):
        return False
    # Rebuild the base URL without our three params, then normalise.
    remaining = [p for p in u.query.split("&") if not any(p.startswith(k + "=") for k in ("tenant", "exp", "sig"))]
    base_query = "&".join(remaining)
    base_url = urlunparse(u._replace(query=base_query))
    normalised = _normalise(base_url)
    payload = f"{normalised}|{tenant}|{exp}"
    expected = _signature(payload)
    # Timing-safe compare — must be same type on both sides. Encode both
    # to bytes explicitly so we never fall back to Python's short-circuiting
    # str ``==`` which would leak timing information per character.
    try:
        return hmac.compare_digest(expected.encode("ascii"), sig.encode("ascii"))
    except UnicodeEncodeError:
        return False
