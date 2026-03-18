from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..deps import get_dua_query_service
from ..schemas.dua import (
    DuaAllItemsResponse,
    DuaCategoriesResponse,
    DuaCategoryItemsResponse,
    DuaDetailResponse,
)
from ...domain.errors import NotReadyError
from ...services.dua_query_service import DuaQueryService

router = APIRouter(prefix="/dua", tags=["Dua"])


def _to_http_error(exc: NotReadyError) -> HTTPException:
    return HTTPException(status_code=503, detail=str(exc))


@router.get(
    "/categories",
    response_model=DuaCategoriesResponse,
    summary="List all Dua categories",
)
async def get_categories(service: DuaQueryService = Depends(get_dua_query_service)):
    try:
        return await service.get_categories()
    except NotReadyError as exc:
        raise _to_http_error(exc) from exc


@router.get(
    "/categories/{category}",
    response_model=DuaCategoryItemsResponse,
    summary="List Duas in a category",
)
async def get_category_items(
    category: str,
    service: DuaQueryService = Depends(get_dua_query_service),
):
    try:
        return await service.get_items_by_category(category)
    except NotReadyError as exc:
        raise _to_http_error(exc) from exc


@router.get(
    "/items",
    response_model=DuaAllItemsResponse,
    summary="List all Duas",
)
async def get_all_items(service: DuaQueryService = Depends(get_dua_query_service)):
    try:
        return await service.get_all_items()
    except NotReadyError as exc:
        raise _to_http_error(exc) from exc


@router.get(
    "/{dua_id}",
    response_model=DuaDetailResponse,
    summary="Get Dua detail",
)
async def get_detail(
    dua_id: str,
    service: DuaQueryService = Depends(get_dua_query_service),
):
    try:
        return await service.get_detail(dua_id)
    except NotReadyError as exc:
        raise _to_http_error(exc) from exc
