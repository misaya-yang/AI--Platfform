from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx


# =============================================================================
# Sensitive Data Filtering for Logging
# =============================================================================
class SensitiveDataFilter(logging.Filter):
    """Filter that redacts sensitive information like API keys from log records."""

    # Patterns to redact
    _PATTERNS = [
        (re.compile(r"Bearer\s+[a-zA-Z0-9_-]+"), "Bearer ***"),
        (re.compile(r"api_key[=:]\s*[a-zA-Z0-9_-]+"), "api_key=***"),
        (re.compile(r"key[=:]\s*[a-zA-Z0-9_-]+"), "key=***"),
    ]

    def filter(self, record: logging.LogRecord) -> bool:
        """Redact sensitive data from log message."""
        if isinstance(record.msg, str):
            record.msg = self._redact(record.msg)
        if record.args:
            record.args = tuple(self._redact(str(arg)) for arg in record.args)
        return True

    def _redact(self, text: str) -> str:
        """Apply all redaction patterns."""
        for pattern, replacement in self._PATTERNS:
            text = pattern.sub(replacement, text)
        return text


# Apply sensitive data filter to embedding logger
logger = logging.getLogger(__name__)
logger.addFilter(SensitiveDataFilter())


# =============================================================================
# Multimodal Embedding Model Registry
# =============================================================================
# Centralized list of embedding models that support multimodal (image) content.
# Used by assistant API to identify multimodal knowledge bases.

MULTIMODAL_EMBEDDING_MODELS: frozenset[str] = frozenset(
    {
        # DashScope multimodal models
        "multimodal-embedding-v1",
        "multimodal-embedding-one-peace-v1",
        "multimodal-embedding-one-peace",
        # Tongyi unified vision models
        "tongyi-embedding-vision-plus",
        # Qwen VL models
        "qwen2.5-vl-embedding",
    }
)


def is_multimodal_embedding_model(model_name: str) -> bool:
    """Check if a model supports multimodal embedding.

    Args:
        model_name: The embedding model name to check

    Returns:
        True if the model supports multimodal (image) embedding
    """
    return model_name in MULTIMODAL_EMBEDDING_MODELS


class EmbeddingError(RuntimeError):
    pass


@dataclass(frozen=True)
class EmbeddingConfig:
    provider: str
    model: str
    api_key: str | None = None
    base_url: str | None = None
    timeout_seconds: float = 30.0
    extra: dict[str, Any] = None

    def __post_init__(self):
        object.__setattr__(self, "extra", self.extra or {})


class BaseEmbedding(ABC):
    """Unified embedding interface (text-first, multimodal-ready)."""

    def __init__(
        self,
        provider: str,
        model: str,
        dimension: int | None = None,
    ):
        self.provider = provider
        self.model = model
        self._dimension: int | None = dimension

    @property
    def dimension(self) -> int:
        if self._dimension is None:
            raise EmbeddingError(
                "Embedding dimension is unknown. Provide it in config or call embed_texts() once."
            )
        return self._dimension

    @property
    def supports_multimodal(self) -> bool:
        return False

    async def close(self) -> None:
        return None

    async def embed_query(self, query: str) -> list[float]:
        # Check cache first
        cached = get_cached_query_embedding(self.provider, self.model, query)
        if cached is not None:
            return cached
        # Compute embedding
        vectors = await self.embed_texts([query], text_type="query")
        result = vectors[0]
        # Cache the result
        set_cached_query_embedding(self.provider, self.model, query, result)
        return result

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return await self.embed_texts(list(texts), text_type="document")

    @abstractmethod
    async def embed_texts(
        self, texts: list[str], text_type: str | None = None
    ) -> list[list[float]]:
        raise NotImplementedError

    async def embed_images(self, images: list[bytes]) -> list[list[float]]:  # pragma: no cover
        raise EmbeddingError(f"{self.provider}:{self.model} does not support image embedding")


