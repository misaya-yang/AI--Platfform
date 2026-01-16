"""
Storage Services.

Provides unified interface for object storage backends (S3, OSS, local filesystem).
"""

from .image_storage import ImageStorageService, StorageBackend, StorageConfig
from .artifact_storage import (
    ArtifactStorageService,
    ArtifactInfo,
    get_artifact_storage,
    init_artifact_storage,
)

__all__ = [
    "ImageStorageService",
    "StorageBackend",
    "StorageConfig",
    "ArtifactStorageService",
    "ArtifactInfo",
    "get_artifact_storage",
    "init_artifact_storage",
]
