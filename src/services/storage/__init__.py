"""
Storage Services.

Provides unified interface for object storage backends (S3, OSS, local filesystem).
"""

from .image_storage import ImageStorageService, StorageBackend

__all__ = ["ImageStorageService", "StorageBackend"]