class GeminiEmbedding(BaseEmbedding):
    """
    Google Gemini Embedding API adapter.

    Features:
    - Task type optimization (RETRIEVAL_DOCUMENT / RETRIEVAL_QUERY)
    - Matryoshka variable dimensions (768/1024/1536/3072)
    - Batch embedding support
    - Connection pooling via httpx

    API Reference: https://ai.google.dev/gemini-api/docs/embeddings

    Usage:
        embedder = GeminiEmbedding(api_key="your-key", dimension=1024)

        # For indexing documents
        doc_vectors = await embedder.embed_texts(texts, text_type="document")

        # For search queries
        query_vector = await embedder.embed_query("search query")
    """

    MODEL_DIMENSIONS: dict[str, int] = {
        "gemini-embedding-001": 768,  # Default, but configurable up to 3072
        "text-embedding-004": 768,  # Vertex AI model
    }

    # Task types for retrieval optimization
    TASK_TYPES: dict[str, str] = {
        "query": "RETRIEVAL_QUERY",
        "document": "RETRIEVAL_DOCUMENT",
        "similarity": "SEMANTIC_SIMILARITY",
        "classification": "CLASSIFICATION",
        "clustering": "CLUSTERING",
    }

    # Gemini API endpoint
    GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models"

    # API limits
    MAX_BATCH_SIZE = 100  # Gemini supports up to 100 texts per batch
    MAX_TOKENS_PER_TEXT = 2048
    MAX_RETRIES = 3
    RETRY_BASE_DELAY = 1.0

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-embedding-001",
        dimension: int = 1024,
        base_url: str | None = None,
        timeout_seconds: float = 30.0,
    ):
        """
        Initialize Gemini Embedding.

        Args:
            api_key: Google AI API key
            model: Model name (default: gemini-embedding-001)
            dimension: Output dimension (768, 1024, 1536, or 3072)
            base_url: Optional API base URL override
            timeout_seconds: Request timeout
        """
        super().__init__(provider="gemini", model=model, dimension=dimension)
        if not api_key:
            raise EmbeddingError("Gemini API key is required")

        self.api_key = api_key
        self.base_url = base_url or self.GEMINI_API_URL
        self.output_dimension = dimension
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds, connect=5.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()

    async def embed_query(self, query: str) -> list[float]:
        """Embed a query using RETRIEVAL_QUERY task type."""
        # Check cache first
        cached = get_cached_query_embedding(self.provider, self.model, query)
        if cached is not None:
            return cached

        vectors = await self.embed_texts([query], text_type="query")
        result = vectors[0]

        # Cache the result
        set_cached_query_embedding(self.provider, self.model, query, result)
        return result

    async def embed_texts(
        self,
        texts: list[str],
        text_type: str | None = None,
    ) -> list[list[float]]:
        """
        Embed texts using Gemini API.

        Args:
            texts: List of text strings to embed
            text_type: "query" for RETRIEVAL_QUERY, "document" for RETRIEVAL_DOCUMENT

        Returns:
            List of embedding vectors
        """
        if not texts:
            return []

        # Determine task type
        task_type = self.TASK_TYPES.get(text_type or "document", "RETRIEVAL_DOCUMENT")

        # Process in batches
        all_vectors: list[list[float]] = []

        for i in range(0, len(texts), self.MAX_BATCH_SIZE):
            batch = texts[i : i + self.MAX_BATCH_SIZE]
            batch_info = f"batch {i // self.MAX_BATCH_SIZE + 1}"

            vectors = await self._embed_batch_with_retry(batch, task_type, batch_info)
            all_vectors.extend(vectors)

        return all_vectors

    async def _embed_batch_with_retry(
        self,
        texts: list[str],
        task_type: str,
        batch_info: str,
    ) -> list[list[float]]:
        """Embed a batch of texts with retry logic."""
        import logging
        import re

        logger = logging.getLogger(__name__)

        last_error: Exception | None = None

        for attempt in range(self.MAX_RETRIES):
            try:
                return await self._embed_batch(texts, task_type)

            except EmbeddingError as e:
                if "429" in str(e) or "500" in str(e) or "503" in str(e):
                    last_error = e
                    logger.warning(
                        f"Gemini embedding retryable error ({batch_info}) "
                        f"attempt {attempt + 1}/{self.MAX_RETRIES}: {e}"
                    )
                else:
                    raise

            except Exception as exc:
                last_error = EmbeddingError(f"Gemini embedding error ({batch_info}): {exc}")
                logger.warning(
                    f"Gemini embedding error ({batch_info}) "
                    f"attempt {attempt + 1}/{self.MAX_RETRIES}: {exc}"
                )

            # Exponential backoff
            if attempt < self.MAX_RETRIES - 1:
                import random

                delay = self.RETRY_BASE_DELAY * (2**attempt)

                # Respect server-provided retry delay if present (e.g., 429 quota errors)
                retry_match = re.search(r'"retryDelay"\s*:\s*"(\d+)s"', str(last_error or ""))
                if retry_match:
                    delay = max(delay, float(retry_match.group(1)))

                delay = delay + random.uniform(0.0, min(0.3, delay))
                await asyncio.sleep(delay)

        raise last_error or EmbeddingError(
            f"Gemini embedding failed after {self.MAX_RETRIES} attempts ({batch_info})"
        )

    async def _embed_batch(
        self,
        texts: list[str],
        task_type: str,
    ) -> list[list[float]]:
        """Call Gemini embedContent API for a batch of texts."""
        # Build request for batch embedding
        # Gemini API: POST /v1beta/models/{model}:batchEmbedContents
        url = f"{self.base_url}/{self.model}:batchEmbedContents"

        # Build requests array
        requests = []
        for text in texts:
            req = {
                "model": f"models/{self.model}",
                "content": {"parts": [{"text": text}]},
                "taskType": task_type,
            }
            if self.output_dimension:
                req["outputDimensionality"] = self.output_dimension
            requests.append(req)

        payload = {"requests": requests}

        response = await self._client.post(
            url,
            json=payload,
            params={"key": self.api_key},
            headers={"Content-Type": "application/json"},
        )

        if response.status_code >= 400:
            raise EmbeddingError(f"Gemini API error: {response.status_code} - {response.text}")

        data = response.json()

        # Parse response
        embeddings = data.get("embeddings", [])
        if not embeddings:
            raise EmbeddingError("Gemini API returned no embeddings")

        vectors: list[list[float]] = []
        for emb in embeddings:
            values = emb.get("values", [])
            if not values:
                raise EmbeddingError("Gemini embedding missing values")
            vectors.append(values)

        if self._dimension is None and vectors:
            self._dimension = len(vectors[0])

        return vectors


