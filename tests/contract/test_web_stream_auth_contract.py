"""Regression guards for authenticated browser streaming transports."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_shared_sse_transports_attach_the_current_bearer_token() -> None:
    source = (ROOT / "web" / "src" / "lib" / "sse.ts").read_text(encoding="utf-8")

    assert 'import { getAuthToken } from "@/lib/api"' in source
    assert 'resolved.set("Authorization", `Bearer ${token}`)' in source
    assert source.count("headers: authenticatedHeaders(init.headers)") == 2, (
        "both sseFetch and sseFetchEvents must use authenticated headers"
    )


def test_agent_stream_direct_fetch_attaches_the_current_bearer_token() -> None:
    source = (ROOT / "web" / "src" / "hooks" / "useAgentStream.ts").read_text(
        encoding="utf-8"
    )

    assert 'import { getAuthToken } from "@/lib/api"' in source
    assert "const token = getAuthToken();" in source
    assert 'Authorization: `Bearer ${token}`' in source
