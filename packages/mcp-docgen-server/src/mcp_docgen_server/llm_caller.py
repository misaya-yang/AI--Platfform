"""LLM caller implementations for the MCP docgen server.

The planner side of docgen expects a simple ``LLMCaller`` protocol:

    async def generate_json(*, system, user, max_tokens=4000) -> dict

Without one the planner falls back to a deterministic template, which
produces a skeleton deck / doc / sheet — functionally worthless for the
user's actual request. This module gives the MCP server a real caller
so the pipeline plans with a model and we don't ship empty outlines.

We target DashScope because (a) the production gateway already has
``DASHSCOPE_API_KEY`` configured, (b) Qwen-3.6-Plus is reliable for JSON
mode, and (c) it's cheaper and faster than Gemini for planning-style
structured-output calls.

If no key is present we return ``None`` from :func:`build_default_llm`
so the pipeline keeps its deterministic fallback — no hard failure, just
a degraded output with a clear log line.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


class DashScopeLLMCaller:
    """Minimal OpenAI-compatible client targeting DashScope's chat endpoint.

    Uses the ``response_format={"type": "json_object"}`` extension which
    Qwen-3.6+ honours. Falls back to parsing plain text if the model
    returns raw JSON without the wrapper.
    """

    #: Default to Qwen-3.6-Plus which is the production-blessed model for
    #: structured output in this gateway.
    DEFAULT_MODEL = "qwen3.5-plus"
    DEFAULT_ENDPOINT = (
        "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    )

    def __init__(
        self,
        api_key: str,
        *,
        model: Optional[str] = None,
        endpoint: Optional[str] = None,
        timeout: float = 60.0,
    ) -> None:
        self._api_key = api_key
        self._model = model or os.environ.get("DOCGEN_LLM_MODEL") or self.DEFAULT_MODEL
        self._endpoint = endpoint or os.environ.get("DOCGEN_LLM_ENDPOINT") or self.DEFAULT_ENDPOINT
        self._timeout = timeout

    async def generate_json(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 4000,
    ) -> dict:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            # Low-but-not-zero temperature — pure 0 makes the planner
            # repeat near-identical layouts across sections.
            "temperature": 0.3,
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                self._endpoint,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            response.raise_for_status()
            data = response.json()

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"unexpected LLM response shape: {data}") from exc

        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("LLM returned empty content")

        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            # Structured-output mode should guarantee valid JSON, but
            # paranoia — planner treats a RuntimeError here as "LLM
            # unavailable" and falls back to deterministic, which is the
            # right behaviour.
            raise RuntimeError(f"LLM did not return valid JSON: {exc}") from exc


def build_default_llm() -> Optional[DashScopeLLMCaller]:
    """Return a caller wired to whatever credentials are in the env, or None.

    Env vars consulted:
      ``DASHSCOPE_API_KEY``  — required for DashScope
      ``DOCGEN_LLM_MODEL``   — override default model (``qwen3.5-plus``)
      ``DOCGEN_LLM_ENDPOINT``— override default endpoint (DashScope compat mode)

    When no key is present we log and return ``None`` so the deterministic
    planner kicks in — a worse output, but not a crash.
    """
    api_key = os.environ.get("DASHSCOPE_API_KEY")
    if not api_key:
        logger.warning(
            "[docgen-llm] DASHSCOPE_API_KEY not set — planner will fall back "
            "to deterministic template; output quality will be noticeably "
            "lower. Configure the key on the mcp-docgen-server container to "
            "enable LLM-backed planning."
        )
        return None
    caller = DashScopeLLMCaller(api_key)
    logger.info(
        "[docgen-llm] DashScope LLM caller wired (model=%s)", caller._model,
    )
    return caller
