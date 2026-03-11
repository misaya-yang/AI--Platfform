from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class QuranWordSchema(BaseModel):
    position: int | None = Field(default=None, description="1-based word position within the ayah")
    arabic: str | None = None
    arabic_simple: str | None = None
    transliteration: str | None = None
    translation: str | None = None
    char_type: str | None = None
    audio_url: str | None = None
    segment: "QuranAudioSegmentSchema | None" = None


class QuranAudioSegmentSchema(BaseModel):
    word_index: int = Field(description="1-based word index inside the verse")
    start_ms: int = Field(description="Start timestamp in milliseconds")
    end_ms: int = Field(description="End timestamp in milliseconds")


class QuranVerseTimingSchema(BaseModel):
    verse_key: str
    timestamp_from_ms: int | None = None
    timestamp_to_ms: int | None = None
    duration_ms: int | None = None
    segments: list[QuranAudioSegmentSchema] = Field(default_factory=list)


class QuranAudioSchema(BaseModel):
    recitation_id: int | None = None
    translation_id: int | None = None
    url: str | None = None


class QuranAyahSchema(BaseModel):
    source_api: str = "quran.foundation"
    source_type: str = "quran"
    verse_key: str
    surah_number: int | None = None
    ayah_number: int | None = None
    juz_number: int | None = None
    hizb_number: int | None = None
    rub_number: int | None = None
    page_number: int | None = None
    arabic_text: str = ""
    transliteration_text: str = ""
    translation_text: str = ""
    words: list[QuranWordSchema] = Field(default_factory=list)
    timing: QuranVerseTimingSchema | None = Field(
        default=None,
        description="Verse-level timing aligned with the chapter audio track",
    )
    audio: QuranAudioSchema = Field(default_factory=QuranAudioSchema)


class QuranChapterSchema(BaseModel):
    chapter_id: int | None = None
    name_simple: str | None = None
    name_complex: str | None = None
    name_arabic: str | None = None
    translated_name: str | None = None
    revelation_place: str | None = None
    verses_count: int | None = None


class QuranTripletAudioSchema(BaseModel):
    verse_key: str
    url: str | None = None


class QuranTripletSchema(BaseModel):
    block_id: str
    ref: str
    chapter_id: int
    group_size: int
    verse_keys: list[str]
    arabic_text: str
    transliteration_text: str
    translation_text: str
    audio_urls: list[QuranTripletAudioSchema]
    children: list[QuranAyahSchema]


class QuranTranslationItemSchema(BaseModel):
    verse_key: str
    surah_number: int | None = None
    ayah_number: int | None = None
    translation_id: int
    translation_text: str = ""


class QuranChapterAudioTrackSchema(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
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
                        "segments": [
                            {"word_index": 1, "start_ms": 0, "end_ms": 580},
                            {"word_index": 2, "start_ms": 580, "end_ms": 1409},
                        ],
                    }
                ],
            }
        }
    )
    chapter_id: int
    recitation_id: int | None = None
    audio_url: str | None = None
    source_api: str
    timings: list[QuranVerseTimingSchema] = Field(default_factory=list)


class QuranChaptersResponse(BaseModel):
    generated_at: str
    source_api: str
    chapters: list[QuranChapterSchema]


class QuranTranslationsResponse(BaseModel):
    generated_at: str
    source_api: str
    translations: list[dict]
    synced_translation_ids: list[int] = Field(default_factory=list)


class QuranRecitationsResponse(BaseModel):
    generated_at: str
    source_api: str
    recitations: list[dict]
    synced_recitation_ids: list[int] = Field(default_factory=list)


class QuranChapterAyahsResponse(BaseModel):
    generated_at: str
    source_api: str
    chapter_id: int
    translation_id: int
    recitation_id: int
    chapter_audio: QuranChapterAudioTrackSchema | None = None
    ayahs: list[QuranAyahSchema]


class QuranAyahDetailResponse(BaseModel):
    generated_at: str
    screen: str = "quran_ayah_detail"
    translation_id: int
    recitation_id: int
    chapter_audio: QuranChapterAudioTrackSchema | None = None
    ayah: QuranAyahSchema


class QuranTripletsResponse(BaseModel):
    generated_at: str
    screen: str = "quran_triplets"
    chapter_id: int
    translation_id: int
    recitation_id: int
    chapter_audio: QuranChapterAudioTrackSchema | None = None
    blocks: list[QuranTripletSchema]


class QuranAudioTextResponse(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
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
                            "segments": [
                                {"word_index": 1, "start_ms": 0, "end_ms": 580}
                            ],
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
                            "segments": [
                                {"word_index": 1, "start_ms": 0, "end_ms": 580}
                            ],
                        },
                        "audio": {
                            "recitation_id": 7,
                            "translation_id": 20,
                            "url": "https://verses.quran.foundation/Alafasy/mp3/001001.mp3",
                        },
                    }
                ],
            }
        }
    )
    generated_at: str
    screen: str = "quran_audio_text"
    chapter_id: int
    translation_id: int
    recitation_id: int
    chapter_audio: QuranChapterAudioTrackSchema
    ayahs: list[QuranAyahSchema]


class QuranAyahTranslationResponse(BaseModel):
    generated_at: str
    source_api: str
    translation_id: int
    item: QuranTranslationItemSchema


class QuranChapterTranslationsResponse(BaseModel):
    generated_at: str
    source_api: str
    chapter_id: int
    translation_id: int
    items: list[QuranTranslationItemSchema]


class QuranAyahMinimalResponse(BaseModel):
    generated_at: str
    source_api: str
    source_type: str = "quran"
    verse_key: str
    surah_number: int | None = None
    ayah_number: int | None = None
    translation_id: int
    recitation_id: int
    arabic_text: str = ""
    transliteration_text: str = ""
    translation_text: str = ""


class QuranChapterAudioResponse(QuranChapterAudioTrackSchema):
    generated_at: str


QuranWordSchema.model_rebuild()
