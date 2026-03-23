"""Fix citation metadata in Qdrant — in-place payload patch.

Parses existing citation_text to build structured source_reference,
and enriches citation_text to comply with Imam.md Section 12-14.

No re-embedding needed — only payload updates via set_payload API.
"""

import re
import sys
import json
import time
import httpx

QDRANT_URL = "http://localhost:6333"
COLLECTION = "kb_islamic-knowledge_1024"
BATCH_SIZE = 100  # points per scroll page
UPDATE_BATCH = 50  # points per set_payload call

client = httpx.Client(base_url=QDRANT_URL, timeout=30)

# ─── Quran citation parser ───────────────────────────────────────────

QURAN_RE = re.compile(
    r"Quran\s+(\d+):(\d+)(?:\s*[-–]\s*(\d+))?",
    re.IGNORECASE,
)

def parse_quran_citation(citation: str, text: str) -> dict | None:
    """Parse 'Quran 26:155' or 'Quran 18:67-69' into structured ref."""
    m = QURAN_RE.search(citation)
    if not m:
        # Try extracting from text content
        m = QURAN_RE.search(text[:500] if text else "")
    if not m:
        return None
    ref = {
        "surah_number": int(m.group(1)),
        "verse_number": int(m.group(2)),
        "translation": "Sahih International",
    }
    if m.group(3):
        ref["verse_end"] = int(m.group(3))
    return ref


def format_quran_citation(ref: dict) -> str:
    """Format to Imam.md §12: 'Quran [Ch]:[Verse] - [Translation]'"""
    base = f"Quran {ref['surah_number']}:{ref['verse_number']}"
    if ref.get("verse_end"):
        base += f"-{ref['verse_end']}"
    return f"{base} - {ref.get('translation', 'Sahih International')}"


# ─── Hadith citation parser ──────────────────────────────────────────

# "Sahih Bukhari, Book 24, Hadith 1403"
HADITH_BOOK_RE = re.compile(
    r"^(.+?),\s*Book\s+(\d+),\s*Hadith\s+(\d+)",
    re.IGNORECASE,
)
# "Sahih Muslim, Hadith 4819"
HADITH_NOBOOK_RE = re.compile(
    r"^(.+?),\s*Hadith\s+(\d+)",
    re.IGNORECASE,
)

def parse_hadith_citation(citation: str) -> dict | None:
    """Parse hadith citation into structured ref."""
    m = HADITH_BOOK_RE.match(citation)
    if m:
        return {
            "collection": m.group(1).strip(),
            "book": int(m.group(2)),
            "hadith_number": int(m.group(3)),
        }
    m = HADITH_NOBOOK_RE.match(citation)
    if m:
        return {
            "collection": m.group(1).strip(),
            "hadith_number": int(m.group(2)),
        }
    return None


def format_hadith_citation(ref: dict) -> str:
    """Format to Imam.md §12: 'Collection, Book [X], Hadith [X]'"""
    collection = ref.get("collection", "Hadith")
    book = ref.get("book")
    number = ref.get("hadith_number")
    if book and number:
        return f"{collection}, Book {book}, Hadith {number}"
    if number:
        return f"{collection}, Hadith {number}"
    return collection


# ─── Tafseer citation parser ─────────────────────────────────────────

TAFSEER_VERSE_RE = re.compile(
    r"(?:Surah|surah)\s+(\w[\w\s-]*?)\s*(?:,\s*)?(?:Verse|verse|Ayah|ayah)\s+(\d+)",
    re.IGNORECASE,
)
# Try to find verse references in text like (26:155) or [Quran 2:282]
TEXT_VERSE_RE = re.compile(r"\((\d{1,3}):(\d{1,3})\)")

