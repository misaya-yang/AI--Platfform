from __future__ import annotations

import asyncio
import hashlib
import math
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Optional, Sequence

import httpx


# =============================================================================
# Multimodal Embedding Model Registry
# =============================================================================
# Centralized list of embedding models that support multimodal (image) content.
# Used by assistant API to identify multimodal knowledge bases.

MULTIMODAL_EMBEDDING_MODELS: FrozenSet[str] = frozenset({
    # DashScope multimodal models
    "multimodal-embedding-v1",
    "multimodal-embedding-one-peace-v1",
    "multimodal-embedding-one-peace",
    # Tongyi unified vision models
    "tongyi-embedding-vision-plus",
    # Qwen VL models
    "qwen2.5-vl-embedding",
})


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
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    timeout_seconds: float = 30.0
    extra: Dict[str, Any] = None

    def __post_init__(self):
        object.__setattr__(self, "extra", self.extra or {})


class BaseEmbedding(ABC):
    """Unified embedding interface (text-first, multimodal-ready)."""

    def __init__(
        self,
        provider: str,
        model: str,
        dimension: Optional[int] = None,
    ):
        self.provider = provider
        self.model = model
        self._dimension: Optional[int] = dimension

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

    async def embed_query(self, query: str) -> List[float]:
        vectors = await self.embed_texts([query], text_type="query")
        return vectors[0]

    async def embed_documents(self, texts: Sequence[str]) -> List[List[float]]:
        return await self.embed_texts(list(texts), text_type="document")

    @abstractmethod
    async def embed_texts(
        self, texts: List[str], text_type: Optional[str] = None
    ) -> List[List[float]]:
        raise NotImplementedError

    async def embed_images(self, images: List[bytes]) -> List[List[float]]:  # pragma: no cover
        raise EmbeddingError(f"{self.provider}:{self.model} does not support image embedding")


