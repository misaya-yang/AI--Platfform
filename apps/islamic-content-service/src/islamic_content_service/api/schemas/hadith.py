from __future__ import annotations

from pydantic import BaseModel, Field


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
    section_number: str | None = None
    section_title: str | None = None
    chapter_id: str | None = None
    chapter_title: str | None = None
    hadith_number: str
    title: str | None = None
    preview_text: str = ""
    arabic_preview_text: str = ""


class HadithDetailSchema(BaseModel):
    collection: str | None = None
    book_number: str
    section_number: str | None = None
    section_title: str | None = None
    chapter_id: str | None = None
    hadith_number: str
    chapter_title: str | None = None
    translation_text: str = ""
    arabic_text: str = ""
    grades: dict[str, list] = Field(default_factory=dict)


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


class HadithChapterSchema(BaseModel):
    chapter_id: str
    chapter_number: int | None = None
    chapter_id_raw: str | None = None
    chapter_title: str
    title_en: str | None = None
    title_ar: str | None = None
    intro_en: str | None = None
    intro_ar: str | None = None
    hadith_count: int


class HadithChaptersResponse(BaseModel):
    generated_at: str
    screen: str = "hadith_chapters"
    source_api: str
    collection_name: str
    book_number: str
    chapters: list[HadithChapterSchema]


class HadithDetailResponse(BaseModel):
    generated_at: str
    screen: str = "hadith_detail"
    source_api: str
    hadith: HadithDetailSchema


class HadithSearchHitSchema(BaseModel):
    collection: str
    book_number: str
    book_title: str | None = None
    chapter_title: str | None = None
    hadith_number: str
    language: str
    preview_text: str = ""


class HadithCollectionDetailResponse(BaseModel):
    generated_at: str
    screen: str = "hadith_collection_detail"
    source_api: str
    collection: HadithCollectionSchema


class HadithRandomResponse(BaseModel):
    generated_at: str
    screen: str = "hadith_random"
    source_api: str
    hadith: HadithDetailSchema


class HadithNeighborsSchema(BaseModel):
    previous: str | None = None
    next: str | None = None


class HadithContextResponse(BaseModel):
    generated_at: str
    screen: str = "hadith_context"
    source_api: str
    collection: str
    hadith_number: str
    neighbors: HadithNeighborsSchema


class HadithSearchResponse(BaseModel):
    generated_at: str
    screen: str = "hadith_search"
    source_api: str
    query: str
    language: str
    collection: str | None = None
    total: int
    limit: int
    offset: int
    items: list[HadithSearchHitSchema]
