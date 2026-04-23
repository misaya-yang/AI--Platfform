from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from ..deps import get_quran_query_service
from ..schemas.quran import (
    QuranAyahDetailResponse,
    QuranAyahMinimalResponse,
    QuranAyahTranslationResponse,
    QuranAudioTextResponse,
    QuranAyahsRangeResponse,
    QuranChapterAudioResponse,
    QuranChapterAyahsResponse,
    QuranChapterDetailResponse,
    QuranChapterTranslationsResponse,
    QuranChaptersResponse,
    QuranHizbsResponse,
    QuranJuzAyahsResponse,
    QuranJuzDetailResponse,
    QuranJuzsResponse,
    QuranPageAyahsResponse,
    QuranRandomAyahResponse,
    QuranRecitationsResponse,
    QuranSajdahsResponse,
    QuranSearchResponse,
    QuranTranslationsResponse,
    QuranTripletsResponse,
)
from ...domain.errors import NotReadyError
from ...services.quran_query_service import QuranQueryService

router = APIRouter(prefix="/quran", tags=["Quran"])

NOT_READY_RESPONSE = {
    "description": "Requested Quran content has not been bootstrapped yet",
    "content": {
        "application/json": {
            "example": {
                "detail": "No Quran ayah data found for 1:1",
            }
        }
    },
}

AUDIO_TEXT_EXAMPLE = {
    "generated_at": "2026-03-09T10:11:13.746769+00:00",
    "screen": "quran_audio_text",
    "chapter_id": 1,
    "translation_id": 20,
    "recitation_id": 7,
    "chapter_audio": {
        "chapter_id": 1,
        "recitation_id": 7,
        "audio_url": "https://download.quranicaudio.com/qdc/mishari_al_afasy/murattal/1.mp3",
        "source_api": "quran.foundation",
        "timings": [
            {
                "verse_key": "1:1",
                "timestamp_from_ms": 0,
                "timestamp_to_ms": 6090,
                "duration_ms": 6090,
                "segments": [{"word_index": 1, "start_ms": 0, "end_ms": 580}],
            }
        ],
    },
    "ayahs": [
        {
            "source_api": "quran.foundation",
            "source_type": "quran",
            "verse_key": "1:1",
            "surah_number": 1,
            "ayah_number": 1,
            "arabic_text": "بِسْمِ اللَّهِ الرَّحْمَٰنِ الرَّحِيمِ",
            "transliteration_text": "bis'mi l-lahi l-rahmani l-rahimi",
            "translation_text": "In the name of Allah, the Entirely Merciful, the Especially Merciful.",
            "words": [
                {
                    "position": 1,
                    "arabic": "بِسْمِ",
                    "transliteration": "bis'mi",
                    "translation": "In the name",
                    "segment": {"word_index": 1, "start_ms": 0, "end_ms": 580},
                }
            ],
            "timing": {
                "verse_key": "1:1",
                "timestamp_from_ms": 0,
                "timestamp_to_ms": 6090,
                "duration_ms": 6090,
                "segments": [{"word_index": 1, "start_ms": 0, "end_ms": 580}],
            },
            "audio": {
                "recitation_id": 7,
                "translation_id": 20,
                "url": "https://verses.quran.foundation/Alafasy/mp3/001001.mp3",
            },
        }
    ],
}

AYAH_DETAIL_EXAMPLE = {
    "generated_at": "2026-03-09T10:11:13.746769+00:00",
    "screen": "quran_ayah_detail",
    "translation_id": 20,
    "recitation_id": 7,
    "chapter_audio": {
        "chapter_id": 1,
        "recitation_id": 7,
        "audio_url": "https://download.quranicaudio.com/qdc/mishari_al_afasy/murattal/1.mp3",
        "source_api": "quran.foundation",
        "timings": [
            {
                "verse_key": "1:1",
                "timestamp_from_ms": 0,
                "timestamp_to_ms": 6090,
                "duration_ms": 6090,
                "segments": [{"word_index": 1, "start_ms": 0, "end_ms": 580}],
            }
        ],
    },
    "ayah": AUDIO_TEXT_EXAMPLE["ayahs"][0],
}

AYAH_MINIMAL_EXAMPLE = {
    "generated_at": "2026-03-11T08:54:28.042639+00:00",
    "source_api": "quran.foundation",
    "source_type": "quran",
    "verse_key": "1:1",
    "surah_number": 1,
    "ayah_number": 1,
    "translation_id": 20,
    "recitation_id": 7,
    "arabic_text": "بِسْمِ ٱللَّهِ ٱلرَّحْمَـٰنِ ٱلرَّحِيمِ",
    "transliteration_text": "bis'mi l-lahi l-raḥmāni l-raḥīmi",
    "translation_text": "In the name of Allāh, the Entirely Merciful, the Especially Merciful.",
}


