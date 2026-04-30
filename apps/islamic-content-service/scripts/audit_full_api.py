#!/usr/bin/env python3
"""Walk Islamic Content public APIs concurrently and report data violations.

Default concurrency of 20 keeps the run under ~15 seconds for a full
7-collection Hadith walk (~3000 requests) while staying well below the
FastAPI server's capacity.  Bump to 50 if you're on localhost.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from math import ceil
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
PLACEHOLDER_EN_RE = re.compile(
    r"^(chapter\s*:?\s*|additional hadiths \(not grouped by sunnah\.com\)|introduction \(unmapped preamble hadiths\))$",
    re.IGNORECASE,
)
PLACEHOLDER_AR = {"", "باب", "باب:", "باب :", "،", ".", ":"}


def is_placeholder_chapter_title(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return True
    return bool(PLACEHOLDER_EN_RE.fullmatch(text)) or text in PLACEHOLDER_AR


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Walk Islamic Content public APIs and report data violations.")
    parser.add_argument(
        "--base-url",
        default=os.getenv("ISLAMIC_CONTENT_AUDIT_BASE_URL", "http://127.0.0.1:8091/api/v1"),
        help="Base API URL, including /api/v1.",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument(
        "--collections",
        default=",".join(DEFAULT_COLLECTIONS),
        help="Comma-separated hadith collection slugs to walk.",
    )
    parser.add_argument(
        "--deep-lists",
        action="store_true",
        help="Fetch every paginated Hadith book-item page and scan every summary row.",
    )
    return parser.parse_args()


class Auditor:
    def __init__(
        self,
        base_url: str,
        timeout: float,
        concurrency: int,
        collections: tuple[str, ...],
        *,
        deep_lists: bool,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.concurrency = concurrency
        self.collections = collections
        self.deep_lists = deep_lists
        self.violations: dict[str, list[str]] = defaultdict(list)
        self.warnings: dict[str, list[str]] = defaultdict(list)

    def fail(self, check: str, detail: str) -> None:
        self.violations[check].append(detail)

    def warn(self, check: str, detail: str) -> None:
        self.warnings[check].append(detail)

    def get(self, path: str) -> dict[str, Any]:
        with urllib.request.urlopen(f"{self.base_url}{path}", timeout=self.timeout) as response:
            payload = json.loads(response.read())
        if not isinstance(payload, dict):
            raise TypeError(f"Expected object response for {path}")
        return payload

    def get_many(self, paths: list[str]) -> list[tuple[str, dict[str, Any] | Exception]]:
        """Fetch multiple paths concurrently.  Returns (path, payload|error) pairs."""
        results: list[tuple[str, dict[str, Any] | Exception]] = []

        def _one(path: str) -> tuple[str, dict[str, Any] | Exception]:
            try:
                return path, self.get(path)
            except Exception as exc:
                return path, exc

        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            futures = {pool.submit(_one, path): path for path in paths}
            for future in as_completed(futures):
                results.append(future.result())
        return results

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

    # ── Quran ──────────────────────────────────────────────

    def walk_quran(self) -> None:
        print("=== QURAN ===", flush=True)
        chapters_raw = self.get("/quran/chapters")
        chapters = chapters_raw.get("chapters", [])
        if len(chapters) != 114:
            self.fail("quran_chapter_count", f"got {len(chapters)} want 114")
            return
        by_id = {chapter.get("chapter_id"): chapter for chapter in chapters}
        for chapter in chapters:
            self.scan_obj(chapter, f"chapter[{chapter.get('chapter_id')}]", "quran_chapter_noise")

        sample_chapters = (1, 2, 18, 36, 55, 67, 78, 96, 112, 113, 114)
        paths = [f"/quran/chapters/{cid}/ayahs" for cid in sample_chapters]
        for path, result in self.get_many(paths):
            if isinstance(result, Exception):
                self.fail("quran_ayahs_endpoint_error", f"{path}: {result}")
                continue
            ayahs = result.get("ayahs", [])
            chapter_id = int(path.split("/")[3])
            expected = (by_id.get(chapter_id) or {}).get("verses_count")
            if expected and len(ayahs) != expected:
                self.fail("quran_ayahs_count_drift", f"chapter {chapter_id}: got {len(ayahs)} want {expected}")
            for ayah in ayahs:
                if not ayah.get("arabic_text"):
                    self.fail("quran_ayah_empty_arabic", str(ayah.get("verse_key")))
                self.scan_obj(ayah, f"ayah[{ayah.get('verse_key')}]", "quran_ayah_noise")

    # ── Dua ────────────────────────────────────────────────

    def walk_dua(self) -> None:
        print("=== DUA ===", flush=True)
        categories = self.get("/dua/categories").get("categories", [])
        if len(categories) != 31:
            self.fail("dua_category_count", f"got {len(categories)} want 31")

        paths = [
            f"/dua/categories/{urllib.parse.quote(str(cat.get('category', '')), safe='')}"
            for cat in categories
        ]
        total_items = 0
        for category, (_path, result) in zip(categories, self.get_many(paths), strict=True):
            cat_name = category.get("category", "?")
            self.scan_obj(category, f"category[{cat_name}]", "dua_category_noise")
            if isinstance(result, Exception):
                self.fail("dua_category_endpoint_error", f"{cat_name}: {result}")
                continue
            items = result.get("duas") or result.get("items", [])
            total_items += len(items)
            for item in items:
                if not item.get("arabic_text"):
                    self.fail("dua_item_empty_arabic", str(item.get("dua_id")))
                if not item.get("english_meaning"):
                    self.fail("dua_item_empty_english", str(item.get("dua_id")))
                self.scan_obj(item, f"dua[{item.get('dua_id')}]", "dua_item_noise")
        if total_items != 72:
            self.fail("dua_total_items", f"got {total_items} want 72")

    # ── Hadith ─────────────────────────────────────────────

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

            # --- Phase 1: fetch every book's chapters + first hadith page concurrently ---
            book_paths: list[tuple[dict[str, Any], str, str]] = []
            for book in books:
                bn = str(book.get("book_number"))
                qb = urllib.parse.quote(bn, safe="")
                book_paths.append((book, bn, qb))

            chapter_paths = [
                f"/hadith/collections/{collection_name}/books/{qb}/chapters"
                for _, _, qb in book_paths
            ]
            hadith_page1_paths = [
                f"/hadith/collections/{collection_name}/books/{qb}/hadiths?page=1&limit=200"
                for _, _, qb in book_paths
            ]
            all_book_paths = chapter_paths + hadith_page1_paths
            book_results = dict(self.get_many(all_book_paths))

            # Collect detail paths to fetch
            detail_paths: list[str] = []

            for book, bn, qb in book_paths:
                book_total = book.get("number_of_hadith") or 0

                # Chapters check
                ch_result = book_results.get(
                    f"/hadith/collections/{collection_name}/books/{qb}/chapters"
                )
                if isinstance(ch_result, Exception):
                    self.fail("hadith_chapters_endpoint_error", f"{collection_name}/{bn}: {ch_result}")
                elif ch_result is not None:
                    chapters = ch_result.get("chapters", [])
                    sum_chapters = sum(ch.get("hadith_count") or 0 for ch in chapters)
                    if sum_chapters != book_total:
                        self.fail(
                            "hadith_chapter_sum_drift",
                            f"{collection_name}/{bn}: chapters={sum_chapters} book={book_total}",
                        )
                    for ch in chapters:
                        if is_placeholder_chapter_title(ch.get("chapter_title")):
                            self.fail(
                                "hadith_placeholder_chapter_title",
                                (
                                    f"{collection_name}/{bn}/chapter={ch.get('chapter_id')}: "
                                    f"{ch.get('chapter_title')!r}"
                                ),
                            )
                        for field in ("title_en", "title_ar"):
                            if ch.get(field) is not None and is_placeholder_chapter_title(ch.get(field)):
                                self.fail(
                                    "hadith_placeholder_chapter_field",
                                    (
                                        f"{collection_name}/{bn}/chapter={ch.get('chapter_id')}/"
                                        f"{field}: {ch.get(field)!r}"
                                    ),
                                )
                        self.scan_obj(ch, f"{collection_name}/{bn}/chapter[{ch.get('chapter_id')}]", "hadith_chapter_noise")

                # Hadith list check
                hp_result = book_results.get(
                    f"/hadith/collections/{collection_name}/books/{qb}/hadiths?page=1&limit=200"
                )
                if isinstance(hp_result, Exception):
                    self.fail("hadith_list_endpoint_error", f"{collection_name}/{bn}: {hp_result}")
                elif hp_result is not None:
                    items = hp_result.get("items", [])
                    api_total = hp_result.get("pagination", {}).get("total_items")
                    if api_total != book_total:
                        self.fail(
                            "hadith_list_total_drift",
                            f"{collection_name}/{bn}: total_items={api_total} book={book_total}",
                        )
                    # Collect detail samples from this book
                    samples = items if len(items) <= 3 else [items[0], items[len(items) // 2], items[-1]]
                    for item in samples:
                        self._scan_hadith_summary_item(collection_name, bn, item)
                        hn = item.get("hadith_number")
                        if not hn:
                            self.fail("hadith_missing_number", f"{collection_name}/{bn}: {item}")
                            continue
                        qh = urllib.parse.quote(str(hn), safe="/")
                        detail_paths.append(f"/hadith/collections/{collection_name}/hadiths/{qh}")

                    # Last-page sample
                    last_page = max(ceil(book_total / 200), 1)
                    if last_page > 1 and not self.deep_lists:
                        detail_paths.append(
                            f"/hadith/collections/{collection_name}/books/{qb}/hadiths?page={last_page}&limit=200"
                        )

            if self.deep_lists:
                self._walk_hadith_book_pages(collection_name, book_paths)

            # --- Phase 2: fetch all detail + last-page requests concurrently ---
            if detail_paths:
                detail_results = dict(self.get_many(detail_paths))
                for path, result in detail_results.items():
                    if isinstance(result, Exception):
                        self.fail("hadith_detail_endpoint_error", f"{path}: {result}")
                        continue
                    # Last-page result — grab the last item
                    if "/books/" in path and "/hadiths?" in path:
                        last_items = result.get("items", [])
                        if last_items:
                            parts = path.split("/")
                            coll_idx = parts.index("collections") + 1
                            book_idx = parts.index("books") + 1
                            self.scan_obj(
                                last_items[-1],
                                f"hadith_last_page[{path}]",
                                "hadith_list_noise",
                            )
                            self._scan_hadith_summary_item(
                                parts[coll_idx],
                                urllib.parse.unquote(parts[book_idx]),
                                last_items[-1],
                            )
                        continue
                    # Detail result
                    hadith = result.get("hadith", {})
                    if not hadith.get("arabic_text") and not hadith.get("translation_text"):
                        self.fail("hadith_detail_empty_text", path)
                    self.scan_obj(result, f"hadith[{path}]", "hadith_detail_noise")

    def _scan_hadith_summary_item(self, collection_name: str, book_number: str, item: dict[str, Any]) -> None:
        label = f"{collection_name}/{book_number}/{item.get('hadith_number')}"
        if not item.get("hadith_number"):
            self.fail("hadith_summary_missing_number", label)
        for field in ("title", "section_title", "chapter_title"):
            value = item.get(field)
            if value is not None and is_placeholder_chapter_title(value):
                self.fail("hadith_summary_placeholder_title", f"{label}/{field}: {value!r}")
        if not str(item.get("arabic_preview_text") or "").strip():
            self.fail("hadith_summary_empty_arabic", label)
        if not str(item.get("preview_text") or "").strip():
            self.warn("hadith_summary_empty_translation_source_gap", label)
        self.scan_obj(item, f"hadith_summary[{label}]", "hadith_list_noise")

    def _walk_hadith_book_pages(
        self,
        collection_name: str,
        book_paths: list[tuple[dict[str, Any], str, str]],
    ) -> None:
        paths: list[tuple[str, str, int, str]] = []
        for book, bn, qb in book_paths:
            book_total = book.get("number_of_hadith") or 0
            pages = max(ceil(book_total / 200), 1)
            for page in range(1, pages + 1):
                path = f"/hadith/collections/{collection_name}/books/{qb}/hadiths?page={page}&limit=200"
                paths.append((bn, path, page, qb))

        page_results = dict(self.get_many([path for _, path, _, _ in paths]))
        seen_by_book: dict[str, int] = defaultdict(int)
        for bn, path, _page, _qb in paths:
            result = page_results.get(path)
            if isinstance(result, Exception):
                self.fail("hadith_list_endpoint_error", f"{collection_name}/{bn}: {result}")
                continue
            if result is None:
                self.fail("hadith_list_endpoint_error", f"{collection_name}/{bn}: missing result for {path}")
                continue
            items = result.get("items", [])
            seen_by_book[bn] += len(items)
            for item in items:
                self._scan_hadith_summary_item(collection_name, bn, item)

        for book, bn, _qb in book_paths:
            book_total = book.get("number_of_hadith") or 0
            if seen_by_book[bn] != book_total:
                self.fail(
                    "hadith_deep_list_count_drift",
                    f"{collection_name}/{bn}: pages={seen_by_book[bn]} book={book_total}",
                )

    def run(self) -> int:
        self.walk_quran()
        self.walk_dua()
        self.walk_hadith()

        print("=== FINAL REPORT ===", flush=True)
        if not self.violations:
            if self.warnings:
                for check, details in self.warnings.items():
                    print(f"\n[WARN:{check}] {len(details)} item(s)")
                    for detail in details[:10]:
                        print(f"  - {detail}")
                    if len(details) > 10:
                        print(f"  ... {len(details) - 10} more")
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
    return Auditor(
        args.base_url,
        args.timeout,
        args.concurrency,
        collections,
        deep_lists=args.deep_lists,
    ).run()


if __name__ == "__main__":
    sys.exit(main())
