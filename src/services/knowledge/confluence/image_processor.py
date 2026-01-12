"""
Confluence Image Processor.

Handles downloading, storing, and embedding images from Confluence pages.
Integrates with storage service (S3/OSS), DashScope multimodal embedding,
and VLM service for generating image descriptions.

Key features:
- Download images from Confluence attachments
- Store images in S3/OSS/local storage
- Generate multimodal embeddings for image-text retrieval
- Generate VLM descriptions for enhanced RAG retrieval (hybrid approach)
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from .models import ConfluenceAttachment, ImageSegment
from .parser import ImageReference, extract_embeddable_images

if TYPE_CHECKING:
    from .client import ConfluenceClient
    from ...storage.image_storage import ImageStorageService
    from ..embedding import DashScopeMultimodalEmbedding
    from ..vlm_service import DashScopeVLMService

logger = logging.getLogger(__name__)


# Max image size for DashScope multimodal API (3MB)
MAX_IMAGE_SIZE_BYTES = 3 * 1024 * 1024


@dataclass
class ImageProcessingResult:
    """Result of processing images from a Confluence page."""

    page_id: str
    document_id: str
    total_images: int
    processed_images: int
    skipped_images: int
    failed_images: int
    segments: List[ImageSegment]
    errors: List[str]

    @property
    def success_rate(self) -> float:
        if self.total_images == 0:
            return 1.0
        return self.processed_images / self.total_images


class ConfluenceImageProcessor:
    """
    Confluence 图片处理器

    负责从 Confluence 页面下载图片、存储到对象存储、生成多模态嵌入和 VLM 描述。

    Workflow:
    1. Get page attachments from Confluence API
    2. Filter for embeddable image types (JPEG, PNG, etc.)
    3. Download images (respecting size limits)
    4. Upload to storage (S3/OSS/local)
    5. Generate VLM descriptions for RAG retrieval (if VLM service enabled)
    6. Generate multimodal embeddings via DashScope (if embedding enabled)
    7. Return ImageSegment objects for vector indexing

    Hybrid RAG Strategy:
    - VLM descriptions provide searchable text for text-based retrieval
    - Multimodal embeddings enable direct image-text similarity search
    - Both approaches complement each other for best retrieval quality
    """

    def __init__(
        self,
        confluence_client: "ConfluenceClient",
        storage_service: "ImageStorageService",
        multimodal_embedding: Optional["DashScopeMultimodalEmbedding"] = None,
        vlm_service: Optional["DashScopeVLMService"] = None,
        max_image_size: int = MAX_IMAGE_SIZE_BYTES,
        max_images_per_page: int = 50,
        generate_vlm_descriptions: bool = True,
        max_concurrent_vlm: int = 5,
        max_concurrent_upload: int = 10,
        max_concurrent_embed: int = 5,
    ):
        """
        初始化图片处理器

        Args:
            confluence_client: Confluence API 客户端
            storage_service: 图片存储服务
            multimodal_embedding: 多模态嵌入服务 (可选)
            vlm_service: VLM 图片描述服务 (可选)
            max_image_size: 单张图片最大大小 (bytes)
            max_images_per_page: 每页最大处理图片数
            generate_vlm_descriptions: 是否生成 VLM 描述
            max_concurrent_vlm: VLM 最大并发数 (默认5)
            max_concurrent_upload: 存储上传最大并发数 (默认10)
            max_concurrent_embed: Embedding 最大并发数 (默认5)
        """
        self.client = confluence_client
        self.storage = storage_service
        self.embedding = multimodal_embedding
        self.vlm_service = vlm_service
        self.max_image_size = max_image_size
        self.max_images_per_page = max_images_per_page
        self.generate_vlm_descriptions = generate_vlm_descriptions
        self.max_concurrent_vlm = max_concurrent_vlm
        self.max_concurrent_upload = max_concurrent_upload
        self.max_concurrent_embed = max_concurrent_embed

        if vlm_service:
            logger.info(
                f"VLM service enabled for image description generation "
                f"(max_concurrent={max_concurrent_vlm})"
            )

    async def process_page_images(
        self,
        page_id: str,
        document_id: str,
        tenant_id: str,
        page_content: Optional[str] = None,
        page_title: Optional[str] = None,
        generate_embeddings: bool = True,
    ) -> ImageProcessingResult:
        """
        处理页面中的图片

        Args:
            page_id: Confluence 页面 ID
            document_id: 知识库文档 ID
            tenant_id: 租户 ID
            page_content: 页面内容 (用于提取图片上下文)
            page_title: 页面标题 (用于 VLM 上下文)
            generate_embeddings: 是否生成多模态嵌入

        Returns:
            ImageProcessingResult 包含处理结果
        """
        errors: List[str] = []
        segments: List[ImageSegment] = []
        skipped = 0
        failed = 0

        try:
            # 1. Get image attachments from Confluence
            attachments = await self.client.get_page_image_attachments(
                page_id=page_id,
                embeddable_only=True
            )

            # 2. Limit number of images
            if len(attachments) > self.max_images_per_page:
                logger.warning(
                    f"Page {page_id} has {len(attachments)} images, "
                    f"limiting to {self.max_images_per_page}"
                )
                attachments = attachments[:self.max_images_per_page]

            total_images = len(attachments)

            if total_images == 0:
                logger.debug(f"No embeddable images found in page {page_id}")
                return ImageProcessingResult(
                    page_id=page_id,
                    document_id=document_id,
                    total_images=0,
                    processed_images=0,
                    skipped_images=0,
                    failed_images=0,
                    segments=[],
                    errors=[],
                )

            # 3. Extract image references from content for context
            image_contexts: Dict[str, str] = {}
            if page_content:
                image_refs = extract_embeddable_images(page_content)
                for ref in image_refs:
                    image_contexts[ref.filename] = ref.context_text or ""

            # 4. Process images concurrently with semaphore-based rate limiting
            vlm_semaphore = asyncio.Semaphore(self.max_concurrent_vlm)
            upload_semaphore = asyncio.Semaphore(self.max_concurrent_upload)
            embed_semaphore = asyncio.Semaphore(self.max_concurrent_embed)

            async def process_with_error_handling(
                attachment: ConfluenceAttachment,
            ) -> Tuple[Optional[ImageSegment], Optional[str]]:
                """Process single image with error handling."""
                try:
                    segment = await self._process_single_image_concurrent(
                        attachment=attachment,
                        document_id=document_id,
                        tenant_id=tenant_id,
                        context=image_contexts.get(attachment.filename, ""),
                        generate_embedding=generate_embeddings,
                        page_title=page_title,
                        vlm_semaphore=vlm_semaphore,
                        upload_semaphore=upload_semaphore,
                        embed_semaphore=embed_semaphore,
                    )
                    return segment, None
                except Exception as e:
                    error_msg = f"Failed to process image {attachment.filename}: {e}"
                    logger.error(error_msg)
                    return None, error_msg

            # Launch all image processing tasks concurrently
            tasks = [process_with_error_handling(att) for att in attachments]
            results = await asyncio.gather(*tasks)

            # Collect results
            for segment, error in results:
                if error:
                    failed += 1
                    errors.append(error)
                elif segment:
                    segments.append(segment)
                else:
                    skipped += 1

            processed = len(segments)
            vlm_count = sum(1 for s in segments if s.vlm_description)
            logger.info(
                f"Processed {processed}/{total_images} images for page {page_id} "
                f"(skipped={skipped}, failed={failed}, vlm_descriptions={vlm_count})"
            )

            return ImageProcessingResult(
                page_id=page_id,
                document_id=document_id,
                total_images=total_images,
                processed_images=processed,
                skipped_images=skipped,
                failed_images=failed,
                segments=segments,
                errors=errors,
            )

        except Exception as e:
            error_msg = f"Failed to process images for page {page_id}: {e}"
            logger.error(error_msg)
            return ImageProcessingResult(
                page_id=page_id,
                document_id=document_id,
                total_images=0,
                processed_images=0,
                skipped_images=0,
                failed_images=1,
                segments=[],
                errors=[error_msg],
            )

    async def _process_single_image(
        self,
        attachment: ConfluenceAttachment,
        document_id: str,
        tenant_id: str,
        context: str = "",
        generate_embedding: bool = True,
        page_title: Optional[str] = None,
    ) -> Optional[ImageSegment]:
        """
        处理单张图片

        Args:
            attachment: Confluence 附件信息
            document_id: 文档 ID
            tenant_id: 租户 ID
            context: 图片上下文文本
            generate_embedding: 是否生成嵌入
            page_title: 页面标题（用于 VLM 上下文）

        Returns:
            ImageSegment 或 None (如果跳过)
        """
        # Check file size
        if attachment.file_size > self.max_image_size:
            logger.info(
                f"Skipping image {attachment.filename}: "
                f"size {attachment.file_size} exceeds limit {self.max_image_size}"
            )
            return None

        # Download image
        logger.debug(f"Downloading image: {attachment.filename}")
        image_bytes = await self.client.download_attachment(attachment)

        if not image_bytes:
            logger.warning(f"Empty image data for {attachment.filename}")
            return None

        # Double-check actual size
        if len(image_bytes) > self.max_image_size:
            logger.info(
                f"Skipping image {attachment.filename}: "
                f"actual size {len(image_bytes)} exceeds limit"
            )
            return None

        # Generate segment ID
        segment_id = str(uuid.uuid4())

        # Upload to storage
        storage_url = await self.storage.upload_image(
            tenant_id=tenant_id,
            document_id=document_id,
            attachment_id=attachment.attachment_id,
            filename=attachment.filename,
            content=image_bytes,
            content_type=attachment.media_type,
            metadata={
                "confluence_attachment_id": attachment.attachment_id,
                "page_id": attachment.page_id,
                "original_filename": attachment.filename,
            },
        )

        # Generate VLM description if service is available
        vlm_description: Optional[str] = None
        if self.generate_vlm_descriptions and self.vlm_service:
            try:
                # Detect image type for better prompts
                image_type = self._detect_image_type(attachment.filename)

                # Build context for VLM
                vlm_context = page_title or ""
                if context:
                    vlm_context = f"{vlm_context} - {context}" if vlm_context else context

                description_result = await self.vlm_service.describe_image(
                    image_bytes=image_bytes,
                    image_type=image_type,
                    context=vlm_context,
                )
                vlm_description = description_result.description
                logger.debug(
                    f"Generated VLM description for {attachment.filename}: "
                    f"{len(vlm_description)} chars, {description_result.tokens_used} tokens"
                )
            except Exception as e:
                logger.warning(f"Failed to generate VLM description for {attachment.filename}: {e}")
                # Continue without VLM description - image still stored

        # Generate embedding if requested
        vector_id: Optional[str] = None
        embedding_vector: Optional[List[float]] = None

        if generate_embedding and self.embedding:
            try:
                # Use image + VLM description + context for combined embedding
                combined_text = ""
                if vlm_description:
                    combined_text = vlm_description
                if context:
                    combined_text = f"{combined_text}\n{context}" if combined_text else context

                if combined_text:
                    embedding_vector = await self.embedding.embed_image_and_text(
                        image_bytes=image_bytes,
                        text=combined_text,
                    )
                else:
                    vectors = await self.embedding.embed_images([image_bytes])
                    if vectors:
                        embedding_vector = vectors[0]

                if embedding_vector:
                    vector_id = segment_id  # Will be set when stored in Qdrant
                    logger.debug(
                        f"Generated embedding for {attachment.filename}, "
                        f"dimension={len(embedding_vector)}"
                    )

            except Exception as e:
                logger.warning(f"Failed to generate embedding for {attachment.filename}: {e}")
                # Continue without embedding - image still stored

        # Create segment with VLM description and attachment metadata for change detection
        segment = ImageSegment(
            segment_id=segment_id,
            document_id=document_id,
            attachment_id=attachment.attachment_id,
            filename=attachment.filename,
            media_type=attachment.media_type,
            file_size=len(image_bytes),
            storage_url=storage_url,
            vector_id=vector_id,
            context_text=context,
            vlm_description=vlm_description,
            embedding=embedding_vector,
            metadata={
                "confluence_attachment_id": attachment.attachment_id,
                "page_id": attachment.page_id,
                "attachment_updated_at": attachment.updated_at,  # For change detection
            },
        )

        logger.debug(f"Created image segment: {segment_id} for {attachment.filename}")
        return segment

    async def _process_single_image_concurrent(
        self,
        attachment: ConfluenceAttachment,
        document_id: str,
        tenant_id: str,
        context: str = "",
        generate_embedding: bool = True,
        page_title: Optional[str] = None,
        vlm_semaphore: Optional[asyncio.Semaphore] = None,
        upload_semaphore: Optional[asyncio.Semaphore] = None,
        embed_semaphore: Optional[asyncio.Semaphore] = None,
    ) -> Optional[ImageSegment]:
        """
        处理单张图片（并发版本，带信号量控制）

        Args:
            attachment: Confluence 附件信息
            document_id: 文档 ID
            tenant_id: 租户 ID
            context: 图片上下文文本
            generate_embedding: 是否生成嵌入
            page_title: 页面标题（用于 VLM 上下文）
            vlm_semaphore: VLM 调用信号量
            upload_semaphore: 上传操作信号量
            embed_semaphore: Embedding 操作信号量

        Returns:
            ImageSegment 或 None (如果跳过)
        """
        # Check file size
        if attachment.file_size > self.max_image_size:
            logger.info(
                f"Skipping image {attachment.filename}: "
                f"size {attachment.file_size} exceeds limit {self.max_image_size}"
            )
            return None

        # Download image (no semaphore needed, usually fast)
        logger.debug(f"Downloading image: {attachment.filename}")
        image_bytes = await self.client.download_attachment(attachment)

        if not image_bytes:
            logger.warning(f"Empty image data for {attachment.filename}")
            return None

        # Double-check actual size
        if len(image_bytes) > self.max_image_size:
            logger.info(
                f"Skipping image {attachment.filename}: "
                f"actual size {len(image_bytes)} exceeds limit"
            )
            return None

        # Generate segment ID
        segment_id = str(uuid.uuid4())

        # Upload to storage with semaphore
        async def upload_with_limit():
            if upload_semaphore:
                async with upload_semaphore:
                    return await self.storage.upload_image(
                        tenant_id=tenant_id,
                        document_id=document_id,
                        attachment_id=attachment.attachment_id,
                        filename=attachment.filename,
                        content=image_bytes,
                        content_type=attachment.media_type,
                        metadata={
                            "confluence_attachment_id": attachment.attachment_id,
                            "page_id": attachment.page_id,
                            "original_filename": attachment.filename,
                        },
                    )
            else:
                return await self.storage.upload_image(
                    tenant_id=tenant_id,
                    document_id=document_id,
                    attachment_id=attachment.attachment_id,
                    filename=attachment.filename,
                    content=image_bytes,
                    content_type=attachment.media_type,
                    metadata={
                        "confluence_attachment_id": attachment.attachment_id,
                        "page_id": attachment.page_id,
                        "original_filename": attachment.filename,
                    },
                )

        storage_url = await upload_with_limit()

        # Generate VLM description with semaphore
        vlm_description: Optional[str] = None
        if self.generate_vlm_descriptions and self.vlm_service:
            async def vlm_with_limit():
                if vlm_semaphore:
                    async with vlm_semaphore:
                        image_type = self._detect_image_type(attachment.filename)
                        vlm_context = page_title or ""
                        if context:
                            vlm_context = f"{vlm_context} - {context}" if vlm_context else context
                        return await self.vlm_service.describe_image(
                            image_bytes=image_bytes,
                            image_type=image_type,
                            context=vlm_context,
                        )
                else:
                    image_type = self._detect_image_type(attachment.filename)
                    vlm_context = page_title or ""
                    if context:
                        vlm_context = f"{vlm_context} - {context}" if vlm_context else context
                    return await self.vlm_service.describe_image(
                        image_bytes=image_bytes,
                        image_type=image_type,
                        context=vlm_context,
                    )

            try:
                description_result = await vlm_with_limit()
                vlm_description = description_result.description
                logger.debug(
                    f"Generated VLM description for {attachment.filename}: "
                    f"{len(vlm_description)} chars, {description_result.tokens_used} tokens"
                )
            except Exception as e:
                logger.warning(f"Failed to generate VLM description for {attachment.filename}: {e}")

        # Generate embedding with semaphore
        vector_id: Optional[str] = None
        embedding_vector: Optional[List[float]] = None

        if generate_embedding and self.embedding:
            async def embed_with_limit():
                if embed_semaphore:
                    async with embed_semaphore:
                        combined_text = ""
                        if vlm_description:
                            combined_text = vlm_description
                        if context:
                            combined_text = f"{combined_text}\n{context}" if combined_text else context

                        if combined_text:
                            return await self.embedding.embed_image_and_text(
                                image_bytes=image_bytes,
                                text=combined_text,
                            )
                        else:
                            vectors = await self.embedding.embed_images([image_bytes])
                            return vectors[0] if vectors else None
                else:
                    combined_text = ""
                    if vlm_description:
                        combined_text = vlm_description
                    if context:
                        combined_text = f"{combined_text}\n{context}" if combined_text else context

                    if combined_text:
                        return await self.embedding.embed_image_and_text(
                            image_bytes=image_bytes,
                            text=combined_text,
                        )
                    else:
                        vectors = await self.embedding.embed_images([image_bytes])
                        return vectors[0] if vectors else None

            try:
                embedding_vector = await embed_with_limit()
                if embedding_vector:
                    vector_id = segment_id
                    logger.debug(
                        f"Generated embedding for {attachment.filename}, "
                        f"dimension={len(embedding_vector)}"
                    )
            except Exception as e:
                logger.warning(f"Failed to generate embedding for {attachment.filename}: {e}")

        # Create segment
        segment = ImageSegment(
            segment_id=segment_id,
            document_id=document_id,
            attachment_id=attachment.attachment_id,
            filename=attachment.filename,
            media_type=attachment.media_type,
            file_size=len(image_bytes),
            storage_url=storage_url,
            vector_id=vector_id,
            context_text=context,
            vlm_description=vlm_description,
            embedding=embedding_vector,
            metadata={
                "confluence_attachment_id": attachment.attachment_id,
                "page_id": attachment.page_id,
                "attachment_updated_at": attachment.updated_at,
            },
        )

        logger.debug(f"Created image segment: {segment_id} for {attachment.filename}")
        return segment

    def _detect_image_type(self, filename: str) -> str:
        """
        Detect image type based on filename for better VLM prompts.

        Args:
            filename: Image filename

        Returns:
            Image type hint: "table", "chart", "diagram", or "general"
        """
        filename_lower = filename.lower()

        # Table indicators
        table_keywords = ["table", "表格", "matrix", "grid", "fee", "price", "rate"]
        if any(kw in filename_lower for kw in table_keywords):
            return "table"

        # Chart indicators
        chart_keywords = ["chart", "graph", "图表", "diagram", "flow", "架构", "架构图"]
        if any(kw in filename_lower for kw in chart_keywords):
            return "chart"

        # Diagram indicators
        diagram_keywords = ["diagram", "流程", "sequence", "uml", "erd"]
        if any(kw in filename_lower for kw in diagram_keywords):
            return "diagram"

        return "general"

    async def delete_document_images(
        self,
        document_id: str,
        tenant_id: str,
    ) -> int:
        """
        删除文档的所有图片

        Args:
            document_id: 文档 ID
            tenant_id: 租户 ID

        Returns:
            删除的图片数量
        """
        try:
            count = await self.storage.delete_document_images(tenant_id, document_id)
            logger.info(f"Deleted {count} images for document {document_id}")
            return count
        except Exception as e:
            logger.error(f"Failed to delete images for document {document_id}: {e}")
            return 0

    async def reprocess_page_images(
        self,
        page_id: str,
        document_id: str,
        tenant_id: str,
        page_content: Optional[str] = None,
    ) -> ImageProcessingResult:
        """
        重新处理页面图片（先删除旧图片）

        Args:
            page_id: 页面 ID
            document_id: 文档 ID
            tenant_id: 租户 ID
            page_content: 页面内容

        Returns:
            处理结果
        """
        # Delete existing images
        await self.delete_document_images(document_id, tenant_id)

        # Process new images
        return await self.process_page_images(
            page_id=page_id,
            document_id=document_id,
            tenant_id=tenant_id,
            page_content=page_content,
        )


async def create_image_processor(
    confluence_client: "ConfluenceClient",
    storage_service: "ImageStorageService",
    dashscope_api_key: Optional[str] = None,
    multimodal_model: str = "multimodal-embedding-v1",
    vlm_model: str = "qwen-vl-max",
    max_image_size: int = MAX_IMAGE_SIZE_BYTES,
    enable_vlm_descriptions: bool = True,
) -> ConfluenceImageProcessor:
    """
    创建图片处理器实例

    Args:
        confluence_client: Confluence 客户端
        storage_service: 存储服务
        dashscope_api_key: DashScope API key (可选)
        multimodal_model: 多模态嵌入模型名称
        vlm_model: VLM 图片描述模型名称
        max_image_size: 最大图片大小
        enable_vlm_descriptions: 是否启用 VLM 描述生成

    Returns:
        ConfluenceImageProcessor 实例
    """
    multimodal_embedding = None
    vlm_service = None

    if dashscope_api_key:
        # Initialize multimodal embedding
        from ..embedding import DashScopeMultimodalEmbedding
        multimodal_embedding = DashScopeMultimodalEmbedding(
            model=multimodal_model,
            api_key=dashscope_api_key,
        )
        logger.info(f"Initialized multimodal embedding with model: {multimodal_model}")

        # Initialize VLM service for image descriptions
        if enable_vlm_descriptions:
            from ..vlm_service import DashScopeVLMService
            vlm_service = DashScopeVLMService(
                api_key=dashscope_api_key,
                model=vlm_model,
            )
            logger.info(f"Initialized VLM service with model: {vlm_model}")

    return ConfluenceImageProcessor(
        confluence_client=confluence_client,
        storage_service=storage_service,
        multimodal_embedding=multimodal_embedding,
        vlm_service=vlm_service,
        max_image_size=max_image_size,
        generate_vlm_descriptions=enable_vlm_descriptions,
    )