class OpenAIEmbedding(BaseEmbedding):
    """OpenAI embeddings adapter (HTTP via httpx)."""

    MODEL_DIMENSIONS: Dict[str, int] = {
        "text-embedding-3-small": 1536,
        "text-embedding-3-large": 3072,
        "text-embedding-ada-002": 1536,
    }

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        timeout_seconds: float = 30.0,
        dimension: Optional[int] = None,
        organization: Optional[str] = None,
    ):
        dim = dimension or self.MODEL_DIMENSIONS.get(model)
        super().__init__(provider="openai", model=model, dimension=dim)
        if not api_key:
            raise EmbeddingError("OpenAI api_key is required")
        headers = {"Authorization": f"Bearer {api_key}"}
        if organization:
            headers["OpenAI-Organization"] = organization
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            headers=headers,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def embed_texts(
        self, texts: List[str], text_type: Optional[str] = None
    ) -> List[List[float]]:
        if not texts:
            return []
        payload: Dict[str, Any] = {"model": self.model, "input": texts}
        resp = await self._client.post("/embeddings", json=payload)
        if resp.status_code >= 400:
            raise EmbeddingError(f"OpenAI embeddings failed: {resp.status_code} {resp.text}")
        data = resp.json().get("data") or []
        data = sorted(data, key=lambda x: x.get("index", 0))
        vectors = [item.get("embedding") for item in data]
        if not vectors or any(v is None for v in vectors):
            raise EmbeddingError("OpenAI embeddings response missing embeddings")
        if self._dimension is None:
            self._dimension = len(vectors[0])
        return vectors  # type: ignore[return-value]


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

    MODEL_DIMENSIONS: Dict[str, int] = {
        "text-embedding-v1": 1536,
        "text-embedding-v2": 1536,
        "text-embedding-v3": 1024,
        "text-embedding-v4": 1024,  # DashScope v4 default dimension
    }

    # Max characters per text (conservative: ~2.5 chars/token)
    # DashScope v1/v2: max 2048 tokens, v3: max 8192 tokens
    # But in practice, shorter is safer to avoid InvalidParameter errors
    MODEL_MAX_CHARS: Dict[str, int] = {
        "text-embedding-v1": 4000,
        "text-embedding-v2": 4000,
        "text-embedding-v3": 8000,   # More conservative for v3
        "text-embedding-v4": 8000,
    }

    # DashScope API limit: max 10 texts per batch for v3, 25 for v1/v2
    # Use 6 to be safe across all models

    # Retry configuration
    MAX_RETRIES = 3
    RETRY_BASE_DELAY = 1.0  # seconds
    REQUEST_TIMEOUT = 60  # seconds for HTTP request
    MAX_BATCH_SIZE = 6

    def __init__(
        self,
        model: str,
        api_key: str,
        dimension: Optional[int] = None,
        base_url: Optional[str] = None,
    ):
        # Try to lookup dimension, fallback to 1024 if model not recognized
        dim = dimension or self.MODEL_DIMENSIONS.get(model) or self.MODEL_DIMENSIONS.get(model.lower()) or 1024
        super().__init__(provider="dashscope", model=model, dimension=dim)
        if not api_key:
            raise EmbeddingError("DashScope api_key is required")
        self.api_key = api_key
        self.base_url = base_url
        self.max_chars = self.MODEL_MAX_CHARS.get(model) or self.MODEL_MAX_CHARS.get(model.lower()) or 6000
        try:
            from dashscope import TextEmbedding  # type: ignore
            import dashscope

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
            truncated = text[:self.max_chars]
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
        text = re.sub(r'\n{3,}', '\n\n', text)
        text = re.sub(r'[ \t]{2,}', ' ', text)
        text = re.sub(r'\n[ \t]+', '\n', text)  # Remove leading spaces on lines
        
        # Remove very long sequences of repeated characters (often from PDF extraction errors)
        text = re.sub(r'(.)\1{20,}', r'\1\1\1', text)
        
        text = text.strip()
        return text if text else "empty"

    async def _call_with_retry(
        self, batch: List[str], batch_info: str, **kwargs: Any
    ) -> List[List[float]]:
        """Call DashScope API with retry and exponential backoff."""
        import logging
        logger = logging.getLogger(__name__)

        last_error: Optional[Exception] = None

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
                delay = self.RETRY_BASE_DELAY * (2 ** attempt)
                await asyncio.sleep(delay)

        # All retries exhausted
        raise last_error or EmbeddingError(f"DashScope embedding failed after {self.MAX_RETRIES} attempts ({batch_info})")

    async def embed_texts(
        self, texts: List[str], text_type: Optional[str] = None
    ) -> List[List[float]]:
        if not texts:
            return []

        # Sanitize and truncate all texts
        processed_texts = [self._truncate_text(self._sanitize_text(t)) for t in texts]

        kwargs: Dict[str, Any] = {}
        if text_type in {"query", "document"}:
            kwargs["text_type"] = text_type

        # Process in batches of MAX_BATCH_SIZE
        all_vectors: List[List[float]] = []
        for i in range(0, len(processed_texts), self.MAX_BATCH_SIZE):
            batch = processed_texts[i:i + self.MAX_BATCH_SIZE]
            batch_info = f"batch {i // self.MAX_BATCH_SIZE + 1}, texts {i}-{i + len(batch) - 1}"

            vectors = await self._call_with_retry(batch, batch_info, **kwargs)
            all_vectors.extend(vectors)

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

    MODEL_DIMENSIONS: Dict[str, int] = {
        "multimodal-embedding-v1": 1024,
        "multimodal-embedding-one-peace": 1536,
        "tongyi-embedding-vision-plus": 1024,
        "qwen2.5-vl-embedding": 1024,  # Latest Qwen VL embedding model
    }

    # Max 3MB for DashScope multimodal API
    MAX_IMAGE_SIZE_BYTES = 3 * 1024 * 1024

    # Supported image MIME types
    SUPPORTED_MEDIA_TYPES = {
        "image/jpeg", "image/jpg", "image/png",
        "image/gif", "image/bmp", "image/webp"
    }

    def __init__(
        self,
        model: str = "multimodal-embedding-v1",
        api_key: str = "",
        dimension: Optional[int] = None,
        base_url: Optional[str] = None,
    ):
        dim = dimension or self.MODEL_DIMENSIONS.get(model) or 1024
        super().__init__(provider="dashscope_multimodal", model=model, dimension=dim)
        if not api_key:
            raise EmbeddingError("DashScope api_key is required for multimodal embedding")
        self.api_key = api_key
        self.base_url = base_url

        try:
            from dashscope import MultiModalEmbedding  # type: ignore
            import dashscope

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
        if image_bytes[:8] == b'\x89PNG\r\n\x1a\n':
            return "image/png"
        elif image_bytes[:2] == b'\xff\xd8':
            return "image/jpeg"
        elif image_bytes[:6] in (b'GIF87a', b'GIF89a'):
            return "image/gif"
        elif image_bytes[:2] == b'BM':
            return "image/bmp"
        elif image_bytes[:4] == b'RIFF' and image_bytes[8:12] == b'WEBP':
            return "image/webp"
        else:
            return "image/png"  # Default fallback

    async def embed_texts(
        self, texts: List[str], text_type: Optional[str] = None
    ) -> List[List[float]]:
        """Embed text using multimodal model.

        Note: While this model supports text, it's primarily designed for images.
        For text-only embedding, consider using DashScopeEmbedding instead.
        """
        if not texts:
            return []

        all_vectors: List[List[float]] = []

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
        images: List[bytes],
        max_concurrent: int = 5,
    ) -> List[List[float]]:
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

        async def embed_single_image(idx: int, image_bytes: bytes) -> List[float]:
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
        all_vectors = await asyncio.gather(*tasks)

        if self._dimension is None and all_vectors:
            self._dimension = len(all_vectors[0])

        return list(all_vectors)

    async def embed_image_and_text(
        self,
        image_bytes: bytes,
        text: Optional[str] = None
    ) -> List[float]:
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
            input_items: List[Dict[str, str]] = [{"image": data_uri}]
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

    def _parse_multimodal_output(self, output: Any) -> List[List[float]]:
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
            raise EmbeddingError(
                f"Unexpected DashScope multimodal output type: {type(output)}"
            )

        vectors: List[List[float]] = []
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

    def __init__(self, model: str, dimension: Optional[int] = None):
        dim = dimension or _infer_local_dimension(model) or self.DEFAULT_DIMENSION
        super().__init__(provider="local", model=model, dimension=dim)

    async def embed_texts(
        self, texts: List[str], text_type: Optional[str] = None
    ) -> List[List[float]]:
        if not texts:
            return []

        vectors: List[List[float]] = []
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


