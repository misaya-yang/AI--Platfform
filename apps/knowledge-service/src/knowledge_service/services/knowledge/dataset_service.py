"""Dataset management service for knowledge base.

Handles dataset CRUD operations, permissions, configuration, and Islamic defaults.
Migrated from KnowledgeService as part of Phase 2 refactoring.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Any

from ...config.settings import Settings
from ...core.auth.user_resolver import UserContext
from ...core.exceptions import PermissionDeniedError, ValidationFailedError
from ...core.observability.logging import get_logger
from ...persistence.database import DatabaseStorage
from .chunking import ChunkingConfig, flatten_chunks, process_document
from .common import ensure_dict as _ensure_dict
from .common import permission_rank as _permission_rank

if TYPE_CHECKING:
    from .knowledge_service import KnowledgeService

logger = get_logger(__name__)


def _require_not_guest(user: UserContext) -> None:
    if not user.is_authenticated or "guest" in (user.roles or []):
        raise PermissionDeniedError("Authentication required")


class DatasetService:
    """Service for managing knowledge base datasets.

    Accepts a ``_ks`` (parent KnowledgeService) reference for shared resources
    like ``vector_store`` and ``embedding_manager``.  Set post-init by the
    parent because vector_store is created after sub-service construction.
    """

    _ks: KnowledgeService | None

    def __init__(self, settings: Settings, database: DatabaseStorage):
        self.settings = settings
        self.db = database
        self._ks = None  # Set post-init by KnowledgeService

    # ========================================================================
    # Dataset CRUD
    # ========================================================================

    async def list_datasets(self, user: UserContext) -> list[dict[str, Any]]:
        datasets = await self.db.list_datasets(
            tenant_id=user.tenant_id, include_public=True, limit=200, offset=0
        )
        visible: list[dict[str, Any]] = []
        for ds in datasets:
            perm = await self._effective_dataset_permission(ds, user)
            if _permission_rank(perm) >= 1:
                ds = dict(ds)
                ds["my_permission"] = perm
                visible.append(ds)

        if visible:
            dataset_ids = [ds["dataset_id"] for ds in visible]
            try:
                stats_batch = await self.db.get_datasets_statistics_batch(dataset_ids)
                for ds in visible:
                    ds_id = ds["dataset_id"]
                    ds_stats = stats_batch.get(ds_id, {})
                    ds["statistics"] = {
                        "document_count": ds_stats.get("document_count", 0),
                        "segment_count": ds_stats.get("segment_count", 0),
                        "available_document_count": ds_stats.get("document_count", 0),
                        "available_segment_count": ds_stats.get("segment_count", 0),
                        "word_count": 0,
                        "hit_count": 0,
                    }
            except Exception as e:
                logger.warning(f"Failed to fetch batch statistics: {e}")
                for ds in visible:
                    ds["statistics"] = {
                        "document_count": 0,
                        "segment_count": 0,
                        "available_document_count": 0,
                        "available_segment_count": 0,
                        "word_count": 0,
                        "hit_count": 0,
                    }

        return [self._redact_dataset_secrets(ds) for ds in visible]

    async def preview_chunking(
        self, user: UserContext, dataset_id: str, text: str, config: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        if dataset_id != "temp_preview":
            await self.require_dataset_access(user, dataset_id, required="viewer")

        chunking_config: ChunkingConfig
        if config:
            chunking_config = ChunkingConfig.from_dict(config)
        else:
            if dataset_id == "temp_preview":
                chunking_config = ChunkingConfig()
            else:
                dataset = await self._get_dataset_or_404(dataset_id)
                index_config = _ensure_dict(dataset.get("index_config"))
                chunking_config = ChunkingConfig.from_dict(index_config.get("chunking", {}))

        doc_id = f"preview_{uuid.uuid4().hex[:8]}"
        chunks = await asyncio.to_thread(process_document, text, chunking_config, doc_id)
        flat_chunks = flatten_chunks(chunks)

        return [
            {
                "content": c.text,
                "token_count": c.token_count,
                "char_count": c.char_count,
                "metadata": c.metadata,
            }
            for c in flat_chunks
        ]

    async def create_dataset(self, user: UserContext, data: dict[str, Any]) -> dict[str, Any]:
        _require_not_guest(user)

        dataset_id = str(data.get("dataset_id") or "").strip()
        if not dataset_id:
            dataset_id = f"kb_{uuid.uuid4().hex[:12]}"

        embedding_provider = str(data.get("embedding_provider") or "local")
        embedding_model = str(data.get("embedding_model") or "hash-384")
        embedding_dimension = int(data.get("embedding_dimension") or 0) or None

        collection_name = str(data.get("collection_name") or "").strip() or None
        visibility = str(data.get("visibility") or "private")

        embedding_config = _ensure_dict(data.get("embedding_config"))
        index_config = _ensure_dict(data.get("index_config"))
        islamic_defaults = self._build_islamic_dataset_defaults()
        index_config = self._apply_islamic_dataset_defaults(
            str(data.get("name") or dataset_id),
            index_config,
            islamic_defaults,
        )

        from .embedding import BaseEmbedding, create_embedding

        embedder: BaseEmbedding | None = None
        dim: int = 0
        collection: str = ""
        try:
            econf = self._ks._resolve_embedding_config(
                provider=embedding_provider,
                model=embedding_model,
                embedding_config=embedding_config,
            )
            embedder = create_embedding(econf, dimension=embedding_dimension)
            if embedder._dimension is None:
                await asyncio.wait_for(
                    embedder.embed_query("test"),
                    timeout=float(econf.timeout_seconds) + 5.0,
                )
            dim = embedder._dimension or 1024
            collection = await self._ks.vector_store.ensure_collection(
                dataset_id=dataset_id,
                dimension=dim,
                collection_name=collection_name,
            )
        except Exception as exc:
            raise ValidationFailedError(f"Failed to create dataset index: {exc}") from exc
        finally:
            if embedder:
                await embedder.close()

        dataset = {
            "dataset_id": dataset_id,
            "name": str(data.get("name") or dataset_id),
            "description": str(data.get("description") or ""),
            "tenant_id": user.tenant_id or "",
            "visibility": visibility,
            "embedding_provider": embedding_provider,
            "embedding_model": embedding_model,
            "embedding_dimension": dim,
            "embedding_config": embedding_config,
            "index_config": index_config,
            "collection_name": collection,
            "created_by": user.user_id,
        }

        await self.db.save_dataset(dataset)
        await self.db.grant_dataset_permission(dataset_id, "user", user.user_id, "owner")
        return self._redact_dataset_secrets(await self._get_dataset_or_404(dataset_id))

    async def update_dataset(
        self, user: UserContext, dataset_id: str, patch: dict[str, Any]
    ) -> dict[str, Any]:
        dataset = await self.require_dataset_access(user, dataset_id, required="owner")

        mutable = {
            "name",
            "description",
            "visibility",
            "embedding_provider",
            "embedding_model",
            "embedding_dimension",
            "embedding_config",
            "index_config",
        }
        filtered = {k: v for k, v in (patch or {}).items() if k in mutable}
        if not filtered:
            return dataset

        old_dim = int(dataset.get("embedding_dimension") or 0)
        new_dim = int(filtered.get("embedding_dimension") or old_dim)
        if new_dim != old_dim:
            docs = await self.db.list_documents(dataset_id=dataset_id, limit=1, offset=0)
            if docs:
                raise ValidationFailedError(
                    "Cannot change embedding_dimension when documents exist; reindex required"
                )

        updated = dict(dataset)
        updated.update(filtered)
        if "index_config" in filtered or "name" in filtered:
            islamic_defaults = self._build_islamic_dataset_defaults()
            updated["index_config"] = self._apply_islamic_dataset_defaults(
                str(updated.get("name") or dataset_id),
                _ensure_dict(updated.get("index_config")),
                islamic_defaults,
            )

        embedding_keys = {
            "embedding_provider",
            "embedding_model",
            "embedding_dimension",
            "embedding_config",
        }
        if embedding_keys.intersection(filtered.keys()):
            from .embedding import BaseEmbedding, create_embedding

            embedder: BaseEmbedding | None = None
            dim: int = 0
            try:
                econf = self._ks._resolve_embedding_config(
                    provider=str(updated.get("embedding_provider") or "local"),
                    model=str(updated.get("embedding_model") or "hash-384"),
                    embedding_config=_ensure_dict(updated.get("embedding_config")),
                )
                embedder = create_embedding(
                    econf, dimension=int(updated.get("embedding_dimension") or 0) or None
                )
                if embedder._dimension is None:
                    await asyncio.wait_for(
                        embedder.embed_query("test"),
                        timeout=float(econf.timeout_seconds) + 5.0,
                    )
                dim = embedder._dimension or 1024
                collection = await self._ks.vector_store.ensure_collection(
                    dataset_id=dataset_id,
                    dimension=dim,
                    collection_name=str(updated.get("collection_name") or "") or None,
                )
                updated["embedding_dimension"] = dim
                updated["collection_name"] = collection
            except Exception as exc:
                raise ValidationFailedError(
                    f"Failed to update dataset embedding/index: {exc}"
                ) from exc
            finally:
                if embedder:
                    await embedder.close()

        await self.db.save_dataset(updated)
        return self._redact_dataset_secrets(await self._get_dataset_or_404(dataset_id))

    async def delete_dataset(
        self,
        user: UserContext,
        dataset_id: str,
        *,
        password: str,
        reason: str | None = None,
    ) -> bool:
        from ...core.auth.password import verify_password

        dataset = await self.require_dataset_access(user, dataset_id, required="owner")

        if not user.is_authenticated:
            raise PermissionDeniedError("Authentication required")

        account = await self.db.get_user(user.user_id)
        account_password_hash = str((account or {}).get("password_hash") or "")
        if not account_password_hash:
            raise PermissionDeniedError("Password confirmation requires account login")
        if not verify_password(password, account_password_hash):
            raise ValidationFailedError("Invalid password")

        collection = str(dataset.get("collection_name") or "")
        try:
            if collection:
                await self._ks.vector_store.delete_collection(collection_name=collection)
        except Exception:
            logger.warning(
                "Failed to delete vector collection for dataset %s",
                dataset_id,
                exc_info=True,
            )

        deleted = await self.db.delete_dataset(
            dataset_id,
            deleted_by=user.user_id,
            delete_reason=reason,
        )
        if deleted:
            try:
                await self.db.log_audit(
                    event_type="knowledge.dataset",
                    action="delete",
                    status="success",
                    user_id=user.user_id,
                    tenant_id=user.tenant_id,
                    resource_type="dataset",
                    resource_id=dataset_id,
                    request_summary={
                        "delete_mode": "soft",
                        "reason": reason or "",
                    },
                )
            except Exception:
                logger.warning(
                    "Failed to write audit log for dataset deletion %s",
                    dataset_id,
                    exc_info=True,
                )
        return deleted

    # ========================================================================
    # Permissions
    # ========================================================================

    async def list_dataset_permissions(
        self, user: UserContext, dataset_id: str
    ) -> list[dict[str, Any]]:
        await self.require_dataset_access(user, dataset_id, required="owner")
        return await self.db.list_dataset_permissions(dataset_id)

    async def grant_dataset_permission(
        self,
        user: UserContext,
        dataset_id: str,
        subject_type: str,
        subject_id: str,
        permission: str,
    ) -> None:
        await self.require_dataset_access(user, dataset_id, required="owner")
        if subject_type not in {"user", "role"}:
            raise ValidationFailedError("subject_type must be user or role")
        if permission not in {"owner", "editor", "viewer"}:
            raise ValidationFailedError("permission must be owner/editor/viewer")
        await self.db.grant_dataset_permission(dataset_id, subject_type, subject_id, permission)

    async def revoke_dataset_permission(
        self, user: UserContext, dataset_id: str, subject_type: str, subject_id: str
    ) -> bool:
        await self.require_dataset_access(user, dataset_id, required="owner")
        return await self.db.revoke_dataset_permission(dataset_id, subject_type, subject_id)

    # ========================================================================
    # Helpers (shared with KnowledgeService callers)
    # ========================================================================

    async def _get_dataset_or_404(self, dataset_id: str) -> dict[str, Any]:
        dataset = await self.db.get_dataset(dataset_id)
        if not dataset:
            raise ValidationFailedError("dataset not found")
        return dataset

    async def _effective_dataset_permission(
        self, dataset: dict[str, Any], user: UserContext
    ) -> str | None:
        if user.tier == "admin" or "admin" in (user.roles or []):
            return "owner"

        created_by = str(dataset.get("created_by") or "")
        if created_by and created_by == user.user_id:
            return "owner"

        rec = await self.db.get_dataset_permission(dataset.get("dataset_id"), "user", user.user_id)
        best = str(rec.get("permission")) if rec else None

        for role in user.roles or []:
            r = await self.db.get_dataset_permission(dataset.get("dataset_id"), "role", role)
            p = str(r.get("permission")) if r else None
            if _permission_rank(p) > _permission_rank(best):
                best = p

        if best:
            return best

        visibility = str(dataset.get("visibility") or "private").lower()
        if visibility == "public":
            return "viewer"
        if (
            visibility == "tenant"
            and dataset.get("tenant_id")
            and dataset.get("tenant_id") == user.tenant_id
        ):
            return "viewer"

        return None

    async def require_dataset_access(
        self, user: UserContext, dataset_id: str, required: str = "viewer"
    ) -> dict[str, Any]:
        dataset = await self._get_dataset_or_404(dataset_id)
        perm = await self._effective_dataset_permission(dataset, user)
        if _permission_rank(perm) < _permission_rank(required):
            raise PermissionDeniedError(
                f"Missing dataset permission: {required} (current={perm or 'none'})"
            )
        return dataset

    def _redact_dataset_secrets(self, dataset: dict[str, Any]) -> dict[str, Any]:
        ds = dict(dataset or {})
        embedding_config = _ensure_dict(ds.get("embedding_config"))
        if "api_key" in embedding_config:
            embedding_config["api_key"] = "*****"
        ds["embedding_config"] = embedding_config

        index_config = _ensure_dict(ds.get("index_config"))
        retrieval = _ensure_dict(index_config.get("retrieval"))
        rerank = _ensure_dict(retrieval.get("rerank"))
        if "api_key" in rerank:
            rerank["api_key"] = "*****"
        if rerank:
            retrieval["rerank"] = rerank
            index_config["retrieval"] = retrieval
        ds["index_config"] = index_config
        return ds

    def sanitize_dataset_for_response(self, dataset: dict[str, Any]) -> dict[str, Any]:
        return self._redact_dataset_secrets(dataset)

    def _build_islamic_dataset_defaults(self) -> dict[str, Any]:
        profile = self.settings.knowledge.islamic_profile
        if not profile.enabled:
            return {}
        return {
            "retrieval": {
                "top_k": profile.top_k,
                "score_threshold": profile.score_threshold,
                "rerank": {
                    "enabled": profile.rerank_enabled,
                    "provider": profile.rerank_provider,
                    "model": profile.rerank_model,
                },
                "islamic": {
                    "multi_query": profile.multi_query,
                    "citation_format": profile.citation_format,
                    "authority_sort": profile.authority_sort,
                    "strict_section_traceability": profile.strict_section_traceability,
                    "max_expanded_queries": profile.max_expanded_queries,
                },
            },
            "chunking": {
                "strict_section_traceability": profile.strict_section_traceability,
            },
        }

    @staticmethod
    def _apply_islamic_dataset_defaults(
        dataset_name: str,
        index_config: dict[str, Any],
        defaults: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        _ = dataset_name
        chunking = _ensure_dict(index_config.get("chunking"))
        retrieval = _ensure_dict(index_config.get("retrieval"))
        islamic_cfg = _ensure_dict(retrieval.get("islamic"))
        chunk_mode = str(chunking.get("mode") or "").lower()

        is_islamic = bool(islamic_cfg) or chunk_mode == "islamic"
        if not is_islamic:
            return index_config

        default_cfg = _ensure_dict(defaults)
        default_retrieval = _ensure_dict(default_cfg.get("retrieval"))
        default_islamic = _ensure_dict(default_retrieval.get("islamic"))
        default_rerank = _ensure_dict(default_retrieval.get("rerank"))
        default_chunking = _ensure_dict(default_cfg.get("chunking"))

        for key, value in default_retrieval.items():
            if key in {"islamic", "rerank"}:
                continue
            retrieval.setdefault(key, value)

        if default_rerank:
            rerank_cfg = _ensure_dict(retrieval.get("rerank"))
            for key, value in default_rerank.items():
                rerank_cfg.setdefault(key, value)
            retrieval["rerank"] = rerank_cfg

        for key, value in default_islamic.items():
            islamic_cfg.setdefault(key, value)
        if islamic_cfg:
            retrieval["islamic"] = islamic_cfg

        if (
            chunk_mode == "islamic"
            and chunking.get("strict_section_traceability") is None
            and default_chunking.get("strict_section_traceability") is not None
        ):
            chunking["strict_section_traceability"] = bool(
                default_chunking.get("strict_section_traceability")
            )

        if (
            chunk_mode == "islamic"
            and islamic_cfg.get("strict_section_traceability")
            and chunking.get("strict_section_traceability") is None
        ):
            chunking["strict_section_traceability"] = True

        if chunking.get("strict_section_traceability") and not islamic_cfg.get(
            "strict_section_traceability"
        ):
            islamic_cfg["strict_section_traceability"] = True
            retrieval["islamic"] = islamic_cfg

        if chunking:
            index_config["chunking"] = chunking
        if retrieval:
            index_config["retrieval"] = retrieval
        return index_config