class DashScopeEmbedding(BaseEmbedding):
    """DashScope text embeddings adapter.

    Uses the official `dashscope` SDK when installed, and runs calls in a thread
    to avoid blocking the event loop.

    Features:
    - Retry with exponential backoff (3 attempts)
    - Configurable HTTP timeout
    - Graceful degradation on API errors

    API Limits:
    - Max 25 texts per batch
    - Max 2048 tokens per text for v1/v2, 8192 for v3/v4
    - Max ~6000 characters per text (safe estimate)
    """

    MODEL_DIMENSIONS: dict[str, int] = {
        "text-embedding-v1": 1536,
        "text-embedding-v2": 1536,
        "text-embedding-v3": 1024,
        "text-embedding-v4": 1024,  # DashScope v4 default dimension
    }

    # Max characters per text (conservative: ~2.5 chars/token)
    # DashScope v1/v2: max 2048 tokens, v3: max 8192 tokens
    # But in practice, shorter is safer to avoid InvalidParameter errors
    MODEL_MAX_CHARS: dict[str, int] = {
        "text-embedding-v1": 4000,
        "text-embedding-v2": 4000,
        "text-embedding-v3": 8000,  # More conservative for v3
        "text-embedding-v4": 8000,
    }

    # DashScope API limit: max 10 texts per batch for v3/v4 (observed 400 when >10)
    # Use 10 for safety across models to avoid InvalidParameter errors.

    # Retry configuration
    MAX_RETRIES = 5
    RETRY_BASE_DELAY = 0.5  # seconds (reduced from 1.0)
    REQUEST_TIMEOUT = 60  # seconds for HTTP request
    MAX_BATCH_SIZE = 10  # DashScope safe batch limit

    def __init__(
        self,
        model: str,
        api_key: str,
        dimension: int | None = None,
        base_url: str | None = None,
    ):
        # Try to lookup dimension, fallback to 1024 if model not recognized
        dim = (
            dimension
            or self.MODEL_DIMENSIONS.get(model)
            or self.MODEL_DIMENSIONS.get(model.lower())
            or 1024
        )
        super().__init__(provider="dashscope", model=model, dimension=dim)
        if not api_key:
            raise EmbeddingError("DashScope api_key is required")
        self.api_key = api_key
        self.base_url = base_url
        self.max_chars = (
            self.MODEL_MAX_CHARS.get(model) or self.MODEL_MAX_CHARS.get(model.lower()) or 6000
        )
        try:
            import dashscope
            from dashscope import TextEmbedding  # type: ignore

            self._TextEmbedding = TextEmbedding
            # Set base URL if provided (careful: this is global)
            if base_url:
                dashscope.base_http_api_url = base_url
        except Exception as exc:  # pragma: no cover
            raise EmbeddingError(
                "dashscope package is required for DashScopeEmbedding (pip install dashscope)"
            ) from exc

    def _truncate_text(self, text: str) -> str:
        """Truncate text to max allowed characters."""
        if not text:
            return ""
        text = text.strip()
        if len(text) > self.max_chars:
            # Truncate at word boundary if possible
            truncated = text[: self.max_chars]
            last_space = truncated.rfind(" ")
            if last_space > self.max_chars * 0.8:  # Keep at least 80% of content
                truncated = truncated[:last_space]
            return truncated
        return text

    def _sanitize_text(self, text: str) -> str:
        """Clean text to avoid API errors."""
        if not text:
            return "empty"  # DashScope doesn't accept empty strings

        # Remove NULL bytes and control characters
        text = text.replace("\x00", "")
        # Remove other control characters except newline/tab
        text = "".join(c if c.isprintable() or c in "\n\t" else " " for c in text)

        # Normalize line endings
        text = text.replace("\r\n", "\n").replace("\r", "\n")

        # Collapse multiple newlines and spaces
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"[ \t]{2,}", " ", text)
        text = re.sub(r"\n[ \t]+", "\n", text)  # Remove leading spaces on lines

        # Remove very long sequences of repeated characters (often from PDF extraction errors)
        text = re.sub(r"(.)\1{20,}", r"\1\1\1", text)

        text = text.strip()
        return text if text else "empty"

    async def _call_with_retry(
        self, batch: list[str], batch_info: str, **kwargs: Any
    ) -> list[list[float]]:
        """Call DashScope API with retry and exponential backoff."""
        import logging

        logger = logging.getLogger(__name__)

        last_error: Exception | None = None

        for attempt in range(self.MAX_RETRIES):
            try:
                # Use asyncio.wait_for to enforce timeout on the thread call
                resp = await asyncio.wait_for(
                    asyncio.to_thread(
                        self._TextEmbedding.call,
                        model=self.model,
                        input=batch,
                        api_key=self.api_key,
                        **kwargs,
                    ),
                    timeout=float(self.REQUEST_TIMEOUT),
                )

                status_code = int(getattr(resp, "status_code", 0) or 0)
                if status_code and status_code >= 400:
                    code = getattr(resp, "code", "") or ""
                    message = getattr(resp, "message", "") or ""
                    # Check if retryable (rate limit or server error)
                    if status_code in (429, 500, 502, 503, 504):
                        raise EmbeddingError(
                            f"DashScope retryable error ({batch_info}): {status_code} {code} {message}"
                        )
                    # Non-retryable error
                    raise EmbeddingError(
                        f"DashScope embeddings failed ({batch_info}): {status_code} {code} {message}"
                    )

                output = getattr(resp, "output", None)
                vectors = _parse_dashscope_embeddings(output)
                return vectors

            except asyncio.TimeoutError:
                last_error = EmbeddingError(
                    f"DashScope embedding timeout ({batch_info}): exceeded {self.REQUEST_TIMEOUT}s"
                )
                logger.warning(
                    f"DashScope embedding timeout on attempt {attempt + 1}/{self.MAX_RETRIES} "
                    f"({batch_info}), retrying..."
                )
            except EmbeddingError as e:
                if "retryable" in str(e).lower() or "timeout" in str(e).lower():
                    last_error = e
                    logger.warning(
                        f"DashScope embedding error on attempt {attempt + 1}/{self.MAX_RETRIES} "
                        f"({batch_info}): {e}, retrying..."
                    )
                else:
                    # Non-retryable error, raise immediately
                    raise
            except Exception as exc:
                last_error = EmbeddingError(f"DashScope embedding error ({batch_info}): {exc}")
                logger.warning(
                    f"DashScope unexpected error on attempt {attempt + 1}/{self.MAX_RETRIES} "
                    f"({batch_info}): {exc}, retrying..."
                )

            # Exponential backoff before retry
            if attempt < self.MAX_RETRIES - 1:
                delay = self.RETRY_BASE_DELAY * (2**attempt)
                await asyncio.sleep(delay)

        # All retries exhausted
        raise last_error or EmbeddingError(
            f"DashScope embedding failed after {self.MAX_RETRIES} attempts ({batch_info})"
        )

    async def embed_texts(
        self, texts: list[str], text_type: str | None = None
    ) -> list[list[float]]:
        if not texts:
            return []

        # Sanitize and truncate all texts
        processed_texts = [self._truncate_text(self._sanitize_text(t)) for t in texts]

        kwargs: dict[str, Any] = {}
        if text_type in {"query", "document"}:
            kwargs["text_type"] = text_type

        # Process in batches of MAX_BATCH_SIZE
        all_vectors: list[list[float]] = []
        for i in range(0, len(processed_texts), self.MAX_BATCH_SIZE):
            batch = processed_texts[i : i + self.MAX_BATCH_SIZE]
            batch_info = f"batch {i // self.MAX_BATCH_SIZE + 1}, texts {i}-{i + len(batch) - 1}"

            try:
                vectors = await self._call_with_retry(batch, batch_info, **kwargs)
                all_vectors.extend(vectors)
            except EmbeddingError as e:
                # On timeout, fall back to smaller batches to reduce request cost.
                if "timeout" in str(e).lower() and len(batch) > 1:
                    for j, text in enumerate(batch):
                        single_info = f"{batch_info} (fallback {j + 1}/{len(batch)})"
                        vectors = await self._call_with_retry([text], single_info, **kwargs)
                        all_vectors.extend(vectors)
                else:
                    raise

        if self._dimension is None and all_vectors:
            self._dimension = len(all_vectors[0])
        return all_vectors


