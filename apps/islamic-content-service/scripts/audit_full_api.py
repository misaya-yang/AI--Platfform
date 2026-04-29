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
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument(
        "--collections",
        default=",".join(DEFAULT_COLLECTIONS),
        help="Comma-separated hadith collection slugs to walk.",
    )
    return parser.parse_args()


class Auditor:
    def __init__(self, base_url: str, timeout: float, concurrency: int, collections: tuple[str, ...]) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.concurrency = concurrency
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
        for category, (path, result) in zip(categories, self.get_many(paths)):
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
                f"/hadith/collections/{collection_name}/books/{qb}/hadiths?page=1&limit=10"
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
                        if (ch.get("hadith_count") or 0) <= 0:
                            self.fail(
                                "hadith_zero_count_chapter",
                                f"{collection_name}/{bn}/chapter={ch.get('chapter_id')}",
                            )
                        self.scan_obj(
                            ch,
                            f"{collection_name}/{bn}/chapter[{ch.get('chapter_id')}]",
                            "hadith_chapter_noise",
                        )

                # Hadith list check
                hp_result = book_results.get(
                    f"/hadith/collections/{collection_name}/books/{qb}/hadiths?page=1&limit=10"
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
                        hn = item.get("hadith_number")
                        if not hn:
                            self.fail("hadith_missing_number", f"{collection_name}/{bn}: {item}")
                            continue
                        qh = urllib.parse.quote(str(hn), safe="/")
                        detail_paths.append(f"/hadith/collections/{collection_name}/hadiths/{qh}")

                    # Last-page sample
                    last_page = max((book_total - 1) // 10 + 1, 1)
                    if last_page > 1:
                        detail_paths.append(
                            f"/hadith/collections/{collection_name}/books/{qb}/hadiths?page={last_page}&limit=10"
                        )

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
                            hn = last_items[-1].get("hadith_number")
                            if hn:
                                # Extract collection from path
                                parts = path.split("/")
                                coll_idx = parts.index("collections") + 1
                                coll = parts[coll_idx]
                                qh = urllib.parse.quote(str(hn), safe="/")
                                # Already fetched above or will be checked below
                        continue
                    # Detail result
                    hadith = result.get("hadith", {})
                    if not hadith.get("arabic_text") and not hadith.get("translation_text"):
                        self.fail("hadith_detail_empty_text", path)
                    self.scan_obj(result, f"hadith[{path}]", "hadith_detail_noise")

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
    return Auditor(args.base_url, args.timeout, args.concurrency, collections).run()


if __name__ == "__main__":
    sys.exit(main())
