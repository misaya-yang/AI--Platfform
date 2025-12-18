from __future__ import annotations

import asyncio
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
    """

    MODEL_DIMENSIONS: Dict[str, int] = {
        "text-embedding-v1": 1536,
        "text-embedding-v2": 1536,
        "text-embedding-v3": 1024,
        "text-embedding-v4": 1024,  # DashScope v4 default dimension
    }

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

    async def embed_texts(
        self, texts: List[str], text_type: Optional[str] = None
    ) -> List[List[float]]:
        if not texts:
            return []

        kwargs: Dict[str, Any] = {}
        if text_type in {"query", "document"}:
            kwargs["text_type"] = text_type

        resp = await asyncio.to_thread(
            self._TextEmbedding.call,
            model=self.model,
            input=texts,
            api_key=self.api_key,
            **kwargs,
        )

        status_code = int(getattr(resp, "status_code", 0) or 0)
        if status_code and status_code >= 400:
            code = getattr(resp, "code", "") or ""
            message = getattr(resp, "message", "") or ""
            raise EmbeddingError(f"DashScope embeddings failed: {status_code} {code} {message}")

        output = getattr(resp, "output", None)
        vectors = _parse_dashscope_embeddings(output)
        if self._dimension is None and vectors:
            self._dimension = len(vectors[0])
        return vectors


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
    if provider in {"dashscope", "aliyun"}:
        return DashScopeEmbedding(
            model=config.model,
            api_key=config.api_key or "",
            dimension=dimension,
            base_url=config.base_url,
        )
    raise EmbeddingError(f"Unsupported embedding provider: {config.provider}")

