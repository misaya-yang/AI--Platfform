from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from ...domain.errors import NotReadyError
from ...services.hadith_query_service import HadithQueryService
from ..deps import get_hadith_query_service
from ..schemas.hadith import (
    HadithBookItemsResponse,
    HadithBooksResponse,
    HadithChaptersResponse,
    HadithCollectionDetailResponse,
    HadithCollectionsResponse,
    HadithContextResponse,
    HadithDetailResponse,
    HadithRandomResponse,
    HadithSearchResponse,
)

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
    "/random",
    response_model=HadithRandomResponse,
    summary="Return one random Hadith (Hadith of the Day)",
)
async def get_random_hadith(
    collection: str | None = Query(default=None, description="Optionally scope to one collection"),
    service: HadithQueryService = Depends(get_hadith_query_service),
):
    try:
        return await service.get_random_hadith(collection_name=collection)
    except NotReadyError as exc:
        raise _to_http_error(exc) from exc


@router.get(
    "/collections/{collection_name}",
    response_model=HadithCollectionDetailResponse,
    summary="Get metadata for a single Hadith collection",
)
async def get_collection_detail(
    collection_name: str,
    service: HadithQueryService = Depends(get_hadith_query_service),
):
    try:
        return await service.get_collection_detail(collection_name)
    except NotReadyError as exc:
        raise _to_http_error(exc) from exc


@router.get(
    # ``:path`` converter so hadith_numbers like "1697/1698b" (5 muslim
    # rows use the slashed-variant notation) survive URL routing.
    # Without this, FastAPI/Starlette interprets the slash as a path
    # separator and the route 404s. The "/context" suffix is still
    # required so we don't accidentally swallow it into the param.
    "/collections/{collection_name}/hadiths/{hadith_number:path}/context",
    response_model=HadithContextResponse,
    summary="Get previous/next hadith numbers for reading navigation",
)
async def get_hadith_context(
    collection_name: str,
    hadith_number: str,
    service: HadithQueryService = Depends(get_hadith_query_service),
):
    try:
        return await service.get_context(collection_name, hadith_number)
    except NotReadyError as exc:
        raise _to_http_error(exc) from exc


@router.get(
    "/search",
    response_model=HadithSearchResponse,
    summary="Search Hadith text across collections",
    description=(
        "Case-insensitive substring search over `hadith_localizations.body_text`. "
        "`lang=en` searches the English translation, `lang=ar` searches the "
        "Arabic original. `collection` optionally scopes to a single collection "
        "(bukhari, muslim, abudawud, tirmidhi, nasai, ibnmajah, nawawi)."
    ),
)
async def search_hadiths(
    q: str = Query(description="Search query"),
    lang: str = Query(default="en", pattern="^(en|ar)$"),
    collection: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    service: HadithQueryService = Depends(get_hadith_query_service),
):
    try:
        return await service.search_hadiths(
            q,
            language=lang,
            collection_name=collection,
            limit=limit,
            offset=offset,
        )
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
    "/collections/{collection_name}/books/{book_number}/chapters",
    response_model=HadithChaptersResponse,
    summary="List chapters in a book",
)
async def get_chapters(
    collection_name: str,
    book_number: str,
    include_empty: bool = Query(
        default=False,
        description="Include source chapters that currently have no linked hadiths.",
    ),
    service: HadithQueryService = Depends(get_hadith_query_service),
):
    try:
        return await service.get_chapters(
            collection_name,
            book_number,
            include_empty=include_empty,
        )
    except NotReadyError as exc:
        raise _to_http_error(exc) from exc


@router.get(
    # ``:path`` converter — see context-route comment above. The 5 muslim
    # hadiths with slashed-variant numbers (e.g. "1697/1698b") need this.
    "/collections/{collection_name}/hadiths/{hadith_number:path}",
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