def _to_http_error(exc: NotReadyError) -> HTTPException:
    return HTTPException(status_code=503, detail=str(exc))


@router.get(
    "/chapters",
    response_model=QuranChaptersResponse,
    summary="List Quran chapters",
)
async def get_chapters(service: QuranQueryService = Depends(get_quran_query_service)):
    try:
        return await service.get_chapters()
    except NotReadyError as exc:
        raise _to_http_error(exc) from exc


@router.get(
    "/resources/translations",
    response_model=QuranTranslationsResponse,
    summary="List available Quran translations metadata",
)
async def get_translations(service: QuranQueryService = Depends(get_quran_query_service)):
    return await service.get_translations()


@router.get(
    "/resources/recitations",
    response_model=QuranRecitationsResponse,
    summary="List available Quran recitations metadata",
)
async def get_recitations(service: QuranQueryService = Depends(get_quran_query_service)):
    return await service.get_recitations()


@router.get(
    "/juzs",
    response_model=QuranJuzsResponse,
    summary="List all 30 Juz with traditional names and start/end verse keys",
    description=(
        "Returns one row per Juz (1..30) aggregated from `quran_ayahs`. "
        "Each row includes the traditional Arabic name (incipit), first/last "
        "verse keys, and a per-chapter ayah-range mapping. Juz names are "
        "service-side constants — upstream Quran Foundation does not expose them."
    ),
)
async def get_juzs(service: QuranQueryService = Depends(get_quran_query_service)):
    try:
        return await service.get_juzs()
    except NotReadyError as exc:
        raise _to_http_error(exc) from exc


@router.get(
    "/juzs/{juz_number}",
    response_model=QuranJuzDetailResponse,
    summary="Get detail for a single Juz",
)
async def get_juz_detail(
    juz_number: int,
    service: QuranQueryService = Depends(get_quran_query_service),
):
    try:
        return await service.get_juz_detail(juz_number)
    except NotReadyError as exc:
        raise _to_http_error(exc) from exc


@router.get(
    "/juzs/{juz_number}/ayahs",
    response_model=QuranJuzAyahsResponse,
    summary="Get all ayahs in a Juz with translation + audio",
    description=(
        "Returns every ayah that belongs to the given Juz (1..30), ordered by "
        "chapter then ayah. Ayah payload is identical to `/chapters/{id}/ayahs` — "
        "word-level text, timings, translation, audio — so the same rendering "
        "pipeline works for both chapter-view and juz-view."
    ),
)
async def get_juz_ayahs(
    juz_number: int,
    translation_id: int | None = Query(default=None),
    recitation_id: int | None = Query(default=None),
    service: QuranQueryService = Depends(get_quran_query_service),
):
    try:
        return await service.get_juz_ayahs(
            juz_number,
            translation_id=translation_id,
            recitation_id=recitation_id,
        )
    except NotReadyError as exc:
        raise _to_http_error(exc) from exc


@router.get(
    "/hizbs",
    response_model=QuranHizbsResponse,
    summary="List all 60 Hizbs (half-Juz divisions)",
)
async def get_hizbs(service: QuranQueryService = Depends(get_quran_query_service)):
    try:
        return await service.get_hizbs()
    except NotReadyError as exc:
        raise _to_http_error(exc) from exc


@router.get(
    "/sajdahs",
    response_model=QuranSajdahsResponse,
    summary="List the 15 canonical sajdah (prostration) points",
    description=(
        "Returns the 15 verses of prostration, enriched with surah name, arabic "
        "text and translation. `sajdah_type` is 'obligatory' or 'recommended' "
        "depending on the verse (classification is canonical; schools may differ "
        "on a few positions)."
    ),
)
async def get_sajdahs(
    translation_id: int | None = Query(default=None),
    service: QuranQueryService = Depends(get_quran_query_service),
):
    try:
        return await service.get_sajdahs(translation_id=translation_id)
    except NotReadyError as exc:
        raise _to_http_error(exc) from exc


@router.get(
    "/pages/{page_number}",
    response_model=QuranPageAyahsResponse,
    summary="Get all ayahs on a standard Mushaf page (1..604)",
)
async def get_page_ayahs(
    page_number: int,
    translation_id: int | None = Query(default=None),
    recitation_id: int | None = Query(default=None),
    service: QuranQueryService = Depends(get_quran_query_service),
):
    try:
        return await service.get_page_ayahs(
            page_number,
            translation_id=translation_id,
            recitation_id=recitation_id,
        )
    except NotReadyError as exc:
        raise _to_http_error(exc) from exc