class DashScopeMultimodalEmbedding(BaseEmbedding):
    """DashScope multimodal embeddings adapter for images.

    Uses the official `dashscope` SDK for multimodal embedding.
    Supports image embedding via DashScope's multimodal-embedding API.

    Models:
    - multimodal-embedding-v1: 1024 dimensions
    - tongyi-embedding-vision-plus: 1024 dimensions (recommended)

    API Limits:
    - Max image size: 3MB (base64 encoded)
    - Supported formats: JPEG, PNG, GIF, BMP, WebP
    """

    MODEL_DIMENSIONS: dict[str, int] = {
        "multimodal-embedding-v1": 1024,
        "multimodal-embedding-one-peace": 1536,
        "tongyi-embedding-vision-plus": 1024,  # Unified to 1024
        "qwen2.5-vl-embedding": 1024,  # Latest Qwen VL embedding model
    }

    # Max 3MB for DashScope multimodal API
    MAX_IMAGE_SIZE_BYTES = 3 * 1024 * 1024

    # Supported image MIME types
    SUPPORTED_MEDIA_TYPES = {
        "image/jpeg",
        "image/jpg",
        "image/png",
        "image/gif",
        "image/bmp",
        "image/webp",
    }

    def __init__(
        self,
        model: str = "multimodal-embedding-v1",
        api_key: str = "",
        dimension: int | None = None,
        base_url: str | None = None,
    ):
        dim = dimension or self.MODEL_DIMENSIONS.get(model) or 1024
        super().__init__(provider="dashscope_multimodal", model=model, dimension=dim)
        if not api_key:
            raise EmbeddingError("DashScope api_key is required for multimodal embedding")
        self.api_key = api_key
        self.base_url = base_url

        try:
            import dashscope
            from dashscope import MultiModalEmbedding  # type: ignore

            self._MultiModalEmbedding = MultiModalEmbedding
            if base_url:
                dashscope.base_http_api_url = base_url
        except ImportError as exc:
            raise EmbeddingError(
                "dashscope package is required for DashScopeMultimodalEmbedding "
                "(pip install dashscope>=1.24.6)"
            ) from exc

    @property
    def supports_multimodal(self) -> bool:
        return True

    def _image_to_base64_data_uri(self, image_bytes: bytes, media_type: str = "image/png") -> str:
        """Convert image bytes to base64 data URI format."""
        import base64

        b64_data = base64.b64encode(image_bytes).decode("utf-8")
        return f"data:{media_type};base64,{b64_data}"

    def _detect_media_type(self, image_bytes: bytes) -> str:
        """Detect image media type from magic bytes."""
        if len(image_bytes) < 8:
            return "image/png"  # Default

        # Check magic bytes
        if image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
            return "image/png"
        elif image_bytes[:2] == b"\xff\xd8":
            return "image/jpeg"
        elif image_bytes[:6] in (b"GIF87a", b"GIF89a"):
            return "image/gif"
        elif image_bytes[:2] == b"BM":
            return "image/bmp"
        elif image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
            return "image/webp"
        else:
            return "image/png"  # Default fallback

    async def embed_texts(
        self, texts: list[str], text_type: str | None = None
    ) -> list[list[float]]:
        """Embed text using multimodal model.

        Note: While this model supports text, it's primarily designed for images.
        For text-only embedding, consider using DashScopeEmbedding instead.
        """
        if not texts:
            return []

        all_vectors: list[list[float]] = []

        for text in texts:
            try:
                # DashScope multimodal API accepts text input as well
                resp = await asyncio.to_thread(
                    self._MultiModalEmbedding.call,
                    model=self.model,
                    input=[{"text": text}],
                    api_key=self.api_key,
                )

                status_code = int(getattr(resp, "status_code", 0) or 0)
                if status_code and status_code >= 400:
                    code = getattr(resp, "code", "") or ""
                    message = getattr(resp, "message", "") or ""
                    raise EmbeddingError(
                        f"DashScope multimodal text embedding failed: {status_code} {code} {message}"
                    )

                output = getattr(resp, "output", None)
                vectors = self._parse_multimodal_output(output)
                all_vectors.extend(vectors)

            except EmbeddingError:
                raise
            except Exception as exc:
                raise EmbeddingError(f"DashScope multimodal text embedding error: {exc}") from exc

        if self._dimension is None and all_vectors:
            self._dimension = len(all_vectors[0])

        return all_vectors

    async def embed_images(
        self,
        images: list[bytes],
        max_concurrent: int = 5,
    ) -> list[list[float]]:
        """Embed images using DashScope multimodal embedding API.

        Supports concurrent processing with configurable parallelism.

        Args:
            images: List of image bytes (JPEG, PNG, GIF, BMP, WebP)
            max_concurrent: Maximum concurrent API calls (default: 5)

        Returns:
            List of embedding vectors (1024 dimensions each)

        Raises:
            EmbeddingError: If image is too large or API call fails
        """
        if not images:
            return []

        # Validate all image sizes first
        for i, image_bytes in enumerate(images):
            if len(image_bytes) > self.MAX_IMAGE_SIZE_BYTES:
                raise EmbeddingError(
                    f"Image {i} exceeds max size: {len(image_bytes)} bytes > {self.MAX_IMAGE_SIZE_BYTES} bytes (3MB)"
                )

        # Use semaphore for concurrent rate limiting
        semaphore = asyncio.Semaphore(max_concurrent)

        async def embed_single_image(idx: int, image_bytes: bytes) -> list[float]:
            """Embed a single image with semaphore-based rate limiting."""
            async with semaphore:
                try:
                    # Detect media type and convert to data URI
                    media_type = self._detect_media_type(image_bytes)
                    data_uri = self._image_to_base64_data_uri(image_bytes, media_type)

                    # Call DashScope multimodal embedding API
                    resp = await asyncio.to_thread(
                        self._MultiModalEmbedding.call,
                        model=self.model,
                        input=[{"image": data_uri}],
                        api_key=self.api_key,
                    )

                    status_code = int(getattr(resp, "status_code", 0) or 0)
                    if status_code and status_code >= 400:
                        code = getattr(resp, "code", "") or ""
                        message = getattr(resp, "message", "") or ""
                        raise EmbeddingError(
                            f"DashScope multimodal image embedding failed for image {idx}: "
                            f"{status_code} {code} {message}"
                        )

                    output = getattr(resp, "output", None)
                    vectors = self._parse_multimodal_output(output)
                    if not vectors:
                        raise EmbeddingError(f"No embedding returned for image {idx}")
                    return vectors[0]

                except EmbeddingError:
                    raise
                except Exception as exc:
                    raise EmbeddingError(
                        f"DashScope multimodal image embedding error for image {idx}: {exc}"
                    ) from exc

        # Launch all embedding tasks concurrently (limited by semaphore)
        tasks = [embed_single_image(i, img) for i, img in enumerate(images)]
        all_vectors = await asyncio.gather(*tasks, return_exceptions=True)

        # Preserve successful embeddings; log and skip failures so one bad
        # image does not discard every other completed embedding in the batch.
        results: list[list[float]] = []
        for i, vec in enumerate(all_vectors):
            if isinstance(vec, BaseException):
                logger.warning("Image embedding failed for image %d: %s", i, vec)
                continue
            results.append(vec)

        if self._dimension is None and results:
            self._dimension = len(results[0])

        return results

    async def embed_image_and_text(
        self, image_bytes: bytes, text: str | None = None
    ) -> list[float]:
        """Embed an image with optional text context.

        This combines image and text into a single multimodal embedding,
        which can improve retrieval when images have associated captions.

        Args:
            image_bytes: Image content
            text: Optional text description or caption

        Returns:
            Single embedding vector combining image and text
        """
        if len(image_bytes) > self.MAX_IMAGE_SIZE_BYTES:
            raise EmbeddingError(
                f"Image exceeds max size: {len(image_bytes)} bytes > {self.MAX_IMAGE_SIZE_BYTES} bytes"
            )

        try:
            media_type = self._detect_media_type(image_bytes)
            data_uri = self._image_to_base64_data_uri(image_bytes, media_type)

            # Build input with image and optional text
            input_items: list[dict[str, str]] = [{"image": data_uri}]
            if text:
                input_items.append({"text": text})

            resp = await asyncio.to_thread(
                self._MultiModalEmbedding.call,
                model=self.model,
                input=input_items,
                api_key=self.api_key,
            )

            status_code = int(getattr(resp, "status_code", 0) or 0)
            if status_code and status_code >= 400:
                code = getattr(resp, "code", "") or ""
                message = getattr(resp, "message", "") or ""
                raise EmbeddingError(
                    f"DashScope multimodal embedding failed: {status_code} {code} {message}"
                )

            output = getattr(resp, "output", None)
            vectors = self._parse_multimodal_output(output)

            if not vectors:
                raise EmbeddingError("No embedding returned from multimodal API")

            return vectors[0]

        except EmbeddingError:
            raise
        except Exception as exc:
            raise EmbeddingError(f"DashScope multimodal embedding error: {exc}") from exc

    def _parse_multimodal_output(self, output: Any) -> list[list[float]]:
        """Parse DashScope multimodal embedding output."""
        if output is None:
            raise EmbeddingError("DashScope multimodal response missing output")

        # Response format: {"embeddings": [{"embedding": [...], "type": "image|text"}]}
        if isinstance(output, dict):
            embeddings_list = output.get("embeddings")
            if embeddings_list is None:
                raise EmbeddingError(
                    f"Unexpected DashScope multimodal output keys: {list(output.keys())}"
                )
            output = embeddings_list

        if not isinstance(output, list):
            raise EmbeddingError(f"Unexpected DashScope multimodal output type: {type(output)}")

        vectors: list[list[float]] = []
        for entry in output:
            if isinstance(entry, dict):
                vec = entry.get("embedding") or entry.get("vector")
                if isinstance(vec, list):
                    vectors.append(vec)
            elif isinstance(entry, list):
                vectors.append(entry)

        if not vectors:
            raise EmbeddingError("DashScope multimodal response has no embeddings")

        return vectors


