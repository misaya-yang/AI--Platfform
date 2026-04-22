"""Artifact storage backends.

Three options live here, controlled by env / constructor:

  * LocalArtifactStore  — filesystem-backed; default for dev.
  * S3ArtifactStore     — any S3-compatible store (MinIO, AWS, R2, OSS).
  * MemoryArtifactStore — in-process, for unit tests.

All three expose the same :class:`ArtifactStore` Protocol.
"""

from .base import Artifact, ArtifactStore, ArtifactStoreError
from .local import LocalArtifactStore
from .memory import MemoryArtifactStore
from .s3 import S3ArtifactStore

__all__ = [
    "Artifact",
    "ArtifactStore",
    "ArtifactStoreError",
    "LocalArtifactStore",
    "MemoryArtifactStore",
    "S3ArtifactStore",
]