def _infer_local_dimension(model: str) -> Optional[int]:
    if not model:
        return None
    match = re.search(r"(\d{2,5})", model)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _parse_dashscope_embeddings(output: Any) -> List[List[float]]:
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
            candidates = candidates.get("embeddings") or candidates.get("data") or candidates.get("results")
        if candidates is None:
            raise EmbeddingError(f"Unexpected DashScope embeddings output keys: {list(output.keys())}")
        output = candidates

    if not isinstance(output, list):
        raise EmbeddingError(f"Unexpected DashScope embeddings output type: {type(output)}")

    items: List[Dict[str, Any]] = []
    vectors: List[List[float]] = []
    for entry in output:
        if isinstance(entry, dict):
            items.append(entry)
        elif isinstance(entry, list):
            vectors.append(entry)

    # If dict items exist, sort by index if present.
    if items:
        def _idx(d: Dict[str, Any]) -> int:
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


def create_embedding(config: EmbeddingConfig, dimension: Optional[int] = None) -> BaseEmbedding:
    provider = (config.provider or "").lower()
    if provider == "openai":
        return OpenAIEmbedding(
            model=config.model,
            api_key=config.api_key or "",
            base_url=config.base_url or "https://api.openai.com/v1",
            timeout_seconds=config.timeout_seconds,
            dimension=dimension,
            organization=config.extra.get("organization") if config.extra else None,
        )
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
    raise EmbeddingError(f"Unsupported embedding provider: {config.provider}")


