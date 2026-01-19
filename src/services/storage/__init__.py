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
from .file_storage import (
    FileStorageService,
    FileInfo,
    get_file_storage,
    init_file_storage,
    shutdown_file_storage,
)

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