def parse_tafseer_citation(citation: str, text: str) -> dict | None:
    """Try to extract surah/verse from tafseer text."""
    # First try citation itself
    m = TAFSEER_VERSE_RE.search(citation)
    if m:
        return {
            "name": "Tafsir Ibn Kathir",
            "surah_name": m.group(1).strip(),
            "verse_number": int(m.group(2)),
        }
    # Try extracting first verse reference from text
    if text:
        verses = TEXT_VERSE_RE.findall(text[:300])
        if verses:
            surah, verse = int(verses[0][0]), int(verses[0][1])
            if 1 <= surah <= 114 and 1 <= verse <= 300:
                return {
                    "name": "Tafsir Ibn Kathir",
                    "surah_number": surah,
                    "verse_number": verse,
                }
    return None


def format_tafseer_citation(ref: dict) -> str:
    """Format to Imam.md §12: 'Tafsir Ibn Kathir, Surah [X], Verse [X]'"""
    name = ref.get("name", "Tafsir Ibn Kathir")
    surah_name = ref.get("surah_name")
    surah_num = ref.get("surah_number")
    verse = ref.get("verse_number")
    if surah_name and verse:
        return f"{name}, Surah {surah_name}, Verse {verse}"
    if surah_num and verse:
        return f"{name}, Surah {surah_num}, Verse {verse}"
    return name


# ─── Fiqh citation parser ────────────────────────────────────────────

FIQH_RE = re.compile(
    r"Islamic Jurisprudence According to the Four Sunni Schools\s*,?\s*(Hanafi|Maliki|Shafi.i|Hanbali)?\s*,?\s*(.*)",
    re.IGNORECASE,
)
# Fallback for shorter citation format
FIQH_RE_SHORT = re.compile(
    r"Islamic Jurisprudence\s*,?\s*(Hanafi|Maliki|Shafi.i|Hanbali)?\s*,?\s*(.*)",
    re.IGNORECASE,
)

def parse_fiqh_citation(citation: str) -> dict | None:
    """Parse fiqh citation."""
    m = FIQH_RE.match(citation) or FIQH_RE_SHORT.match(citation)
    if m:
        ref = {"book": "Islamic Jurisprudence According to the Four Sunni Schools"}
        school = m.group(1)
        topic = m.group(2)
        if school:
            ref["school"] = school.strip()
        if topic:
            # Strip any redundant prefix that leaked from regex
            clean = re.sub(r"^(?:According to the Four Sunni Schools)\s*,?\s*", "", topic.strip())
            if clean:
                ref["topic"] = clean
        return ref
    return None


def format_fiqh_citation(ref: dict) -> str:
    """Format to Imam.md §12."""
    parts = [ref.get("book", "Islamic Jurisprudence According to the Four Sunni Schools")]
    if ref.get("school"):
        parts.append(ref["school"])
    if ref.get("topic"):
        parts.append(ref["topic"])
    return ", ".join(parts)


# ─── Main processing ─────────────────────────────────────────────────

def process_point(point: dict) -> dict | None:
    """Process a single point, return payload update or None if no change."""
    payload = point.get("payload", {})
    source_type = payload.get("source_type", "")
    citation = payload.get("citation_text", "")
    text = payload.get("text", "")
    old_ref = payload.get("source_reference")

    new_ref = None
    new_citation = None

    if source_type == "quran":
        ref = parse_quran_citation(citation, text)
        if ref:
            new_ref = ref
            new_citation = format_quran_citation(ref)

    elif source_type == "hadith":
        ref = parse_hadith_citation(citation)
        if ref:
            new_ref = ref
            # Only update citation if it's missing book number
            formatted = format_hadith_citation(ref)
            if formatted != citation:
                new_citation = formatted

    elif source_type in ("tafsir", "tafseer"):
        ref = parse_tafseer_citation(citation, text)
        if ref:
            new_ref = ref
            formatted = format_tafseer_citation(ref)
            if formatted != citation and formatted != "Tafsir Ibn Kathir":
                new_citation = formatted

    elif source_type == "fiqh":
        ref = parse_fiqh_citation(citation)
        if ref:
            new_ref = ref

    # Build update payload
    update = {}
    if new_ref and new_ref != old_ref:
        update["source_reference"] = new_ref
    if new_citation and new_citation != citation:
        update["citation_text"] = new_citation

    return update if update else None


