"""
Image Storage Service.

Provides unified interface for storing and retrieving images from various backends:
- S3 (Amazon Web Services)
- OSS (Alibaba Cloud Object Storage Service)
- Local filesystem (for development)

Used for storing Confluence images before multimodal embedding.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote
import unicodedata

logger = logging.getLogger(__name__)


def _sanitize_for_s3_metadata(value: str) -> str:
    """
    Sanitize a string for S3 metadata (ASCII only).

    S3 metadata can only contain ASCII characters. This function:
    1. Normalizes unicode to ASCII equivalents where possible
    2. Removes non-ASCII characters that can't be normalized
    3. Replaces special whitespace characters with regular spaces
    """
    if not value:
        return value
    # Normalize unicode (e.g., convert special spaces to regular spaces)
    normalized = unicodedata.normalize('NFKD', value)
    # Keep only ASCII characters
    ascii_str = normalized.encode('ascii', 'ignore').decode('ascii')
    # Clean up multiple spaces
    return ' '.join(ascii_str.split())


class StorageBackend(str, Enum):
    """Supported storage backends"""
    S3 = "s3"
    OSS = "oss"
    LOCAL = "local"


@dataclass
class ImageUploadParams:
    """Parameters for batch image upload"""
    tenant_id: str
    document_id: str
    attachment_id: str
    filename: str
    content: bytes
    content_type: str
    metadata: Optional[Dict[str, str]] = None


@dataclass
class StorageConfig:
    """Storage configuration"""
    backend: StorageBackend = StorageBackend.LOCAL

    # S3 configuration
    s3_bucket: str = ""
    s3_region: str = "us-east-1"
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_endpoint_url: Optional[str] = None  # For S3-compatible services

    # OSS configuration
    oss_bucket: str = ""
    oss_endpoint: str = ""
    oss_access_key: str = ""
    oss_secret_key: str = ""

    # Local storage configuration
    local_base_path: str = "./data/images"

    # Common settings
    url_expiry_seconds: int = 3600  # Pre-signed URL expiry


class BaseStorageBackend(ABC):
    """Abstract base class for storage backends"""

    @abstractmethod
    async def upload(
        self,
        key: str,
        content: bytes,
        content_type: str,
        metadata: Optional[Dict[str, str]] = None,
    ) -> str:
        """
        Upload content to storage.

        Args:
            key: Storage key (path)
            content: Binary content
            content_type: MIME type
            metadata: Optional metadata

        Returns:
            URL or key for accessing the content
        """
        pass

    @abstractmethod
    async def download(self, key: str) -> bytes:
        """
        Download content from storage.

        Args:
            key: Storage key

        Returns:
            Binary content
        """
        pass

    @abstractmethod
    async def delete(self, key: str) -> bool:
        """
        Delete content from storage.

        Args:
            key: Storage key

        Returns:
            True if deleted, False otherwise
        """
        pass

    @abstractmethod
    async def delete_prefix(self, prefix: str) -> int:
        """
        Delete all objects with a given prefix.

        Args:
            prefix: Key prefix

        Returns:
            Number of deleted objects
        """
        pass

    @abstractmethod
    async def exists(self, key: str) -> bool:
        """
        Check if object exists.

        Args:
            key: Storage key

        Returns:
            True if exists
        """
        pass

    @abstractmethod
    def get_url(self, key: str, expiry_seconds: int = 3600) -> str:
        """
        Get URL for accessing the content.

        Args:
            key: Storage key
            expiry_seconds: URL expiry time for pre-signed URLs

        Returns:
            URL for accessing the content
        """
        pass

    async def generate_presigned_upload_url(
        self,
        key: str,
        content_type: str,
        expiry_seconds: int = 900,
        metadata: Optional[Dict[str, str]] = None,
    ) -> Optional[Dict[str, str]]:
        """
        Generate a presigned URL for direct upload (client-side upload).

        This allows clients to upload directly to storage without going through
        the backend, reducing server load and enabling larger file uploads.

        Args:
            key: Storage key (path) where the file will be stored
            content_type: Expected MIME type of the file
            expiry_seconds: URL expiry time in seconds (default 15 minutes)
            metadata: Optional metadata to attach to the object

        Returns:
            Dictionary with 'url' and optional 'fields' for POST/PUT upload,
            or None if presigned URLs are not supported by this backend.
        """
        return None  # Default: not supported

    async def close(self) -> None:
        """
        Close and cleanup resources.

        Override in subclasses that need cleanup.
        """
        pass


class LocalStorageBackend(BaseStorageBackend):
    """Local filesystem storage backend for development"""

    def __init__(self, base_path: str):
        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)

    def _get_full_path(self, key: str) -> Path:
        """Get full path for a key"""
        return self.base_path / key

    async def upload(
        self,
        key: str,
        content: bytes,
        content_type: str,
        metadata: Optional[Dict[str, str]] = None,
    ) -> str:
        full_path = self._get_full_path(key)
        full_path.parent.mkdir(parents=True, exist_ok=True)

        # Write content
        await asyncio.to_thread(full_path.write_bytes, content)

        # Store metadata in a sidecar file
        if metadata:
            import json
            meta_path = full_path.with_suffix(full_path.suffix + ".meta")
            await asyncio.to_thread(
                meta_path.write_text,
                json.dumps({"content_type": content_type, **metadata})
            )

        logger.debug(f"Uploaded {len(content)} bytes to local: {key}")
        return f"file://{full_path.absolute()}"

    async def download(self, key: str) -> bytes:
        full_path = self._get_full_path(key)
        if not full_path.exists():
            raise FileNotFoundError(f"File not found: {key}")
        return await asyncio.to_thread(full_path.read_bytes)

    async def delete(self, key: str) -> bool:
        full_path = self._get_full_path(key)
        if full_path.exists():
            await asyncio.to_thread(full_path.unlink)
            # Also delete metadata file if exists
            meta_path = full_path.with_suffix(full_path.suffix + ".meta")
            if meta_path.exists():
                await asyncio.to_thread(meta_path.unlink)
            return True
        return False

    async def delete_prefix(self, prefix: str) -> int:
        prefix_path = self._get_full_path(prefix)

        # If prefix ends with /, treat it as a directory and delete all files inside
        if prefix.endswith("/"):
            if not prefix_path.exists():
                return 0
            deleted = 0
            for path in prefix_path.rglob("*"):
                if path.is_file():
                    await asyncio.to_thread(path.unlink)
                    # Don't count .meta sidecar files
                    if not path.name.endswith(".meta"):
                        deleted += 1
            return deleted

        # Otherwise, match files starting with the prefix name
        if not prefix_path.parent.exists():
            return 0

        deleted = 0
        for path in prefix_path.parent.glob(f"{prefix_path.name}*"):
            if path.is_file():
                await asyncio.to_thread(path.unlink)
                # Don't count .meta sidecar files
                if not path.name.endswith(".meta"):
                    deleted += 1
        return deleted

    async def exists(self, key: str) -> bool:
        return self._get_full_path(key).exists()

    def get_url(self, key: str, expiry_seconds: int = 3600) -> str:
        full_path = self._get_full_path(key)
        return f"file://{full_path.absolute()}"


class S3StorageBackend(BaseStorageBackend):
    """Amazon S3 storage backend"""

    def __init__(
        self,
        bucket: str,
        region: str,
        access_key: str,
        secret_key: str,
        endpoint_url: Optional[str] = None,
    ):
        self.bucket = bucket
        self.region = region
        self.access_key = access_key
        self.secret_key = secret_key
        self.endpoint_url = endpoint_url
        self._client = None
        self._client_context = None

    async def _get_client(self):
        """Get or create S3 client"""
        if self._client is None:
            try:
                import aioboto3
            except ImportError:
                raise ImportError("aioboto3 is required for S3 storage. Install with: pip install aioboto3")

            session = aioboto3.Session()
            self._client_context = session.client(
                "s3",
                region_name=self.region,
                aws_access_key_id=self.access_key,
                aws_secret_access_key=self.secret_key,
                endpoint_url=self.endpoint_url,
            )
            self._client = await self._client_context.__aenter__()
        return self._client

    async def close(self):
        """Close the S3 client and release resources"""
        if self._client_context is not None:
            try:
                await self._client_context.__aexit__(None, None, None)
            except Exception as e:
                logger.warning(f"Error closing S3 client: {e}")
            finally:
                self._client = None
                self._client_context = None

    async def upload(
        self,
        key: str,
        content: bytes,
        content_type: str,
        metadata: Optional[Dict[str, str]] = None,
    ) -> str:
        client = await self._get_client()
        extra_args = {"ContentType": content_type}
        if metadata:
            extra_args["Metadata"] = metadata

        await client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=content,
            **extra_args,
        )

        logger.debug(f"Uploaded {len(content)} bytes to S3: {key}")
        return self.get_url(key)

    async def download(self, key: str) -> bytes:
        client = await self._get_client()
        response = await client.get_object(Bucket=self.bucket, Key=key)
        async with response["Body"] as stream:
            return await stream.read()

    async def delete(self, key: str) -> bool:
        client = await self._get_client()
        try:
            await client.delete_object(Bucket=self.bucket, Key=key)
            return True
        except Exception as e:
            logger.warning(f"Failed to delete {key}: {e}")
            return False

    async def delete_prefix(self, prefix: str) -> int:
        client = await self._get_client()
        deleted = 0

        paginator = client.get_paginator("list_objects_v2")
        async for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            objects = page.get("Contents", [])
            if not objects:
                continue

            delete_keys = [{"Key": obj["Key"]} for obj in objects]
            await client.delete_objects(
                Bucket=self.bucket,
                Delete={"Objects": delete_keys},
            )
            deleted += len(delete_keys)

        return deleted

    async def exists(self, key: str) -> bool:
        client = await self._get_client()
        try:
            await client.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:
            return False

    def get_url(self, key: str, expiry_seconds: int = 3600) -> str:
        """Get S3 URL (non-presigned for now, can be enhanced)"""
        if self.endpoint_url:
            return f"{self.endpoint_url}/{self.bucket}/{key}"
        return f"https://{self.bucket}.s3.{self.region}.amazonaws.com/{key}"

    async def generate_presigned_upload_url(
        self,
        key: str,
        content_type: str,
        expiry_seconds: int = 900,
        metadata: Optional[Dict[str, str]] = None,
    ) -> Optional[Dict[str, str]]:
        """
        Generate a presigned URL for direct upload to S3.

        Uses PUT method for simple uploads. For multipart uploads with additional
        form fields, use generate_presigned_post instead.

        Args:
            key: Storage key (path) where the file will be stored
            content_type: Expected MIME type of the file
            expiry_seconds: URL expiry time in seconds (default 15 minutes)
            metadata: Optional metadata to attach to the object

        Returns:
            Dictionary with 'url', 'method', and 'headers' for PUT upload
        """
        client = await self._get_client()

        # Build params for presigned URL
        params = {
            "Bucket": self.bucket,
            "Key": key,
            "ContentType": content_type,
        }

        # Add metadata if provided
        if metadata:
            params["Metadata"] = {
                _sanitize_for_s3_metadata(k): _sanitize_for_s3_metadata(v)
                for k, v in metadata.items()
            }

        try:
            # Generate presigned PUT URL
            presigned_url = await client.generate_presigned_url(
                "put_object",
                Params=params,
                ExpiresIn=expiry_seconds,
            )

            return {
                "url": presigned_url,
                "method": "PUT",
                "headers": {
                    "Content-Type": content_type,
                },
                "key": key,
                "bucket": self.bucket,
                "expiry_seconds": expiry_seconds,
            }
        except Exception as e:
            logger.error(f"Failed to generate presigned URL for {key}: {e}")
            return None

    async def generate_presigned_download_url(
        self,
        key: str,
        expiry_seconds: int = 3600,
        filename: Optional[str] = None,
    ) -> Optional[str]:
        """
        Generate a presigned URL for downloading from S3.

        Args:
            key: Storage key
            expiry_seconds: URL expiry time in seconds
            filename: Optional filename for Content-Disposition header

        Returns:
            Presigned download URL or None if failed
        """
        client = await self._get_client()

        params = {
            "Bucket": self.bucket,
            "Key": key,
        }

        if filename:
            params["ResponseContentDisposition"] = f'attachment; filename="{filename}"'

        try:
            presigned_url = await client.generate_presigned_url(
                "get_object",
                Params=params,
                ExpiresIn=expiry_seconds,
            )
            return presigned_url
        except Exception as e:
            logger.error(f"Failed to generate presigned download URL for {key}: {e}")
            return None


class OSSStorageBackend(BaseStorageBackend):
    """Alibaba Cloud OSS storage backend"""

    def __init__(
        self,
        bucket: str,
        endpoint: str,
        access_key: str,
        secret_key: str,
    ):
        self.bucket_name = bucket
        self.endpoint = endpoint
        self.access_key = access_key
        self.secret_key = secret_key
        self._bucket = None

    def _get_bucket(self):
        """Get OSS bucket instance"""
        if self._bucket is None:
            try:
                import oss2
            except ImportError:
                raise ImportError("oss2 is required for OSS storage. Install with: pip install oss2")

            auth = oss2.Auth(self.access_key, self.secret_key)
            self._bucket = oss2.Bucket(auth, self.endpoint, self.bucket_name)
        return self._bucket

    async def upload(
        self,
        key: str,
        content: bytes,
        content_type: str,
        metadata: Optional[Dict[str, str]] = None,
    ) -> str:
        bucket = self._get_bucket()

        headers = {"Content-Type": content_type}
        if metadata:
            for k, v in metadata.items():
                headers[f"x-oss-meta-{k}"] = v

        await asyncio.to_thread(bucket.put_object, key, content, headers=headers)

        logger.debug(f"Uploaded {len(content)} bytes to OSS: {key}")
        return self.get_url(key)

    async def download(self, key: str) -> bytes:
        bucket = self._get_bucket()
        result = await asyncio.to_thread(bucket.get_object, key)
        return await asyncio.to_thread(result.read)

    async def delete(self, key: str) -> bool:
        bucket = self._get_bucket()
        try:
            await asyncio.to_thread(bucket.delete_object, key)
            return True
        except Exception as e:
            logger.warning(f"Failed to delete {key}: {e}")
            return False

    async def delete_prefix(self, prefix: str) -> int:
        bucket = self._get_bucket()
        deleted = 0

        # Import oss2 here since it's lazy-loaded
        try:
            import oss2
        except ImportError:
            raise ImportError("oss2 is required for OSS storage. Install with: pip install oss2")

        def list_objects():
            return list(oss2.ObjectIterator(bucket, prefix=prefix))

        for obj in await asyncio.to_thread(list_objects):
            await asyncio.to_thread(bucket.delete_object, obj.key)
            deleted += 1

        return deleted

    async def exists(self, key: str) -> bool:
        bucket = self._get_bucket()
        return await asyncio.to_thread(bucket.object_exists, key)

    def get_url(self, key: str, expiry_seconds: int = 3600) -> str:
        """Get OSS URL"""
        # Public URL (for public buckets or pre-signed URLs)
        return f"https://{self.bucket_name}.{self.endpoint}/{quote(key)}"


class ImageStorageService:
    """
    High-level image storage service.

    Provides storage operations specifically for Confluence images.
    """

    def __init__(self, config: StorageConfig):
        self.config = config
        self._backend = self._create_backend()

    def _create_backend(self) -> BaseStorageBackend:
        """Create storage backend based on configuration"""
        if self.config.backend == StorageBackend.S3:
            return S3StorageBackend(
                bucket=self.config.s3_bucket,
                region=self.config.s3_region,
                access_key=self.config.s3_access_key,
                secret_key=self.config.s3_secret_key,
                endpoint_url=self.config.s3_endpoint_url,
            )
        elif self.config.backend == StorageBackend.OSS:
            return OSSStorageBackend(
                bucket=self.config.oss_bucket,
                endpoint=self.config.oss_endpoint,
                access_key=self.config.oss_access_key,
                secret_key=self.config.oss_secret_key,
            )
        else:
            return LocalStorageBackend(self.config.local_base_path)

    @staticmethod
    def _generate_key(
        tenant_id: str,
        document_id: str,
        attachment_id: str,
        filename: str,
    ) -> str:
        """
        Generate storage key for an image.

        Structure: knowledge/confluence/{tenant_id}/{document_id}/images/{attachment_id}_{filename}
        """
        # Sanitize filename
        safe_filename = "".join(c for c in filename if c.isalnum() or c in "._-")
        return f"knowledge/confluence/{tenant_id}/{document_id}/images/{attachment_id}_{safe_filename}"

    async def upload_image(
        self,
        tenant_id: str,
        document_id: str,
        attachment_id: str,
        filename: str,
        content: bytes,
        content_type: str,
        metadata: Optional[Dict[str, str]] = None,
    ) -> str:
        """
        Upload an image to storage.

        Args:
            tenant_id: Tenant ID
            document_id: Document ID
            attachment_id: Confluence attachment ID
            filename: Original filename
            content: Image binary content
            content_type: MIME type (e.g., "image/png")
            metadata: Optional metadata

        Returns:
            Storage URL
        """
        key = self._generate_key(tenant_id, document_id, attachment_id, filename)

        # Add image-specific metadata
        image_metadata = {
            "tenant_id": tenant_id,
            "document_id": document_id,
            "attachment_id": attachment_id,
            "original_filename": _sanitize_for_s3_metadata(filename),
            "content_hash": hashlib.md5(content).hexdigest(),
        }
        if metadata:
            # Sanitize all incoming metadata values to ensure S3 compatibility
            sanitized_metadata = {
                k: _sanitize_for_s3_metadata(str(v)) if isinstance(v, str) else str(v)
                for k, v in metadata.items()
            }
            image_metadata.update(sanitized_metadata)

        url = await self._backend.upload(key, content, content_type, image_metadata)
        logger.info(f"Uploaded image {filename} ({len(content)} bytes) -> {key}")
        return url

    async def upload_images_batch(
        self,
        images: List[ImageUploadParams],
        max_concurrent: int = 10,
    ) -> List[str]:
        """
        Upload multiple images concurrently with rate limiting.

        Args:
            images: List of ImageUploadParams for batch upload
            max_concurrent: Maximum concurrent uploads (default: 10)

        Returns:
            List of storage URLs for each uploaded image
        """
        if not images:
            return []

        semaphore = asyncio.Semaphore(max_concurrent)

        async def upload_with_limit(params: ImageUploadParams) -> str:
            """Upload a single image with semaphore-based rate limiting."""
            async with semaphore:
                return await self.upload_image(
                    tenant_id=params.tenant_id,
                    document_id=params.document_id,
                    attachment_id=params.attachment_id,
                    filename=params.filename,
                    content=params.content,
                    content_type=params.content_type,
                    metadata=params.metadata,
                )

        # Launch all uploads concurrently (limited by semaphore)
        tasks = [upload_with_limit(params) for params in images]
        urls = await asyncio.gather(*tasks)

        logger.info(f"Batch uploaded {len(urls)} images (max_concurrent={max_concurrent})")
        return list(urls)

    async def generate_presigned_upload_url(
        self,
        tenant_id: str,
        document_id: str,
        attachment_id: str,
        filename: str,
        content_type: str,
        expiry_seconds: int = 900,
        metadata: Optional[Dict[str, str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Generate a presigned URL for direct upload to storage.

        This allows frontend to upload directly to S3/OSS without going through
        the backend, reducing server load and enabling larger file uploads.

        Args:
            tenant_id: Tenant ID
            document_id: Document ID
            attachment_id: Attachment ID
            filename: Original filename
            content_type: MIME type of the file
            expiry_seconds: URL expiry time (default 15 minutes)
            metadata: Optional metadata to attach

        Returns:
            Dictionary with upload URL and instructions, or None if not supported
        """
        key = self._generate_key(tenant_id, document_id, attachment_id, filename)

        # Add standard metadata
        full_metadata = {
            "tenant-id": tenant_id,
            "document-id": document_id,
            "attachment-id": attachment_id,
            "original-filename": filename,
        }
        if metadata:
            full_metadata.update(metadata)

        result = await self._backend.generate_presigned_upload_url(
            key=key,
            content_type=content_type,
            expiry_seconds=expiry_seconds,
            metadata=full_metadata,
        )

        if result:
            result["storage_key"] = key
            result["filename"] = filename
            logger.info(f"Generated presigned upload URL for {key} (expires in {expiry_seconds}s)")

        return result

    def supports_presigned_urls(self) -> bool:
        """Check if the current backend supports presigned URLs.

        Note: OSS presigned URLs are not implemented yet.
        Only S3 backend supports presigned uploads currently.
        """
        # Only S3 has presigned upload implemented
        # OSS support can be added by implementing generate_presigned_upload_url in OSSBackend
        return self.config.backend == StorageBackend.S3

    async def download_image(
        self,
        tenant_id: str,
        document_id: str,
        attachment_id: str,
        filename: str,
    ) -> bytes:
        """
        Download an image from storage.

        Args:
            tenant_id: Tenant ID
            document_id: Document ID
            attachment_id: Confluence attachment ID
            filename: Original filename

        Returns:
            Image binary content
        """
        key = self._generate_key(tenant_id, document_id, attachment_id, filename)
        return await self._backend.download(key)

    async def delete_image(
        self,
        tenant_id: str,
        document_id: str,
        attachment_id: str,
        filename: str,
    ) -> bool:
        """
        Delete an image from storage.

        Args:
            tenant_id: Tenant ID
            document_id: Document ID
            attachment_id: Confluence attachment ID
            filename: Original filename

        Returns:
            True if deleted successfully
        """
        key = self._generate_key(tenant_id, document_id, attachment_id, filename)
        return await self._backend.delete(key)

    async def delete_document_images(
        self,
        tenant_id: str,
        document_id: str,
    ) -> int:
        """
        Delete all images for a document.

        Args:
            tenant_id: Tenant ID
            document_id: Document ID

        Returns:
            Number of deleted images
        """
        prefix = f"knowledge/confluence/{tenant_id}/{document_id}/images/"
        deleted = await self._backend.delete_prefix(prefix)
        logger.info(f"Deleted {deleted} images for document {document_id}")
        return deleted

    async def image_exists(
        self,
        tenant_id: str,
        document_id: str,
        attachment_id: str,
        filename: str,
    ) -> bool:
        """
        Check if an image exists in storage.

        Args:
            tenant_id: Tenant ID
            document_id: Document ID
            attachment_id: Confluence attachment ID
            filename: Original filename

        Returns:
            True if image exists
        """
        key = self._generate_key(tenant_id, document_id, attachment_id, filename)
        return await self._backend.exists(key)

    def get_image_url(
        self,
        tenant_id: str,
        document_id: str,
        attachment_id: str,
        filename: str,
        expiry_seconds: int = 3600,
    ) -> str:
        """
        Get URL for accessing an image.

        Args:
            tenant_id: Tenant ID
            document_id: Document ID
            attachment_id: Confluence attachment ID
            filename: Original filename
            expiry_seconds: URL expiry time

        Returns:
            Image URL
        """
        key = self._generate_key(tenant_id, document_id, attachment_id, filename)
        return self._backend.get_url(key, expiry_seconds)

    async def exists_by_key(self, storage_key: str) -> bool:
        """
        Check if a file exists by its storage key.

        This is useful for verifying presigned URL uploads where
        we have the full storage key but not the individual components.

        Args:
            storage_key: Full storage key (path)

        Returns:
            True if file exists
        """
        return await self._backend.exists(storage_key)

    def get_url_by_key(self, storage_key: str, expiry_seconds: int = 3600) -> str:
        """
        Get URL for a file by its storage key.

        This is useful for presigned URL uploads where we have
        the full storage key but not the individual components.

        Args:
            storage_key: Full storage key (path)
            expiry_seconds: URL expiry time

        Returns:
            File URL
        """
        return self._backend.get_url(storage_key, expiry_seconds)

    async def close(self) -> None:
        """Close the storage service and release resources"""
        if self._backend is not None:
            await self._backend.close()

    async def __aenter__(self) -> "ImageStorageService":
        """Async context manager entry"""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit - cleanup resources"""
        await self.close()