def create_multimodal_embedding(
    api_key: str,
    model: str = "multimodal-embedding-v1",
    base_url: Optional[str] = None,
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
    vector: List[float]
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

    MODEL_DIMENSIONS: Dict[str, int] = {
        "tongyi-embedding-vision-plus": 1024,
        "multimodal-embedding-v1": 1024,
        "qwen2.5-vl-embedding": 1024,
    }

    MAX_IMAGE_SIZE_BYTES = 3 * 1024 * 1024  # 3MB
    MAX_TEXT_CHARS = 8000

    SUPPORTED_MEDIA_TYPES = {
        "image/jpeg", "image/jpg", "image/png",
        "image/gif", "image/bmp", "image/webp"
    }

    def __init__(
        self,
        api_key: str,
        model: str = "tongyi-embedding-vision-plus",
        dimension: Optional[int] = None,
        base_url: Optional[str] = None,
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
            from dashscope import MultiModalEmbedding
            import dashscope

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

        if image_bytes[:8] == b'\x89PNG\r\n\x1a\n':
            return "image/png"
        elif image_bytes[:2] == b'\xff\xd8':
            return "image/jpeg"
        elif image_bytes[:6] in (b'GIF87a', b'GIF89a'):
            return "image/gif"
        elif image_bytes[:2] == b'BM':
            return "image/bmp"
        elif image_bytes[:4] == b'RIFF' and len(image_bytes) > 12 and image_bytes[8:12] == b'WEBP':
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
        text = re.sub(r'\s+', ' ', text).strip()

        # Truncate if needed
        if len(text) > self.MAX_TEXT_CHARS:
            text = text[:self.MAX_TEXT_CHARS]

        return text if text else "empty"

    async def _call_api(self, input_items: List[Dict[str, str]]) -> List[float]:
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

    def _parse_output(self, output: Any) -> List[List[float]]:
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

        vectors: List[List[float]] = []
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
        texts: List[str],
        text_type: Optional[str] = None,
    ) -> List[List[float]]:
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

        vectors: List[List[float]] = []

        for text in texts:
            sanitized = self._sanitize_text(text)
            vec = await self._call_api([{"text": sanitized}])
            vectors.append(vec)

        if self._dimension is None and vectors:
            self._dimension = len(vectors[0])

        return vectors

    async def embed_query(self, query: str) -> List[float]:
        """Embed a query for cross-modal search.

        The resulting vector can find both relevant text AND images.
        """
        vectors = await self.embed_texts([query])
        return vectors[0]

    async def embed_images(
        self,
        images: List[bytes],
        max_concurrent: Optional[int] = None,
    ) -> List[List[float]]:
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
                raise EmbeddingError(
                    f"Image {i} exceeds 3MB limit ({len(img)} bytes)"
                )

        # Process concurrently
        async def embed_single(idx: int, img_bytes: bytes) -> List[float]:
            media_type = self._detect_media_type(img_bytes)
            data_uri = self._to_base64_data_uri(img_bytes, media_type)
            return await self._call_api([{"image": data_uri}])

        tasks = [embed_single(i, img) for i, img in enumerate(images)]
        vectors = await asyncio.gather(*tasks)

        if self._dimension is None and vectors:
            self._dimension = len(vectors[0])

        return list(vectors)

    async def embed_image_with_context(
        self,
        image_bytes: bytes,
        context_text: Optional[str] = None,
    ) -> List[float]:
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

        input_items: List[Dict[str, str]] = [{"image": data_uri}]
        if context_text:
            input_items.append({"text": self._sanitize_text(context_text)})

        return await self._call_api(input_items)

    async def embed_image_and_text(
        self,
        image_bytes: bytes,
        text: Optional[str] = None,
    ) -> List[float]:
        """Embed image with optional text - alias for embed_image_with_context.

        Provides compatibility with DashScopeMultimodalEmbedding interface.
        """
        return await self.embed_image_with_context(image_bytes, text)

    async def embed_mixed_batch(
        self,
        items: List[Dict[str, Any]],
    ) -> List[UnifiedEmbeddingResult]:
        """Embed a batch of mixed text and image content.

        Args:
            items: List of dicts with either:
                   {"type": "text", "content": "text string"}
                   {"type": "image", "content": image_bytes}
                   {"type": "mixed", "text": str, "image": bytes}

        Returns:
            List of UnifiedEmbeddingResult with vectors and metadata
        """
        results: List[UnifiedEmbeddingResult] = []

        for item in items:
            item_type = item.get("type", "text")

            if item_type == "text":
                text = item.get("content", "")
                vec = (await self.embed_texts([text]))[0]
                results.append(UnifiedEmbeddingResult(
                    vector=vec,
                    content_type="text",
                    dimension=len(vec),
                    model=self.model,
                ))

            elif item_type == "image":
                img = item.get("content", b"")
                vec = (await self.embed_images([img]))[0]
                results.append(UnifiedEmbeddingResult(
                    vector=vec,
                    content_type="image",
                    dimension=len(vec),
                    model=self.model,
                ))

            elif item_type == "mixed":
                text = item.get("text", "")
                img = item.get("image", b"")
                vec = await self.embed_image_with_context(img, text)
                results.append(UnifiedEmbeddingResult(
                    vector=vec,
                    content_type="mixed",
                    dimension=len(vec),
                    model=self.model,
                ))

        return results


def create_unified_embedding(
    api_key: str,
    model: str = "tongyi-embedding-vision-plus",
    base_url: Optional[str] = None,
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
