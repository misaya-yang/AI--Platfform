from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from ..deps import get_hadith_query_service
from ..schemas.hadith import (
    HadithBookItemsResponse,
    HadithBooksResponse,
    HadithCollectionsResponse,
    HadithDetailResponse,
)
from ...domain.errors import NotReadyError
from ...services.hadith_query_service import HadithQueryService

router = APIRouter(prefix="/hadith", tags=["Hadith"])


def _to_http_error(exc: NotReadyError) -> HTTPException:
    return HTTPException(status_code=503, detail=str(exc))


@router.get(
    "/collections",
    response_model=HadithCollectionsResponse,
    summary="List Hadith collections",
)
async def get_collections(service: HadithQueryService = Depends(get_hadith_query_service)):
    try:
        return await service.get_collections()
    except NotReadyError as exc:
        raise _to_http_error(exc) from exc


@router.get(
    "/collections/{collection_name}/books",
    response_model=HadithBooksResponse,
    summary="List books inside a Hadith collection",
)
async def get_books(
    collection_name: str,
    service: HadithQueryService = Depends(get_hadith_query_service),
):
    try:
        return await service.get_books(collection_name)
    except NotReadyError as exc:
        raise _to_http_error(exc) from exc


@router.get(
    "/collections/{collection_name}/books/{book_number}/hadiths",
    response_model=HadithBookItemsResponse,
    summary="List Hadith items in one book",
)
async def get_book_items(
    collection_name: str,
    book_number: str,
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
    service: HadithQueryService = Depends(get_hadith_query_service),
):
    try:
        return await service.get_book_items(collection_name, book_number, page=page, limit=limit)
    except NotReadyError as exc:
        raise _to_http_error(exc) from exc


@router.get(
    "/collections/{collection_name}/hadiths/{hadith_number}",
    response_model=HadithDetailResponse,
    summary="Get Hadith detail",
)
async def get_detail(
    collection_name: str,
    hadith_number: str,
    service: HadithQueryService = Depends(get_hadith_query_service),
):
    try:
        return await service.get_detail(collection_name, hadith_number)
    except NotReadyError as exc:
        raise _to_http_error(exc) from exc
