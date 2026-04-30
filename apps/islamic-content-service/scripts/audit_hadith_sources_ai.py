#!/usr/bin/env python3
"""Audit non-book0 Hadith rows against external sources and AI review.

The script is intentionally read-only.  It treats external sources as evidence
and the LLM as a suspicious-case reviewer, never as a content author.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import difflib
import io
import json
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from typing import Any

import asyncpg

SCHEMA = "islamic_content"
COLLECTIONS = ("bukhari", "muslim", "abudawud", "tirmidhi", "nasai", "ibnmajah", "nawawi")
HF_BASE_URL = "https://huggingface.co/datasets/meeAtif/hadith_datasets/resolve/main"
HF_FILES = {
    "bukhari": "Sahih al-Bukhari.csv",
    "muslim": "Sahih Muslim.csv",
    "abudawud": "Sunan Abi Dawud.csv",
    "tirmidhi": "Jami` at-Tirmidhi.csv",
    "nasai": "Sunan an-Nasa'i.csv",
    "ibnmajah": "Sunan Ibn Majah.csv",
}
FAWAZ_BASE_URL = "https://cdn.jsdelivr.net/gh/fawazahmed0/hadith-api@1/editions"
AHMEDBASET_BASE_URL = "https://raw.githubusercontent.com/AhmedBaset/hadith-json/main/db/by_book/the_9_books"
BIDI_NOISE_CHARS = (
    "\u200e",
    "\u200f",
    "\u202a",
    "\u202b",
    "\u202c",
    "\u202d",
    "\u202e",
    "\u2066",
    "\u2067",
    "\u2068",
    "\u2069",
    "\ufffd",
)
PLACEHOLDER_EN_RE = re.compile(
    r"^(chapter\s*:?\s*|additional hadiths \(not grouped by sunnah\.com\)|introduction \(unmapped preamble hadiths\))$",
    re.IGNORECASE,
)
PLACEHOLDER_AR = {"", "باب", "باب:", "باب :", "،", ".", ":"}


@dataclass(frozen=True)
class LocalHadith:
    id: int
    collection_name: str
    book_number: str
    hadith_number: str
    book_title: str
    chapter_id: str
    chapter_order: int | None
    chapter_title_en: str
    chapter_title_ar: str
    english_text: str
    arabic_text: str

    @property
    def key(self) -> str:
        return f"{self.collection_name}/{self.book_number}/{self.hadith_number}"


@dataclass
class Issue:
    severity: str
    check: str
    key: str
    detail: str
    evidence: dict[str, Any]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only Hadith source/AI audit.")
    parser.add_argument(
        "--dsn",
        default=os.getenv("ISLAMIC_CONTENT_DATABASE__DSN") or os.getenv("GATEWAY_DATABASE__DSN"),
        help="PostgreSQL DSN. Defaults to service env.",
    )
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--collections", default=",".join(COLLECTIONS))
    parser.add_argument("--out", default="/tmp/hadith_source_ai_audit.json")
    parser.add_argument("--markdown-out", default="/tmp/hadith_source_ai_audit.md")
    parser.add_argument("--ai-review", action="store_true", help="Use Gemini to review suspicious candidates.")
    parser.add_argument("--ai-max-candidates", type=int, default=80)
    parser.add_argument("--ai-sample-per-collection", type=int, default=5)
    parser.add_argument("--seed", type=int, default=20260430)
    return parser.parse_args()


def _clean(value: Any) -> str:
    text = str(value or "")
    for char in BIDI_NOISE_CHARS:
        text = text.replace(char, "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _norm(value: Any) -> str:
    text = _clean(value)
    text = re.sub(r"[\u064b-\u065f\u0670\u0640]", "", text)
    return text.casefold()


def _title_norm(value: Any) -> str:
    text = _norm(value)
    text = re.sub(r"^chapter\s*:\s*", "", text)
    text = re.sub(r"[\u064b-\u065f\u0670\u0640]", "", text)
    text = re.sub(r"[\W_]+", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def _arabic_ratio(value: str) -> float:
    text = _clean(value)
    chars = [ch for ch in text if not ch.isspace()]
    if not chars:
        return 0.0
    arabic = sum(1 for ch in chars if "\u0600" <= ch <= "\u06ff" or "\u0750" <= ch <= "\u077f")
    return arabic / len(chars)


def _latin_ratio(value: str) -> float:
    text = _clean(value)
    chars = [ch for ch in text if ch.isalpha()]
    if not chars:
        return 0.0
    latin = sum(1 for ch in chars if "a" <= ch.casefold() <= "z")
    return latin / len(chars)


def _is_placeholder_title(title_en: str | None, title_ar: str | None = None) -> bool:
    en = _clean(title_en)
    return not en or bool(PLACEHOLDER_EN_RE.fullmatch(en))


def _similarity(left: str, right: str, *, title: bool = False) -> float:
    a = _title_norm(left) if title else _norm(left)
    b = _title_norm(right) if title else _norm(right)
    if not a or not b:
        return 0.0
    if a == b or a in b or b in a:
        return 1.0
    return difflib.SequenceMatcher(None, a[:2500], b[:2500]).ratio()


def _fetch_bytes(url: str, timeout: float) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "islamic-content-audit/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _fetch_json(url: str, timeout: float) -> dict[str, Any]:
    payload = json.loads(_fetch_bytes(url, timeout).decode("utf-8", "replace"))
    if not isinstance(payload, dict):
        raise TypeError(f"Expected JSON object from {url}")
    return payload


def _reference_number(reference: str) -> str:
    match = re.search(r":([^/:?#]+)(?:[?#].*)?$", reference or "")
    return match.group(1) if match else ""


async def load_local_hadiths(dsn: str, collections: tuple[str, ...]) -> list[LocalHadith]:
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            f"""
            SELECT
                hi.id,
                hi.collection_name,
                hi.book_number,
                hi.chapter_id,
                hi.hadith_number,
                coalesce(hb.title, '') AS book_title,
                hc.chapter_order,
                coalesce(hc.title_en, '') AS chapter_title_en,
                coalesce(hc.title_ar, '') AS chapter_title_ar,
                coalesce(en.body_text, '') AS english_text,
                coalesce(ar.body_text, '') AS arabic_text
            FROM {SCHEMA}.hadith_items hi
            LEFT JOIN {SCHEMA}.hadith_books hb
                ON hb.collection_name = hi.collection_name
               AND hb.book_number = hi.book_number
            LEFT JOIN {SCHEMA}.hadith_chapters hc ON hc.id = hi.chapter_ref_id
            LEFT JOIN {SCHEMA}.hadith_localizations en
                ON en.hadith_item_id = hi.id AND en.language = 'en'
            LEFT JOIN {SCHEMA}.hadith_localizations ar
                ON ar.hadith_item_id = hi.id AND ar.language = 'ar'
            WHERE hi.collection_name = ANY($1::text[])
              AND hi.book_number <> '0'
            ORDER BY hi.collection_name, hi.book_number, hi.hadith_number
            """,
            list(collections),
        )
        return [LocalHadith(**dict(row)) for row in rows]
    finally:
        await conn.close()


def load_fawaz_sources(collections: tuple[str, ...], timeout: float) -> dict[str, dict[str, Any]]:
    sources: dict[str, dict[str, Any]] = {}
    for collection in collections:
        collection_data: dict[str, Any] = {}
        for lang in ("eng", "ara"):
            url = f"{FAWAZ_BASE_URL}/{lang}-{urllib.parse.quote(collection)}.min.json"
            try:
                payload = _fetch_json(url, timeout)
            except Exception as exc:
                collection_data[f"{lang}_error"] = repr(exc)
                continue
            rows = {}
            for hadith in payload.get("hadiths", []):
                number = _clean(hadith.get("hadithnumber"))
                if number:
                    rows[number] = hadith
            metadata = payload.get("metadata") or {}
            sections = {
                str(key): _clean(value)
                for key, value in (metadata.get("sections") or metadata.get("section") or {}).items()
                if str(key).strip()
            }
            collection_data[lang] = {
                "name": metadata.get("name"),
                "sections": sections,
                "rows": rows,
                "count": len(rows),
            }
        sources[collection] = collection_data
    return sources


def load_hf_sources(collections: tuple[str, ...], timeout: float) -> dict[str, dict[str, dict[str, str]]]:
    sources: dict[str, dict[str, dict[str, str]]] = {}
    for collection in collections:
        filename = HF_FILES.get(collection)
        if not filename:
            continue
        url = f"{HF_BASE_URL}/{urllib.parse.quote(filename)}"
        rows: dict[str, dict[str, str]] = {}
        try:
            raw = _fetch_bytes(url, timeout)
        except Exception as exc:
            sources[collection] = {"__error__": {"error": repr(exc)}}
            continue
        wrapper = io.StringIO(raw.decode("utf-8-sig", "replace"))
        for row in csv.DictReader(wrapper):
            number = _reference_number(row.get("Reference", ""))
            if not number:
                continue
            rows[number] = {
                "book": _clean(row.get("Book")),
                "chapter_number": _clean(row.get("Chapter_Number")),
                "chapter_title_ar": _clean(row.get("Chapter_Title_Arabic")),
                "chapter_title_en": _clean(row.get("Chapter_Title_English")),
                "arabic_text": _clean(row.get("Arabic_Text")),
                "english_text": _clean(row.get("English_Text")),
                "grade": _clean(row.get("Grade")),
                "reference": _clean(row.get("Reference")),
            }
        sources[collection] = rows
    return sources


def _english_text(value: Any) -> str:
    if isinstance(value, dict):
        return _clean(" ".join(str(value.get(key) or "") for key in ("narrator", "text")))
    return _clean(value)


def load_ahmedbaset_sources(collections: tuple[str, ...], timeout: float) -> dict[str, dict[str, Any]]:
    sources: dict[str, dict[str, Any]] = {}
    for collection in collections:
        url = f"{AHMEDBASET_BASE_URL}/{urllib.parse.quote(collection)}.json"
        try:
            payload = _fetch_json(url, timeout)
        except Exception as exc:
            sources[collection] = {"error": repr(exc), "rows": {}, "chapters": {}}
            continue
        rows = {}
        for hadith in payload.get("hadiths", []):
            number = _clean(hadith.get("id"))
            if not number:
                continue
            rows[number] = {
                "arabic_text": _clean(hadith.get("arabic")),
                "english_text": _english_text(hadith.get("english")),
                "chapter_id": _clean(hadith.get("chapterId")),
            }
        chapters = {
            _clean(chapter.get("id")): {
                "title_ar": _clean(chapter.get("arabic")),
                "title_en": _clean(chapter.get("english")),
            }
            for chapter in payload.get("chapters", [])
            if _clean(chapter.get("id"))
        }
        sources[collection] = {
            "metadata": payload.get("metadata") or {},
            "rows": rows,
            "chapters": chapters,
            "count": len(rows),
        }
    return sources


def _add_issue(issues: list[Issue], severity: str, check: str, row: LocalHadith, detail: str, **evidence: Any) -> None:
    issues.append(Issue(severity=severity, check=check, key=row.key, detail=detail, evidence=evidence))


def audit_rows(
    rows: list[LocalHadith],
    *,
    fawaz: dict[str, dict[str, Any]],
    hf: dict[str, dict[str, dict[str, str]]],
    ahmedbaset: dict[str, dict[str, Any]],
) -> tuple[list[Issue], dict[str, Any]]:
    issues: list[Issue] = []
    coverage = defaultdict(Counter)
    for row in rows:
        coverage[row.collection_name]["local"] += 1

        if _is_placeholder_title(row.book_title):
            _add_issue(issues, "hard", "book_title_placeholder", row, f"book_title={row.book_title!r}")
        if _is_placeholder_title(row.chapter_title_en, row.chapter_title_ar):
            _add_issue(
                issues,
                "hard",
                "chapter_title_placeholder",
                row,
                f"title_en={row.chapter_title_en!r}, title_ar={row.chapter_title_ar!r}",
            )
        if not row.english_text:
            _add_issue(issues, "warn", "english_text_missing", row, "English localization is empty")
        elif _arabic_ratio(row.english_text) > 0.15 or _latin_ratio(row.english_text) < 0.35:
            _add_issue(issues, "hard", "english_language_suspicious", row, "English text language ratio is suspicious")
        if not row.arabic_text:
            _add_issue(issues, "hard", "arabic_text_missing", row, "Arabic localization is empty")
        elif _arabic_ratio(row.arabic_text) < 0.45:
            _add_issue(issues, "hard", "arabic_language_suspicious", row, "Arabic text language ratio is suspicious")

        fawaz_collection = fawaz.get(row.collection_name, {})
        eng = fawaz_collection.get("eng") or {}
        ara = fawaz_collection.get("ara") or {}
        source_book_title = (eng.get("sections") or {}).get(row.book_number)
        if source_book_title:
            coverage[row.collection_name]["fawaz_book_title_matched"] += 1
            if _similarity(row.book_title, source_book_title, title=True) < 0.96:
                _add_issue(
                    issues,
                    "hard",
                    "book_title_source_mismatch",
                    row,
                    f"DB book title differs from fawaz section {row.book_number}",
                    db=row.book_title,
                    fawaz=source_book_title,
                )

        fawaz_en_row = (eng.get("rows") or {}).get(row.hadith_number)
        fawaz_ar_row = (ara.get("rows") or {}).get(row.hadith_number)
        if fawaz_en_row:
            coverage[row.collection_name]["fawaz_hadith_matched"] += 1
            reference = fawaz_en_row.get("reference") if isinstance(fawaz_en_row.get("reference"), dict) else {}
            ref_book = "" if reference.get("book") is None else str(reference.get("book")).strip()
            if row.collection_name != "muslim" and ref_book and ref_book != "0" and ref_book != row.book_number:
                _add_issue(
                    issues,
                    "hard",
                    "fawaz_reference_book_mismatch",
                    row,
                    f"fawaz reference.book={ref_book}, DB book={row.book_number}",
                )
            if row.collection_name != "muslim":
                source_text = _clean(fawaz_en_row.get("text"))
                if source_text and row.english_text and _norm(source_text) != _norm(row.english_text):
                    score = _similarity(source_text, row.english_text)
                    if score < 0.92:
                        _add_issue(
                            issues,
                            "warn",
                            "english_text_source_mismatch",
                            row,
                            f"DB English differs from fawaz, similarity={score:.3f}",
                            similarity=round(score, 3),
                        )
        if fawaz_ar_row and row.collection_name != "muslim":
            source_text = _clean(fawaz_ar_row.get("text"))
            if source_text and row.arabic_text and _norm(source_text) != _norm(row.arabic_text):
                score = _similarity(source_text, row.arabic_text)
                if score < 0.90:
                    _add_issue(
                        issues,
                        "warn",
                        "arabic_text_source_mismatch",
                        row,
                        f"DB Arabic differs from fawaz, similarity={score:.3f}",
                        similarity=round(score, 3),
                    )

        hf_row = (hf.get(row.collection_name) or {}).get(row.hadith_number)
        if hf_row:
            coverage[row.collection_name]["hf_hadith_matched"] += 1
            hf_title_en = hf_row.get("chapter_title_en", "")
            hf_title_ar = hf_row.get("chapter_title_ar", "")
            if hf_title_en and row.chapter_title_en:
                score = _similarity(row.chapter_title_en, hf_title_en, title=True)
                if score < 0.55:
                    _add_issue(
                        issues,
                        "info",
                        "hf_chapter_title_en_variant",
                        row,
                        f"DB chapter title differs from HF, similarity={score:.3f}",
                        db=row.chapter_title_en,
                        hf=hf_title_en,
                        similarity=round(score, 3),
                    )
            if hf_title_ar and row.chapter_title_ar:
                score = _similarity(row.chapter_title_ar, hf_title_ar, title=True)
                if score < 0.45:
                    _add_issue(
                        issues,
                        "info",
                        "hf_chapter_title_ar_variant",
                        row,
                        f"DB Arabic chapter title differs from HF, similarity={score:.3f}",
                        db=row.chapter_title_ar,
                        hf=hf_title_ar,
                        similarity=round(score, 3),
                    )
            if hf_row.get("english_text") and not row.english_text:
                _add_issue(issues, "hard", "english_missing_but_hf_has_text", row, "HF has English text, DB missing")
            if hf_row.get("english_text") and row.english_text:
                score = _similarity(hf_row["english_text"], row.english_text)
                if score < 0.55:
                    _add_issue(
                        issues,
                        "info",
                        "hf_english_text_variant",
                        row,
                        f"DB English differs from HF, similarity={score:.3f}",
                        similarity=round(score, 3),
                    )
            if hf_row.get("arabic_text") and not row.arabic_text:
                _add_issue(issues, "hard", "arabic_missing_but_hf_has_text", row, "HF has Arabic text, DB missing")
            if hf_row.get("arabic_text") and row.arabic_text:
                score = _similarity(hf_row["arabic_text"], row.arabic_text)
                if score < 0.50:
                    _add_issue(
                        issues,
                        "info",
                        "hf_arabic_text_variant",
                        row,
                        f"DB Arabic differs from HF, similarity={score:.3f}",
                        similarity=round(score, 3),
                    )

        ab_collection = ahmedbaset.get(row.collection_name, {})
        ab_row = (ab_collection.get("rows") or {}).get(row.hadith_number)
        if ab_row:
            coverage[row.collection_name]["ahmedbaset_hadith_matched"] += 1
            if ab_row.get("english_text") and not row.english_text:
                _add_issue(
                    issues,
                    "hard",
                    "english_missing_but_ahmedbaset_has_text",
                    row,
                    "AhmedBaset has English text, DB missing",
                )
            if ab_row.get("arabic_text") and not row.arabic_text:
                _add_issue(
                    issues,
                    "hard",
                    "arabic_missing_but_ahmedbaset_has_text",
                    row,
                    "AhmedBaset has Arabic text, DB missing",
                )

    source_counts = {
        collection: {
            "fawaz_eng": (fawaz.get(collection, {}).get("eng") or {}).get("count", 0),
            "fawaz_ara": (fawaz.get(collection, {}).get("ara") or {}).get("count", 0),
            "hf": len([key for key in (hf.get(collection) or {}) if key != "__error__"]),
            "ahmedbaset": (ahmedbaset.get(collection) or {}).get("count", 0),
        }
        for collection in COLLECTIONS
    }
    summary = {
        "coverage": {collection: dict(counter) for collection, counter in sorted(coverage.items())},
        "source_counts": source_counts,
    }
    return issues, summary


def _issue_summary(issues: list[Issue]) -> dict[str, Any]:
    by_severity = Counter(issue.severity for issue in issues)
    by_check = Counter(issue.check for issue in issues)
    return {
        "total": len(issues),
        "by_severity": dict(sorted(by_severity.items())),
        "by_check": dict(sorted(by_check.items())),
    }


def _select_ai_candidates(
    issues: list[Issue],
    rows: list[LocalHadith],
    *,
    max_candidates: int,
    sample_per_collection: int,
    seed: int,
) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    by_key = {row.key: row for row in rows}
    candidates: list[dict[str, Any]] = []
    for issue in issues:
        if issue.severity == "info":
            continue
        row = by_key.get(issue.key)
        if not row:
            continue
        candidates.append({"kind": "issue", "issue": asdict(issue), "row": _row_payload(row)})
        if len(candidates) >= max_candidates:
            break
    selected_keys = {item["row"]["key"] for item in candidates}
    rows_by_collection: dict[str, list[LocalHadith]] = defaultdict(list)
    for row in rows:
        if row.key not in selected_keys:
            rows_by_collection[row.collection_name].append(row)
    for _collection, collection_rows in sorted(rows_by_collection.items()):
        sample = rng.sample(collection_rows, min(sample_per_collection, len(collection_rows)))
        for row in sample:
            candidates.append({"kind": "sample", "row": _row_payload(row)})
    return candidates[: max_candidates + sample_per_collection * len(COLLECTIONS)]


def _row_payload(row: LocalHadith) -> dict[str, Any]:
    payload = asdict(row)
    payload["key"] = row.key
    return payload


def _gemini_key() -> str:
    return (
        os.getenv("ISLAMIC_CONTENT_GEMINI__API_KEY")
        or os.getenv("GEMINI_API_KEY")
        or os.getenv("GOOGLE_API_KEY")
        or ""
    ).strip()


def run_gemini_review(candidates: list[dict[str, Any]], timeout: float) -> dict[str, Any]:
    api_key = _gemini_key()
    if not api_key:
        return {"enabled": False, "error": "GEMINI_API_KEY is not configured", "reviews": []}
    model = os.getenv("ISLAMIC_CONTENT_GEMINI__MODEL", "gemini-2.0-flash")
    base_url = os.getenv(
        "ISLAMIC_CONTENT_GEMINI__BASE_URL",
        "https://generativelanguage.googleapis.com/v1beta/openai",
    ).rstrip("/")
    reviews: list[dict[str, Any]] = []
    batches = [candidates[index:index + 12] for index in range(0, len(candidates), 12)]
    for batch_number, batch in enumerate(batches, start=1):
        compact = []
        for item in batch:
            row = item["row"]
            compact.append(
                {
                    "kind": item["kind"],
                    "issue": item.get("issue", {}),
                    "key": row["key"],
                    "book_title": row["book_title"],
                    "chapter_title_en": row["chapter_title_en"],
                    "chapter_title_ar": row["chapter_title_ar"],
                    "english_preview": row["english_text"][:700],
                    "arabic_preview": row["arabic_text"][:700],
                }
            )
        prompt = (
            "You are auditing a Hadith database. Do not generate missing religious content. "
            "Classify each row only as ok, suspicious, source_gap, or needs_human. "
            "Flag placeholders, wrong language, clearly wrong title/body pairing, or fake defaults. "
            "Return JSON only: {\"reviews\":[{\"key\":\"...\",\"verdict\":\"ok|suspicious|source_gap|needs_human\","
            "\"reason\":\"short reason\"}]}.\n\n"
            f"Rows:\n{json.dumps(compact, ensure_ascii=False)}"
        )
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": "You are a conservative religious text data auditor."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
            "max_tokens": 2048,
        }
        request = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8", "replace"))
            content = data["choices"][0]["message"]["content"].strip()
            if content.startswith("```"):
                content = re.sub(r"^```(?:json)?|```$", "", content, flags=re.MULTILINE).strip()
            parsed = json.loads(content)
            if isinstance(parsed, dict) and isinstance(parsed.get("reviews"), list):
                reviews.extend(parsed["reviews"])
            else:
                reviews.append({"batch": batch_number, "verdict": "needs_human", "reason": "Unexpected AI JSON shape"})
        except Exception as exc:
            reviews.append({"batch": batch_number, "verdict": "needs_human", "reason": f"AI review failed: {exc!r}"})
        time.sleep(0.2)
    return {"enabled": True, "model": model, "candidate_count": len(candidates), "reviews": reviews}


def write_markdown(path: str, payload: dict[str, Any]) -> None:
    issues = payload["issues"]
    issue_summary = payload["issue_summary"]
    ai = payload.get("ai_review") or {}
    hard = issue_summary["by_severity"].get("hard", 0)
    warn = issue_summary["by_severity"].get("warn", 0)
    lines = [
        "# Hadith Source + AI Audit",
        "",
        f"- generated_at: {payload['generated_at']}",
        f"- local_non_book0_rows: {payload['local_non_book0_rows']}",
        f"- hard_issues: {hard}",
        f"- warning_issues: {warn}",
        f"- ai_review_enabled: {ai.get('enabled', False)}",
        "",
        "## Source Counts",
        "",
        "| collection | local | fawaz_eng | fawaz_ara | HF | AhmedBaset |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    coverage = payload["source_summary"]["coverage"]
    source_counts = payload["source_summary"]["source_counts"]
    for collection in COLLECTIONS:
        counts = source_counts.get(collection, {})
        lines.append(
            f"| {collection} | {coverage.get(collection, {}).get('local', 0)} | "
            f"{counts.get('fawaz_eng', 0)} | {counts.get('fawaz_ara', 0)} | "
            f"{counts.get('hf', 0)} | {counts.get('ahmedbaset', 0)} |"
        )
    lines.extend(["", "## Issue Summary", "", "```json", json.dumps(issue_summary, ensure_ascii=False, indent=2), "```"])
    if issues:
        lines.extend(["", "## First Issues", ""])
        for issue in issues[:50]:
            lines.append(f"- [{issue['severity']}] {issue['check']} {issue['key']}: {issue['detail']}")
    if ai:
        verdict_counts = Counter(item.get("verdict", "unknown") for item in ai.get("reviews", []))
        lines.extend(["", "## AI Review", "", "```json", json.dumps(dict(verdict_counts), ensure_ascii=False, indent=2), "```"])
        for item in ai.get("reviews", [])[:50]:
            lines.append(f"- [{item.get('verdict')}] {item.get('key', 'batch')}: {item.get('reason')}")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


async def main() -> int:
    args = parse_args()
    if not args.dsn:
        print("ERROR: --dsn or ISLAMIC_CONTENT_DATABASE__DSN is required", file=sys.stderr)
        return 2
    collections = tuple(item.strip() for item in args.collections.split(",") if item.strip())
    print("Loading local non-book0 Hadith rows...", flush=True)
    local_rows = await load_local_hadiths(args.dsn, collections)
    print(f"Loaded {len(local_rows)} local rows", flush=True)

    print("Loading fawaz hadith-api source...", flush=True)
    fawaz = load_fawaz_sources(collections, args.timeout)
    print("Loading HuggingFace meeAtif source...", flush=True)
    hf = load_hf_sources(collections, args.timeout)
    print("Loading AhmedBaset/hadith-json source...", flush=True)
    ahmedbaset = load_ahmedbaset_sources(collections, args.timeout)

    issues, source_summary = audit_rows(local_rows, fawaz=fawaz, hf=hf, ahmedbaset=ahmedbaset)
    ai_review: dict[str, Any] = {"enabled": False, "reviews": []}
    if args.ai_review:
        print("Running Gemini review for suspicious candidates and stratified samples...", flush=True)
        candidates = _select_ai_candidates(
            issues,
            local_rows,
            max_candidates=args.ai_max_candidates,
            sample_per_collection=args.ai_sample_per_collection,
            seed=args.seed,
        )
        ai_review = run_gemini_review(candidates, args.timeout)

    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "local_non_book0_rows": len(local_rows),
        "issue_summary": _issue_summary(issues),
        "source_summary": source_summary,
        "issues": [asdict(issue) for issue in issues],
        "ai_review": ai_review,
    }
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    write_markdown(args.markdown_out, payload)

    print(json.dumps({
        "out": args.out,
        "markdown_out": args.markdown_out,
        "local_non_book0_rows": len(local_rows),
        "issue_summary": payload["issue_summary"],
        "ai_review": {
            "enabled": ai_review.get("enabled", False),
            "review_count": len(ai_review.get("reviews", [])),
            "verdicts": dict(Counter(item.get("verdict", "unknown") for item in ai_review.get("reviews", []))),
        },
    }, ensure_ascii=False, indent=2))
    return 1 if payload["issue_summary"]["by_severity"].get("hard", 0) else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
