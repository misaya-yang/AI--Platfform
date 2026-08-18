from __future__ import annotations

import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from knowledge_service.core.exceptions import ValidationFailedError
from knowledge_service.services.knowledge import ingestion_service as ingestion_module
from knowledge_service.services.knowledge import worker as worker_module
from knowledge_service.services.knowledge.processing_mode import ProcessingMode
from knowledge_service.services.knowledge.worker import (
    KnowledgeIngestTask,
    KnowledgeWorker,
)

TASK = KnowledgeIngestTask(dataset_id="dataset-a", document_id="document-a")


class _VectorStore:
    def __init__(self) -> None:
        self.delete_calls: list[dict[str, Any]] = []

    async def delete_document_points(self, **kwargs: Any) -> list[str]:
        self.delete_calls.append(dict(kwargs))
        return []


class _Database:
    def __init__(self, *, content: str | None = None) -> None:
        self.events: list[str] = []
        self.dataset = {
            "dataset_id": "dataset-a",
            "tenant_id": "tenant-a",
            "collection_name": "collection-a",
            "index_config": {},
        }
        self.document = {
            "document_id": "document-a",
            "dataset_id": "dataset-a",
            "source_type": "upload",
            "status": "processing",
            "enabled": True,
            "archived": False,
            "content": content,
            "metadata": {"processing_mode": "text_only"},
        }

    async def get_dataset(
        self,
        dataset_id: str,
        *,
        connection: Any | None = None,
    ) -> dict[str, Any]:
        del connection
        assert dataset_id == "dataset-a"
        self.events.append("get-dataset")
        return deepcopy(self.dataset)

    async def get_document(
        self,
        document_id: str,
        *,
        connection: Any | None = None,
    ) -> dict[str, Any]:
        del connection
        assert document_id == "document-a"
        self.events.append("get-document")
        return deepcopy(self.document)

    async def get_image_segments_by_document(
        self,
        document_id: str,
        *,
        connection: Any | None = None,
    ) -> list[dict[str, Any]]:
        del connection
        assert document_id == "document-a"
        self.events.append("get-image-segments")
        raise AssertionError("oversized stored content reached the generation sweep")

    async def update_document_status(
        self,
        document_id: str,
        status: str,
        progress: float | None = None,
        error: str | None = None,
        *,
        connection: Any | None = None,
    ) -> None:
        del progress, error, connection
        assert document_id == "document-a"
        self.events.append(f"status:{status}")

    async def execute(self, *_args: Any, **_kwargs: Any) -> None:
        self.events.append("content-sql")

    async def update_document_content(self, document_id: str, _content: str) -> None:
        assert document_id == "document-a"
        self.events.append("content-update")

    async def update_document_fields(
        self,
        document_id: str,
        _fields: dict[str, Any],
        **_kwargs: Any,
    ) -> None:
        assert document_id == "document-a"
        self.events.append("fields-update")


class _Service:
    def __init__(self, database: _Database) -> None:
        self.db = database
        self.vector_store = _VectorStore()
        self.settings = SimpleNamespace(
            knowledge=SimpleNamespace(
                large_file_threshold=1,
                pdf_split_enabled=True,
                pdf_split_max_size_bytes=1,
                pdf_split_min_pages_per_part=1,
                ocr_enabled=True,
                ocr_strategy="hybrid",
                streaming_batch_size=1,
            )
        )
        self.image_storage_service = SimpleNamespace(
            download_original_file=AsyncMock(return_value=b"%PDF-fake")
        )
        self.ingest_calls: list[tuple[str, str]] = []
        self._worker: KnowledgeWorker | None = None

    async def ingest_document(self, dataset_id: str, document_id: str) -> None:
        self.ingest_calls.append((dataset_id, document_id))


def _set_tiny_text_budget(
    monkeypatch: pytest.MonkeyPatch,
    *,
    chars: int,
    bytes_: int = 1_000,
) -> None:
    # The worker imports the enforcement functions, whose limits live in the
    # ingestion module globals. Patch the authoritative limits, not a test copy.
    monkeypatch.setattr(ingestion_module, "MAX_EXTRACTED_TEXT_CHARS", chars)
    monkeypatch.setattr(ingestion_module, "MAX_EXTRACTED_TEXT_BYTES", bytes_)


@pytest.mark.asyncio
async def test_prepare_rejects_oversized_stored_content_before_generation_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_tiny_text_budget(monkeypatch, chars=8)
    database = _Database(content="123456789")
    service = _Service(database)
    worker = KnowledgeWorker(service)  # type: ignore[arg-type]

    with pytest.raises(ValidationFailedError, match="8 character limit"):
        await worker._prepare_document_generation(
            TASK,
            connection=SimpleNamespace(name="owner"),
        )

    assert database.events == ["get-dataset", "get-document"]
    assert service.vector_store.delete_calls == []
    assert service.ingest_calls == []


