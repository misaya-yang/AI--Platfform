from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from ..deps import get_dua_query_service
from ..schemas.dua import (
    DuaAllItemsResponse,
    DuaByOccasionResponse,
    DuaCategoriesResponse,
    DuaCategoryItemsResponse,
    DuaDetailResponse,
    DuaOccasionListResponse,
    DuaRandomResponse,
    DuaSearchResponse,
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
    "/random",
    response_model=DuaRandomResponse,
    summary="Return one random Dua (Dua of the Day)",
)
async def get_random_dua(service: DuaQueryService = Depends(get_dua_query_service)):
    try:
        return await service.get_random()
    except NotReadyError as exc:
        raise _to_http_error(exc) from exc


@router.get(
    "/search",
    response_model=DuaSearchResponse,
    summary="Search Duas by title or text",
    description=(
        "Case-insensitive search across `title`, `arabic_text`, `transliteration`, "
        "`english_meaning`, and `urdu_meaning`. Returns paginated matches."
    ),
)
async def search_duas(
    q: str = Query(description="Search query"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    service: DuaQueryService = Depends(get_dua_query_service),
):
    try:
        return await service.search_duas(q, limit=limit, offset=offset)
    except NotReadyError as exc:
        raise _to_http_error(exc) from exc


@router.get(
    "/occasions",
    response_model=DuaOccasionListResponse,
    summary="List all distinct occasions with dua counts",
)
async def list_occasions(service: DuaQueryService = Depends(get_dua_query_service)):
    try:
        return await service.list_occasions()
    except NotReadyError as exc:
        raise _to_http_error(exc) from exc


@router.get(
    "/by-occasion/{occasion}",
    response_model=DuaByOccasionResponse,
    summary="List Duas by occasion",
    description=(
        "Filter Duas by the `occasion` column (e.g. 'Morning', 'Before eating', "
        "'Entering mosque'). Matches are case-insensitive and substring — so "
        "`/dua/by-occasion/morning` matches 'Morning adhkar' too."
    ),
)
async def get_by_occasion(
    occasion: str,
    service: DuaQueryService = Depends(get_dua_query_service),
):
    try:
        return await service.get_items_by_occasion(occasion)
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