class LocalHashEmbedding(BaseEmbedding):
    """Lightweight local embedding via feature hashing (no external dependencies)."""

    DEFAULT_DIMENSION = 384

    def __init__(self, model: str, dimension: int | None = None):
        dim = dimension or _infer_local_dimension(model) or self.DEFAULT_DIMENSION
        super().__init__(provider="local", model=model, dimension=dim)

    async def embed_texts(
        self, texts: list[str], text_type: str | None = None
    ) -> list[list[float]]:
        if not texts:
            return []

        vectors: list[list[float]] = []
        dim = self.dimension
        token_re = re.compile(r"[A-Za-z0-9\u4e00-\u9fff]+")

        for text in texts:
            vec = [0.0] * dim
            for token in token_re.findall((text or "").lower()):
                digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
                idx = int.from_bytes(digest[:4], "little") % dim
                sign = 1.0 if (digest[4] & 1) == 0 else -1.0
                vec[idx] += sign

            norm = math.sqrt(sum(v * v for v in vec))
            if norm > 0:
                vec = [v / norm for v in vec]
            vectors.append(vec)

        return vectors


def _infer_local_dimension(model: str) -> int | None:
    if not model:
        return None
    match = re.search(r"(\d{2,5})", model)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _parse_dashscope_embeddings(output: Any) -> list[list[float]]:
    """Best-effort parser for DashScope embedding outputs."""
    if output is None:
        raise EmbeddingError("DashScope embeddings response missing output")

    # Common shape: {"embeddings": [{"embedding": [...]}, ...]}
    if isinstance(output, dict):
        candidates = (
            output.get("embeddings")
            or output.get("data")
            or output.get("results")
            or output.get("output")
        )
        if isinstance(candidates, dict):
            candidates = (
                candidates.get("embeddings") or candidates.get("data") or candidates.get("results")
            )
        if candidates is None:
            raise EmbeddingError(
                f"Unexpected DashScope embeddings output keys: {list(output.keys())}"
            )
        output = candidates

    if not isinstance(output, list):
        raise EmbeddingError(f"Unexpected DashScope embeddings output type: {type(output)}")

    items: list[dict[str, Any]] = []
    vectors: list[list[float]] = []
    for entry in output:
        if isinstance(entry, dict):
            items.append(entry)
        elif isinstance(entry, list):
            vectors.append(entry)

    # If dict items exist, sort by index if present.
    if items:

        def _idx(d: dict[str, Any]) -> int:
            for k in ("index", "text_index", "id"):
                v = d.get(k)
                if isinstance(v, int):
                    return v
            return 0

        items = sorted(items, key=_idx)
        for d in items:
            vec = d.get("embedding") or d.get("vector") or d.get("values")
            if not isinstance(vec, list):
                raise EmbeddingError("DashScope embeddings item missing vector")
            vectors.append(vec)

    if not vectors:
        raise EmbeddingError("DashScope embeddings response empty")
    return vectors


