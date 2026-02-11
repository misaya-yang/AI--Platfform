"""
Storage Services.

Provides unified interface for object storage backends (S3, OSS, local filesystem).
"""

from .artifact_storage import (
    ArtifactInfo,
    ArtifactStorageService,
    get_artifact_storage,
    init_artifact_storage,
)
from .file_storage import (
    FileInfo,
    FileStorageService,
    get_file_storage,
    init_file_storage,
    shutdown_file_storage,
)
from .image_storage import ImageStorageService, StorageBackend, StorageConfig

__all__ = [
    # Image storage
    "ImageStorageService",
    "StorageBackend",
    "StorageConfig",
    # Artifact storage
    "ArtifactStorageService",
    "ArtifactInfo",
    "get_artifact_storage",
    "init_artifact_storage",
    # File storage
    "FileStorageService",
    "FileInfo",
    "get_file_storage",
    "init_file_storage",
    "shutdown_file_storage",
]
