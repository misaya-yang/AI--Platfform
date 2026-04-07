#!/usr/bin/env python3
"""Retrieval Quality Evaluation Framework (P3).

Measures Recall@k, MRR, and NDCG@10 across retrieval modes (dense, bm25, hybrid)
against 50 labeled Islamic KB query-answer pairs.

Usage:
    python3 scripts/evaluate_retrieval.py [--server URL] [--top-k 10] [--modes hybrid,dense,bm25]
    python3 scripts/evaluate_retrieval.py --csv results.csv  # Export results

Environment:
    SERVER_URL: Knowledge service URL (default: http://localhost:8092)
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import logging
import math
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

SERVER_URL = os.getenv("SERVER_URL", "http://localhost:8092")
DATASET_ID = os.getenv("DATASET_ID", "islamic-knowledge")

# =============================================================================
# 50 Labeled Query-Answer Pairs for Islamic KB Evaluation
# =============================================================================
# Each entry: (query, expected_keywords, source_type_hint)
# expected_keywords: words/phrases that MUST appear in a relevant result
# A result is "relevant" if ANY expected keyword matches (case-insensitive substring)

EVAL_DATASET: list[dict[str, Any]] = [
    # --- Quran (15 queries) ---
    {"query": "Surah Al-Fatiha", "keywords": ["al-fatiha", "1:"], "source": "quran"},
    {"query": "Ayat al-Kursi verse of the throne", "keywords": ["2:255", "kursi", "throne"], "source": "quran"},
    {"query": "Surah Al-Ikhlas monotheism", "keywords": ["112:", "ikhlas", "ahad"], "source": "quran"},
    {"query": "patience in the Quran", "keywords": ["sabr", "patient", "صبر"], "source": "quran"},
    {"query": "creation of heavens and earth", "keywords": ["heaven", "earth", "creat", "سماوات"], "source": "quran"},
    {"query": "story of Prophet Yusuf", "keywords": ["yusuf", "joseph", "12:"], "source": "quran"},
    {"query": "Surah Al-Baqarah fasting verse", "keywords": ["fast", "صيام", "2:183", "2:184", "2:185"], "source": "quran"},
    {"query": "verse about charity and spending", "keywords": ["charity", "spend", "infaq", "انفق", "صدق"], "source": "quran"},
    {"query": "Surah An-Nur light verse", "keywords": ["24:35", "nur", "light", "نور"], "source": "quran"},
    {"query": "Quran on justice and fairness", "keywords": ["justice", "just", "fair", "عدل", "قسط"], "source": "quran"},
    {"query": "day of judgment resurrection", "keywords": ["judgment", "resurrect", "qiyam", "يوم القيامة"], "source": "quran"},
    {"query": "marriage in Islam Quran", "keywords": ["marriage", "wife", "wives", "nikah", "زوج"], "source": "quran"},
    {"query": "Quran prohibition of riba interest", "keywords": ["riba", "interest", "usury", "ربا", "2:275"], "source": "quran"},
    {"query": "Surah Al-Mulk sovereignty", "keywords": ["67:", "mulk", "sovereign", "dominion"], "source": "quran"},
    {"query": "bismillah in the name of Allah", "keywords": ["bismillah", "بسم الله", "name of"], "source": "quran"},

    # --- Hadith (15 queries) ---
    {"query": "hadith about intention niyyah", "keywords": ["intention", "niyyah", "innam", "action"], "source": "hadith"},
    {"query": "five pillars of Islam hadith", "keywords": ["pillar", "shahad", "salah", "zakat", "hajj", "fasting"], "source": "hadith"},
    {"query": "Sahih Bukhari hadith on prayer", "keywords": ["bukhari", "prayer", "salah", "صلاة"], "source": "hadith"},
    {"query": "hadith about kindness to parents", "keywords": ["parent", "mother", "father", "والد", "kind"], "source": "hadith"},
    {"query": "hadith on seeking knowledge", "keywords": ["knowledge", "learn", "seek", "علم", "طلب"], "source": "hadith"},
    {"query": "Prophet Muhammad on forgiveness", "keywords": ["forgiv", "pardon", "عفو", "مغفر"], "source": "hadith"},
    {"query": "hadith about fasting Ramadan", "keywords": ["fast", "ramadan", "صيام", "رمضان"], "source": "hadith"},
    {"query": "hadith on truthfulness honesty", "keywords": ["truth", "honest", "صدق", "sidq"], "source": "hadith"},
    {"query": "Sahih Muslim hadith on faith iman", "keywords": ["muslim", "faith", "iman", "إيمان", "believ"], "source": "hadith"},
    {"query": "hadith about neighbor rights", "keywords": ["neighbor", "neighbour", "جار"], "source": "hadith"},
    {"query": "hadith on Friday Jummah prayer", "keywords": ["friday", "jumu", "جمعة"], "source": "hadith"},
    {"query": "hadith about dua supplication", "keywords": ["dua", "supplicat", "دعاء", "pray"], "source": "hadith"},
    {"query": "hadith on charity sadaqah", "keywords": ["charity", "sadaq", "صدقة", "give"], "source": "hadith"},
    {"query": "hadith about Quran recitation reward", "keywords": ["recit", "quran", "reward", "تلاوة", "letter"], "source": "hadith"},
    {"query": "hadith on backbiting gheebah", "keywords": ["backbit", "gheeb", "غيبة", "slander"], "source": "hadith"},

    # --- Tafseer (10 queries) ---
    {"query": "Tafsir Ibn Kathir Surah Al-Baqarah", "keywords": ["ibn kathir", "baqarah", "2:"], "source": "tafseer"},
    {"query": "tafseer meaning of Iqamat As-Salah", "keywords": ["iqam", "salah", "prayer", "صلاة"], "source": "tafseer"},
    {"query": "tafsir explanation of bismillah", "keywords": ["bismillah", "بسم", "name", "merciful"], "source": "tafseer"},
    {"query": "exegesis of ayat al-kursi", "keywords": ["kursi", "throne", "2:255", "living"], "source": "tafseer"},
    {"query": "tafseer of Surah Yasin", "keywords": ["yasin", "36:", "ya sin"], "source": "tafseer"},
    {"query": "Qurtubi tafsir commentary", "keywords": ["qurtubi", "قرطبي"], "source": "tafseer"},
    {"query": "tafsir on jihad and striving", "keywords": ["jihad", "striv", "جهاد", "fight"], "source": "tafseer"},
    {"query": "tafseer of last two verses Al-Baqarah", "keywords": ["2:285", "2:286", "baqarah", "messeng"], "source": "tafseer"},
    {"query": "tafsir on taqwa God-consciousness", "keywords": ["taqwa", "conscious", "piety", "تقوى", "fear"], "source": "tafseer"},
    {"query": "Atlas of the Quran geography", "keywords": ["atlas", "geograph", "map", "place"], "source": "tafseer"},

    # --- Fiqh / General Islamic (10 queries) ---
    {"query": "how to perform wudu ablution", "keywords": ["wudu", "ablution", "wash", "وضوء"], "source": "fiqh"},
    {"query": "rules of zakah calculation", "keywords": ["zakah", "zakat", "calculat", "nisab", "2.5"], "source": "fiqh"},
    {"query": "conditions for valid prayer salah", "keywords": ["condition", "valid", "prayer", "salah", "qibla"], "source": "fiqh"},
    {"query": "Hanafi school of thought rulings", "keywords": ["hanafi", "حنفي", "abu hanif"], "source": "fiqh"},
    {"query": "pilgrimage Hajj rituals steps", "keywords": ["hajj", "pilgrim", "ritual", "tawaf", "حج"], "source": "fiqh"},
    {"query": "Islamic inheritance faraid law", "keywords": ["inherit", "faraid", "فرائض", "estate", "share"], "source": "fiqh"},
    {"query": "halal and haram food in Islam", "keywords": ["halal", "haram", "food", "حلال", "حرام", "eat"], "source": "fiqh"},
    {"query": "tashahhud sitting in prayer", "keywords": ["tashahhud", "sitting", "تشهد", "tahiyy"], "source": "fiqh"},
    {"query": "sunnah prayers voluntary nawafil", "keywords": ["sunnah", "voluntary", "nawafil", "نوافل", "extra"], "source": "fiqh"},
    {"query": "Islamic funeral janazah prayer", "keywords": ["funeral", "janaz", "death", "جنازة", "burial"], "source": "fiqh"},
]

assert len(EVAL_DATASET) == 50, f"Expected 50 queries, got {len(EVAL_DATASET)}"


# =============================================================================
# Metrics
# =============================================================================

def is_relevant(result_text: str, keywords: list[str]) -> bool:
    """Check if a result is relevant based on keyword matching."""
    text_lower = result_text.lower()
    return any(kw.lower() in text_lower for kw in keywords)


def recall_at_k(relevant_flags: list[bool], k: int) -> float:
    """Recall@k: fraction of queries with at least one relevant result in top-k."""
    return 1.0 if any(relevant_flags[:k]) else 0.0


def reciprocal_rank(relevant_flags: list[bool]) -> float:
    """MRR: 1/rank of first relevant result."""
    for i, rel in enumerate(relevant_flags):
        if rel:
            return 1.0 / (i + 1)
    return 0.0


def ndcg_at_k(relevant_flags: list[bool], k: int) -> float:
    """NDCG@k: normalized discounted cumulative gain."""
    dcg = sum(
        (1.0 if rel else 0.0) / math.log2(i + 2)
        for i, rel in enumerate(relevant_flags[:k])
    )
    # Ideal: all relevant results at the top
    num_relevant = sum(relevant_flags[:k])
    idcg = sum(1.0 / math.log2(i + 2) for i in range(num_relevant))
    return dcg / idcg if idcg > 0 else 0.0


# =============================================================================
# Evaluation Runner
# =============================================================================

@dataclass
class QueryResult:
    query: str
    mode: str
    source_hint: str
    keywords: list[str]
    num_results: int = 0
    relevant_flags: list[bool] = field(default_factory=list)
    recall_1: float = 0.0
    recall_3: float = 0.0
    recall_5: float = 0.0
    recall_10: float = 0.0
    mrr: float = 0.0
    ndcg_10: float = 0.0
    latency_ms: float = 0.0
    error: str = ""


@dataclass
class ModeMetrics:
    mode: str
    num_queries: int = 0
    avg_recall_1: float = 0.0
    avg_recall_3: float = 0.0
    avg_recall_5: float = 0.0
    avg_recall_10: float = 0.0
    avg_mrr: float = 0.0
    avg_ndcg_10: float = 0.0
    avg_latency_ms: float = 0.0
    failures: int = 0


async def evaluate_query(
    client: httpx.AsyncClient,
    server_url: str,
    dataset_id: str,
    entry: dict[str, Any],
    mode: str,
    top_k: int,
    semaphore: asyncio.Semaphore,
) -> QueryResult:
    """Evaluate a single query against the retrieval API."""
    qr = QueryResult(
        query=entry["query"],
        mode=mode,
        source_hint=entry.get("source", ""),
        keywords=entry["keywords"],
    )

    async with semaphore:
        start = time.time()
        try:
            resp = await client.post(
                f"{server_url}/api/v1/knowledge/{dataset_id}/retrieve",
                headers={
                    "Content-Type": "application/json",
                    "X-User-Id": "admin",
                    "X-Tenant-Id": "default",
                },
                json={"query": entry["query"], "mode": mode, "top_k": top_k},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results", [])
        except Exception as e:
            qr.error = str(e)[:200]
            qr.latency_ms = (time.time() - start) * 1000
            return qr

        qr.latency_ms = (time.time() - start) * 1000
        qr.num_results = len(results)

        # Check relevance of each result
        for r in results:
            text = r.get("text", "")
            meta = r.get("metadata", {})
            # Also check metadata fields for keyword matching
            search_text = f"{text} {meta.get('source_type', '')} {meta.get('citation_text', '')}"
            qr.relevant_flags.append(is_relevant(search_text, entry["keywords"]))

        # Compute metrics
        qr.recall_1 = recall_at_k(qr.relevant_flags, 1)
        qr.recall_3 = recall_at_k(qr.relevant_flags, 3)
        qr.recall_5 = recall_at_k(qr.relevant_flags, 5)
        qr.recall_10 = recall_at_k(qr.relevant_flags, min(10, len(qr.relevant_flags)))
        qr.mrr = reciprocal_rank(qr.relevant_flags)
        qr.ndcg_10 = ndcg_at_k(qr.relevant_flags, min(10, len(qr.relevant_flags)))

    return qr


async def run_evaluation(
    server_url: str,
    dataset_id: str,
    modes: list[str],
    top_k: int = 10,
    concurrency: int = 5,
) -> dict[str, ModeMetrics]:
    """Run full evaluation across all modes."""
    semaphore = asyncio.Semaphore(concurrency)
    all_results: dict[str, list[QueryResult]] = {m: [] for m in modes}

    async with httpx.AsyncClient() as client:
        for mode in modes:
            logger.info(f"Evaluating mode: {mode} ({len(EVAL_DATASET)} queries)")
            tasks = [
                evaluate_query(client, server_url, dataset_id, entry, mode, top_k, semaphore)
                for entry in EVAL_DATASET
            ]
            results = await asyncio.gather(*tasks)
            all_results[mode] = results

    # Aggregate metrics per mode
    metrics: dict[str, ModeMetrics] = {}
    for mode, results in all_results.items():
        mm = ModeMetrics(mode=mode, num_queries=len(results))
        valid = [r for r in results if not r.error]
        mm.failures = len(results) - len(valid)

        if valid:
            mm.avg_recall_1 = sum(r.recall_1 for r in valid) / len(valid)
            mm.avg_recall_3 = sum(r.recall_3 for r in valid) / len(valid)
            mm.avg_recall_5 = sum(r.recall_5 for r in valid) / len(valid)
            mm.avg_recall_10 = sum(r.recall_10 for r in valid) / len(valid)
            mm.avg_mrr = sum(r.mrr for r in valid) / len(valid)
            mm.avg_ndcg_10 = sum(r.ndcg_10 for r in valid) / len(valid)
            mm.avg_latency_ms = sum(r.latency_ms for r in valid) / len(valid)

        metrics[mode] = mm

    # Print detailed results
    _print_report(metrics, all_results)
    return metrics


def _print_report(
    metrics: dict[str, ModeMetrics],
    all_results: dict[str, list[QueryResult]],
) -> None:
    """Print a formatted evaluation report."""
    print("\n" + "=" * 80)
    print("RETRIEVAL QUALITY EVALUATION REPORT")
    print("=" * 80)

    # Summary table
    print(f"\n{'Mode':<10} {'Recall@1':>9} {'Recall@3':>9} {'Recall@5':>9} {'Recall@10':>10} {'MRR':>7} {'NDCG@10':>8} {'Latency':>9} {'Fail':>5}")
    print("-" * 80)
    for mode, mm in metrics.items():
        print(
            f"{mode:<10} {mm.avg_recall_1:>8.1%} {mm.avg_recall_3:>8.1%} "
            f"{mm.avg_recall_5:>8.1%} {mm.avg_recall_10:>9.1%} "
            f"{mm.avg_mrr:>6.3f} {mm.avg_ndcg_10:>7.3f} "
            f"{mm.avg_latency_ms:>7.0f}ms {mm.failures:>4}",
        )
    print("-" * 80)

    # Per-source breakdown for hybrid mode
    if "hybrid" in all_results:
        print("\nHybrid Mode — Breakdown by Source Type:")
        by_source: dict[str, list[QueryResult]] = {}
        for r in all_results["hybrid"]:
            by_source.setdefault(r.source_hint, []).append(r)

        print(f"  {'Source':<10} {'Recall@1':>9} {'Recall@5':>9} {'MRR':>7} {'Queries':>8}")
        print(f"  {'-'*46}")
        for src, results in sorted(by_source.items()):
            valid = [r for r in results if not r.error]
            if valid:
                r1 = sum(r.recall_1 for r in valid) / len(valid)
                r5 = sum(r.recall_5 for r in valid) / len(valid)
                mrr = sum(r.mrr for r in valid) / len(valid)
                print(f"  {src:<10} {r1:>8.1%} {r5:>8.1%} {mrr:>6.3f} {len(valid):>7}")

    # Failed queries
    for mode, results in all_results.items():
        missed = [r for r in results if not r.error and r.recall_5 == 0.0]
        if missed:
            print(f"\n{mode}: {len(missed)} queries with zero Recall@5:")
            for r in missed[:10]:
                print(f"  - \"{r.query}\" (expected: {r.keywords[:3]})")

    print()


def export_csv(
    metrics: dict[str, ModeMetrics],
    all_results: dict[str, list[QueryResult]],
    path: str,
) -> None:
    """Export detailed results to CSV."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "mode", "query", "source_hint", "num_results",
        "recall@1", "recall@3", "recall@5", "recall@10",
        "mrr", "ndcg@10", "latency_ms", "error",
    ])
    for mode, results in all_results.items():
        for r in results:
            writer.writerow([
                r.mode, r.query, r.source_hint, r.num_results,
                f"{r.recall_1:.3f}", f"{r.recall_3:.3f}",
                f"{r.recall_5:.3f}", f"{r.recall_10:.3f}",
                f"{r.mrr:.3f}", f"{r.ndcg_10:.3f}",
                f"{r.latency_ms:.0f}", r.error,
            ])
    with open(path, "w") as f:
        f.write(buf.getvalue())
    logger.info(f"Results exported to {path}")


