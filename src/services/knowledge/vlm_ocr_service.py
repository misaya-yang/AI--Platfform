"""
VLM-based OCR Service for High-Accuracy Text Extraction.

Supports multiple backends:
- Gemini (default) — google-genai SDK, uses Gemini 3 Flash or 2.5 Flash
- DashScope — Qwen-VL models

Gemini 3 Flash achieves the lowest edit distance (0.115) on OmniDocBench,
making it the best choice for OCR tasks including Arabic/RTL text.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
from typing import Any

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

    Supports Gemini (default) and DashScope backends. Automatically selects
    backend based on model name, or you can specify provider explicitly.

    Optimized for bilingual Arabic+English scanned documents with RTL layout.
    """

    # Keep OCR_PROMPT as class attribute for backward compatibility with tests
    OCR_PROMPT = OCR_PROMPT

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-2.5-flash",
        provider: str = "auto",
        concurrency: int = 4,
        timeout_seconds: int = 30,
        max_retries: int = 2,
    ) -> None:
        """Initialize VLM OCR service.

        Args:
            api_key: API key for the chosen provider.
            model: Model name. Gemini models start with "gemini-", others use DashScope.
            provider: "gemini", "dashscope", or "auto" (infer from model name).
            concurrency: Max concurrent OCR requests.
            timeout_seconds: Per-request timeout.
            max_retries: Retry count on failure.
        """
        if not api_key:
            raise ValueError("API key is required for VLM OCR service")

        self.api_key = api_key
        self.model = model
        self.concurrency = max(1, concurrency)
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(1, max_retries)
        self._semaphore = asyncio.Semaphore(self.concurrency)

        # Resolve provider
        if provider == "auto":
            provider = "gemini" if model.startswith("gemini-") else "dashscope"

        self.provider = provider

        if provider == "gemini":
            self._backend = _GeminiOCRBackend(api_key=api_key, model=model)
        elif provider == "dashscope":
            self._backend = _DashScopeOCRBackend(api_key=api_key, model=model)
        else:
            raise ValueError(f"Unknown OCR provider: {provider}. Use 'gemini' or 'dashscope'.")

        logger.info(
            f"VLMOCRService initialized: provider={provider}, model={model}, concurrency={concurrency}"
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
