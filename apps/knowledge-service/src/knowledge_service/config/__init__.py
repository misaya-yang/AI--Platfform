"""Knowledge Service configuration.

All settings are read from environment variables with the ``KNOWLEDGE_`` prefix
and double-underscore nesting (e.g. ``KNOWLEDGE_QDRANT__URL``).
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------

class AppSettings(BaseModel):
    """General application settings."""

    host: str = "0.0.0.0"
    port: int = 8092
    log_level: str = "INFO"
    allow_anonymous: bool = False


class DatabaseSettings(BaseModel):
    """PostgreSQL connection pool settings."""

    dsn: str = "postgresql://localhost:5432/gateway"
    pool_min_size: int = 2
    pool_max_size: int = 10


class RedisSettings(BaseModel):
    """Optional Redis cache for retrieval results."""

    enabled: bool = False
    url: str = "redis://localhost:6379/2"


class QdrantSettings(BaseModel):
    """Qdrant vector database settings."""

    url: str = "http://localhost:6333"
    api_key: str | None = None
    prefer_grpc: bool = False
    timeout_seconds: float = 120.0
    max_retries: int = 5
    retry_base_delay: float = 2.0


class EmbeddingSettings(BaseModel):
    """Text embedding provider settings."""

    provider: str = "gemini"  # gemini | dashscope | siliconflow
    api_key: str = ""
    model: str = "gemini-embedding-001"
    dimension: int = 1024
    batch_size: int = 50
    max_concurrent: int = 5
    base_url: str | None = None
    timeout_seconds: float = 30.0


class MultimodalEmbeddingSettings(BaseModel):
    """Multimodal (image+text) embedding settings."""

    provider: str = "dashscope"
    api_key: str = ""
    model: str = "tongyi-embedding-vision-plus"
    dimension: int = 1024
    max_concurrent: int = 5


class OCRSettings(BaseModel):
    """OCR and VLM-based document recognition settings."""

    enabled: bool = True
    strategy: str = "hybrid"  # tesseract | vlm | hybrid
    languages: str = "eng+ara"
    render_dpi: int = 200
    page_concurrency: int = 3
    tesseract_timeout_seconds: int = 60
    min_text_chars_for_ocr: int = 200

    vlm_provider: str = "gemini"  # gemini | dashscope | auto
    vlm_model: str = "gemini-3-flash-preview"
    vlm_concurrency: int = 4
    vlm_timeout_seconds: int = 30
    vlm_batch_size: int = 5
    vlm_max_concurrent: int = 8
    vlm_quality_threshold: float = 0.5

    @field_validator("languages")
    @classmethod
    def validate_ocr_languages(cls, v: str) -> str:
        ALLOWED_SINGLE = frozenset(
            {
                "eng", "ara", "chi_sim", "chi_tra", "fra", "deu",
                "spa", "rus", "jpn", "kor", "por", "ita", "nld",
                "tur", "vie", "tha",
            }
        )
        ALLOWED_COMBOS = frozenset(
            {"eng+ara", "ara+eng", "chi_sim+eng", "jpn+eng", "kor+eng"}
        )
        if not v:
            return "eng+ara"
        if v in ALLOWED_COMBOS or v in ALLOWED_SINGLE:
            return v
        if "+" in v:
            parts = v.split("+")
            if all(p.strip() in ALLOWED_SINGLE for p in parts):
                return v
            raise ValueError(f"Invalid OCR language combination: {v}")
        raise ValueError(f"Invalid OCR language: {v}")


class ProcessingSettings(BaseModel):
    """Document processing and worker settings."""

    worker_concurrency: int = 2
    document_worker_concurrency: int = 3

    # Large file handling
    large_file_threshold: int = 50 * 1024 * 1024  # 50 MB
    max_memory_processing_size: int = 100 * 1024 * 1024  # 100 MB
    streaming_batch_size: int = 20
    streaming_min_batch_size: int = 5
    streaming_max_batch_size: int = 50

    # Chunking / hierarchical indexing
    hierarchical_l2_chunk_size: int = 8000
    hierarchical_l2_chunk_overlap: int = 400
    hierarchical_l3_chunk_size: int = 2000
    hierarchical_l3_chunk_overlap: int = 200
    hierarchical_l1_top_k: int = 5
    hierarchical_l2_top_k: int = 10
    hierarchical_l3_top_k: int = 5

    # Retrieval concurrency
    dataset_fanout_max_concurrency: int = 3
    retrieval_query_max_concurrency: int = 3
    retrieval_cache_ttl_seconds: int = 45

    # Document type detection
    detection_sample_pages: int = 5
    detection_native_pdf_threshold: float = 0.8
    detection_scanned_pdf_threshold: float = 0.2
    detection_min_chars_per_page: int = 50

    # Scanned PDF
    scanned_min_images_for_image_only: int = 5
    image_position_offset: int = 1_000_000

    # PDF auto-split
    pdf_split_enabled: bool = True
    pdf_split_max_size_bytes: int = 20 * 1024 * 1024  # 20 MB
    pdf_split_min_pages_per_part: int = 5


class StorageS3Settings(BaseModel):
    """S3 / S3-compatible storage settings."""

    bucket: str = ""
    region: str = "us-east-1"
    access_key: str = ""
    secret_key: str = ""
    endpoint_url: str | None = None


class StorageSettings(BaseModel):
    """Object storage for uploaded files and extracted images."""

    backend: str = "local"  # local | s3
    local_base_path: str = "./data/files"
    url_expiry_seconds: int = 3600
    key_prefix: str = "dev"
    s3: StorageS3Settings = Field(default_factory=StorageS3Settings)


# ---------------------------------------------------------------------------
# Root Settings
# ---------------------------------------------------------------------------

class Settings(BaseSettings):
    """Root settings loaded from environment variables prefixed ``KNOWLEDGE_``."""

    model_config = SettingsConfigDict(
        env_prefix="KNOWLEDGE_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    app: AppSettings = Field(default_factory=AppSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    qdrant: QdrantSettings = Field(default_factory=QdrantSettings)
    embeddings: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    multimodal: MultimodalEmbeddingSettings = Field(
        default_factory=MultimodalEmbeddingSettings,
    )
    ocr: OCRSettings = Field(default_factory=OCRSettings)
    processing: ProcessingSettings = Field(default_factory=ProcessingSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
