"""
LLM Model Service.

Manages LLM model configurations.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from ai_gateway_core.logging import get_logger

from ...persistence.database import DatabaseStorage
from ...services.billing.model_pricing import get_pricing_service

logger = get_logger(__name__)


class ModelService:
    """Service for managing LLM models."""

    def __init__(self, database: DatabaseStorage):
        self.db = database

    async def list_models(
        self,
        tenant_id: str,
        provider_id: str | None = None,
        model_type: str | None = None,
        include_disabled: bool = False,
        access_level: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        List models for a tenant.

        Args:
            tenant_id: Tenant ID
            provider_id: Optional filter by provider
            include_disabled: Include disabled models
            access_level: Filter by max access level (public, premium, admin)
        """
        conditions = ["tenant_id = $1"]
        params: list[Any] = [tenant_id]
        param_idx = 2

        if provider_id:
            conditions.append(f"provider_id = ${param_idx}")
            params.append(provider_id)
            param_idx += 1

        if model_type:
            conditions.append(f"model_type = ${param_idx}")
            params.append(model_type)
            param_idx += 1

        if not include_disabled:
            conditions.append("is_enabled = true")

        # Access level filtering: show models up to the user's access level
        if access_level:
            if access_level == "public":
                conditions.append("access_level = 'public'")
            elif access_level == "premium":
                conditions.append("access_level IN ('public', 'premium')")
            # admin can see all

        where_clause = " AND ".join(conditions)

        query = f"""
            SELECT model_id, tenant_id, provider_id, display_name,
                   model_type,
                   context_window, max_output_tokens, supports_vision, supports_tools,
                   input_price_per_1k, output_price_per_1k, access_level,
                   is_enabled, sort_order, created_at, updated_at
            FROM llm_models
            WHERE {where_clause}
            ORDER BY provider_id, sort_order, display_name
        """
        rows = await self.db.fetch(query, *params)
        return [self._row_to_dict(row) for row in rows]

    async def get_model(
        self,
        tenant_id: str,
        model_id: str,
        provider_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Get a specific model.

        ``provider_id`` disambiguates when the same model_id exists under
        multiple providers (e.g. ``gemini-3.1-pro-preview`` on both
        ``google`` AI Studio and ``google-vertex`` Vertex Express Mode).
        When omitted, returns the first match by sort_order — preserves
        pre-migration-055 callers that only knew about one provider per
        model_id.
        """
        if provider_id is not None:
            query = """
                SELECT model_id, tenant_id, provider_id, display_name,
                       model_type,
                       context_window, max_output_tokens, supports_vision, supports_tools,
                       input_price_per_1k, output_price_per_1k, access_level,
                       is_enabled, sort_order, created_at, updated_at
                FROM llm_models
                WHERE tenant_id = $1 AND provider_id = $2 AND model_id = $3
            """
            row = await self.db.fetchrow(query, tenant_id, provider_id, model_id)
        else:
            query = """
                SELECT model_id, tenant_id, provider_id, display_name,
                       model_type,
                       context_window, max_output_tokens, supports_vision, supports_tools,
                       input_price_per_1k, output_price_per_1k, access_level,
                       is_enabled, sort_order, created_at, updated_at
                FROM llm_models
                WHERE tenant_id = $1 AND model_id = $2
                ORDER BY sort_order, provider_id
                LIMIT 1
            """
            row = await self.db.fetchrow(query, tenant_id, model_id)
        return self._row_to_dict(row) if row else None

    async def get_provider_model(
        self,
        tenant_id: str,
        provider_id: str,
        model_id: str,
    ) -> dict[str, Any] | None:
        """Get a model scoped to one provider to avoid model_id ambiguity."""
        return await self.get_model(
            tenant_id=tenant_id,
            model_id=model_id,
            provider_id=provider_id,
        )

    async def create_model(
        self,
        tenant_id: str,
        model_id: str,
        provider_id: str,
        display_name: str,
        model_type: str = "llm",
        context_window: int = 128000,
        max_output_tokens: int = 4096,
        supports_vision: bool = False,
        supports_tools: bool = True,
        input_price_per_1k: Decimal = Decimal("0"),
        output_price_per_1k: Decimal = Decimal("0"),
        access_level: str = "public",
        is_enabled: bool = True,
        sort_order: int = 0,
    ) -> dict[str, Any]:
        """Create a new model."""
        query = """
            INSERT INTO llm_models (
                model_id, tenant_id, provider_id, display_name, model_type,
                context_window, max_output_tokens, supports_vision, supports_tools,
                input_price_per_1k, output_price_per_1k, access_level,
                is_enabled, sort_order
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
            RETURNING model_id, tenant_id, provider_id, display_name,
                      model_type,
                      context_window, max_output_tokens, supports_vision, supports_tools,
                      input_price_per_1k, output_price_per_1k, access_level,
                      is_enabled, sort_order, created_at, updated_at
        """
        row = await self.db.fetchrow(
            query,
            model_id,
            tenant_id,
            provider_id,
            display_name,
            model_type,
            context_window,
            max_output_tokens,
            supports_vision,
            supports_tools,
            input_price_per_1k,
            output_price_per_1k,
            access_level,
            is_enabled,
            sort_order,
        )
        row_dict = self._row_to_dict(row)

        # Sync with model_pricing table for usage recording
        await self._sync_single_model_pricing(
            model_id=model_id,
            input_price_per_1k=float(input_price_per_1k),
            output_price_per_1k=float(output_price_per_1k),
            provider=provider_id,
            display_name=display_name,
            context_window=context_window,
            max_output_tokens=max_output_tokens,
            supports_vision=supports_vision,
            supports_tools=supports_tools,
        )

        return row_dict

    async def update_model(
        self,
        tenant_id: str,
        model_id: str,
        new_model_id: str | None = None,
        display_name: str | None = None,
        model_type: str | None = None,
        context_window: int | None = None,
        max_output_tokens: int | None = None,
        supports_vision: bool | None = None,
        supports_tools: bool | None = None,
        input_price_per_1k: Decimal | None = None,
        output_price_per_1k: Decimal | None = None,
        access_level: str | None = None,
        is_enabled: bool | None = None,
        sort_order: int | None = None,
        provider_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Update a model. ``new_model_id`` renames the primary key.

        ``provider_id`` disambiguates when the same model_id exists under
        multiple providers. Post-migration-055 callers should pass it to
        avoid updating the wrong row. Omitting it falls back to the
        pre-migration behaviour of matching the first row by tenant+id.
        """
        updates = []
        params = []
        param_idx = 1

        if new_model_id is not None and new_model_id != model_id:
            updates.append(f"model_id = ${param_idx}")
            params.append(new_model_id)
            param_idx += 1

        if display_name is not None:
            updates.append(f"display_name = ${param_idx}")
            params.append(display_name)
            param_idx += 1

        if model_type is not None:
            updates.append(f"model_type = ${param_idx}")
            params.append(model_type)
            param_idx += 1

        if context_window is not None:
            updates.append(f"context_window = ${param_idx}")
            params.append(context_window)
            param_idx += 1

        if max_output_tokens is not None:
            updates.append(f"max_output_tokens = ${param_idx}")
            params.append(max_output_tokens)
            param_idx += 1

        if supports_vision is not None:
            updates.append(f"supports_vision = ${param_idx}")
            params.append(supports_vision)
            param_idx += 1

        if supports_tools is not None:
            updates.append(f"supports_tools = ${param_idx}")
            params.append(supports_tools)
            param_idx += 1

        if input_price_per_1k is not None:
            updates.append(f"input_price_per_1k = ${param_idx}")
            params.append(input_price_per_1k)
            param_idx += 1

        if output_price_per_1k is not None:
            updates.append(f"output_price_per_1k = ${param_idx}")
            params.append(output_price_per_1k)
            param_idx += 1

        if access_level is not None:
            updates.append(f"access_level = ${param_idx}")
            params.append(access_level)
            param_idx += 1

        if is_enabled is not None:
            updates.append(f"is_enabled = ${param_idx}")
            params.append(is_enabled)
            param_idx += 1

        if sort_order is not None:
            updates.append(f"sort_order = ${param_idx}")
            params.append(sort_order)
            param_idx += 1

        if not updates:
            return await self.get_model(tenant_id, model_id, provider_id=provider_id)

        updates.append(f"updated_at = ${param_idx}")
        params.append(datetime.now(timezone.utc))
        param_idx += 1

        # WHERE clause composition. With the post-migration-055 PK of
        # (tenant_id, provider_id, model_id), callers that know their
        # provider MUST pass it here — otherwise the UPDATE could match
        # multiple rows when the same model_id exists under ``google``
        # and ``google-vertex``. Callers that don't pass provider_id get
        # the old 2-column semantics; at worst that's a no-op when no
        # row matches.
        if provider_id is not None:
            params.extend([tenant_id, provider_id, model_id])
            where_clause = (
                f"WHERE tenant_id = ${param_idx} "
                f"AND provider_id = ${param_idx + 1} "
                f"AND model_id = ${param_idx + 2}"
            )
        else:
            params.extend([tenant_id, model_id])
            where_clause = f"WHERE tenant_id = ${param_idx} AND model_id = ${param_idx + 1}"

        query = f"""
            UPDATE llm_models
            SET {", ".join(updates)}
            {where_clause}
            RETURNING model_id, tenant_id, provider_id, display_name,
                      model_type,
                      context_window, max_output_tokens, supports_vision, supports_tools,
                      input_price_per_1k, output_price_per_1k, access_level,
                      is_enabled, sort_order, created_at, updated_at
        """
        row = await self.db.fetchrow(query, *params)

        if row:
            row_dict = self._row_to_dict(row)
            # Sync with model_pricing table for usage recording.
            # Use the RETURNING row's model_id (reflects a rename) so the
            # pricing table stays keyed to the new id. An old pricing row
            # for the original id is left alone — historic usage records
            # still reference it.
            await self._sync_single_model_pricing(
                model_id=row["model_id"],
                input_price_per_1k=float(row["input_price_per_1k"] or 0),
                output_price_per_1k=float(row["output_price_per_1k"] or 0),
                provider=row["provider_id"],
                display_name=row["display_name"],
                context_window=row["context_window"],
                max_output_tokens=row["max_output_tokens"],
                supports_vision=row["supports_vision"],
                supports_tools=row["supports_tools"],
            )
            return row_dict

        return None

    async def delete_model(
        self,
        tenant_id: str,
        model_id: str,
        provider_id: str | None = None,
    ) -> bool:
        """Delete a model.

        ``provider_id`` scopes the delete to one row when the same
        model_id exists under multiple providers. Without it, callers
        could unintentionally delete more than one row post-migration-055
        — flag this in any new caller and pass provider_id wherever the
        lookup context has it.
        """
        if provider_id is not None:
            result = await self.db.execute(
                "DELETE FROM llm_models WHERE tenant_id = $1 AND provider_id = $2 AND model_id = $3",
                tenant_id,
                provider_id,
                model_id,
            )
        else:
            result = await self.db.execute(
                "DELETE FROM llm_models WHERE tenant_id = $1 AND model_id = $2",
                tenant_id,
                model_id,
            )
        return "DELETE" in str(result) and "DELETE 0" not in str(result)

    async def toggle_model(
        self,
        tenant_id: str,
        model_id: str,
        is_enabled: bool,
        provider_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Toggle model enabled state."""
        return await self.update_model(
            tenant_id,
            model_id,
            is_enabled=is_enabled,
            provider_id=provider_id,
        )

    async def get_models_for_assistant(
        self,
        tenant_id: str,
        user_access_level: str = "public",
    ) -> list[dict[str, Any]]:
        """
        Get models available for the AI assistant.

        Only returns enabled models from enabled providers that the user has access to.
        """
        # Map user access level to SQL filter
        if user_access_level == "admin":
            access_filter = ""  # Admin sees all
        elif user_access_level == "premium":
            access_filter = "AND m.access_level IN ('public', 'premium')"
        else:
            access_filter = "AND m.access_level = 'public'"

        query = f"""
            SELECT m.model_id, m.tenant_id, m.provider_id, m.display_name,
                   m.model_type,
                   m.context_window, m.max_output_tokens, m.supports_vision, m.supports_tools,
                   m.input_price_per_1k, m.output_price_per_1k, m.access_level,
                   m.is_enabled, m.sort_order, m.created_at, m.updated_at
            FROM llm_models m
            INNER JOIN llm_providers p ON m.tenant_id = p.tenant_id AND m.provider_id = p.provider_id
            WHERE m.tenant_id = $1
              AND m.is_enabled = true
              AND p.is_enabled = true
              AND m.model_type IN ('llm', 'multimodal')
              {access_filter}
            ORDER BY p.display_name, m.sort_order, m.display_name
        """
        rows = await self.db.fetch(query, tenant_id)
        return [self._row_to_dict(row) for row in rows]

    async def sync_pricing_from_llm_models(
        self,
        tenant_id: str,
        include_disabled: bool = True,
    ) -> int:
        """
        Sync pricing records from llm_models to model_pricing table.

        Returns:
            Number of models successfully synced.
        """
        models = await self.list_models(
            tenant_id=tenant_id,
            include_disabled=include_disabled,
        )
        synced = 0
        for row in models:
            ok = await self._sync_single_model_pricing(
                model_id=row["model_id"],
                input_price_per_1k=float(row.get("input_price_per_1k") or 0),
                output_price_per_1k=float(row.get("output_price_per_1k") or 0),
                provider=row.get("provider_id"),
                display_name=row.get("display_name"),
                context_window=row.get("context_window"),
                max_output_tokens=row.get("max_output_tokens"),
                supports_vision=row.get("supports_vision"),
                supports_tools=row.get("supports_tools"),
            )
            if ok:
                synced += 1
        return synced

    async def _sync_single_model_pricing(
        self,
        *,
        model_id: str,
        input_price_per_1k: float,
        output_price_per_1k: float,
        provider: str | None = None,
        display_name: str | None = None,
        context_window: int | None = None,
        max_output_tokens: int | None = None,
        supports_vision: bool | None = None,
        supports_tools: bool | None = None,
    ) -> bool:
        try:
            pricing_svc = get_pricing_service()
            await pricing_svc.update_pricing(
                model=model_id,
                input_price_per_1k=input_price_per_1k,
                output_price_per_1k=output_price_per_1k,
                provider=provider,
                display_name=display_name,
                context_window=context_window,
                max_output_tokens=max_output_tokens,
                supports_vision=supports_vision,
                supports_tools=supports_tools,
            )
            logger.info(f"Synced pricing for model {model_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to sync pricing for model {model_id}: {e}")
            return False

    def _row_to_dict(self, row) -> dict[str, Any]:
        """Convert database row to dictionary."""
        if not row:
            return {}
        result = dict(row)
        # Convert Decimal to float for JSON serialization
        if "input_price_per_1k" in result and result["input_price_per_1k"] is not None:
            result["input_price_per_1k"] = float(result["input_price_per_1k"])
        if "output_price_per_1k" in result and result["output_price_per_1k"] is not None:
            result["output_price_per_1k"] = float(result["output_price_per_1k"])
        return result