class _StreamingLoader:
    def __init__(self, **_kwargs: Any) -> None:
        pass

    async def iter_batches(self, _path: str, _on_progress: Any):
        for page_number in (1, 2):
            yield SimpleNamespace(
                total_pages=2,
                pages=[
                    SimpleNamespace(
                        page_number=page_number,
                        text="abcde",
                        images=[],
                    )
                ],
                batch_index=page_number,
                start_page=page_number,
                end_page=page_number,
            )


@pytest.mark.asyncio
async def test_large_file_rejects_cumulative_batch_before_second_temp_append_or_index(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_tiny_text_budget(monkeypatch, chars=20)
    monkeypatch.setattr(worker_module, "StreamingDocumentLoader", _StreamingLoader)
    database = _Database()
    service = _Service(database)
    hierarchy = SimpleNamespace(index_document=AsyncMock())
    worker = KnowledgeWorker(  # type: ignore[arg-type]
        service,
        hierarchical_indexer=hierarchy,
    )
    appended: list[str] = []
    worker._append_text = lambda _path, text: appended.append(text)  # type: ignore[method-assign]
    source_path = tmp_path / "source.pdf"
    source_path.write_bytes(b"%PDF-fake")
    doc = {
        "metadata": {
            "original_file_key": "knowledge/originals/document-a.pdf",
        }
    }

    with pytest.raises(ValidationFailedError, match="20 character limit"):
        await worker._process_large_file(
            TASK,
            doc,
            ProcessingMode.TEXT_ONLY,
            source_path=str(source_path),
        )

    assert appended == ["[Page 1]\nabcde\n\n"]
    assert database.events == []
    hierarchy.index_document.assert_not_awaited()
    assert service.ingest_calls == []


@pytest.mark.asyncio
async def test_direct_hierarchical_rejects_extracted_text_before_status_or_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_tiny_text_budget(monkeypatch, chars=8)
    database = _Database()
    service = _Service(database)
    hierarchy = SimpleNamespace(index_document=AsyncMock())
    worker = KnowledgeWorker(  # type: ignore[arg-type]
        service,
        hierarchical_indexer=hierarchy,
    )
    worker._extract_text_from_content = AsyncMock(  # type: ignore[method-assign]
        return_value="123456789"
    )
    doc = {
        "metadata": {
            "original_file_key": "knowledge/originals/document-a.txt",
            "mime_type": "text/plain",
        }
    }

    with pytest.raises(ValidationFailedError, match="8 character limit"):
        await worker._process_with_hierarchical_indexer(
            TASK,
            doc,
            ProcessingMode.TEXT_ONLY,
        )

    assert database.events == []
    hierarchy.index_document.assert_not_awaited()
    assert service.ingest_calls == []


class _Pixmap:
    width = 1
    height = 1
    n = 3

    @staticmethod
    def tobytes(format_: str) -> bytes:
        assert format_ == "png"
        return b"png"


class _PdfPage:
    @staticmethod
    def get_pixmap(*, matrix: Any) -> _Pixmap:
        assert matrix is not None
        return _Pixmap()


class _PdfDocument:
    def __init__(self) -> None:
        self.pages = [_PdfPage(), _PdfPage()]
        self.closed = False

    def __len__(self) -> int:
        return len(self.pages)

    def __getitem__(self, index: int) -> _PdfPage:
        return self.pages[index]

    def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_vlm_ocr_rejects_cumulative_page_text_before_content_or_ingestion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_tiny_text_budget(monkeypatch, chars=20)
    pdf_document = _PdfDocument()
    monkeypatch.setitem(
        sys.modules,
        "fitz",
        SimpleNamespace(
            Matrix=lambda *_args: object(),
            open=lambda **_kwargs: pdf_document,
        ),
    )
    database = _Database()
    service = _Service(database)
    vlm_ocr = SimpleNamespace(
        ocr_pdf_pages=AsyncMock(return_value=["abcde", "fghij"]),
    )
    worker = KnowledgeWorker(  # type: ignore[arg-type]
        service,
        vlm_ocr_service=vlm_ocr,
    )
    doc = {
        "metadata": {
            "original_file_key": "knowledge/originals/document-a.pdf",
        }
    }

    with pytest.raises(ValidationFailedError, match="20 character limit"):
        await worker._process_scanned_with_vlm_ocr(TASK, doc)

    assert pdf_document.closed is True
    assert database.events == ["status:processing"]
    assert service.ingest_calls == []
