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
    chapter_id: str | None = None
    hadith_number: str
    title: str | None = None
    preview_text: str = ""
    arabic_preview_text: str = ""


class HadithDetailSchema(BaseModel):
    collection: str | None = None
    book_number: str
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


class HadithDetailResponse(BaseModel):
    generated_at: str
    screen: str = "hadith_detail"
    source_api: str
    hadith: HadithDetailSchema
