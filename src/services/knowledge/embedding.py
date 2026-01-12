from __future__ import annotations

import asyncio
import hashlib
import math
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

import httpx


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
            
            try:
                resp = await asyncio.to_thread(
                    self._TextEmbedding.call,
                    model=self.model,
                    input=batch,
                    api_key=self.api_key,
                    **kwargs,
                )

                status_code = int(getattr(resp, "status_code", 0) or 0)
                if status_code and status_code >= 400:
                    code = getattr(resp, "code", "") or ""
                    message = getattr(resp, "message", "") or ""
                    # Log which batch failed for debugging
                    batch_info = f"batch {i // self.MAX_BATCH_SIZE + 1}, texts {i}-{i + len(batch) - 1}"
                    raise EmbeddingError(
                        f"DashScope embeddings failed ({batch_info}): {status_code} {code} {message}"
                    )

                output = getattr(resp, "output", None)
                vectors = _parse_dashscope_embeddings(output)
                all_vectors.extend(vectors)
                
            except EmbeddingError:
                raise
            except Exception as exc:
                batch_info = f"batch {i // self.MAX_BATCH_SIZE + 1}"
                raise EmbeddingError(f"DashScope embedding error ({batch_info}): {exc}") from exc

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
