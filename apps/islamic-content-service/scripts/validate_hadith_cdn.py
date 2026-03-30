#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from urllib.error import HTTPError
from urllib.request import urlopen


def _get_json(url: str) -> dict:
    with urlopen(url, timeout=30) as response:
        return json.load(response)


def _get_cdn_json(base_url: str, path: str) -> dict:
    normalized = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    errors: list[str] = []
    for suffix in (".min.json", ".json"):
        url = f"{normalized}{suffix}"
        try:
            return _get_json(url)
        except HTTPError as exc:
            if exc.code == 404:
                errors.append(f"{url} -> 404")
                continue
            raise
    raise RuntimeError("CDN resource not found:\n" + "\n".join(errors))


def _sections_from_payload(payload: dict) -> dict[str, str]:
    metadata = payload.get("metadata") or {}
    sections = metadata.get("sections") or metadata.get("section") or {}
    return {
        str(section_number): str(title)
        for section_number, title in sections.items()
        if str(section_number).strip() and not (str(section_number) == "0" and not str(title).strip())
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate Hadith API data against the canonical CDN source.")
    parser.add_argument("--service-base-url", default="http://localhost:8091")
    parser.add_argument("--cdn-base-url", default="https://cdn.jsdelivr.net/gh/fawazahmed0/hadith-api@1")
    parser.add_argument("--collection", default="bukhari")
    parser.add_argument("--samples", type=int, default=3)
    args = parser.parse_args()

    edition = _get_cdn_json(args.cdn_base_url, f"editions/eng-{args.collection}")
    sections = _sections_from_payload(edition)
    books_payload = _get_json(
        f"{args.service_base_url.rstrip('/')}/api/v1/hadith/collections/{args.collection}/books"
    )
    books = books_payload.get("books") or []
    books_by_number = {str(book["book_number"]): book for book in books}
    synthetic_gap = books_by_number.get("0")
    if synthetic_gap and str(synthetic_gap.get("title") or "").startswith("Unmapped (CDN section gap)"):
        books_by_number.pop("0", None)

    errors: list[str] = []
    if len(books_by_number) != len(sections):
        errors.append(f"book count mismatch: service={len(books_by_number)} cdn={len(sections)}")

    for section_number, expected_title in sections.items():
        actual = books_by_number.get(section_number)
        if actual is None:
            errors.append(f"missing book/section {section_number}: {expected_title}")
            continue
        actual_title = str(actual.get("title") or "").strip()
        if actual_title != expected_title:
            errors.append(
                f"title mismatch for {section_number}: service={actual_title!r} cdn={expected_title!r}"
            )

    sampled_sections = list(sorted(sections, key=lambda item: int(item)))[: max(args.samples, 0)]
    for section_number in sampled_sections:
        payload = _get_json(
            f"{args.service_base_url.rstrip('/')}/api/v1/hadith/collections/{args.collection}/books/{section_number}/hadiths?limit=1"
        )
        items = payload.get("items") or []
        if not items:
            errors.append(f"no hadith items returned for section {section_number}")
            continue
        first_item = items[0]
        actual_title = str(first_item.get("section_title") or first_item.get("title") or "").strip()
        if actual_title != sections[section_number]:
            errors.append(
                f"item section mismatch for {section_number}: service={actual_title!r} cdn={sections[section_number]!r}"
            )

    if errors:
        print(json.dumps({"ok": False, "collection": args.collection, "errors": errors}, ensure_ascii=False, indent=2))
        return 1

    print(
        json.dumps(
            {
                "ok": True,
                "collection": args.collection,
                "books_validated": len(sections),
                "sampled_sections": sampled_sections,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
