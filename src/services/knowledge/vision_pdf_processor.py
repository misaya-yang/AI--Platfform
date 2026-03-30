"""
Vision PDF Processor

Page-as-image processing for scanned PDFs using multimodal embedding.
"""

from __future__ import annotations

import contextlib
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .common import import_pymupdf

logger = logging.getLogger(__name__)


@dataclass
class ProcessingResult:
    success: bool
    total_pages: int
    processed_pages: int
    failed_pages: int
    segments_created: int = 0
    error: str | None = None
    extracted_texts: dict[int, str] | None = None  # page_number -> OCR text


class VisionPDFProcessor:
    """
    Render PDF pages to images and embed each page using a multimodal embedder.
    """

    def __init__(
        self,
        embedder: Any,
        vector_store: Any,
        database: Any,
        render_dpi: int = 200,
        max_image_bytes: int = 3 * 1024 * 1024,
        batch_size: int = 8,
        position_offset: int = 1_000_000,
    ) -> None:
        self.embedder = embedder
        self.vector_store = vector_store
        self.db = database
        self.render_dpi = render_dpi
        self.max_image_bytes = max_image_bytes
        self.batch_size = batch_size
        self.position_offset = position_offset

    @staticmethod
    def _get_fitz():
        return import_pymupdf()

    def _render_page(self, page: Any) -> tuple[bytes | None, tuple[int, int] | None]:
        """Render a PDF page to PNG bytes, shrinking if needed to fit size limits."""
        fitz = self._get_fitz()
        dpi_candidates = [self.render_dpi, 150, 120]
        for dpi in dpi_candidates:
            try:
                pix = page.get_pixmap(dpi=dpi, alpha=False)
                img_bytes = pix.tobytes("png")
                if len(img_bytes) <= self.max_image_bytes:
                    return img_bytes, (pix.width, pix.height)
            except Exception as exc:
                logger.debug(f"Render failed at dpi={dpi}: {exc}")
        # Final attempt: scale down aggressively
        try:
            mat = fitz.Matrix(1.0, 1.0)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            img_bytes = pix.tobytes("png")
            if len(img_bytes) <= self.max_image_bytes:
                return img_bytes, (pix.width, pix.height)
        except Exception as exc:
            logger.debug(f"Render failed at low scale: {exc}")
        return None, None

    async def process(
        self,
        pdf_bytes: bytes,
        document_id: str,
        dataset_id: str,
        collection: str,
        on_progress: Callable[[int, int], Awaitable[None]] | None = None,
        storage_service: Any | None = None,
        tenant_id: str = "default",
        text_extractor: Callable[[bytes], Awaitable[str]] | None = None,
        page_offset: int = 0,
    ) -> ProcessingResult:
        fitz = self._get_fitz()
        doc = None

        try:
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            total_pages = len(doc)
            processed_pages = 0
            failed_pages = 0
            segments_created = 0
            extracted_texts: dict[int, str] = {}

            from qdrant_client.http import models as qmodels

            batch_images: list[bytes] = []
            batch_meta: list[dict] = []

            async def flush_batch() -> None:
                nonlocal segments_created
                if not batch_images:
                    return
                vectors = await self.embedder.embed_images(batch_images)
                if not vectors:
                    return
                points = []
                for idx, vector in enumerate(vectors):
                    meta = batch_meta[idx]
                    seg_id = meta["segment_id"]
                    payload = {
                        "dataset_id": dataset_id,
                        "document_id": document_id,
                        "segment_id": seg_id,
                        "position": meta["position"],
                        "text": meta["text"],
                        "content_type": "image",
                        "image_id": meta["image_id"],
                        "image_mime_type": meta["image_mime_type"],
                        "image_width": meta["image_width"],
                        "image_height": meta["image_height"],
                        "image_page": meta["page_number"],
                    }
                    points.append(qmodels.PointStruct(id=seg_id, vector=vector, payload=payload))

                await self.vector_store.upsert(collection_name=collection, points=points)

                for meta in batch_meta:
                    await self.db.save_image_segment(
                        {
                            "segment_id": meta["segment_id"],
                            "document_id": document_id,
                            "dataset_id": dataset_id,
                            "position": meta["position"],
                            "text": meta["text"],
                            "content_type": "image",
                            "image_url": meta.get("image_url"),
                            "image_attachment_id": meta.get("image_attachment_id"),
                            "image_filename": meta.get("image_filename"),
                            "image_media_type": meta.get("image_media_type"),
                            "image_file_size": meta.get("image_file_size"),
                            "vector_id": meta["segment_id"],
                            "metadata": {
                                "page_number": meta["page_number"],
                                "width": meta["image_width"],
                                "height": meta["image_height"],
                                "source_position": meta["source_position"],
                            },
                        }
                    )
                segments_created += len(batch_meta)
                batch_images.clear()
                batch_meta.clear()

            for page_index in range(total_pages):
                page = doc[page_index]
                img_bytes, dims = self._render_page(page)
                if not img_bytes or not dims:
                    failed_pages += 1
                    continue

                width, height = dims
                global_page_index = page_offset + page_index
                page_number = global_page_index + 1

                # Extract text via VLM OCR callback (reuses rendered image)
                if text_extractor and img_bytes:
                    try:
                        page_text = await text_extractor(img_bytes)
                        if page_text:
                            extracted_texts[page_number] = page_text
                    except Exception as exc:
                        logger.debug(f"Text extraction failed for page {page_number}: {exc}")

                image_url = None
                image_attachment_id = None
                image_filename = None
                image_media_type = "image/png"
                image_file_size = len(img_bytes)

                if storage_service:
                    try:
                        image_attachment_id = f"page_{page_number}"
                        image_filename = f"page_{page_number}.png"
                        image_url = await storage_service.upload_image(
                            tenant_id=tenant_id,
                            document_id=document_id,
                            attachment_id=image_attachment_id,
                            filename=image_filename,
                            content=img_bytes,
                            content_type=image_media_type,
                            metadata={
                                "width": str(width),
                                "height": str(height),
                                "page_number": str(page_number),
                            },
                        )
                    except Exception as exc:
                        logger.warning(f"Failed to upload page image {page_number}: {exc}")

                batch_images.append(img_bytes)
                batch_meta.append(
                    {
                        "segment_id": str(uuid.uuid4()),
                        "position": self.position_offset + global_page_index,
                        "text": f"[Page {page_number}]",
                        "image_id": f"{document_id}_page_{page_number}",
                        "image_mime_type": image_media_type,
                        "image_width": width,
                        "image_height": height,
                        "page_number": page_number,
                        "source_position": global_page_index,
                        "image_url": image_url,
                        "image_attachment_id": image_attachment_id,
                        "image_filename": image_filename,
                        "image_media_type": image_media_type,
                        "image_file_size": image_file_size,
                    }
                )

                processed_pages += 1
                if on_progress:
                    await on_progress(processed_pages, total_pages)

                if len(batch_images) >= self.batch_size:
                    await flush_batch()

            await flush_batch()

            return ProcessingResult(
                success=True,
                total_pages=total_pages,
                processed_pages=processed_pages,
                failed_pages=failed_pages,
                segments_created=segments_created,
                extracted_texts=extracted_texts if extracted_texts else None,
            )
        except Exception as exc:
            logger.error(f"VisionPDFProcessor failed: {exc}")
            return ProcessingResult(
                success=False,
                total_pages=0,
                processed_pages=0,
                failed_pages=0,
                error=str(exc),
            )
        finally:
            if doc is not None:
                with contextlib.suppress(Exception):
                    doc.close()
