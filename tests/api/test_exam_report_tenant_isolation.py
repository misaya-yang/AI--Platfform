from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI, HTTPException, Request

from src.api.v1 import exams as exam_routes
from src.core.auth.user_resolver import UserContext

EXAM_ID = "00000000-0000-0000-0000-000000000011"
REPORT_ID = "00000000-0000-0000-0000-000000000022"
TENANT_A = "tenant-a"


def _request_with_database(database: object) -> Request:
    app = FastAPI()
    app.state.database = database
    return Request(
        {
            "type": "http",
            "app": app,
            "headers": [],
            "method": "GET",
            "path": "/api/v1/exams",
        }
    )


def _tenant_admin(tenant_id: str = TENANT_A) -> UserContext:
    return UserContext(
        user_id="admin-user",
        tenant_id=tenant_id,
        is_authenticated=True,
        roles=["admin"],
    )


class _ReportDatabase:
    def __init__(self) -> None:
        self.fetch_call: tuple[str, tuple[object, ...]] | None = None
        self.fetchrow_call: tuple[str, tuple[object, ...]] | None = None

    async def fetch(self, query: str, *args: object) -> list[dict]:
        self.fetch_call = (query, args)
        return []

    async def fetchrow(self, query: str, *args: object) -> None:
        self.fetchrow_call = (query, args)
        # This represents a report belonging to another tenant.  A correctly
        # scoped query receives tenant-a and therefore must not return it.
        return None


def _compact_sql(query: str) -> str:
    return " ".join(query.split())


@pytest.mark.asyncio
async def test_list_reports_scopes_query_to_callers_tenant() -> None:
    database = _ReportDatabase()

    result = await exam_routes.list_reports(
        EXAM_ID,
        _request_with_database(database),
        user=_tenant_admin(),
    )

    assert result == {"reports": []}
    assert database.fetch_call is not None
    query, args = database.fetch_call
    assert "JOIN exams e ON e.id = r.exam_id" in _compact_sql(query)
    assert "r.exam_id = $1 AND e.tenant_id = $2" in _compact_sql(query)
    assert args == (uuid.UUID(EXAM_ID), TENANT_A)


@pytest.mark.asyncio
async def test_get_report_hides_cross_tenant_report_as_not_found() -> None:
    database = _ReportDatabase()

    with pytest.raises(HTTPException) as exc_info:
        await exam_routes.get_report(
            EXAM_ID,
            REPORT_ID,
            _request_with_database(database),
            user=_tenant_admin(),
        )

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Report not found"
    assert database.fetchrow_call is not None
    query, args = database.fetchrow_call
    assert "JOIN exams e ON e.id = r.exam_id" in _compact_sql(query)
    assert "r.id = $1 AND e.tenant_id = $2" in _compact_sql(query)
    assert args == (uuid.UUID(REPORT_ID), TENANT_A)
