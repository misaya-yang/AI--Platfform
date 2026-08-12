"""
Tests for ResponseCapMiddleware — uniform tool-result size cap.

Covers: pass-through on small results, string truncation, dict field
truncation with structure preservation, and per-tool overrides.
"""

from __future__ import annotations

import pytest
from assistant_service.core.agent.middlewares.response_cap import (
    ResponseCapMiddleware,
)
from assistant_service.core.tools.tool_registry import ToolCallResult


def _result(payload) -> ToolCallResult:
    return ToolCallResult(
        call_id="c1",
        tool_name="demo",
        success=True,
        result=payload,
    )


@pytest.mark.asyncio
async def test_small_result_passes_through_unchanged() -> None:
    mw = ResponseCapMiddleware(max_tokens=25000)
    small = _result("hello world")
    out = await mw.on_tool_result(ctx=None, tool_name="demo", arguments={}, result=small)
    # None signals passthrough — caller uses the original.
    assert out is None


@pytest.mark.asyncio
async def test_large_string_result_is_truncated_with_hint() -> None:
    mw = ResponseCapMiddleware(max_tokens=1000)  # ~4000 chars
    big = _result("x" * 20_000)
    out = await mw.on_tool_result(ctx=None, tool_name="demo", arguments={}, result=big)
    assert out is not None
    assert isinstance(out.result, str)
    # Length stays near the budget (char-per-token * token budget).
    assert len(out.result) <= 4000 + 10
    assert "truncated" in out.result
    assert "1000-token" in out.result
    assert out.metadata.get("response_cap_applied") is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("spoofed_applied", "spoofed_max_tokens"),
    [
        (False, 999_999),
        (0, "unlimited"),
        ("tool-claimed-cap", 0),
    ],
)
async def test_host_overwrites_spoofed_response_cap_metadata(
    spoofed_applied: object,
    spoofed_max_tokens: object,
) -> None:
    mw = ResponseCapMiddleware(max_tokens=1_000)
    big = _result("x" * 20_000)
    big.metadata = {
        "response_cap_applied": spoofed_applied,
        "response_cap_max_tokens": spoofed_max_tokens,
        "tool_receipt": "preserved",
    }

    out = await mw.on_tool_result(ctx=None, tool_name="demo", arguments={}, result=big)

    assert out is not None
    assert out.metadata["response_cap_applied"] is True
    assert out.metadata["response_cap_max_tokens"] == 1_000
    assert out.metadata["tool_receipt"] == "preserved"


@pytest.mark.asyncio
async def test_dict_result_preserves_structure_truncates_largest_string() -> None:
    mw = ResponseCapMiddleware(max_tokens=500)  # ~2000 chars budget
    payload = {
        "url": "https://example.com/",
        "status": 200,
        "content_type": "text/html",
        "truncated": False,
        "content": "A" * 20_000,  # the fat field
    }
    out = await mw.on_tool_result(
        ctx=None,
        tool_name="web_fetch",
        arguments={},
        result=_result(payload),
    )
    assert out is not None
    assert isinstance(out.result, dict)
    # Structural keys still there.
    assert out.result["url"] == "https://example.com/"
    assert out.result["status"] == 200
    assert out.result["content_type"] == "text/html"
    # The big field was cut down.
    assert len(out.result["content"]) < len(payload["content"])
    assert "truncated" in out.result["content"]


@pytest.mark.asyncio
async def test_per_tool_override_honored() -> None:
    # Global big (25K), per-tool override tiny (250 tokens ~= 1000 chars).
    mw = ResponseCapMiddleware(
        max_tokens=25000,
        per_tool_overrides={"fs_read": 250},
    )
    big = _result("y" * 10_000)

    # Different tool — should NOT truncate (10K < 100K char budget).
    out_other = await mw.on_tool_result(ctx=None, tool_name="other_tool", arguments={}, result=big)
    assert out_other is None  # passthrough

    # Overridden tool — should truncate.
    out_fs = await mw.on_tool_result(
        ctx=None, tool_name="fs_read", arguments={}, result=_result("y" * 10_000)
    )
    assert out_fs is not None
    assert len(out_fs.result) <= 1000 + 10
    assert out_fs.metadata.get("response_cap_max_tokens") == 250


@pytest.mark.asyncio
async def test_none_or_missing_payload_passes_through() -> None:
    mw = ResponseCapMiddleware(max_tokens=25000)
    # Error-only result (result=None)
    errlike = ToolCallResult(
        call_id="c1",
        tool_name="demo",
        success=False,
        error="boom",
        result=None,
    )
    out = await mw.on_tool_result(ctx=None, tool_name="demo", arguments={}, result=errlike)
    assert out is None