# =============================================================================
# SiliconFlow Embedding
# =============================================================================
class SiliconFlowEmbedding(BaseEmbedding):
    """SiliconFlow embedding adapter.

    Uses SiliconFlow API for text embedding with OpenAI-compatible interface.

    API Reference: https://docs.siliconflow.cn/docs/embeddings

    Features:
    - OpenAI-compatible API
    - Support for BGE and other models
    - Configurable HTTP timeout
    - Batch embedding support

    Models:
    - BAAI/bge-m3: 1024 dimensions
    - Pro/BAAI/bge-m3: 1024 dimensions
    - BAAI/bge-large-zh-v1.5: 1024 dimensions
    - BAAI/bge-large-en-v1.5: 1024 dimensions
    - netease-youdao/bce-embedding-base_v1: 512 dimensions
    """

    MODEL_DIMENSIONS: dict[str, int] = {
        "BAAI/bge-m3": 1024,
        "Pro/BAAI/bge-m3": 1024,
        "BAAI/bge-large-zh-v1.5": 1024,
        "BAAI/bge-large-en-v1.5": 1024,
        "netease-youdao/bce-embedding-base_v1": 512,
    }

    # API endpoint (embeddings endpoint)
    SILICONFLOW_API_URL = "https://api.siliconflow.cn/v1/embeddings"

    # API limits
    MAX_BATCH_SIZE = 100
    MAX_RETRIES = 3
    RETRY_BASE_DELAY = 1.0

    def __init__(
        self,
        api_key: str,
        model: str = "BAAI/bge-large-zh-v1.5",
        dimension: int | None = None,
        base_url: str | None = None,
        timeout_seconds: float = 30.0,
    ):
        """Initialize SiliconFlow Embedding.

        Args:
            api_key: SiliconFlow API key
            model: Model name (default: BAAI/bge-large-zh-v1.5)
            dimension: Output dimension (auto-detected from model if not provided)
            base_url: Optional API base URL override
            timeout_seconds: Request timeout
        """
        dim = dimension or self.MODEL_DIMENSIONS.get(model) or 1024
        super().__init__(provider="siliconflow", model=model, dimension=dim)

        if not api_key:
            raise EmbeddingError("SiliconFlow API key is required")

        self.api_key = api_key
        self.base_url = base_url or self.SILICONFLOW_API_URL
        self.timeout_seconds = timeout_seconds
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds, connect=5.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()

    async def embed_texts(
        self,
        texts: list[str],
        text_type: str | None = None,
    ) -> list[list[float]]:
        """Embed texts using SiliconFlow API.

        Args:
            texts: List of text strings to embed
            text_type: Not used by SiliconFlow (kept for interface compatibility)

        Returns:
            List of embedding vectors
        """
        if not texts:
            return []

        # Sanitize: replace empty/whitespace-only strings with placeholder
        # to avoid API errors from providers that reject empty input.
        sanitized = [t if t and t.strip() else "empty" for t in texts]

        # Process in batches
        all_vectors: list[list[float]] = []

        for i in range(0, len(sanitized), self.MAX_BATCH_SIZE):
            batch = sanitized[i : i + self.MAX_BATCH_SIZE]
            batch_info = f"batch {i // self.MAX_BATCH_SIZE + 1}"

            vectors = await self._embed_batch_with_retry(batch, batch_info)
            all_vectors.extend(vectors)

        return all_vectors

    async def _embed_batch_with_retry(
        self,
        texts: list[str],
        batch_info: str,
    ) -> list[list[float]]:
        """Embed a batch of texts with retry logic."""
        import logging

        logger = logging.getLogger(__name__)

        last_error: Exception | None = None

        for attempt in range(self.MAX_RETRIES):
            try:
                return await self._embed_batch(texts)

            except EmbeddingError as e:
                err_str = str(e)
                retryable = any(code in err_str for code in ("429", "500", "502", "503"))
                last_error = e
                if retryable:
                    logger.warning(
                        f"SiliconFlow embedding retryable error ({batch_info}) "
                        f"attempt {attempt + 1}/{self.MAX_RETRIES}: {e}"
                    )
                else:
                    raise

            except Exception as exc:
                last_error = EmbeddingError(f"SiliconFlow embedding error ({batch_info}): {exc}")
                logger.warning(
                    f"SiliconFlow embedding error ({batch_info}) "
                    f"attempt {attempt + 1}/{self.MAX_RETRIES}: {exc}"
                )

            # Exponential backoff
            if attempt < self.MAX_RETRIES - 1:
                import random

                delay = self.RETRY_BASE_DELAY * (2**attempt)
                delay = delay + random.uniform(0.0, min(0.3, delay))
                await asyncio.sleep(delay)

        raise last_error or EmbeddingError(
            f"SiliconFlow embedding failed after {self.MAX_RETRIES} attempts ({batch_info})"
        )

    async def _embed_batch(
        self,
        texts: list[str],
    ) -> list[list[float]]:
        """Call SiliconFlow embeddings API for a batch of texts."""
        payload = {
            "model": self.model,
            "input": texts,
        }

        response = await self._client.post(
            self.base_url,
            json=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )

        if response.status_code >= 400:
            raise EmbeddingError(f"SiliconFlow API error: {response.status_code} - {response.text}")

        data = response.json()

        # Parse response
        embeddings = data.get("data", [])
        if not embeddings:
            raise EmbeddingError("SiliconFlow API returned no embeddings")

        vectors: list[list[float]] = []
        for emb in embeddings:
            values = emb.get("embedding", [])
            if not values:
                raise EmbeddingError("SiliconFlow embedding missing values")
            vectors.append(values)

        if self._dimension is None and vectors:
            self._dimension = len(vectors[0])

        return vectors


# =============================================================================
# Embedder Cache - Singleton per config to reuse HTTP connections
# =============================================================================
_embedder_cache: dict[str, BaseEmbedding] = {}
_embedder_cache_lock = asyncio.Lock()

# =============================================================================
# Query Embedding Cache - TTLCache for query embeddings (lock-free, async-safe)
# =============================================================================
from cachetools import TTLCache

_query_embedding_cache: TTLCache[str, list[float]] = TTLCache(maxsize=1000, ttl=1800)


def _get_query_cache_key(provider: str, model: str, query: str) -> str:
    """Generate cache key for query embedding."""
    return f"{provider}:{model}:{hashlib.md5(query.encode()).hexdigest()}"


def get_cached_query_embedding(provider: str, model: str, query: str) -> list[float] | None:
    """Get cached query embedding if exists."""
    key = _get_query_cache_key(provider, model, query)
    return _query_embedding_cache.get(key)


def set_cached_query_embedding(
    provider: str, model: str, query: str, embedding: list[float]
) -> None:
    """Cache query embedding."""
    key = _get_query_cache_key(provider, model, query)
    _query_embedding_cache[key] = embedding


def _make_cache_key(config: EmbeddingConfig, dimension: int | None) -> str:
    """Generate cache key for embedder config."""
    return f"{config.provider}:{config.model}:{config.api_key[:8] if config.api_key else ''}:{dimension or ''}"


async def get_cached_embedder(
    config: EmbeddingConfig, dimension: int | None = None
) -> BaseEmbedding:
    """Get or create cached embedder instance.

    This helps reduce first-call latency by reusing HTTP connections.
    """
    cache_key = _make_cache_key(config, dimension)

    async with _embedder_cache_lock:
        if cache_key not in _embedder_cache:
            _embedder_cache[cache_key] = create_embedding(config, dimension)
        return _embedder_cache[cache_key]


def create_embedding(config: EmbeddingConfig, dimension: int | None = None) -> BaseEmbedding:
    provider = (config.provider or "").lower()
    if provider in {"local", "builtin", "hash"}:
        return LocalHashEmbedding(
            model=config.model or "hash-384",
            dimension=dimension or (config.extra or {}).get("dimension"),
        )
    if provider in {"dashscope", "aliyun"}:
        return DashScopeEmbedding(
            model=config.model,
            api_key=config.api_key or "",
            dimension=dimension,
            base_url=config.base_url,
        )
    if provider in {"dashscope_multimodal", "aliyun_multimodal", "multimodal"}:
        return DashScopeMultimodalEmbedding(
            model=config.model or "multimodal-embedding-v1",
            api_key=config.api_key or "",
            dimension=dimension,
            base_url=config.base_url,
        )
    if provider in {"unified_multimodal", "unified", "cross_modal"}:
        return UnifiedMultimodalEmbedding(
            model=config.model or "tongyi-embedding-vision-plus",
            api_key=config.api_key or "",
            dimension=dimension,
            base_url=config.base_url,
            max_concurrent=(config.extra or {}).get("max_concurrent", 5),
        )
    if provider in {"gemini", "google"}:
        return GeminiEmbedding(
            api_key=config.api_key or "",
            model=config.model or "gemini-embedding-001",
            dimension=dimension or 1024,
            base_url=config.base_url,
            timeout_seconds=config.timeout_seconds,
        )
    if provider in {"siliconflow", "silicon", "sf"}:
        return SiliconFlowEmbedding(
            api_key=config.api_key or "",
            model=config.model or "BAAI/bge-large-zh-v1.5",
            dimension=dimension,
            base_url=config.base_url,
            timeout_seconds=config.timeout_seconds,
        )
    if provider in {"openai"}:
        raise EmbeddingError(
            "OpenAI embedding provider has been removed. "
            "Please update your dataset to use 'gemini', 'dashscope', or 'siliconflow'."
        )
    raise EmbeddingError(f"Unsupported embedding provider: {config.provider}")