@router.get(
    "/ayahs/random",
    response_model=QuranRandomAyahResponse,
    summary="Get one random ayah (Ayah of the Day)",
)
async def get_random_ayah(
    translation_id: int | None = Query(default=None),
    recitation_id: int | None = Query(default=None),
    service: QuranQueryService = Depends(get_quran_query_service),
):
    try:
        return await service.get_random_ayah(
            translation_id=translation_id,
            recitation_id=recitation_id,
        )
    except NotReadyError as exc:
        raise _to_http_error(exc) from exc


@router.get(
    "/ayahs/range",
    response_model=QuranAyahsRangeResponse,
    summary="Batch-fetch ayahs in an inclusive range (may span multiple surahs)",
    description="Pass `from` and `to` as verse_keys like `from=1:1&to=2:5`.",
)
async def get_ayahs_range(
    from_key: str = Query(alias="from", description="Start verse_key, e.g. 1:1"),
    to_key: str = Query(alias="to", description="End verse_key, e.g. 2:5"),
    translation_id: int | None = Query(default=None),
    recitation_id: int | None = Query(default=None),
    service: QuranQueryService = Depends(get_quran_query_service),
):
    try:
        return await service.get_ayahs_range(
            from_key, to_key,
            translation_id=translation_id,
            recitation_id=recitation_id,
        )
    except NotReadyError as exc:
        raise _to_http_error(exc) from exc


@router.get(
    "/chapters/{chapter_id}",
    response_model=QuranChapterDetailResponse,
    summary="Get metadata for a single Surah (no ayahs)",
)
async def get_chapter_detail(
    chapter_id: int,
    service: QuranQueryService = Depends(get_quran_query_service),
):
    try:
        return await service.get_chapter_detail(chapter_id)
    except NotReadyError as exc:
        raise _to_http_error(exc) from exc


@router.get(
    "/search",
    response_model=QuranSearchResponse,
    summary="Search ayahs by Arabic text or translation",
    description=(
        "Case-insensitive substring search across `arabic_text` and the given "
        "translation's `translation_text`. Ranks arabic matches first. Use "
        "`translation_id` to pick which translation's body to search (defaults "
        "to the service-configured default)."
    ),
)
async def search_quran(
    q: str = Query(description="Search query (Arabic letters or translation text)"),
    translation_id: int | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    service: QuranQueryService = Depends(get_quran_query_service),
):
    try:
        return await service.search_ayahs(
            q,
            translation_id=translation_id,
            limit=limit,
            offset=offset,
        )
    except NotReadyError as exc:
        raise _to_http_error(exc) from exc


@router.get(
    "/chapters/{chapter_id}/ayahs",
    response_model=QuranChapterAyahsResponse,
    summary="Get a chapter with verse text and linked chapter audio metadata",
)
async def get_chapter_ayahs(
    chapter_id: int,
    translation_id: int | None = Query(
        default=None,
        description="Optional synced translation resource id. Defaults to the configured default translation.",
    ),
    recitation_id: int | None = Query(
        default=None,
        description="Optional synced recitation resource id. Defaults to the configured default recitation.",
    ),
    service: QuranQueryService = Depends(get_quran_query_service),
):
    try:
        return await service.get_chapter_ayahs(
            chapter_id,
            translation_id=translation_id,
            recitation_id=recitation_id,
        )
    except NotReadyError as exc:
        raise _to_http_error(exc) from exc


@router.get(
    "/chapters/{chapter_id}/audio-text",
    response_model=QuranAudioTextResponse,
    summary="Get chapter audio and text in one payload",
    description="Returns chapter-level audio track, verse timings, word-level segments, and ayah text together.",
    responses={
        200: {
            "description": "Chapter audio, timings, and ayah text returned successfully",
            "content": {"application/json": {"example": AUDIO_TEXT_EXAMPLE}},
        },
        503: NOT_READY_RESPONSE,
    },
)
async def get_audio_text(
    chapter_id: int,
    translation_id: int | None = Query(
        default=None,
        description="Optional synced translation resource id. Defaults to the configured default translation.",
    ),
    recitation_id: int | None = Query(
        default=None,
        description="Optional synced recitation resource id. Defaults to the configured default recitation.",
    ),
    service: QuranQueryService = Depends(get_quran_query_service),
):
    try:
        return await service.get_chapter_audio_text(
            chapter_id,
            translation_id=translation_id,
            recitation_id=recitation_id,
        )
    except NotReadyError as exc:
        raise _to_http_error(exc) from exc


