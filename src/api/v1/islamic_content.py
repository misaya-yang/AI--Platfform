from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from ..deps import get_islamic_content_service
from ..schemas.islamic_content import (
    CanonicalSummaryResponse,
    DuaCategoriesResponse,
    DuaItemsResponse,
    HadithBookItemsResponse,
    HadithBooksResponse,
    HadithCollectionsResponse,
    HadithDetailResponse,
    PrayerTimesResponse,
    QiblaResponse,
    QuranAyahDetailResponse,
    QuranAyahTranslationResponse,
    QuranChaptersResponse,
    QuranChapterAyahsResponse,
    QuranChapterTranslationsResponse,
    QuranHomeResponse,
    QuranRecitationsResponse,
    QuranTranslationsResponse,
    QuranTripletsResponse,
    SyncManifestResponse,
)
from ...services.islamic_content import IslamicContentError, IslamicContentService

router = APIRouter(prefix="/islamic", tags=["Islamic Content"])


def _to_http_error(exc: IslamicContentError) -> HTTPException:
    message = str(exc)
    status_code = 503
    if "not found" in message.lower():
        status_code = 404
    elif "unsupported" in message.lower():
        status_code = 400
    return HTTPException(status_code=status_code, detail=message)


@router.get("/manifest", response_model=SyncManifestResponse)
async def get_islamic_manifest(
    service: IslamicContentService = Depends(get_islamic_content_service),
):
    try:
        return await service.get_manifest()
    except IslamicContentError as exc:
        raise _to_http_error(exc) from exc


@router.get(
    "/canonical/summary",
    response_model=CanonicalSummaryResponse,
    summary="查看 canonical PostgreSQL 落库概况",
)
async def get_islamic_canonical_summary(
    service: IslamicContentService = Depends(get_islamic_content_service),
):
    return await service.get_canonical_summary()


@router.get("/quran/home", response_model=QuranHomeResponse)
async def get_quran_home(
    continue_verse_key: str | None = Query(default=None),
    use_cache: bool = Query(default=True),
    service: IslamicContentService = Depends(get_islamic_content_service),
):
    try:
        return await service.get_quran_home(
            continue_verse_key=continue_verse_key,
            use_cache=use_cache,
        )
    except IslamicContentError as exc:
        raise _to_http_error(exc) from exc


@router.get("/quran/resources/translations", response_model=QuranTranslationsResponse)
async def get_quran_translations(
    use_cache: bool = Query(default=True),
    service: IslamicContentService = Depends(get_islamic_content_service),
):
    try:
        return await service.get_quran_translations(use_cache=use_cache)
    except IslamicContentError as exc:
        raise _to_http_error(exc) from exc


@router.get("/quran/resources/recitations", response_model=QuranRecitationsResponse)
async def get_quran_recitations(
    use_cache: bool = Query(default=True),
    service: IslamicContentService = Depends(get_islamic_content_service),
):
    try:
        return await service.get_quran_recitations(use_cache=use_cache)
    except IslamicContentError as exc:
        raise _to_http_error(exc) from exc


@router.get("/quran/chapters", response_model=QuranChaptersResponse)
async def get_quran_chapters(
    use_cache: bool = Query(default=True),
    service: IslamicContentService = Depends(get_islamic_content_service),
):
    try:
        return await service.get_quran_chapters(use_cache=use_cache)
    except IslamicContentError as exc:
        raise _to_http_error(exc) from exc


@router.get("/quran/chapters/{chapter_id}/ayahs", response_model=QuranChapterAyahsResponse)
async def get_quran_chapter_ayahs(
    chapter_id: int,
    translation_id: int | None = Query(default=None),
    recitation_id: int | None = Query(default=None),
    use_cache: bool = Query(default=True),
    service: IslamicContentService = Depends(get_islamic_content_service),
):
    try:
        return await service.get_quran_chapter_ayahs(
            chapter_id,
            translation_id=translation_id,
            recitation_id=recitation_id,
            use_cache=use_cache,
        )
    except IslamicContentError as exc:
        raise _to_http_error(exc) from exc


@router.get(
    "/quran/chapters/{chapter_id}/translations",
    response_model=QuranChapterTranslationsResponse,
)
async def get_quran_chapter_translations(
    chapter_id: int,
    translation_id: int | None = Query(default=None),
    use_cache: bool = Query(default=True),
    service: IslamicContentService = Depends(get_islamic_content_service),
):
    try:
        return await service.get_quran_chapter_translations(
            chapter_id,
            translation_id=translation_id,
            use_cache=use_cache,
        )
    except IslamicContentError as exc:
        raise _to_http_error(exc) from exc


@router.get("/quran/chapters/{chapter_id}/triplets", response_model=QuranTripletsResponse)
async def get_quran_triplets(
    chapter_id: int,
    translation_id: int | None = Query(default=None),
    recitation_id: int | None = Query(default=None),
    group_size: int = Query(default=3, ge=1, le=10),
    use_cache: bool = Query(default=True),
    service: IslamicContentService = Depends(get_islamic_content_service),
):
    try:
        return await service.get_quran_triplets(
            chapter_id,
            translation_id=translation_id,
            recitation_id=recitation_id,
            group_size=group_size,
            use_cache=use_cache,
        )
    except IslamicContentError as exc:
        raise _to_http_error(exc) from exc


