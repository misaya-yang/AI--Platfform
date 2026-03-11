from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from ..deps import get_quran_query_service
from ..schemas.quran import (
    QuranAyahDetailResponse,
    QuranAyahMinimalResponse,
    QuranAyahTranslationResponse,
    QuranAudioTextResponse,
    QuranChapterAudioResponse,
    QuranChapterAyahsResponse,
    QuranChapterTranslationsResponse,
    QuranChaptersResponse,
    QuranRecitationsResponse,
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
