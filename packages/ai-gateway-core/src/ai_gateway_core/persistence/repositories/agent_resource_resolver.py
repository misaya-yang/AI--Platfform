"""Runtime authorization for Agent-bound live Knowledge resources."""

from __future__ import annotations

from typing import Any


class AgentKnowledgeAuthorizationError(RuntimeError):
    """One or more bound Datasets are unavailable to the current principal."""

    def __init__(self, code: str = "AGENT_KNOWLEDGE_UNAVAILABLE"):
        self.code = code
        super().__init__(code)


async def authorized_dataset_ids(
    connection: Any,
    *,
    tenant_id: str,
    user_id: str,
    dataset_ids: list[str],
    is_tenant_admin: bool,
) -> set[str]:
    """Apply the existing Dataset user/role/visibility contract in SQL."""

    if not dataset_ids:
        return set()
    rows = await connection.fetch(
        """
        SELECT dataset.dataset_id
        FROM datasets AS dataset
        WHERE dataset.tenant_id = $1
          AND dataset.dataset_id = ANY($3::varchar[])
          AND dataset.is_deleted = FALSE
          AND (
              $4::boolean
              OR dataset.created_by = $2
              OR dataset.visibility IN ('tenant', 'public')
              OR EXISTS (
                  SELECT 1 FROM dataset_permissions AS permission
                  WHERE permission.dataset_id = dataset.dataset_id
                    AND permission.subject_type = 'user'
                    AND permission.subject_id = $2
                    AND permission.permission IN ('viewer', 'editor', 'owner')
              )
              OR EXISTS (
                  SELECT 1
                  FROM dataset_permissions AS permission
                  JOIN users AS actor
                    ON actor.tenant_id = dataset.tenant_id
                   AND actor.user_id = $2
                  WHERE permission.dataset_id = dataset.dataset_id
                    AND permission.subject_type = 'role'
                    AND permission.subject_id = ANY(actor.roles)
                    AND permission.permission IN ('viewer', 'editor', 'owner')
              )
          )
        """,
        tenant_id,
        user_id,
        dataset_ids,
        is_tenant_admin,
    )
    return {str(row["dataset_id"]) for row in rows}


class DatabaseAgentKnowledgeResolver:
    """Gateway resolver that rechecks every Dataset binding for every run."""

    def __init__(self, pool_holder: Any):
        self._holder = pool_holder

    @property
    def _pool(self) -> Any:
        return getattr(self._holder, "_pool", None)

    async def resolve(
        self,
        *,
        tenant_id: str,
        user_id: str,
        bindings: list[dict[str, Any]],
        is_tenant_admin: bool = False,
        **_kwargs: Any,
    ) -> list[dict[str, Any]]:
        if not getattr(self._holder, "enabled", False) or self._pool is None:
            raise AgentKnowledgeAuthorizationError()
        normalized = [
            dict(binding)
            for binding in bindings
            if isinstance(binding, dict) and str(binding.get("dataset_id") or "")
        ]
        dataset_ids = [str(binding["dataset_id"]) for binding in normalized]
        async with self._pool.acquire() as connection:
            allowed = await authorized_dataset_ids(
                connection,
                tenant_id=tenant_id,
                user_id=user_id,
                dataset_ids=dataset_ids,
                is_tenant_admin=is_tenant_admin,
            )
        if allowed != set(dataset_ids):
            raise AgentKnowledgeAuthorizationError()
        return [binding for binding in normalized if str(binding["dataset_id"]) in allowed]


__all__ = [
    "AgentKnowledgeAuthorizationError",
    "DatabaseAgentKnowledgeResolver",
    "authorized_dataset_ids",
]
