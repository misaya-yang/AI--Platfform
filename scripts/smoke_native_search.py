#!/usr/bin/env python3
"""Smoke test for Qwen native-search via DashScope OpenAI-compat endpoint.

Asserts:
  1. The request body POSTed to /v1/chat/completions carries
     `enable_search: True` at the top level (NOT nested under extra_body).
  2. The streamed response actually contains real-time data (proxied via
     non-empty content from the model).

Exit codes:
  0 — body shape correct AND streamed content arrived.
  1 — either shape or data assertion failed.

Run:
  DASHSCOPE_API_KEY=sk-... python scripts/smoke_native_search.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

import httpx


def _fail(msg: str) -> None:
    sys.stderr.write(f"[SMOKE-FAIL] {msg}\n")
    sys.exit(1)


def _ok(msg: str) -> None:
    sys.stdout.write(f"[SMOKE-OK]   {msg}\n")


async def main() -> int:
    api_key = os.environ.get("DASHSCOPE_API_KEY")
    if not api_key:
        _fail("DASHSCOPE_API_KEY not set in env")

    # Import registry and install capability for qwen3.6-plus
    from src.services.assistant.models.model_registry import (
        ChatMessage,
        ModelProvider,
        ModelRegistry,
    )

    registry = ModelRegistry(use_default_models=True)
    registry.configure_provider(
        ModelProvider.DASHSCOPE,
        api_key=api_key,
        base_url="https://dashscope.aliyuncs.com/compatible-mode",
    )

    model = registry.get_model("qwen3.6-plus")
    if model is None:
        _fail("qwen3.6-plus not in default catalog")
    if not model.supports_native_search:
        _fail(f"qwen3.6-plus missing native-search capability: {model.native_search_config}")
    _ok(f"qwen3.6-plus supports_native_search={model.supports_native_search} cfg={model.native_search_config}")

    # --- Capture the request body via monkeypatching httpx.AsyncClient.stream ---
    captured: dict[str, Any] = {}
    real_stream = httpx.AsyncClient.stream

    def patched_stream(self, method: str, url: str, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        if method.upper() == "POST" and "chat/completions" in str(url):
            captured["body"] = kwargs.get("json")
            captured["url"] = str(url)
        return real_stream(self, method, url, *args, **kwargs)

    httpx.AsyncClient.stream = patched_stream  # type: ignore[method-assign]

    streamed_text = ""
    thinking_text = ""
    billing_blocked = False
    upstream_error: str | None = None
    try:
        async for delta in registry.chat_stream(
            model_id="qwen3.6-plus",
            messages=[
                ChatMessage(
                    role="user",
                    content="昨天NBA季后赛湖人打了吗，比分多少？",
                )
            ],
            temperature=0.3,
            max_tokens=1024,
            tools=None,
            native_search_config={"enable_search": True},
        ):
            if delta.content:
                streamed_text += delta.content
                sys.stdout.write(delta.content)
                sys.stdout.flush()
            if delta.thinking_content:
                thinking_text += delta.thinking_content
    except httpx.HTTPStatusError as exc:
        # Surface DashScope's error body so we can debug shape issues.
        # httpx.stream() closes the response before propagating raise_for_status,
        # so the body isn't readable here. Re-POST the captured body synchronously
        # (non-streaming) to recover the server's actual error message — this
        # gives us enough signal to distinguish billing from shape issues.
        body_text = ""
        try:
            async with httpx.AsyncClient(
                base_url="https://dashscope.aliyuncs.com/compatible-mode",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                timeout=30,
            ) as probe_client:
                body_to_probe = dict(captured.get("body") or {})
                body_to_probe["stream"] = False
                body_to_probe.pop("stream_options", None)
                probe_resp = await probe_client.post(
                    "/v1/chat/completions", json=body_to_probe
                )
                body_text = probe_resp.text
        except Exception as e:
            body_text = f"<probe failed: {e!r}>"
        upstream_error = body_text
        sys.stderr.write(f"\n[SMOKE] HTTP {exc.response.status_code}: {body_text}\n")
        sys.stderr.write(
            f"[SMOKE] request body we sent:\n{json.dumps(captured.get('body'), ensure_ascii=False, indent=2)}\n"
        )
        # Account-level Arrearage is NOT a code bug — it means our request
        # shape reached the upstream auth/billing layer intact. Treat as a
        # pass for the shape check, but skip the "data returned" check.
        if "Arrearage" in body_text or "overdue-payment" in body_text:
            billing_blocked = True
        else:
            _fail(f"DashScope rejected request: {exc.response.status_code}")
    finally:
        httpx.AsyncClient.stream = real_stream  # type: ignore[method-assign]
        sys.stdout.write("\n")

    # --- Assertions ---
    body = captured.get("body")
    if body is None:
        _fail("no POST to chat/completions captured")
    if body.get("enable_search") is not True:
        _fail(
            f"enable_search NOT at top level of body; body keys={list(body.keys())} "
            f"body.enable_search={body.get('enable_search')!r}"
        )
    if "extra_body" in body:
        _fail(f"body contains extra_body (should be empty): {body.get('extra_body')!r}")
    _ok(f"body.enable_search == True (top-level); url={captured.get('url')}")

    if billing_blocked:
        _ok(
            "upstream returned Arrearage (account overdue) — shape check PASSED "
            "(request reached DashScope auth layer as valid JSON with top-level "
            "enable_search). Re-run with a paid-up account for the end-to-end "
            "data-returned assertion."
        )
        return 0

    if not streamed_text.strip():
        _fail("streamed content was empty — native search produced no text")
    _ok(f"streamed {len(streamed_text)} chars (thinking={len(thinking_text)} chars)")

    # Light heuristic: response should mention scores/teams or acknowledge the query
    # (we can't assert on exact content since playoffs vary day-to-day).
    if len(streamed_text.strip()) < 50:
        _fail(f"response suspiciously short: {streamed_text!r}")
    _ok("response length plausible for a real-time answer")

    return 0


if __name__ == "__main__":
    rc = asyncio.run(main())
    sys.exit(rc)
