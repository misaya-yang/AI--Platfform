#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from typing import Any

NOISE_CODEPOINTS = (
    0x200E,  # LRM
    0x200F,  # RLM
    0x202A,
    0x202B,
    0x202C,
    0x202D,
    0x202E,
    0x2066,
    0x2067,
    0x2068,
    0x2069,
    0xFFFD,
)
NOISE_CHARS = tuple(chr(cp) for cp in NOISE_CODEPOINTS)
DEFAULT_COLLECTIONS = ("bukhari", "muslim", "abudawud", "tirmidhi", "nasai", "ibnmajah", "nawawi")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Walk Islamic Content public APIs and report data violations.")
    parser.add_argument(
        "--base-url",
        default=os.getenv("ISLAMIC_CONTENT_AUDIT_BASE_URL", "http://127.0.0.1:8091/api/v1"),
        help="Base API URL, including /api/v1.",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--collections",
        default=",".join(DEFAULT_COLLECTIONS),
        help="Comma-separated hadith collection slugs to walk.",
    )
    return parser.parse_args()


class Auditor:
    def __init__(self, base_url: str, timeout: float, collections: tuple[str, ...]) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.collections = collections
        self.violations: dict[str, list[str]] = defaultdict(list)

    def fail(self, check: str, detail: str) -> None:
        self.violations[check].append(detail)

    def get(self, path: str) -> dict[str, Any]:
        with urllib.request.urlopen(f"{self.base_url}{path}", timeout=self.timeout) as response:
            payload = json.loads(response.read())
        if not isinstance(payload, dict):
            raise TypeError(f"Expected object response for {path}")
        return payload

    def scan_obj(self, obj: Any, path_label: str, check_name: str) -> None:
        if isinstance(obj, dict):
            for key, value in obj.items():
                self.scan_obj(value, f"{path_label}.{key}", check_name)
        elif isinstance(obj, list):
            for index, value in enumerate(obj):
                self.scan_obj(value, f"{path_label}[{index}]", check_name)
        elif isinstance(obj, str) and any(ch in obj for ch in NOISE_CHARS):
            bad = [f"U+{ord(ch):04X}" for ch in obj if ch in NOISE_CHARS]
            self.fail(check_name, f"{path_label}: {bad[:5]}")

    def walk_quran(self) -> None:
        print("=== QURAN ===", flush=True)
        chapters = self.get("/quran/chapters").get("chapters", [])
        if len(chapters) != 114:
            self.fail("quran_chapter_count", f"got {len(chapters)} want 114")
            return
        by_id = {chapter.get("chapter_id"): chapter for chapter in chapters}
        for chapter in chapters:
            self.scan_obj(chapter, f"chapter[{chapter.get('chapter_id')}]", "quran_chapter_noise")

        sample_chapters = (1, 2, 18, 36, 55, 67, 78, 96, 112, 113, 114)
        for chapter_id in sample_chapters:
            try:
                response = self.get(f"/quran/chapters/{chapter_id}/ayahs")
            except Exception as exc:
                self.fail("quran_ayahs_endpoint_error", f"{chapter_id}: {exc}")
                continue
            ayahs = response.get("ayahs", [])
            expected = (by_id.get(chapter_id) or {}).get("verses_count")
            if expected and len(ayahs) != expected:
                self.fail("quran_ayahs_count_drift", f"chapter {chapter_id}: got {len(ayahs)} want {expected}")
            for ayah in ayahs:
                if not ayah.get("arabic_text"):
                    self.fail("quran_ayah_empty_arabic", str(ayah.get("verse_key")))
                self.scan_obj(ayah, f"ayah[{ayah.get('verse_key')}]", "quran_ayah_noise")

    def walk_dua(self) -> None:
        print("=== DUA ===", flush=True)
        categories = self.get("/dua/categories").get("categories", [])
        if len(categories) != 31:
            self.fail("dua_category_count", f"got {len(categories)} want 31")

        total_items = 0
        for category in categories:
            category_name = category.get("category")
            self.scan_obj(category, f"category[{category_name}]", "dua_category_noise")
            try:
                quoted = urllib.parse.quote(str(category_name), safe="")
                response = self.get(f"/dua/categories/{quoted}")
            except Exception as exc:
                self.fail("dua_category_endpoint_error", f"{category_name}: {exc}")
                continue
            items = response.get("duas") or response.get("items", [])
            total_items += len(items)
            for item in items:
                if not item.get("arabic_text"):
                    self.fail("dua_item_empty_arabic", str(item.get("dua_id")))
                if not item.get("english_meaning"):
                    self.fail("dua_item_empty_english", str(item.get("dua_id")))
                self.scan_obj(item, f"dua[{item.get('dua_id')}]", "dua_item_noise")
        if total_items != 72:
            self.fail("dua_total_items", f"got {total_items} want 72")

    def walk_hadith(self) -> None:
        print("=== HADITH ===", flush=True)
        collections_response = self.get("/hadith/collections")
        collections = {item.get("name"): item for item in collections_response.get("collections", [])}
        for collection_name in self.collections:
            collection = collections.get(collection_name)
            if collection is None:
                self.fail("hadith_collection_missing", collection_name)
                continue

            books = self.get(f"/hadith/collections/{collection_name}/books").get("books", [])
            sum_books = sum(book.get("number_of_hadith") or 0 for book in books)
            if sum_books != collection.get("total_hadith"):
                self.fail(
                    "hadith_books_sum_drift",
                    f"{collection_name}: sum(books)={sum_books} total={collection.get('total_hadith')}",
                )
            if len(books) != collection.get("total_books"):
                self.fail(
                    "hadith_books_count_drift",
                    f"{collection_name}: books={len(books)} total_books={collection.get('total_books')}",
                )

            for book in books:
                book_number = str(book.get("book_number"))
                book_total = book.get("number_of_hadith") or 0
                quoted_book = urllib.parse.quote(book_number, safe="")

                try:
                    chapters_payload = self.get(f"/hadith/collections/{collection_name}/books/{quoted_book}/chapters")
                except Exception as exc:
                    self.fail("hadith_chapters_endpoint_error", f"{collection_name}/{book_number}: {exc}")
                    continue
                chapters = chapters_payload.get("chapters", [])
                sum_chapters = sum(chapter.get("hadith_count") or 0 for chapter in chapters)
                if sum_chapters != book_total:
                    self.fail(
                        "hadith_chapter_sum_drift",
                        f"{collection_name}/{book_number}: chapters={sum_chapters} book={book_total}",
                    )
                for chapter in chapters:
                    if (chapter.get("hadith_count") or 0) <= 0:
                        self.fail(
                            "hadith_zero_count_chapter",
                            f"{collection_name}/{book_number}/chapter={chapter.get('chapter_id')}",
                        )
                    self.scan_obj(chapter, f"{collection_name}/{book_number}/chapter[{chapter.get('chapter_id')}]", "hadith_chapter_noise")

                try:
                    page1 = self.get(f"/hadith/collections/{collection_name}/books/{quoted_book}/hadiths?page=1&limit=10")
                except Exception as exc:
                    self.fail("hadith_list_endpoint_error", f"{collection_name}/{book_number}: {exc}")
                    continue
                items = page1.get("items", [])
                api_total = page1.get("pagination", {}).get("total_items")
                if api_total != book_total:
                    self.fail(
                        "hadith_list_total_drift",
                        f"{collection_name}/{book_number}: total_items={api_total} book={book_total}",
                    )

                samples = items if len(items) <= 3 else [items[0], items[len(items) // 2], items[-1]]
                last_page = max((book_total - 1) // 10 + 1, 1)
                if last_page > 1:
                    try:
                        last = self.get(
                            f"/hadith/collections/{collection_name}/books/{quoted_book}/hadiths?page={last_page}&limit=10"
                        )
                        last_items = last.get("items", [])
                        if last_items:
                            samples.append(last_items[-1])
                    except Exception as exc:
                        self.fail("hadith_last_page_endpoint_error", f"{collection_name}/{book_number}: {exc}")

                for item in samples[:5]:
                    hadith_number = item.get("hadith_number")
                    if not hadith_number:
                        self.fail("hadith_missing_number", f"{collection_name}/{book_number}: {item}")
                        continue
                    try:
                        quoted_hadith = urllib.parse.quote(str(hadith_number), safe="/")
                        detail = self.get(f"/hadith/collections/{collection_name}/hadiths/{quoted_hadith}")
                    except Exception as exc:
                        self.fail("hadith_detail_endpoint_error", f"{collection_name}/{book_number}/{hadith_number}: {exc}")
                        continue
                    hadith = detail.get("hadith", {})
                    if not hadith.get("arabic_text") and not hadith.get("translation_text"):
                        self.fail("hadith_empty_detail_text", f"{collection_name}/{hadith_number}")
                    self.scan_obj(detail, f"hadith[{collection_name}/{hadith_number}]", "hadith_detail_noise")

    def run(self) -> int:
        self.walk_quran()
        self.walk_dua()
        self.walk_hadith()

        print("=== FINAL REPORT ===", flush=True)
        if not self.violations:
            print("ALL GREEN - 0 violations across Quran, Dua, and Hadith")
            return 0

        for check, details in self.violations.items():
            print(f"\n[{check}] {len(details)} violation(s)")
            for detail in details[:10]:
                print(f"  - {detail}")
            if len(details) > 10:
                print(f"  ... {len(details) - 10} more")
        return 1


def main() -> int:
    args = parse_args()
    collections = tuple(item.strip() for item in args.collections.split(",") if item.strip())
    return Auditor(args.base_url, args.timeout, collections).run()


if __name__ == "__main__":
    sys.exit(main())
