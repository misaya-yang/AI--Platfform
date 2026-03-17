from __future__ import annotations

from pydantic import BaseModel


class DuaCategorySchema(BaseModel):
    category: str
    dua_count: int


class DuaItemSchema(BaseModel):
    dua_id: str
    category: str
    title: str
    arabic_text: str = ""
    transliteration: str = ""
    english_meaning: str = ""
    urdu_meaning: str = ""
    source: str = ""
    reference: str = ""
    authenticity: str = ""
    occasion: str = ""
    data_source: str = ""
    verification_status: str = ""


class DuaCategoriesResponse(BaseModel):
    generated_at: str
    screen: str = "dua_categories"
    source: str
    total_categories: int
    total_duas: int
    categories: list[DuaCategorySchema]


class DuaCategoryItemsResponse(BaseModel):
    generated_at: str
    screen: str = "dua_category_items"
    source: str
    category: str
    total: int
    items: list[DuaItemSchema]


class DuaAllItemsResponse(BaseModel):
    generated_at: str
    screen: str = "dua_all_items"
    source: str
    total: int
    items: list[DuaItemSchema]


class DuaDetailResponse(BaseModel):
    generated_at: str
    screen: str = "dua_detail"
    source: str
    dua: DuaItemSchema
