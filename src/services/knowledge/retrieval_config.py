"""
Production-grade Retrieval Configuration Module

Supports configurable retrieval pipelines:
- Vector retrieval (semantic search)
- Keyword retrieval (BM25)
- Hybrid retrieval (vector + keyword fusion)
- RRF (Reciprocal Rank Fusion)
- Weighted fusion (alpha blending)
- MMR (Maximal Marginal Relevance) for diversity
- Reranking (cross-encoder models)

Compatible with Dify / Alibaba Cloud style configurations.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional


class RetrievalMode(str, Enum):
    """Retrieval mode"""
    VECTOR = "vector"
    KEYWORD = "keyword" 
    HYBRID = "hybrid"


class FusionStrategy(str, Enum):
    """Fusion strategy for hybrid retrieval"""
    RRF = "rrf"              # Reciprocal Rank Fusion
    WEIGHTED = "weighted"     # Alpha-weighted combination


class RerankProvider(str, Enum):
    """Supported rerank model providers"""
    DASHSCOPE = "dashscope"
    COHERE = "cohere"
    JINA = "jina"
    BGE = "bge"
    CUSTOM = "custom"


@dataclass
class VectorRetrievalConfig:
    """Vector retrieval configuration"""
    enabled: bool = True
    top_k: int = 20
    score_threshold: Optional[float] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "top_k": self.top_k,
            "score_threshold": self.score_threshold,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "VectorRetrievalConfig":
        if not data:
            return cls()
        return cls(
            enabled=bool(data.get("enabled", True)),
            top_k=int(data.get("top_k", 20)),
            score_threshold=float(data["score_threshold"]) if data.get("score_threshold") is not None else None,
        )


@dataclass
class KeywordRetrievalConfig:
    """Keyword (BM25) retrieval configuration"""
    enabled: bool = True
    top_k: int = 20
    candidate_pool_size: int = 200  # Initial candidate pool for BM25 scoring
    
    # BM25 parameters
    bm25_k1: float = 1.2
    bm25_b: float = 0.75
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "top_k": self.top_k,
            "candidate_pool_size": self.candidate_pool_size,
            "bm25_k1": self.bm25_k1,
            "bm25_b": self.bm25_b,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "KeywordRetrievalConfig":
        if not data:
            return cls()
        return cls(
            enabled=bool(data.get("enabled", True)),
            top_k=int(data.get("top_k", 20)),
            candidate_pool_size=int(data.get("candidate_pool_size", 200)),
            bm25_k1=float(data.get("bm25_k1", 1.2)),
            bm25_b=float(data.get("bm25_b", 0.75)),
        )


@dataclass
class FusionConfig:
    """Fusion configuration for hybrid retrieval"""
    strategy: FusionStrategy = FusionStrategy.RRF
    
    # RRF parameters
    rrf_k: int = 60  # RRF constant (higher = more weight to lower ranks)
    rrf_weights: Dict[str, float] = field(default_factory=lambda: {"vector": 1.0, "keyword": 1.0})
    
    # Weighted fusion parameters
    alpha: float = 0.75  # Weight for vector scores (1-alpha for keyword)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy": self.strategy.value,
            "rrf_k": self.rrf_k,
            "rrf_weights": self.rrf_weights,
            "alpha": self.alpha,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FusionConfig":
        if not data:
            return cls()
        
        strategy_str = str(data.get("strategy", "rrf")).lower()
        strategy = FusionStrategy.RRF if strategy_str == "rrf" else FusionStrategy.WEIGHTED
        
        return cls(
            strategy=strategy,
            rrf_k=int(data.get("rrf_k", 60)),
            rrf_weights=data.get("rrf_weights") or {"vector": 1.0, "keyword": 1.0},
            alpha=float(data.get("alpha", 0.75)),
        )


@dataclass
class RerankConfig:
    """Reranking configuration"""
    enabled: bool = False  # Off by default; preset configs (balanced/accurate/sota) enable it explicitly
    provider: RerankProvider = RerankProvider.DASHSCOPE
    model: str = "gte-rerank"
    top_n: Optional[int] = None  # Number of results to keep after reranking
    score_threshold: Optional[float] = None
    
    # Provider-specific settings
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "provider": self.provider.value,
            "model": self.model,
            "top_n": self.top_n,
            "score_threshold": self.score_threshold,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RerankConfig":
        if not data:
            return cls()
        
        if isinstance(data, bool):
            return cls(enabled=data)
        
        provider_str = str(data.get("provider", "dashscope")).lower()
        provider_map = {
            "dashscope": RerankProvider.DASHSCOPE,
            "cohere": RerankProvider.COHERE,
            "jina": RerankProvider.JINA,
            "bge": RerankProvider.BGE,
        }
        provider = provider_map.get(provider_str, RerankProvider.DASHSCOPE)
        
        return cls(
            enabled=bool(data.get("enabled", False)),
            provider=provider,
            model=str(data.get("model", "gte-rerank")),
            top_n=int(data["top_n"]) if data.get("top_n") is not None else None,
            score_threshold=float(data["score_threshold"]) if data.get("score_threshold") is not None else None,
            api_key=data.get("api_key"),
            base_url=data.get("base_url"),
        )


@dataclass
class MMRConfig:
    """MMR (Maximal Marginal Relevance) configuration for diversity"""
    enabled: bool = False
    lambda_mult: float = 0.5  # Balance between relevance (1.0) and diversity (0.0)
    similarity_threshold: Optional[float] = None  # Filter out very similar results
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "lambda": self.lambda_mult,
            "similarity_threshold": self.similarity_threshold,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MMRConfig":
        if not data:
            return cls()
        
        if isinstance(data, bool):
            return cls(enabled=data)
        
        return cls(
            enabled=bool(data.get("enabled", False)),
            lambda_mult=float(data.get("lambda", data.get("lambda_mult", 0.5))),
            similarity_threshold=float(data["similarity_threshold"]) if data.get("similarity_threshold") is not None else None,
        )


@dataclass
class MultimodalConfig:
    """Multimodal retrieval configuration for image-text cross-modal search"""
    enabled: bool = True  # Enable multimodal retrieval for datasets with images

    # Image search settings
    image_search_enabled: bool = True  # Directly search image segments (not just associated)
    image_score_threshold: float = 0.2  # Lower threshold for images (typically score lower)
    text_score_threshold: float = 0.3  # Threshold for text segments
    use_separate_thresholds: bool = True  # Use different thresholds for text/image

    # Image boosting
    image_boost: float = 1.0  # Boost factor for image results (>1 = prefer images)

    # VLM Reranking
    vlm_rerank_enabled: bool = False  # Use VLM for multimodal reranking
    vlm_rerank_weight: float = 0.4  # Weight of VLM score in combined ranking

    # Content type filtering
    content_type_filter: Optional[str] = None  # "text" | "image" | None (all)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "image_search_enabled": self.image_search_enabled,
            "image_score_threshold": self.image_score_threshold,
            "text_score_threshold": self.text_score_threshold,
            "use_separate_thresholds": self.use_separate_thresholds,
            "image_boost": self.image_boost,
            "vlm_rerank_enabled": self.vlm_rerank_enabled,
            "vlm_rerank_weight": self.vlm_rerank_weight,
            "content_type_filter": self.content_type_filter,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MultimodalConfig":
        if not data:
            return cls()

        if isinstance(data, bool):
            return cls(enabled=data)

        return cls(
            enabled=bool(data.get("enabled", True)),
            image_search_enabled=bool(data.get("image_search_enabled", True)),
            image_score_threshold=float(data.get("image_score_threshold", 0.2)),
            text_score_threshold=float(data.get("text_score_threshold", 0.3)),
            use_separate_thresholds=bool(data.get("use_separate_thresholds", True)),
            image_boost=float(data.get("image_boost", 1.0)),
            vlm_rerank_enabled=bool(data.get("vlm_rerank_enabled", False)),
            vlm_rerank_weight=float(data.get("vlm_rerank_weight", 0.4)),
            content_type_filter=data.get("content_type_filter"),
        )


@dataclass
class IslamicEnhancementConfig:
    """Islamic-specific retrieval enhancements (opt-in per dataset).

    All features default to OFF. Enable per-dataset via index_config.retrieval.islamic.
    When disabled, these hooks are never executed — zero overhead for non-Islamic datasets.
    """
    multi_query: bool = False       # PRE_RETRIEVAL: expand query with Islamic synonyms/transliterations
    citation_format: bool = False   # POST_RANKING: attach formatted citations to results
    authority_sort: bool = False    # POST_RANKING: sort results by Quran > Hadith > Tafseer > Fiqh
    contextual_prefix: bool = False # INDEX-TIME: prepend context prefix before embedding (requires re-index)

    # multi_query parameters
    max_expanded_queries: int = 3   # Maximum number of expanded queries (including original)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "multi_query": self.multi_query,
            "citation_format": self.citation_format,
            "authority_sort": self.authority_sort,
            "contextual_prefix": self.contextual_prefix,
            "max_expanded_queries": self.max_expanded_queries,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "IslamicEnhancementConfig":
        if not data:
            return cls()
        if isinstance(data, bool):
            # Shorthand: islamic: true enables multi_query + citation + authority_sort
            return cls(multi_query=data, citation_format=data, authority_sort=data)
        return cls(
            multi_query=bool(data.get("multi_query", False)),
            citation_format=bool(data.get("citation_format", False)),
            authority_sort=bool(data.get("authority_sort", False)),
            contextual_prefix=bool(data.get("contextual_prefix", False)),
            max_expanded_queries=int(data.get("max_expanded_queries", 3)),
        )


@dataclass
class RetrievalConfig:
    """Complete retrieval pipeline configuration"""
    mode: RetrievalMode = RetrievalMode.HYBRID
    top_k: int = 5  # Final number of results to return
    score_threshold: Optional[float] = None  # Minimum score threshold

    # Component configs
    vector: VectorRetrievalConfig = field(default_factory=VectorRetrievalConfig)
    keyword: KeywordRetrievalConfig = field(default_factory=KeywordRetrievalConfig)
    fusion: FusionConfig = field(default_factory=FusionConfig)
    rerank: RerankConfig = field(default_factory=RerankConfig)
    mmr: MMRConfig = field(default_factory=MMRConfig)
    multimodal: MultimodalConfig = field(default_factory=MultimodalConfig)  # Multimodal retrieval
    islamic: IslamicEnhancementConfig = field(default_factory=IslamicEnhancementConfig)  # Islamic enhancements

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "mode": self.mode.value,
            "top_k": self.top_k,
            "score_threshold": self.score_threshold,
            "vector": self.vector.to_dict(),
            "keyword": self.keyword.to_dict(),
            "fusion": self.fusion.to_dict(),
            "rerank": self.rerank.to_dict(),
            "mmr": self.mmr.to_dict(),
            "multimodal": self.multimodal.to_dict(),
        }
        # Only include islamic config if any feature is enabled (keep output clean for non-Islamic datasets)
        islamic_dict = self.islamic.to_dict()
        if any(islamic_dict.get(k) for k in ("multi_query", "citation_format", "authority_sort", "contextual_prefix")):
            result["islamic"] = islamic_dict
        return result
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RetrievalConfig":
        if not data:
            return cls()
        
        mode_str = str(data.get("mode", "hybrid")).lower()
        mode_map = {
            "vector": RetrievalMode.VECTOR,
            "keyword": RetrievalMode.KEYWORD,
            "hybrid": RetrievalMode.HYBRID,
            "semantic": RetrievalMode.VECTOR,
            "fulltext": RetrievalMode.KEYWORD,
        }
        mode = mode_map.get(mode_str, RetrievalMode.HYBRID)
        
        return cls(
            mode=mode,
            top_k=int(data.get("top_k", 5)),
            score_threshold=float(data["score_threshold"]) if data.get("score_threshold") is not None else None,
            vector=VectorRetrievalConfig.from_dict(data.get("vector") or {}),
            keyword=KeywordRetrievalConfig.from_dict(data.get("keyword") or {}),
            fusion=FusionConfig.from_dict(data.get("fusion") or {}),
            rerank=RerankConfig.from_dict(data.get("rerank") or {}),
            mmr=MMRConfig.from_dict(data.get("mmr") or {}),
            multimodal=MultimodalConfig.from_dict(data.get("multimodal") or {}),
            islamic=IslamicEnhancementConfig.from_dict(data.get("islamic") or {}),
        )

    @classmethod
    def from_flat_dict(cls, data: Dict[str, Any]) -> "RetrievalConfig":
        """Create from flat parameter dict (for API compatibility)"""
        if not data:
            return cls()
        
        mode_str = str(data.get("mode", "hybrid")).lower()
        mode_map = {
            "vector": RetrievalMode.VECTOR,
            "keyword": RetrievalMode.KEYWORD,
            "hybrid": RetrievalMode.HYBRID,
        }
        mode = mode_map.get(mode_str, RetrievalMode.HYBRID)
        
        return cls(
            mode=mode,
            top_k=int(data.get("top_k", 5)),
            score_threshold=float(data["score_threshold"]) if data.get("score_threshold") is not None else None,
            vector=VectorRetrievalConfig(
                enabled=mode in (RetrievalMode.VECTOR, RetrievalMode.HYBRID),
                top_k=int(data.get("vector_top_k", 20)),
            ),
            keyword=KeywordRetrievalConfig(
                enabled=mode in (RetrievalMode.KEYWORD, RetrievalMode.HYBRID),
                top_k=int(data.get("keyword_top_k", 20)),
                candidate_pool_size=int(data.get("keyword_candidate_k", 200)),
            ),
            fusion=FusionConfig(
                strategy=FusionStrategy.RRF if data.get("fusion", "rrf") == "rrf" else FusionStrategy.WEIGHTED,
                rrf_k=int(data.get("rrf_k", 60)),
                alpha=float(data.get("alpha", 0.75)),
            ),
            rerank=RerankConfig(
                enabled=bool(data.get("rerank", False)),
                model=str(data.get("rerank_model", "gte-rerank")),
                top_n=int(data["rerank_top_n"]) if data.get("rerank_top_n") is not None else None,
            ),
            mmr=MMRConfig(
                enabled=bool(data.get("mmr", False)),
                lambda_mult=float(data.get("mmr_lambda", 0.5)),
                similarity_threshold=float(data["mmr_threshold"]) if data.get("mmr_threshold") is not None else None,
            ),
        )


@dataclass
class DatasetIndexConfig:
    """Complete dataset index configuration (chunking + retrieval)"""
    chunking: Dict[str, Any] = field(default_factory=dict)
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)
    
    def to_dict(self) -> Dict[str, Any]:
        chunking_dict = self.chunking
        if hasattr(self.chunking, 'to_dict'):
            chunking_dict = self.chunking.to_dict()
        return {
            "chunking": chunking_dict,
            "retrieval": self.retrieval.to_dict(),
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DatasetIndexConfig":
        if not data:
            return cls()
        
        return cls(
            chunking=data.get("chunking") or {},
            retrieval=RetrievalConfig.from_dict(data.get("retrieval") or {}),
        )


# Default configurations for common use cases
# NOTE: "sota" is the recommended default for production (hybrid + rerank + threshold)
DEFAULT_CONFIGS = {
    "fast": RetrievalConfig(
        mode=RetrievalMode.VECTOR,
        top_k=5,
        score_threshold=0.2,
        rerank=RerankConfig(enabled=False),
        mmr=MMRConfig(enabled=False),
    ),
    "balanced": RetrievalConfig(
        mode=RetrievalMode.HYBRID,
        top_k=5,
        score_threshold=0.3,
        fusion=FusionConfig(strategy=FusionStrategy.RRF, rrf_k=60, alpha=0.6),
        rerank=RerankConfig(enabled=True, model="gte-rerank"),  # CHANGED: enable rerank by default
        mmr=MMRConfig(enabled=False),
    ),
    "accurate": RetrievalConfig(
        mode=RetrievalMode.HYBRID,
        top_k=5,
        score_threshold=0.35,
        fusion=FusionConfig(strategy=FusionStrategy.RRF, rrf_k=60, alpha=0.6),
        rerank=RerankConfig(enabled=True, model="gte-rerank"),
        mmr=MMRConfig(enabled=False),
    ),
    "diverse": RetrievalConfig(
        mode=RetrievalMode.HYBRID,
        top_k=5,
        score_threshold=0.3,
        fusion=FusionConfig(strategy=FusionStrategy.RRF, rrf_k=60, alpha=0.6),
        rerank=RerankConfig(enabled=True, model="gte-rerank"),
        mmr=MMRConfig(enabled=True, lambda_mult=0.5),
    ),
    # SOTA: State-of-the-art configuration for best accuracy
    "sota": RetrievalConfig(
        mode=RetrievalMode.HYBRID,
        top_k=5,
        score_threshold=0.3,
        fusion=FusionConfig(strategy=FusionStrategy.RRF, rrf_k=60, alpha=0.6),
        rerank=RerankConfig(enabled=True, model="gte-rerank", top_n=10),
        mmr=MMRConfig(enabled=True, lambda_mult=0.7),  # Slight diversity
    ),
    # Islamic strict: Optimized for Islamic knowledge base with multi-madhab coverage
    "islamic_strict": RetrievalConfig(
        mode=RetrievalMode.HYBRID,
        top_k=10,  # More results for multi-madhab coverage
        score_threshold=0.25,  # Lower threshold for bilingual Arabic/English content
        fusion=FusionConfig(strategy=FusionStrategy.RRF, rrf_k=60, alpha=0.6),
        rerank=RerankConfig(enabled=True, model="gte-rerank", top_n=15),
        mmr=MMRConfig(enabled=True, lambda_mult=0.6),  # Ensure madhab diversity
        islamic=IslamicEnhancementConfig(
            multi_query=True,
            citation_format=True,
            authority_sort=True,
        ),
    ),
}


def get_preset_config(name: str) -> RetrievalConfig:
    """Get a preset retrieval configuration"""
    return DEFAULT_CONFIGS.get(name, DEFAULT_CONFIGS["balanced"])
