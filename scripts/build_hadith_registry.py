#!/usr/bin/env python3
"""Build unified hadith metadata registry from multiple data sources.

Merges:
1. AhmedBaset/hadith-json (GitHub)  — book/chapter names (EN+AR), 17 collections
2. meeAtif/hadith_datasets (HuggingFace) — grading data, 6 major collections
3. fawazahmed0/hadith-api (CDN) — section_details for cross-reference

Output: data/hadith_registry.json — lookup table keyed by (collection_slug, hadith_number)

Usage:
    python scripts/build_hadith_registry.py
    python scripts/build_hadith_registry.py --output data/hadith_registry.json
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import urllib.parse
from pathlib import Path

import httpx

logger = logging.getLogger("build_registry")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# AhmedBaset repo structure
AHMEDBASET_BASE = "https://raw.githubusercontent.com/AhmedBaset/hadith-json/main/db/by_chapter"
AHMEDBASET_BOOKS = {
    # category/slug → display name
    "the_9_books/bukhari": "Sahih al-Bukhari",
    "the_9_books/muslim": "Sahih Muslim",
    "the_9_books/abudawud": "Sunan Abu Dawud",
    "the_9_books/tirmidhi": "Jami at-Tirmidhi",
    "the_9_books/nasai": "Sunan an-Nasai",
    "the_9_books/ibnmajah": "Sunan Ibn Majah",
    "the_9_books/malik": "Muwatta Malik",
    "the_9_books/ahmed": "Musnad Ahmad",
    "the_9_books/darimi": "Sunan ad-Darimi",
    "other_books/riyadussalihin": "Riyad as-Salihin",
    "other_books/shamail": "Shama'il Muhammadiyah",
    "other_books/bulugh": "Bulugh Al-Maram",
    "other_books/adab": "Al-Adab Al-Mufrad",
    "other_books/mishkat": "Mishkat al-Masabih",
    "forties/nawawi": "Forty Hadith an-Nawawi",
    "forties/qudsi": "Forty Hadith Qudsi",
    "forties/shahwaliullah": "Forty Hadith Shah Waliullah Dehlawi",
}

# Normalize to our existing collection slugs
SLUG_MAP = {
    "the_9_books/bukhari": "bukhari",
    "the_9_books/muslim": "muslim",
    "the_9_books/abudawud": "abudawud",
    "the_9_books/tirmidhi": "tirmidhi",
    "the_9_books/nasai": "nasai",
    "the_9_books/ibnmajah": "ibnmajah",
    "the_9_books/malik": "malik",
    "the_9_books/ahmed": "ahmed",
    "the_9_books/darimi": "darimi",
    "other_books/riyadussalihin": "riyadussalihin",
    "other_books/shamail": "shamail",
    "other_books/bulugh": "bulugh",
    "other_books/adab": "adab",
    "other_books/mishkat": "mishkat",
    "forties/nawawi": "nawawi",
    "forties/qudsi": "qudsi",
    "forties/shahwaliullah": "dehlawi",
}

# HuggingFace dataset files
HF_BASE = "https://huggingface.co/datasets/meeAtif/hadith_datasets/resolve/main"
HF_COLLECTIONS = {
    "Sahih al-Bukhari": "bukhari",
    "Sahih Muslim": "muslim",
    "Sunan Abi Dawud": "abudawud",
    "Jami` at-Tirmidhi": "tirmidhi",
    "Sunan an-Nasa'i": "nasai",
    "Sunan Ibn Majah": "ibnmajah",
}

# CDN for cross-reference
CDN_BASE = "https://cdn.jsdelivr.net/gh/fawazahmed0/hadith-api@1"
CDN_COLLECTIONS = ["bukhari", "muslim", "abudawud", "tirmidhi", "nasai", "ibnmajah", "malik", "nawawi", "qudsi", "dehlawi"]


def _get_client() -> httpx.Client:
    return httpx.Client(timeout=120.0, follow_redirects=True, transport=httpx.HTTPTransport(retries=3))


# ---------------------------------------------------------------------------
# Source 1: AhmedBaset/hadith-json — book/chapter names
# ---------------------------------------------------------------------------

def _fetch_ahmedbaset_index(client: httpx.Client) -> dict[str, list[int]]:
    """Discover available books (chapter counts) via the GitHub API."""
    # We'll try fetching chapter 1 for each book to get metadata,
    # then iterate chapters until 404.
    result: dict[str, list[int]] = {}

    for cat_slug in AHMEDBASET_BOOKS:
        # Try to get the tree listing from GitHub API
        slug = SLUG_MAP[cat_slug]
        chapters = []
        for ch in range(1, 200):  # Most books have < 100 chapters
            url = f"{AHMEDBASET_BASE}/{cat_slug}/{ch}.json"
            try:
                resp = client.get(url)
                if resp.status_code == 404:
                    break
                resp.raise_for_status()
                chapters.append(ch)
            except httpx.HTTPStatusError:
                break
            except Exception as exc:
                logger.warning("Error fetching %s ch%d: %s", slug, ch, exc)
                break
        if chapters:
            result[cat_slug] = chapters
            logger.info("  %s: %d chapters found", slug, len(chapters))
        else:
            logger.warning("  %s: no chapters found", slug)

    return result


def fetch_ahmedbaset_metadata(client: httpx.Client) -> dict[str, dict]:
    """Download book/chapter metadata from AhmedBaset.

    Returns: {collection_slug: {book_number: {book_name_en, book_name_ar, hadith_count, hadiths: [...]}}}
    """
    logger.info("=== Fetching AhmedBaset/hadith-json metadata ===")
    all_data: dict[str, dict] = {}

    for cat_slug, display_name in AHMEDBASET_BOOKS.items():
        slug = SLUG_MAP[cat_slug]
        books: dict[int, dict] = {}

        for ch in range(1, 200):
            url = f"{AHMEDBASET_BASE}/{cat_slug}/{ch}.json"
            try:
                resp = client.get(url)
                if resp.status_code == 404:
                    break
                resp.raise_for_status()
                data = resp.json()
            except httpx.HTTPStatusError:
                break
            except Exception as exc:
                logger.warning("Error fetching %s ch%d: %s", slug, ch, exc)
                break

            meta = data.get("metadata", {})
            hadiths = data.get("hadiths", [])

            book_name_en = (
                meta.get("english", {}).get("title")
                or meta.get("english", {}).get("introduction")
                or ""
            )
            book_name_ar = (
                meta.get("arabic", {}).get("title")
                or meta.get("arabic", {}).get("introduction")
                or ""
            )

            # Extract hadith numbers
            hadith_entries = []
            for h in hadiths:
                hadith_entries.append({
                    "id": h.get("id"),
                    "idInBook": h.get("idInBook"),
                    "narrator": (h.get("english") or {}).get("narrator", ""),
                })

            books[ch] = {
                "book_name_en": book_name_en.strip(),
                "book_name_ar": book_name_ar.strip(),
                "hadith_count": len(hadiths),
                "hadiths": hadith_entries,
            }

        if books:
            all_data[slug] = {
                "display_name": display_name,
                "books": books,
            }
            total_h = sum(b["hadith_count"] for b in books.values())
            logger.info("  %s: %d books, %d hadiths", slug, len(books), total_h)

    return all_data


# ---------------------------------------------------------------------------
# Source 2: meeAtif/hadith_datasets — grading
# ---------------------------------------------------------------------------

def fetch_huggingface_grading(client: httpx.Client) -> dict[str, dict[str, str]]:
    """Download grading data from HuggingFace.

    Returns: {collection_slug: {hadith_number_str: grade}}
    """
    logger.info("=== Fetching meeAtif/hadith_datasets grading ===")
    all_grades: dict[str, dict[str, str]] = {}

    for hf_name, slug in HF_COLLECTIONS.items():
        encoded = urllib.parse.quote(f"{hf_name}.json")
        url = f"{HF_BASE}/{encoded}"

        try:
            resp = client.get(url)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("Failed to download HF %s: %s", hf_name, exc)
            continue

        grades: dict[str, str] = {}
        chapter_titles: dict[str, tuple[str, str]] = {}  # hadith_num → (en, ar)

        for item in data:
            # Extract hadith number from Reference field (e.g. "Sahih al-Bukhari 1")
            ref = item.get("Reference", "")
            hnum = _extract_hadith_number(ref)
            if not hnum:
                continue

            grade = (item.get("Grade") or "").strip()
            if grade:
                grades[hnum] = grade

            ch_en = (item.get("Chapter_Title_English") or "").strip()
            ch_ar = (item.get("Chapter_Title_Arabic") or "").strip()
            if ch_en or ch_ar:
                chapter_titles[hnum] = (ch_en, ch_ar)

        all_grades[slug] = grades
        logger.info("  %s: %d hadiths with grading, %d with chapter titles",
                     slug, len(grades), len(chapter_titles))

        # Store chapter titles too
        all_grades[f"{slug}_chapters"] = {
            hnum: f"{en}|{ar}" for hnum, (en, ar) in chapter_titles.items()
        }

    return all_grades


def _extract_hadith_number(ref: str) -> str | None:
    """Extract hadith number from reference like 'Sahih al-Bukhari 1' or 'Sunan Abu Dawud 3585'."""
    if not ref:
        return None
    m = re.search(r"(\d+)\s*$", ref.strip())
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Source 3: fawazahmed0 CDN — section_details cross-reference
# ---------------------------------------------------------------------------

def fetch_cdn_sections(client: httpx.Client) -> dict[str, dict]:
    """Download section_details from CDN for cross-reference.

    Returns: {collection_slug: {book_number_str: book_name}}
    """
    logger.info("=== Fetching fawazahmed0 CDN section_details ===")
    all_sections: dict[str, dict] = {}

    for coll in CDN_COLLECTIONS:
        url = f"{CDN_BASE}/editions/eng-{coll}.json"
        try:
            resp = client.get(url)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("Failed CDN %s: %s", coll, exc)
            continue

        meta = data.get("metadata", {})
        sections = meta.get("sections", {})
        section_details = meta.get("section_details", {})

        book_names: dict[str, str] = {}
        for sec_num, title in sections.items():
            book_names[str(sec_num)] = str(title)

        all_sections[coll] = {
            "sections": book_names,
            "section_details": section_details,
        }
        logger.info("  %s: %d sections", coll, len(book_names))

    return all_sections


# ---------------------------------------------------------------------------
# Merge into unified registry
# ---------------------------------------------------------------------------

def build_registry(
    ahmedbaset: dict[str, dict],
    hf_grades: dict[str, dict[str, str]],
    cdn_sections: dict[str, dict],
) -> dict:
    """Merge all sources into a unified registry.

    Output structure:
    {
      "collections": {
        "bukhari": {
          "name_en": "Sahih al-Bukhari",
          "name_ar": "صحيح البخاري",
          "books": {
            "1": {"name_en": "Revelation", "name_ar": "بدء الوحي"},
            ...
          },
          "hadiths": {
            "1": {"book": 1, "book_name_en": "Revelation", "grade": "sahih", "chapter_en": "...", "chapter_ar": "..."},
            ...
          }
        }
      },
      "stats": {...}
    }
    """
    registry: dict[str, dict] = {}
    stats = {"total_hadiths": 0, "with_grade": 0, "with_book_name": 0, "collections": 0}

    # Collection display names (merged from all sources)
    DISPLAY_NAMES = {slug: info["display_name"] for slug, info in ahmedbaset.items()}

    for slug in set(list(ahmedbaset.keys()) + list(cdn_sections.keys())):
        ab_data = ahmedbaset.get(slug, {})
        cdn_data = cdn_sections.get(slug, {})
        grades = hf_grades.get(slug, {})
        chapter_data = hf_grades.get(f"{slug}_chapters", {})

        display_name = ab_data.get("display_name") or DISPLAY_NAMES.get(slug, slug.title())

        # Build book name lookup from AhmedBaset (primary) + CDN (fallback)
        book_names: dict[str, dict[str, str]] = {}
        ab_books = ab_data.get("books", {})
        cdn_secs = cdn_data.get("sections", {})

        for book_num_str, info in ab_books.items():
            book_names[str(book_num_str)] = {
                "name_en": info.get("book_name_en", ""),
                "name_ar": info.get("book_name_ar", ""),
            }

        # Supplement with CDN section names where AhmedBaset is missing
        for sec_num, sec_name in cdn_secs.items():
            if str(sec_num) not in book_names:
                book_names[str(sec_num)] = {"name_en": sec_name, "name_ar": ""}
            elif not book_names[str(sec_num)].get("name_en"):
                book_names[str(sec_num)]["name_en"] = sec_name

        # Build hadith-level registry
        hadiths: dict[str, dict] = {}

        # From AhmedBaset: hadith → book mapping
        for book_num, info in ab_books.items():
            bk = book_names.get(str(book_num), {})
            for h in info.get("hadiths", []):
                hid = str(h.get("id", ""))
                if not hid:
                    continue
                entry: dict = {
                    "book": int(book_num),
                    "book_name_en": bk.get("name_en", ""),
                    "book_name_ar": bk.get("name_ar", ""),
                }
                # Add grading from HuggingFace
                grade = grades.get(hid, "")
                if grade:
                    entry["grade"] = grade
                    stats["with_grade"] += 1

                # Add chapter titles from HuggingFace
                ch = chapter_data.get(hid)
                if ch:
                    parts = ch.split("|", 1)
                    entry["chapter_en"] = parts[0] if parts else ""
                    entry["chapter_ar"] = parts[1] if len(parts) > 1 else ""

                if entry.get("book_name_en"):
                    stats["with_book_name"] += 1

                hadiths[hid] = entry
                stats["total_hadiths"] += 1

        # Build hadith mapping from CDN section_details for hadiths not in AhmedBaset
        section_details = cdn_data.get("section_details", {})
        for sec_num, detail in section_details.items():
            if not isinstance(detail, dict):
                continue
            first = detail.get("hadithnumber_first")
            last = detail.get("hadithnumber_last")
            if first is None or last is None:
                continue
            bk = book_names.get(str(sec_num), {})
            for hnum in range(int(first), int(last) + 1):
                hnum_str = str(hnum)
                if hnum_str in hadiths:
                    continue  # AhmedBaset data takes precedence
                entry = {
                    "book": int(sec_num),
                    "book_name_en": bk.get("name_en", ""),
                    "book_name_ar": bk.get("name_ar", ""),
                }
                grade = grades.get(hnum_str, "")
                if grade:
                    entry["grade"] = grade
                    stats["with_grade"] += 1
                ch = chapter_data.get(hnum_str)
                if ch:
                    parts = ch.split("|", 1)
                    entry["chapter_en"] = parts[0] if parts else ""
                    entry["chapter_ar"] = parts[1] if len(parts) > 1 else ""
                if entry.get("book_name_en"):
                    stats["with_book_name"] += 1
                hadiths[hnum_str] = entry
                stats["total_hadiths"] += 1

        # Arabic collection name from AhmedBaset metadata (first book's arabic title usually has it)
        name_ar = ""
        if ab_books:
            first_book = next(iter(ab_books.values()), {})
            # The Arabic name is often in the first chapter's metadata
            name_ar = first_book.get("book_name_ar", "").split(",")[0].strip() if first_book.get("book_name_ar") else ""

        registry[slug] = {
            "name_en": display_name,
            "name_ar": name_ar,
            "books": book_names,
            "hadiths": hadiths,
        }
        stats["collections"] += 1

    # Default grades for Bukhari and Muslim (all sahih by scholarly consensus)
    for slug in ("bukhari", "muslim"):
        if slug in registry:
            for hnum, entry in registry[slug]["hadiths"].items():
                if not entry.get("grade"):
                    entry["grade"] = "Sahih"
                    stats["with_grade"] += 1

    return {"collections": registry, "stats": stats}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    p = argparse.ArgumentParser(description="Build unified hadith metadata registry")
    p.add_argument("--output", default="data/hadith_registry.json", help="Output file path")
    p.add_argument("--skip-ahmedbaset", action="store_true", help="Skip AhmedBaset download (slow)")
    p.add_argument("--skip-hf", action="store_true", help="Skip HuggingFace download")
    p.add_argument("--skip-cdn", action="store_true", help="Skip CDN download")
    args = p.parse_args()

    client = _get_client()

    try:
        ahmedbaset = {} if args.skip_ahmedbaset else fetch_ahmedbaset_metadata(client)
        hf_grades = {} if args.skip_hf else fetch_huggingface_grading(client)
        cdn_sections = {} if args.skip_cdn else fetch_cdn_sections(client)

        registry = build_registry(ahmedbaset, hf_grades, cdn_sections)

        # Ensure output directory exists
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)

        with open(out, "w", encoding="utf-8") as f:
            json.dump(registry, f, ensure_ascii=False, indent=2)

        stats = registry["stats"]
        logger.info("=== Registry built ===")
        logger.info("  Collections: %d", stats["collections"])
        logger.info("  Total hadiths: %d", stats["total_hadiths"])
        logger.info("  With book name: %d (%.1f%%)",
                     stats["with_book_name"],
                     100 * stats["with_book_name"] / max(stats["total_hadiths"], 1))
        logger.info("  With grading: %d (%.1f%%)",
                     stats["with_grade"],
                     100 * stats["with_grade"] / max(stats["total_hadiths"], 1))
        logger.info("  Output: %s (%.1f KB)", out, out.stat().st_size / 1024)

    finally:
        client.close()


if __name__ == "__main__":
    main()
