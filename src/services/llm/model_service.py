"""
LLM Model Service.

Manages LLM model configurations.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from ...core.observability.logging import get_logger
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
        provider_id: Optional[str] = None,
        include_disabled: bool = False,
        access_level: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        List models for a tenant.

        Args:
            tenant_id: Tenant ID
            provider_id: Optional filter by provider
            include_disabled: Include disabled models
            access_level: Filter by max access level (public, premium, admin)
        """
        conditions = ["tenant_id = $1"]
        params: List[Any] = [tenant_id]
        param_idx = 2

        if provider_id:
            conditions.append(f"provider_id = ${param_idx}")
            params.append(provider_id)
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
    ) -> Optional[Dict[str, Any]]:
        """Get a specific model."""
        query = """
            SELECT model_id, tenant_id, provider_id, display_name,
                   context_window, max_output_tokens, supports_vision, supports_tools,
                   input_price_per_1k, output_price_per_1k, access_level,
                   is_enabled, sort_order, created_at, updated_at
            FROM llm_models
            WHERE tenant_id = $1 AND model_id = $2
        """
        row = await self.db.fetchrow(query, tenant_id, model_id)
        return self._row_to_dict(row) if row else None

    async def create_model(
        self,
        tenant_id: str,
        model_id: str,
        provider_id: str,
        display_name: str,
        context_window: int = 128000,
        max_output_tokens: int = 4096,
        supports_vision: bool = False,
        supports_tools: bool = True,
        input_price_per_1k: Decimal = Decimal("0"),
        output_price_per_1k: Decimal = Decimal("0"),
        access_level: str = "public",
        is_enabled: bool = True,
        sort_order: int = 0,
    ) -> Dict[str, Any]:
        """Create a new model."""
        query = """
            INSERT INTO llm_models (
                model_id, tenant_id, provider_id, display_name,
                context_window, max_output_tokens, supports_vision, supports_tools,
                input_price_per_1k, output_price_per_1k, access_level,
                is_enabled, sort_order
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
            RETURNING model_id, tenant_id, provider_id, display_name,
                      context_window, max_output_tokens, supports_vision, supports_tools,
                      input_price_per_1k, output_price_per_1k, access_level,
                      is_enabled, sort_order, created_at, updated_at
        """
        row = await self.db.fetchrow(
            query,
            model_id, tenant_id, provider_id, display_name,
            context_window, max_output_tokens, supports_vision, supports_tools,
            input_price_per_1k, output_price_per_1k, access_level,
            is_enabled, sort_order,
        )
        row_dict = self._row_to_dict(row)
        
        # Sync with model_pricing table for usage recording
        try:
            pricing_svc = get_pricing_service()
            await pricing_svc.update_pricing(
                model=model_id,
                input_price_per_1k=float(input_price_per_1k),
                output_price_per_1k=float(output_price_per_1k),
                provider=provider_id,
                display_name=display_name,
                context_window=context_window,
                max_output_tokens=max_output_tokens,
                supports_vision=supports_vision,
                supports_tools=supports_tools,
            )
            logger.info(f"Synced pricing for model {model_id}")
        except Exception as e:
            logger.error(f"Failed to sync pricing for model {model_id}: {e}")

        return row_dict

    async def update_model(
        self,
        tenant_id: str,
        model_id: str,
        display_name: Optional[str] = None,
        context_window: Optional[int] = None,
        max_output_tokens: Optional[int] = None,
        supports_vision: Optional[bool] = None,
        supports_tools: Optional[bool] = None,
        input_price_per_1k: Optional[Decimal] = None,
        output_price_per_1k: Optional[Decimal] = None,
        access_level: Optional[str] = None,
        is_enabled: Optional[bool] = None,
        sort_order: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """Update a model."""
        updates = []
        params = []
        param_idx = 1

        if display_name is not None:
            updates.append(f"display_name = ${param_idx}")
            params.append(display_name)
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
            return await self.get_model(tenant_id, model_id)

        updates.append(f"updated_at = ${param_idx}")
        params.append(datetime.now(timezone.utc))
        param_idx += 1

        params.extend([tenant_id, model_id])

        query = f"""
            UPDATE llm_models
            SET {', '.join(updates)}
            WHERE tenant_id = ${param_idx} AND model_id = ${param_idx + 1}
            RETURNING model_id, tenant_id, provider_id, display_name,
                      context_window, max_output_tokens, supports_vision, supports_tools,
                      input_price_per_1k, output_price_per_1k, access_level,
                      is_enabled, sort_order, created_at, updated_at
        """
        row = await self.db.fetchrow(query, *params)
        
        if row:
            row_dict = self._row_to_dict(row)
            # Sync with model_pricing table for usage recording
            try:
                pricing_svc = get_pricing_service()
                # Use values from the updated row to ensure we have the latest state
                await pricing_svc.update_pricing(
                    model=model_id,
                    input_price_per_1k=float(row['input_price_per_1k'] or 0),
                    output_price_per_1k=float(row['output_price_per_1k'] or 0),
                    provider=row['provider_id'],
                    display_name=row['display_name'],
                    context_window=row['context_window'],
                    max_output_tokens=row['max_output_tokens'],
                    supports_vision=row['supports_vision'],
                    supports_tools=row['supports_tools'],
                )
                logger.info(f"Synced pricing for model {model_id}")
            except Exception as e:
                logger.error(f"Failed to sync pricing for model {model_id}: {e}")
            return row_dict
            
        return None

    async def delete_model(
        self,
        tenant_id: str,
        model_id: str,
    ) -> bool:
        """Delete a model."""
        result = await self.db.execute(
            "DELETE FROM llm_models WHERE tenant_id = $1 AND model_id = $2",
            tenant_id, model_id,
        )
        return "DELETE 1" in str(result)

    async def toggle_model(
        self,
        tenant_id: str,
        model_id: str,
        is_enabled: bool,
    ) -> Optional[Dict[str, Any]]:
        """Toggle model enabled state."""
        return await self.update_model(tenant_id, model_id, is_enabled=is_enabled)

    async def get_models_for_assistant(
        self,
        tenant_id: str,
        user_access_level: str = "public",
    ) -> List[Dict[str, Any]]:
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
                   m.context_window, m.max_output_tokens, m.supports_vision, m.supports_tools,
                   m.input_price_per_1k, m.output_price_per_1k, m.access_level,
                   m.is_enabled, m.sort_order, m.created_at, m.updated_at
            FROM llm_models m
            INNER JOIN llm_providers p ON m.tenant_id = p.tenant_id AND m.provider_id = p.provider_id
            WHERE m.tenant_id = $1
              AND m.is_enabled = true
              AND p.is_enabled = true
              {access_filter}
            ORDER BY p.display_name, m.sort_order, m.display_name
        """
        rows = await self.db.fetch(query, tenant_id)
        return [self._row_to_dict(row) for row in rows]

    def _row_to_dict(self, row) -> Dict[str, Any]:
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