def scroll_all_points():
    """Scroll through all points in the collection."""
    offset = None
    total = 0
    updates = []

    while True:
        body = {
            "limit": BATCH_SIZE,
            "with_payload": {
                "include": ["citation_text", "source_type", "source_reference", "text"]
            },
            "with_vector": False,
        }
        if offset:
            body["offset"] = offset

        resp = client.post(
            f"/collections/{COLLECTION}/points/scroll",
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()

        points = data["result"]["points"]
        if not points:
            break

        for point in points:
            total += 1
            update = process_point(point)
            if update:
                updates.append((point["id"], update))

        offset = data["result"].get("next_page_offset")
        if not offset:
            break

        if total % 5000 == 0:
            print(f"  Scanned {total} points, {len(updates)} to update...")

    return total, updates


def apply_updates(updates: list[tuple[str, dict]]):
    """Apply payload updates in batches using set_payload with point ID lists.

    Groups points that share the same payload key set, then sends
    individual set_payload calls per point (Qdrant requires same payload
    for all points in a single call). Uses batched concurrent approach.
    """
    applied = 0
    errors = 0
    batch_ids: list[str] = []

    for idx, (point_id, payload_update) in enumerate(updates):
        try:
            resp = client.post(
                f"/collections/{COLLECTION}/points/payload",
                json={
                    "payload": payload_update,
                    "points": [point_id],
                },
            )
            resp.raise_for_status()
            applied += 1
        except Exception as e:
            errors += 1
            if errors <= 5:
                print(f"  Error updating {point_id}: {e}")

        if applied % 5000 == 0 and applied > 0:
            print(f"  Applied {applied}/{len(updates)} updates... ({errors} errors)")

    if errors:
        print(f"  Total errors: {errors}")
    return applied


def print_stats(updates: list[tuple[str, dict]]):
    """Print summary statistics."""
    ref_updates = sum(1 for _, u in updates if "source_reference" in u)
    cite_updates = sum(1 for _, u in updates if "citation_text" in u)

    # Count by source type (from source_reference content)
    quran = sum(1 for _, u in updates if u.get("source_reference", {}).get("translation"))
    hadith = sum(1 for _, u in updates if u.get("source_reference", {}).get("collection"))
    tafseer = sum(1 for _, u in updates if u.get("source_reference", {}).get("name", "").startswith("Tafsir"))
    fiqh = sum(1 for _, u in updates if str(u.get("source_reference", {}).get("book", "")).startswith("Islamic"))

    print(f"\n{'='*50}")
    print(f"Citation Metadata Fix Summary")
    print(f"{'='*50}")
    print(f"source_reference updated: {ref_updates}")
    print(f"citation_text updated:    {cite_updates}")
    print(f"  - Quran:   {quran}")
    print(f"  - Hadith:  {hadith}")
    print(f"  - Tafseer: {tafseer}")
    print(f"  - Fiqh:    {fiqh}")


def main():
    print(f"Scanning {COLLECTION}...")
    t0 = time.time()

    total, updates = scroll_all_points()
    scan_time = time.time() - t0
    print(f"\nScanned {total} points in {scan_time:.1f}s")
    print(f"Found {len(updates)} points to update")

    if not updates:
        print("Nothing to update!")
        return

    # Dry run mode
    if "--dry-run" in sys.argv:
        print("\n[DRY RUN] Sample updates:")
        for point_id, update in updates[:10]:
            print(f"  {point_id}: {json.dumps(update, ensure_ascii=False)[:200]}")
        print_stats(updates)
        return

    print(f"\nApplying {len(updates)} updates...")
    t1 = time.time()
    applied = apply_updates(updates)
    update_time = time.time() - t1
    print(f"Applied {applied} updates in {update_time:.1f}s")

    print_stats(updates)
    print(f"\nTotal time: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