@router.get("/quran/ayahs/{verse_key}", response_model=QuranAyahDetailResponse)
async def get_quran_ayah_detail(
    verse_key: str,
    translation_id: int | None = Query(default=None),
    recitation_id: int | None = Query(default=None),
    use_cache: bool = Query(default=True),
    service: IslamicContentService = Depends(get_islamic_content_service),
):
    try:
        return await service.get_quran_ayah(
            verse_key,
            translation_id=translation_id,
            recitation_id=recitation_id,
            use_cache=use_cache,
        )
    except IslamicContentError as exc:
        raise _to_http_error(exc) from exc


@router.get(
    "/quran/ayahs/{verse_key}/translation",
    response_model=QuranAyahTranslationResponse,
)
async def get_quran_ayah_translation(
    verse_key: str,
    translation_id: int | None = Query(default=None),
    use_cache: bool = Query(default=True),
    service: IslamicContentService = Depends(get_islamic_content_service),
):
    try:
        return await service.get_quran_ayah_translation(
            verse_key,
            translation_id=translation_id,
            use_cache=use_cache,
        )
    except IslamicContentError as exc:
        raise _to_http_error(exc) from exc


@router.get("/hadith/collections", response_model=HadithCollectionsResponse)
async def get_hadith_collections(
    use_cache: bool = Query(default=True),
    service: IslamicContentService = Depends(get_islamic_content_service),
):
    try:
        return await service.get_hadith_collections(use_cache=use_cache)
    except IslamicContentError as exc:
        raise _to_http_error(exc) from exc


@router.get("/hadith/collections/{collection_name}/books", response_model=HadithBooksResponse)
async def get_hadith_books(
    collection_name: str,
    use_cache: bool = Query(default=True),
    service: IslamicContentService = Depends(get_islamic_content_service),
):
    try:
        return await service.get_hadith_books(collection_name, use_cache=use_cache)
    except IslamicContentError as exc:
        raise _to_http_error(exc) from exc


@router.get(
    "/hadith/collections/{collection_name}/books/{book_number}/hadiths",
    response_model=HadithBookItemsResponse,
)
async def get_hadith_book_items(
    collection_name: str,
    book_number: str,
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=100),
    use_cache: bool = Query(default=True),
    service: IslamicContentService = Depends(get_islamic_content_service),
):
    try:
        return await service.get_hadith_book_items(
            collection_name,
            book_number,
            page=page,
            limit=limit,
            use_cache=use_cache,
        )
    except IslamicContentError as exc:
        raise _to_http_error(exc) from exc


@router.get(
    "/hadith/collections/{collection_name}/hadiths/{hadith_number}",
    response_model=HadithDetailResponse,
)
async def get_hadith_detail(
    collection_name: str,
    hadith_number: str,
    use_cache: bool = Query(default=True),
    service: IslamicContentService = Depends(get_islamic_content_service),
):
    try:
        return await service.get_hadith_detail(
            collection_name,
            hadith_number,
            use_cache=use_cache,
        )
    except IslamicContentError as exc:
        raise _to_http_error(exc) from exc


@router.get("/duas/categories", response_model=DuaCategoriesResponse)
async def get_dua_categories(
    service: IslamicContentService = Depends(get_islamic_content_service),
):
    try:
        return await service.get_dua_categories()
    except IslamicContentError as exc:
        raise _to_http_error(exc) from exc


@router.get("/duas/items", response_model=DuaItemsResponse)
async def get_dua_items(
    category: str | None = Query(default=None),
    search: str | None = Query(default=None),
    service: IslamicContentService = Depends(get_islamic_content_service),
):
    try:
        return await service.get_duas(category=category, search=search)
    except IslamicContentError as exc:
        raise _to_http_error(exc) from exc


@router.get("/prayer-times", response_model=PrayerTimesResponse)
async def get_prayer_times(
    latitude: float = Query(...),
    longitude: float = Query(...),
    prayer_date: str | None = Query(default=None),
    method: int | None = Query(default=None),
    service: IslamicContentService = Depends(get_islamic_content_service),
):
    try:
        return await service.get_prayer_times(
            latitude=latitude,
            longitude=longitude,
            prayer_date=prayer_date,
            method=method,
        )
    except IslamicContentError as exc:
        raise _to_http_error(exc) from exc


@router.get("/qibla", response_model=QiblaResponse)
async def get_qibla(
    latitude: float = Query(...),
    longitude: float = Query(...),
    service: IslamicContentService = Depends(get_islamic_content_service),
):
    try:
        return await service.get_qibla(latitude=latitude, longitude=longitude)
    except IslamicContentError as exc:
        raise _to_http_error(exc) from exc