@router.get(
    "/chapters/{chapter_id}/triplets",
    response_model=QuranTripletsResponse,
    summary="Get 3-ayah Quran blocks for AI Quran",
)
async def get_triplets(
    chapter_id: int,
    translation_id: int | None = Query(
        default=None,
        description="Optional synced translation resource id. Defaults to the configured default translation.",
    ),
    recitation_id: int | None = Query(
        default=None,
        description="Optional synced recitation resource id. Defaults to the configured default recitation.",
    ),
    service: QuranQueryService = Depends(get_quran_query_service),
):
    try:
        return await service.get_chapter_triplets(
            chapter_id,
            translation_id=translation_id,
            recitation_id=recitation_id,
        )
    except NotReadyError as exc:
        raise _to_http_error(exc) from exc


@router.get(
    "/ayahs/{verse_key}",
    response_model=QuranAyahDetailResponse,
    summary="Get one ayah with text, words, and audio timing",
    responses={
        200: {
            "description": "Ayah detail with linked chapter audio and word segments",
            "content": {"application/json": {"example": AYAH_DETAIL_EXAMPLE}},
        },
        503: NOT_READY_RESPONSE,
    },
)
async def get_ayah(
    verse_key: str,
    translation_id: int | None = Query(
        default=None,
        description="Optional synced translation resource id. Defaults to the configured default translation.",
    ),
    recitation_id: int | None = Query(
        default=None,
        description="Optional synced recitation resource id. Defaults to the configured default recitation.",
    ),
    service: QuranQueryService = Depends(get_quran_query_service),
):
    try:
        return await service.get_ayah_detail(
            verse_key,
            translation_id=translation_id,
            recitation_id=recitation_id,
        )
    except NotReadyError as exc:
        raise _to_http_error(exc) from exc


@router.get(
    "/ayahs/{verse_key}/minimal",
    response_model=QuranAyahMinimalResponse,
    summary="Get one ayah as minimal Arabic/transliteration/translation text",
    description="Returns the smallest text-only ayah payload without chapter audio, word timings, or word-by-word details.",
    responses={
        200: {
            "description": "Minimal ayah payload returned successfully",
            "content": {"application/json": {"example": AYAH_MINIMAL_EXAMPLE}},
        },
        503: NOT_READY_RESPONSE,
    },
)
async def get_ayah_minimal(
    verse_key: str,
    translation_id: int | None = Query(
        default=None,
        description="Optional synced translation resource id. Defaults to the configured default translation.",
    ),
    recitation_id: int | None = Query(
        default=None,
        description="Optional synced recitation resource id. Defaults to the configured default recitation.",
    ),
    service: QuranQueryService = Depends(get_quran_query_service),
):
    try:
        return await service.get_ayah_minimal(
            verse_key,
            translation_id=translation_id,
            recitation_id=recitation_id,
        )
    except NotReadyError as exc:
        raise _to_http_error(exc) from exc


@router.get(
    "/ayahs/{verse_key}/translation",
    response_model=QuranAyahTranslationResponse,
    summary="Get translation text for a single ayah",
)
async def get_ayah_translation(
    verse_key: str,
    translation_id: int | None = Query(
        default=None,
        description="Optional synced translation resource id. Defaults to the configured default translation.",
    ),
    service: QuranQueryService = Depends(get_quran_query_service),
):
    try:
        return await service.get_ayah_translation(
            verse_key,
            translation_id=translation_id,
        )
    except NotReadyError as exc:
        raise _to_http_error(exc) from exc


@router.get(
    "/chapters/{chapter_id}/translations",
    response_model=QuranChapterTranslationsResponse,
    summary="Get chapter translations only",
)
async def get_chapter_translations(
    chapter_id: int,
    translation_id: int | None = Query(
        default=None,
        description="Optional synced translation resource id. Defaults to the configured default translation.",
    ),
    service: QuranQueryService = Depends(get_quran_query_service),
):
    try:
        return await service.get_chapter_translations(
            chapter_id,
            translation_id=translation_id,
        )
    except NotReadyError as exc:
        raise _to_http_error(exc) from exc


@router.get(
    "/chapters/{chapter_id}/audio",
    response_model=QuranChapterAudioResponse,
    summary="Get chapter audio track with verse and word timing segments",
)
async def get_chapter_audio(
    chapter_id: int,
    recitation_id: int | None = Query(
        default=None,
        description="Optional synced recitation resource id. Defaults to the configured default recitation.",
    ),
    service: QuranQueryService = Depends(get_quran_query_service),
):
    try:
        return await service.get_chapter_audio(
            chapter_id,
            recitation_id=recitation_id,
        )
    except NotReadyError as exc:
        raise _to_http_error(exc) from exc