# =============================================================================
# Main
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="Retrieval Quality Evaluation")
    parser.add_argument("--server", default=SERVER_URL, help="Knowledge service URL")
    parser.add_argument("--dataset", default=DATASET_ID, help="Dataset ID")
    parser.add_argument("--top-k", type=int, default=10, help="Top-K results")
    parser.add_argument("--modes", default="hybrid,dense,bm25", help="Comma-separated modes")
    parser.add_argument("--concurrency", type=int, default=5, help="Concurrent queries")
    parser.add_argument("--csv", default="", help="Export CSV path")
    args = parser.parse_args()

    modes = [m.strip() for m in args.modes.split(",")]
    logger.info(f"Server: {args.server}, Dataset: {args.dataset}, Modes: {modes}, Top-K: {args.top_k}")

    all_results: dict[str, list[QueryResult]] = {}

    async def _run():
        nonlocal all_results
        semaphore = asyncio.Semaphore(args.concurrency)
        async with httpx.AsyncClient() as client:
            for mode in modes:
                logger.info(f"Evaluating mode: {mode} ({len(EVAL_DATASET)} queries)")
                tasks = [
                    evaluate_query(
                        client, args.server, args.dataset, entry, mode, args.top_k, semaphore,
                    )
                    for entry in EVAL_DATASET
                ]
                results = await asyncio.gather(*tasks)
                all_results[mode] = results

        # Aggregate
        metrics: dict[str, ModeMetrics] = {}
        for mode, results in all_results.items():
            mm = ModeMetrics(mode=mode, num_queries=len(results))
            valid = [r for r in results if not r.error]
            mm.failures = len(results) - len(valid)
            if valid:
                mm.avg_recall_1 = sum(r.recall_1 for r in valid) / len(valid)
                mm.avg_recall_3 = sum(r.recall_3 for r in valid) / len(valid)
                mm.avg_recall_5 = sum(r.recall_5 for r in valid) / len(valid)
                mm.avg_recall_10 = sum(r.recall_10 for r in valid) / len(valid)
                mm.avg_mrr = sum(r.mrr for r in valid) / len(valid)
                mm.avg_ndcg_10 = sum(r.ndcg_10 for r in valid) / len(valid)
                mm.avg_latency_ms = sum(r.latency_ms for r in valid) / len(valid)
            metrics[mode] = mm

        _print_report(metrics, all_results)

        if args.csv:
            export_csv(metrics, all_results, args.csv)

    asyncio.run(_run())


if __name__ == "__main__":
    main()