def create_multimodal_embedding(
    api_key: str,
    model: str = "multimodal-embedding-v1",
    base_url: str | None = None,
) -> DashScopeMultimodalEmbedding:
    """Convenience factory for creating multimodal embedding instances.

    Args:
        api_key: DashScope API key
        model: Model name (default: multimodal-embedding-v1)
        base_url: Optional base URL override

    Returns:
        DashScopeMultimodalEmbedding instance
    """
    return DashScopeMultimodalEmbedding(
        model=model,
        api_key=api_key,
        base_url=base_url,
    )


# ============================================================
# Unified Multimodal Embedding (Phase 2: Cross-Modal Search)
# ============================================================


@dataclass
class UnifiedEmbeddingResult:
    """Result from unified multimodal embedding."""

    vector: list[float]
    content_type: str  # "text" | "image" | "mixed"
    dimension: int
    model: str


class UnifiedMultimodalEmbedding(BaseEmbedding):
    """Unified embedding for text and images in the SAME vector space.

    This is the key component for cross-modal retrieval (Dify 1.11 approach).
    Both text and images are embedded using the same multimodal model,
    ensuring they can be directly compared via cosine similarity.

    Key Features:
    - Text queries can find relevant images
    - Image queries can find relevant text
    - Mixed content (text + image) embedding supported
    - Consistent 1024D vector space

    Recommended Model:
    - tongyi-embedding-vision-plus: Best for unified cross-modal search

    Usage:
        # Create unified embedding instance
        unified = UnifiedMultimodalEmbedding(api_key="your-key")

        # Embed text
        text_vectors = await unified.embed_texts(["What is our refund policy?"])

        # Embed images
        image_vectors = await unified.embed_images([image_bytes])

        # Both vectors are in the same space - can compare directly!
        similarity = cosine_similarity(text_vectors[0], image_vectors[0])
    """

    # Recommended model for unified cross-modal embedding
    DEFAULT_MODEL = "tongyi-embedding-vision-plus"

    MODEL_DIMENSIONS: dict[str, int] = {
        "tongyi-embedding-vision-plus": 1024,  # Unified to 1024
        "multimodal-embedding-v1": 1024,
        "qwen2.5-vl-embedding": 1024,
    }

    MAX_IMAGE_SIZE_BYTES = 3 * 1024 * 1024  # 3MB
    MAX_TEXT_CHARS = 8000

    SUPPORTED_MEDIA_TYPES = {
        "image/jpeg",
        "image/jpg",
        "image/png",
        "image/gif",
        "image/bmp",
        "image/webp",
    }

    def __init__(
        self,
        api_key: str,
        model: str = "tongyi-embedding-vision-plus",
        dimension: int | None = None,
        base_url: str | None = None,
        max_concurrent: int = 5,
    ):
        """Initialize unified multimodal embedding.

        Args:
            api_key: DashScope API key
            model: Model to use (recommended: tongyi-embedding-vision-plus)
            dimension: Vector dimension (auto-detected from model)
            base_url: Optional API base URL override
            max_concurrent: Max concurrent API calls
        """
        dim = dimension or self.MODEL_DIMENSIONS.get(model, 1024)
        super().__init__(provider="unified_multimodal", model=model, dimension=dim)

        if not api_key:
            raise EmbeddingError("API key is required for UnifiedMultimodalEmbedding")

        self.api_key = api_key
        self.base_url = base_url
        self.max_concurrent = max_concurrent
        self._semaphore = asyncio.Semaphore(max_concurrent)

        try:
            import dashscope
            from dashscope import MultiModalEmbedding

            self._MultiModalEmbedding = MultiModalEmbedding
            if base_url:
                dashscope.base_http_api_url = base_url
        except ImportError as exc:
            raise EmbeddingError(
                "dashscope package required (pip install dashscope>=1.24.6)"
            ) from exc

    @property
    def supports_multimodal(self) -> bool:
        return True

    def _detect_media_type(self, image_bytes: bytes) -> str:
        """Detect image MIME type from magic bytes."""
        if len(image_bytes) < 8:
            return "image/png"

        if image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
            return "image/png"
        elif image_bytes[:2] == b"\xff\xd8":
            return "image/jpeg"
        elif image_bytes[:6] in (b"GIF87a", b"GIF89a"):
            return "image/gif"
        elif image_bytes[:2] == b"BM":
            return "image/bmp"
        elif image_bytes[:4] == b"RIFF" and len(image_bytes) > 12 and image_bytes[8:12] == b"WEBP":
            return "image/webp"
        return "image/png"

    def _to_base64_data_uri(self, image_bytes: bytes, media_type: str) -> str:
        """Convert image bytes to base64 data URI."""
        import base64

        b64 = base64.b64encode(image_bytes).decode("utf-8")
        return f"data:{media_type};base64,{b64}"

    def _sanitize_text(self, text: str) -> str:
        """Clean and truncate text for embedding."""
        if not text:
            return "empty"

        # Remove control characters
        text = text.replace("\x00", "")
        text = "".join(c if c.isprintable() or c in "\n\t" else " " for c in text)

        # Normalize whitespace
        text = re.sub(r"\s+", " ", text).strip()

        # Truncate if needed
        if len(text) > self.MAX_TEXT_CHARS:
            text = text[: self.MAX_TEXT_CHARS]

        return text if text else "empty"

    async def _call_api(self, input_items: list[dict[str, str]]) -> list[float]:
        """Call DashScope multimodal embedding API."""
        async with self._semaphore:
            try:
                resp = await asyncio.to_thread(
                    self._MultiModalEmbedding.call,
                    model=self.model,
                    input=input_items,
                    api_key=self.api_key,
                )

                status_code = int(getattr(resp, "status_code", 0) or 0)
                if status_code and status_code >= 400:
                    code = getattr(resp, "code", "") or ""
                    message = getattr(resp, "message", "") or ""
                    raise EmbeddingError(
                        f"Unified embedding API failed: {status_code} {code} {message}"
                    )

                output = getattr(resp, "output", None)
                vectors = self._parse_output(output)
                if not vectors:
                    raise EmbeddingError("No vectors returned from API")
                return vectors[0]

            except EmbeddingError:
                raise
            except Exception as exc:
                raise EmbeddingError(f"Unified embedding error: {exc}") from exc

    def _parse_output(self, output: Any) -> list[list[float]]:
        """Parse API response to extract vectors."""
        if output is None:
            raise EmbeddingError("Response missing output")

        if isinstance(output, dict):
            embeddings_list = output.get("embeddings")
            if embeddings_list is None:
                raise EmbeddingError(f"Unexpected output keys: {list(output.keys())}")
            output = embeddings_list

        if not isinstance(output, list):
            raise EmbeddingError(f"Unexpected output type: {type(output)}")

        vectors: list[list[float]] = []
        for entry in output:
            if isinstance(entry, dict):
                vec = entry.get("embedding") or entry.get("vector")
                if isinstance(vec, list):
                    vectors.append(vec)
            elif isinstance(entry, list):
                vectors.append(entry)

        return vectors

    async def embed_texts(
        self,
        texts: list[str],
        text_type: str | None = None,
    ) -> list[list[float]]:
        """Embed text in the unified multimodal space.

        IMPORTANT: Use this instead of DashScopeEmbedding when you need
        cross-modal retrieval (text queries finding images).

        Args:
            texts: List of text strings
            text_type: Ignored (for API compatibility)

        Returns:
            List of 1024D vectors in the same space as image embeddings
        """
        if not texts:
            return []

        vectors: list[list[float]] = []

        for text in texts:
            sanitized = self._sanitize_text(text)
            vec = await self._call_api([{"text": sanitized}])
            vectors.append(vec)

        if self._dimension is None and vectors:
            self._dimension = len(vectors[0])

        return vectors

    async def embed_query(self, query: str) -> list[float]:
        """Embed a query for cross-modal search.

        The resulting vector can find both relevant text AND images.
        """
        vectors = await self.embed_texts([query])
        return vectors[0]

    async def embed_images(
        self,
        images: list[bytes],
        max_concurrent: int | None = None,
    ) -> list[list[float]]:
        """Embed images in the unified multimodal space.

        Args:
            images: List of image bytes
            max_concurrent: Override max concurrent calls

        Returns:
            List of 1024D vectors in the same space as text embeddings
        """
        if not images:
            return []

        # Validate sizes
        for i, img in enumerate(images):
            if len(img) > self.MAX_IMAGE_SIZE_BYTES:
                raise EmbeddingError(f"Image {i} exceeds 3MB limit ({len(img)} bytes)")

        # Process concurrently
        async def embed_single(idx: int, img_bytes: bytes) -> list[float]:
            media_type = self._detect_media_type(img_bytes)
            data_uri = self._to_base64_data_uri(img_bytes, media_type)
            return await self._call_api([{"image": data_uri}])

        tasks = [embed_single(i, img) for i, img in enumerate(images)]
        vectors = await asyncio.gather(*tasks, return_exceptions=True)

        # Preserve successful embeddings; log and skip failures.
        results: list[list[float]] = []
        for i, vec in enumerate(vectors):
            if isinstance(vec, BaseException):
                logger.warning("Multimodal embedding failed for image %d: %s", i, vec)
                continue
            results.append(vec)

        if self._dimension is None and results:
            self._dimension = len(results[0])

        return results

    async def embed_image_with_context(
        self,
        image_bytes: bytes,
        context_text: str | None = None,
    ) -> list[float]:
        """Embed image with optional text context.

        This creates a combined embedding that captures both visual
        content and textual context (e.g., captions, surrounding text).

        Args:
            image_bytes: Image content
            context_text: Optional text context (caption, description)

        Returns:
            Combined embedding vector
        """
        if len(image_bytes) > self.MAX_IMAGE_SIZE_BYTES:
            raise EmbeddingError("Image exceeds 3MB limit")

        media_type = self._detect_media_type(image_bytes)
        data_uri = self._to_base64_data_uri(image_bytes, media_type)

        input_items: list[dict[str, str]] = [{"image": data_uri}]
        if context_text:
            input_items.append({"text": self._sanitize_text(context_text)})

        return await self._call_api(input_items)

    async def embed_image_and_text(
        self,
        image_bytes: bytes,
        text: str | None = None,
    ) -> list[float]:
        """Embed image with optional text - alias for embed_image_with_context.

        Provides compatibility with DashScopeMultimodalEmbedding interface.
        """
        return await self.embed_image_with_context(image_bytes, text)

    async def embed_mixed_batch(
        self,
        items: list[dict[str, Any]],
    ) -> list[UnifiedEmbeddingResult]:
        """Embed a batch of mixed text and image content.

        Args:
            items: List of dicts with either:
                   {"type": "text", "content": "text string"}
                   {"type": "image", "content": image_bytes}
                   {"type": "mixed", "text": str, "image": bytes}

        Returns:
            List of UnifiedEmbeddingResult with vectors and metadata
        """
        results: list[UnifiedEmbeddingResult] = []

        for item in items:
            item_type = item.get("type", "text")

            if item_type == "text":
                text = item.get("content", "")
                vec = (await self.embed_texts([text]))[0]
                results.append(
                    UnifiedEmbeddingResult(
                        vector=vec,
                        content_type="text",
                        dimension=len(vec),
                        model=self.model,
                    )
                )

            elif item_type == "image":
                img = item.get("content", b"")
                vec = (await self.embed_images([img]))[0]
                results.append(
                    UnifiedEmbeddingResult(
                        vector=vec,
                        content_type="image",
                        dimension=len(vec),
                        model=self.model,
                    )
                )

            elif item_type == "mixed":
                text = item.get("text", "")
                img = item.get("image", b"")
                vec = await self.embed_image_with_context(img, text)
                results.append(
                    UnifiedEmbeddingResult(
                        vector=vec,
                        content_type="mixed",
                        dimension=len(vec),
                        model=self.model,
                    )
                )

        return results


def create_unified_embedding(
    api_key: str,
    model: str = "tongyi-embedding-vision-plus",
    base_url: str | None = None,
    max_concurrent: int = 5,
) -> UnifiedMultimodalEmbedding:
    """Create a unified multimodal embedding instance.

    This is the recommended way to create embeddings for cross-modal search.
    Both text and images will be in the same 1024D vector space.

    Args:
        api_key: DashScope API key
        model: Model name (default: tongyi-embedding-vision-plus)
        base_url: Optional API base URL
        max_concurrent: Max concurrent API calls

    Returns:
        UnifiedMultimodalEmbedding instance

    Example:
        ```python
        unified = create_unified_embedding(api_key=os.environ["DASHSCOPE_KEY"])

        # Embed text query
        query_vec = await unified.embed_query("What does the architecture diagram show?")

        # This query can now find both:
        # 1. Text segments discussing architecture
        # 2. Image segments containing architecture diagrams
        ```
    """
    return UnifiedMultimodalEmbedding(
        api_key=api_key,
        model=model,
        base_url=base_url,
        max_concurrent=max_concurrent,
    )
