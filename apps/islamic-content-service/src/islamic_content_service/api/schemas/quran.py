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


class QuranJuzSchema(BaseModel):
    juz_number: int = Field(description="1..30")
    name_arabic: str = Field(description="Traditional Juz name in Arabic (incipit)")
    name_simple: str = Field(description="Simplified Latin spelling (e.g. 'Alif Lam Mim')")
    name_transliteration: str = Field(description="Scholarly transliteration with diacritics")
    first_verse_key: str = Field(description="verse_key of the first ayah in this Juz (e.g. '1:1')")
    last_verse_key: str = Field(description="verse_key of the last ayah in this Juz (e.g. '2:141')")
    start_chapter_id: int = Field(description="chapter_id of the first ayah")
    start_chapter_name_simple: str | None = Field(default=None, description="Surah name of the first ayah (e.g. 'Al-Fatihah')")
    start_chapter_name_arabic: str | None = None
    start_ayah_number: int = Field(description="ayah_number of the first ayah")
    verses_count: int = Field(description="Total ayahs in this Juz")
    verse_mapping: dict[str, str] = Field(
        default_factory=dict,
        description="chapter_id -> ayah range string, e.g. {'1': '1-7', '2': '1-141'}",
    )


class QuranJuzsResponse(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "generated_at": "2026-04-23T03:00:00+00:00",
                "screen": "quran_juzs",
                "source_api": "quran.foundation+internal",
                "juzs": [
                    {
                        "juz_number": 1,
                        "name_arabic": "الم",
                        "name_simple": "Alif Lam Mim",
                        "name_transliteration": "Alif Lām Mīm",
                        "first_verse_key": "1:1",
                        "last_verse_key": "2:141",
                        "start_chapter_id": 1,
                        "start_chapter_name_simple": "Al-Fatihah",
                        "start_chapter_name_arabic": "ٱلْفَاتِحَة",
                        "start_ayah_number": 1,
                        "verses_count": 148,
                        "verse_mapping": {"1": "1-7", "2": "1-141"},
                    }
                ],
            }
        }
    )
    generated_at: str
    screen: str = "quran_juzs"
    source_api: str
    juzs: list[QuranJuzSchema]


class QuranJuzDetailResponse(BaseModel):
    generated_at: str
    screen: str = "quran_juz_detail"
    source_api: str
    juz: QuranJuzSchema


class QuranJuzAyahsResponse(BaseModel):
    generated_at: str
    screen: str = "quran_juz_ayahs"
    source_api: str
    juz: QuranJuzSchema
    translation_id: int
    recitation_id: int
    ayahs: list[QuranAyahSchema]


class QuranSearchHitSchema(BaseModel):
    verse_key: str
    surah_number: int
    ayah_number: int
    chapter_name_simple: str | None = None
    chapter_name_arabic: str | None = None
    arabic_text: str = ""
    translation_text: str = ""
    match_field: str = Field(description="Which field matched: 'arabic' or 'translation'")


class QuranSearchResponse(BaseModel):
    generated_at: str
    screen: str = "quran_search"
    source_api: str
    query: str
    translation_id: int
    total: int
    limit: int
    offset: int
    items: list[QuranSearchHitSchema]


class QuranChapterDetailResponse(BaseModel):
    generated_at: str
    screen: str = "quran_chapter_detail"
    source_api: str
    chapter: QuranChapterSchema


class QuranRandomAyahResponse(BaseModel):
    generated_at: str
    screen: str = "quran_random_ayah"
    source_api: str
    translation_id: int
    recitation_id: int
    ayah: QuranAyahSchema


class QuranPageAyahsResponse(BaseModel):
    generated_at: str
    screen: str = "quran_page_ayahs"
    source_api: str
    page_number: int
    translation_id: int
    recitation_id: int
    ayahs: list[QuranAyahSchema]


class QuranAyahsRangeResponse(BaseModel):
    generated_at: str
    screen: str = "quran_ayahs_range"
    source_api: str
    from_verse_key: str
    to_verse_key: str
    translation_id: int
    recitation_id: int
    ayahs: list[QuranAyahSchema]


class QuranSajdahPointSchema(BaseModel):
    sajdah_number: int = Field(description="1..15 in recitation order")
    verse_key: str
    surah_number: int
    ayah_number: int
    sajdah_type: str = Field(description="'obligatory' or 'recommended'")
    chapter_name_simple: str | None = None
    chapter_name_arabic: str | None = None
    arabic_text: str = ""
    translation_text: str = ""


class QuranSajdahsResponse(BaseModel):
    generated_at: str
    screen: str = "quran_sajdahs"
    source_api: str
    translation_id: int
    total: int
    sajdahs: list[QuranSajdahPointSchema]


class QuranHizbSchema(BaseModel):
    hizb_number: int = Field(description="1..60")
    juz_number: int = Field(description="Parent Juz (each Juz = 2 Hizbs)")
    first_verse_key: str
    last_verse_key: str
    start_chapter_id: int
    start_chapter_name_simple: str | None = None
    start_chapter_name_arabic: str | None = None
    start_ayah_number: int
    verses_count: int


class QuranHizbsResponse(BaseModel):
    generated_at: str
    screen: str = "quran_hizbs"
    source_api: str
    hizbs: list[QuranHizbSchema]


QuranWordSchema.model_rebuild()
