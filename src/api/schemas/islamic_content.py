from __future__ import annotations

from pydantic import BaseModel, Field


class QuranWordSchema(BaseModel):
    position: int | None = None
    arabic: str | None = None
    arabic_simple: str | None = None
    transliteration: str | None = None
    translation: str | None = None
    char_type: str | None = None
    audio_url: str | None = None


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


class QuranHomeResponse(BaseModel):
    screen: str = "quran_home"
    version: str = "v1"
    generated_at: str
    header: dict[str, object]
    continue_reading: dict | None = None
    chapters: list[QuranChapterSchema]


class QuranChaptersResponse(BaseModel):
    generated_at: str
    source_api: str
    chapters: list[QuranChapterSchema]


class QuranChapterAyahsResponse(BaseModel):
    generated_at: str
    source_api: str
    chapter_id: int
    translation_id: int
    recitation_id: int
    ayahs: list[QuranAyahSchema]


class QuranAyahDetailResponse(BaseModel):
    generated_at: str
    screen: str = "quran_ayah_detail"
    translation_id: int
    recitation_id: int
    ayah: QuranAyahSchema


class QuranTripletsResponse(BaseModel):
    generated_at: str
    screen: str = "quran_triplets"
    chapter_id: int
    translation_id: int
    recitation_id: int
    blocks: list[QuranTripletSchema]


class QuranTranslationsResponse(BaseModel):
    generated_at: str
    source_api: str
    translations: list[dict]


class QuranChapterTranslationsResponse(BaseModel):
    generated_at: str
    source_api: str
    chapter_id: int
    translation_id: int
    items: list[QuranTranslationItemSchema]


class QuranAyahTranslationResponse(BaseModel):
    generated_at: str
    source_api: str
    translation_id: int
    item: QuranTranslationItemSchema


class QuranRecitationsResponse(BaseModel):
    generated_at: str
    source_api: str
    recitations: list[dict]


class HadithCollectionSchema(BaseModel):
    name: str | None = None
    title: str | None = None
    short_intro: str | None = None
    has_books: bool | None = None
    has_chapters: bool | None = None
    total_books: int | None = None
    total_hadith: int | None = None


class HadithBookSchema(BaseModel):
    book_number: str
    title: str | None = None
    hadith_start_number: int | None = None
    hadith_end_number: int | None = None
    number_of_hadith: int | None = None


class HadithSummarySchema(BaseModel):
    collection: str | None = None
    book_number: str
    chapter_id: str
    hadith_number: str
    title: str | None = None
    preview_text: str = ""
    arabic_preview_text: str = ""


class HadithDetailSchema(BaseModel):
    collection: str | None = None
    book_number: str
    chapter_id: str
    hadith_number: str
    chapter_title: str | None = None
    translation_text: str = ""
    arabic_text: str = ""
    grades: dict[str, list] = Field(default_factory=dict)
    share_actions: list[str] = Field(default_factory=list)


class HadithCollectionsResponse(BaseModel):
    generated_at: str
    screen: str = "hadith_collections"
    source_api: str
    collections: list[HadithCollectionSchema]


class HadithBooksResponse(BaseModel):
    generated_at: str
    screen: str = "hadith_books"
    source_api: str
    collection: HadithCollectionSchema
    books: list[HadithBookSchema]


class HadithBookItemsResponse(BaseModel):
    generated_at: str
    screen: str = "hadith_book_items"
    source_api: str
    collection_name: str
    book_number: str
    items: list[HadithSummarySchema]
    pagination: dict = Field(default_factory=dict)


class HadithDetailResponse(BaseModel):
    generated_at: str
    screen: str = "hadith_detail"
    source_api: str
    hadith: HadithDetailSchema


class DuaCategorySchema(BaseModel):
    category: str
    dua_count: int


class DuaItemSchema(BaseModel):
    dua_id: str
    title: str
    category: str
    arabic_text: str = ""
    transliteration_text: str = ""
    translation_text: str = ""
    reference: str = ""
    benefit: str = ""


class DuaCategoriesResponse(BaseModel):
    screen: str = "dua_category_home"
    generated_at: str
    source_api: str
    categories: list[DuaCategorySchema]


class DuaItemsResponse(BaseModel):
    screen: str = "dua_list"
    generated_at: str
    source_api: str
    items: list[DuaItemSchema]


class PrayerItemSchema(BaseModel):
    name: str
    time: str | None = None


class PrayerTimesResponse(BaseModel):
    screen: str = "prayer_times_home"
    generated_at: str
    source_api: str
    location: dict
    date: dict
    prayers: list[PrayerItemSchema]
    meta: dict = Field(default_factory=dict)


class QiblaResponse(BaseModel):
    screen: str = "qiblah_home"
    generated_at: str
    source_api: str
    location: dict
    qiblah_bearing: float | int | None = None
    meta: dict = Field(default_factory=dict)


class SyncManifestResponse(BaseModel):
    generated_at: str
    cache_dir: str
    steps: list[dict]


class CanonicalSummaryResponse(BaseModel):
    database_enabled: bool
    generated_at: str
    counts: dict[str, int] = Field(default_factory=dict)
