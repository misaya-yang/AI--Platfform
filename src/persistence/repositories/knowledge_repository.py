"""
Knowledge Base (KBMS) repository

Provides data access abstractions for datasets/documents/segments and permissions.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..database import DatabaseStorage
    from ..redis import RedisStorage


class KnowledgeRepository(ABC):
    # ========= Dataset =========

    @abstractmethod
    async def save_dataset(self, dataset: Dict[str, Any]) -> None:
        raise NotImplementedError

    @abstractmethod
    async def get_dataset(self, dataset_id: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    async def list_datasets(
        self,
        tenant_id: Optional[str] = None,
        include_public: bool = True,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    async def delete_dataset(self, dataset_id: str) -> bool:
        raise NotImplementedError

    # ========= Permissions =========

    @abstractmethod
    async def grant_dataset_permission(
        self, dataset_id: str, subject_type: str, subject_id: str, permission: str
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    async def revoke_dataset_permission(
        self, dataset_id: str, subject_type: str, subject_id: str
    ) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def list_dataset_permissions(self, dataset_id: str) -> List[Dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    async def get_dataset_permission(
        self, dataset_id: str, subject_type: str, subject_id: str
    ) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    # ========= Document =========

    @abstractmethod
    async def save_document(self, document: Dict[str, Any]) -> None:
        raise NotImplementedError

    @abstractmethod
    async def get_document(self, document_id: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    async def list_documents(
        self,
        dataset_id: str,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    async def update_document_status(
        self,
        document_id: str,
        status: str,
        progress: Optional[float] = None,
        error: Optional[str] = None,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    async def delete_document(self, document_id: str) -> bool:
        raise NotImplementedError

    # ========= Segment =========

    @abstractmethod
    async def insert_segments(self, segments: List[Dict[str, Any]]) -> None:
        raise NotImplementedError

    @abstractmethod
    async def delete_segments_by_document(self, document_id: str) -> int:
        raise NotImplementedError

    @abstractmethod
    async def list_segments(
        self,
        dataset_id: str,
        document_id: Optional[str] = None,
        query_text: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    async def get_segment(self, segment_id: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    async def update_segment(
        self,
        segment_id: str,
        text: str,
        token_count: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
        vector_id: Optional[str] = None,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    async def delete_segment(self, segment_id: str) -> bool:
        raise NotImplementedError


class DatabaseKnowledgeRepository(KnowledgeRepository):
    def __init__(
        self,
        database: "DatabaseStorage",
        redis: Optional["RedisStorage"] = None,
    ):
        self.database = database
        self.redis = redis

    async def save_dataset(self, dataset: Dict[str, Any]) -> None:
        await self.database.save_dataset(dataset)

    async def get_dataset(self, dataset_id: str) -> Optional[Dict[str, Any]]:
        return await self.database.get_dataset(dataset_id)

    async def list_datasets(
        self,
        tenant_id: Optional[str] = None,
        include_public: bool = True,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        return await self.database.list_datasets(
            tenant_id=tenant_id, include_public=include_public, limit=limit, offset=offset
        )

    async def delete_dataset(self, dataset_id: str) -> bool:
        return await self.database.delete_dataset(dataset_id)

    async def grant_dataset_permission(
        self, dataset_id: str, subject_type: str, subject_id: str, permission: str
    ) -> None:
        await self.database.grant_dataset_permission(
            dataset_id=dataset_id,
            subject_type=subject_type,
            subject_id=subject_id,
            permission=permission,
        )

    async def revoke_dataset_permission(
        self, dataset_id: str, subject_type: str, subject_id: str
    ) -> bool:
        return await self.database.revoke_dataset_permission(
            dataset_id=dataset_id, subject_type=subject_type, subject_id=subject_id
        )

    async def list_dataset_permissions(self, dataset_id: str) -> List[Dict[str, Any]]:
        return await self.database.list_dataset_permissions(dataset_id)

    async def get_dataset_permission(
        self, dataset_id: str, subject_type: str, subject_id: str
    ) -> Optional[Dict[str, Any]]:
        return await self.database.get_dataset_permission(
            dataset_id=dataset_id, subject_type=subject_type, subject_id=subject_id
        )

    async def save_document(self, document: Dict[str, Any]) -> None:
        await self.database.save_document(document)

    async def get_document(self, document_id: str) -> Optional[Dict[str, Any]]:
        return await self.database.get_document(document_id)

    async def list_documents(
        self,
        dataset_id: str,
        status: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        return await self.database.list_documents(
            dataset_id=dataset_id, status=status, limit=limit, offset=offset
        )

    async def update_document_status(
        self,
        document_id: str,
        status: str,
        progress: Optional[float] = None,
        error: Optional[str] = None,
    ) -> None:
        await self.database.update_document_status(
            document_id=document_id, status=status, progress=progress, error=error
        )

    async def delete_document(self, document_id: str) -> bool:
        return await self.database.delete_document(document_id)

    async def insert_segments(self, segments: List[Dict[str, Any]]) -> None:
        await self.database.insert_segments(segments)

    async def delete_segments_by_document(self, document_id: str) -> int:
        return await self.database.delete_segments_by_document(document_id)

    async def list_segments(
        self,
        dataset_id: str,
        document_id: Optional[str] = None,
        query_text: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        return await self.database.list_segments(
            dataset_id=dataset_id,
            document_id=document_id,
            query_text=query_text,
            limit=limit,
            offset=offset,
        )

    async def get_segment(self, segment_id: str) -> Optional[Dict[str, Any]]:
        return await self.database.get_segment(segment_id)

    async def update_segment(
        self,
        segment_id: str,
        text: str,
        token_count: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
        vector_id: Optional[str] = None,
    ) -> None:
        await self.database.update_segment(
            segment_id=segment_id,
            text=text,
            token_count=token_count,
            metadata=metadata,
            vector_id=vector_id,
        )

    async def delete_segment(self, segment_id: str) -> bool:
        return await self.database.delete_segment(segment_id)

