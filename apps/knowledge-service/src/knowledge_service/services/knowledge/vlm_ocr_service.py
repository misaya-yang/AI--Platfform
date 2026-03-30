"""
VLM-based OCR Service for High-Accuracy Text Extraction.

Supports multiple backends:
- Gemini (default) — google-genai SDK, uses Gemini 3 Flash or 2.5 Flash
- DashScope — Qwen-VL models
- SiliconFlow — DeepSeek-OCR with multi-key round-robin (free/low-cost)

Gemini 3 Flash achieves the lowest edit distance (0.115) on OmniDocBench,
making it the best choice for OCR tasks including Arabic/RTL text.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)


# Specialized OCR prompt for Arabic+English bilingual documents
OCR_PROMPT = (
    "You are a highly accurate OCR engine. Extract ALL text from this scanned document image.\n"
    "Rules:\n"
    "1. Preserve the original language (Arabic and/or English) exactly as written.\n"
    "2. Maintain right-to-left (RTL) reading order for Arabic text.\n"
    "3. Preserve paragraph breaks and logical structure.\n"
    "4. If there are tables, reproduce them in a readable text format.\n"
    "5. If there are headers or titles, mark them clearly.\n"
    "6. Do NOT translate, summarize, or interpret — only transcribe.\n"
    "7. If text is partially illegible, transcribe what you can and mark unclear parts with [?].\n"
    "Output the extracted text only, with no additional commentary."
)


def _detect_media_type(image_bytes: bytes) -> str:
    """Detect image media type from magic bytes."""
    if len(image_bytes) >= 8 and image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if len(image_bytes) >= 2 and image_bytes[:2] == b"\xff\xd8":
        return "image/jpeg"
    return "image/png"


# ============================================================================
# Gemini Backend
# ============================================================================


class _GeminiOCRBackend:
    """OCR backend using Google Gemini API (google-genai SDK)."""

    def __init__(self, api_key: str, model: str) -> None:
        from google import genai

        self._client = genai.Client(api_key=api_key)
        self._model = model

    async def call(self, image_bytes: bytes) -> str:
        from google.genai import types

        mime_type = _detect_media_type(image_bytes)
        image_part = types.Part.from_bytes(data=image_bytes, mime_type=mime_type)

        response = await asyncio.to_thread(
            self._client.models.generate_content,
            model=self._model,
            contents=[image_part, OCR_PROMPT],
        )

        if not response or not response.text:
            return ""
        return response.text.strip()


# ============================================================================
# DashScope Backend
# ============================================================================


class _DashScopeOCRBackend:
    """OCR backend using DashScope Qwen-VL API."""

    def __init__(self, api_key: str, model: str) -> None:
        from dashscope import MultiModalConversation

        self._mmc = MultiModalConversation
        self._api_key = api_key
        self._model = model

    async def call(self, image_bytes: bytes) -> str:
        media_type = _detect_media_type(image_bytes)
        b64_data = base64.b64encode(image_bytes).decode("utf-8")
        image_uri = f"data:{media_type};base64,{b64_data}"

        messages = [
            {
                "role": "user",
                "content": [
                    {"image": image_uri},
                    {"text": OCR_PROMPT},
                ],
            }
        ]

        response = await asyncio.to_thread(
            self._mmc.call,
            model=self._model,
            messages=messages,
            api_key=self._api_key,
            max_tokens=4096,
        )

        status_code = getattr(response, "status_code", None)
        if status_code and int(status_code) >= 400:
            code = getattr(response, "code", "") or ""
            message = getattr(response, "message", "") or ""
            raise RuntimeError(f"DashScope OCR API error: {status_code} {code} {message}")

        output = getattr(response, "output", None)
        if not output:
            return ""

        choices = output.get("choices", [])
        if not choices:
            return ""

        message_content = choices[0].get("message", {}).get("content", [])
        text_parts: list[str] = []
        for item in message_content:
            if isinstance(item, dict) and "text" in item:
                text_parts.append(item["text"])
            elif isinstance(item, str):
                text_parts.append(item)

        return "".join(text_parts).strip()


# ============================================================================
# SiliconFlow Backend (DeepSeek-OCR with multi-key round-robin)
# ============================================================================

_SILICONFLOW_DEFAULT_URL = "https://api.siliconflow.cn/v1/chat/completions"
_SILICONFLOW_DEFAULT_MODEL = "deepseek-ai/DeepSeek-OCR"


class _SiliconFlowOCRBackend:
    """OCR backend using SiliconFlow API (OpenAI-compatible) with multi-key rotation.

    Supports round-robin across multiple API keys with automatic rotation
    on 429 rate limits. Concurrency is 5 requests per key.
    """

    def __init__(
        self,
        api_keys: list[str],
        model: str = _SILICONFLOW_DEFAULT_MODEL,
        base_url: str = _SILICONFLOW_DEFAULT_URL,
        max_retries: int = 3,
    ) -> None:
        if not api_keys:
            raise ValueError("At least one SiliconFlow API key is required")
        self._api_keys = api_keys
        self._model = model
        self._base_url = base_url
        self._max_retries = max_retries
        self._key_index = 0
        self._lock = asyncio.Lock()
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(120.0, connect=10.0),
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=25),
        )

    async def _next_key(self) -> str:
        """Round-robin key selection."""
        async with self._lock:
            key = self._api_keys[self._key_index % len(self._api_keys)]
            self._key_index += 1
            return key

    async def call(self, image_bytes: bytes) -> str:
        mime = _detect_media_type(image_bytes)
        b64 = base64.b64encode(image_bytes).decode()

        payload = {
            "model": self._model,
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                {"type": "text", "text": OCR_PROMPT},
            ]}],
            "max_tokens": 4096,
        }

        for attempt in range(self._max_retries):
            key = await self._next_key()
            try:
                resp = await self._client.post(
                    self._base_url,
                    json=payload,
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                )
                if resp.status_code == 429:
                    delay = 1 if len(self._api_keys) > 1 else 2 ** (attempt + 1)
                    logger.warning(
                        "SiliconFlow OCR 429 on key ..%s, rotating (attempt %d/%d)",
                        key[-6:], attempt + 1, self._max_retries,
                    )
                    await asyncio.sleep(delay)
                    continue
                if resp.status_code >= 400:
                    logger.warning("SiliconFlow OCR error %d: %s", resp.status_code, resp.text[:200])
                    if attempt < self._max_retries - 1:
                        await asyncio.sleep(2 ** attempt)
                    continue
                data = resp.json()
                text = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                return text
            except Exception as exc:
                if attempt < self._max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
                else:
                    logger.error("SiliconFlow OCR failed after %d attempts: %s", self._max_retries, exc)
        return ""

    async def close(self) -> None:
        await self._client.aclose()


# ============================================================================
# Unified VLM OCR Service
# ============================================================================

# Known Gemini vision models
GEMINI_MODELS = frozenset({
    "gemini-3-flash-preview",
    "gemini-3.0-flash",
    "gemini-2.5-flash",
    "gemini-2.5-flash-preview-05-20",
    "gemini-2.0-flash",
    "gemini-2.0-flash-001",
    "gemini-1.5-flash",
    "gemini-1.5-pro",
})


class VLMOCRService:
    """High-accuracy OCR using vision language models.

    Supports Gemini, DashScope, and SiliconFlow backends. Automatically selects
    backend based on model name, or you can specify provider explicitly.

    SiliconFlow supports multi-key round-robin for higher throughput.
    Optimized for bilingual Arabic+English scanned documents with RTL layout.
    """

    # Keep OCR_PROMPT as class attribute for backward compatibility with tests
    OCR_PROMPT = OCR_PROMPT

    def __init__(
        self,
        api_key: str = "",
        model: str = "gemini-2.5-flash",
        provider: str = "auto",
        concurrency: int = 4,
        timeout_seconds: int = 30,
        max_retries: int = 2,
        api_keys: list[str] | None = None,
        base_url: str | None = None,
    ) -> None:
        """Initialize VLM OCR service.

        Args:
            api_key: API key for single-key providers (Gemini, DashScope).
            model: Model name.
            provider: "gemini", "dashscope", "siliconflow", or "auto".
            concurrency: Max concurrent OCR requests (overridden for siliconflow).
            timeout_seconds: Per-request timeout.
            max_retries: Retry count on failure.
            api_keys: Multiple API keys for round-robin (siliconflow).
            base_url: Custom API base URL override.
        """
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(1, max_retries)

        # Resolve provider
        if provider == "auto":
            if model.startswith("gemini-"):
                provider = "gemini"
            elif "deepseek" in model.lower():
                provider = "siliconflow"
            else:
                provider = "dashscope"

        self.provider = provider

        if provider == "siliconflow":
            keys = api_keys or ([api_key] if api_key else [])
            if not keys or not any(keys):
                raise ValueError("At least one API key is required for SiliconFlow OCR")
            self.concurrency = 5 * len(keys)
            self._semaphore = asyncio.Semaphore(self.concurrency)
            self._backend = _SiliconFlowOCRBackend(
                api_keys=keys,
                model=model or _SILICONFLOW_DEFAULT_MODEL,
                base_url=base_url or _SILICONFLOW_DEFAULT_URL,
                max_retries=self.max_retries,
            )
            logger.info(
                "VLMOCRService initialized: provider=siliconflow, model=%s, keys=%d, concurrency=%d",
                model, len(keys), self.concurrency,
            )
        else:
            if not api_key:
                raise ValueError("API key is required for VLM OCR service")
            self.api_key = api_key
            self.concurrency = max(1, concurrency)
            self._semaphore = asyncio.Semaphore(self.concurrency)

            if provider == "gemini":
                self._backend = _GeminiOCRBackend(api_key=api_key, model=model)
            elif provider == "dashscope":
                self._backend = _DashScopeOCRBackend(api_key=api_key, model=model)
            else:
                raise ValueError(
                    f"Unknown OCR provider: {provider}. Use 'gemini', 'dashscope', or 'siliconflow'."
                )
            logger.info(
                "VLMOCRService initialized: provider=%s, model=%s, concurrency=%d",
                provider, model, self.concurrency,
            )

    async def ocr_image(self, image_bytes: bytes) -> str:
        """Extract text from a single image using VLM.

        Args:
            image_bytes: PNG or JPEG image bytes.

        Returns:
            Extracted text, or empty string on failure.
        """
        async with self._semaphore:
            return await self._call_with_retry(image_bytes)

    async def ocr_pdf_pages(self, images: list[bytes]) -> list[str]:
        """Extract text from multiple page images concurrently.

        Args:
            images: List of page image bytes.

        Returns:
            List of extracted texts, one per page (empty string on failure).
        """
        if not images:
            return []

        tasks = [self.ocr_image(img) for img in images]
        return list(await asyncio.gather(*tasks))

    async def _call_with_retry(self, image_bytes: bytes) -> str:
        """Call OCR backend with retry logic."""
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                return await self._backend.call(image_bytes)
            except Exception as e:
                last_error = e
                if attempt < self.max_retries - 1:
                    wait = (attempt + 1) * 2
                    logger.warning(
                        f"VLM OCR attempt {attempt + 1} failed, retrying in {wait}s: {e}"
                    )
                    await asyncio.sleep(wait)

        logger.error(f"VLM OCR failed after {self.max_retries} attempts: {last_error}")
        return ""

    # Keep _detect_media_type as instance method for backward compatibility with tests
    def _detect_media_type(self, image_bytes: bytes) -> str:
        return _detect_media_type(image_bytes)
