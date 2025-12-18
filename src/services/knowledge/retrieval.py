from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


_RE_LATIN_WORD = re.compile(r"[A-Za-z0-9]+")
_RE_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")


def tokenize(text: str) -> List[str]:
    """Tokenize text for lightweight lexical search.

    - Latin: lowercased alnum words
    - CJK: character bigrams (fallback to single chars for short runs)
    """
    t = (text or "").lower()
    if not t:
        return []

    tokens: List[str] = []
    tokens.extend(_RE_LATIN_WORD.findall(t))

    for run in _RE_CJK_RUN.findall(t):
        if len(run) <= 2:
            tokens.extend(list(run))
        else:
            tokens.extend(run[i : i + 2] for i in range(0, len(run) - 1))

    return [tok for tok in tokens if tok]


def bm25_scores(
    query_tokens: Sequence[str],
    documents_tokens: Sequence[Sequence[str]],
    *,
    k1: float = 1.2,
    b: float = 0.75,
) -> List[float]:
    if not documents_tokens:
        return []
    if not query_tokens:
        return [0.0 for _ in documents_tokens]

    N = len(documents_tokens)
    df: Counter[str] = Counter()
    doc_lens: List[int] = []

    for doc in documents_tokens:
        terms = [t for t in doc if t]
        doc_lens.append(len(terms))
        for term in set(terms):
            df[term] += 1

    avgdl = sum(doc_lens) / max(N, 1)
    q_terms = list(Counter([t for t in query_tokens if t]).keys())

    scores: List[float] = []
    for doc in documents_tokens:
        tf = Counter([t for t in doc if t])
        dl = len(doc)
        score = 0.0
        for term in q_terms:
            f = float(tf.get(term, 0))
            if f <= 0:
                continue
            n = float(df.get(term, 0))
            idf = math.log(1.0 + (N - n + 0.5) / (n + 0.5))
            denom = f + k1 * (1.0 - b + b * (dl / max(avgdl, 1e-9)))
            score += idf * (f * (k1 + 1.0)) / max(denom, 1e-9)
        scores.append(float(score))
    return scores


def reciprocal_rank_fusion(
    ranked_lists: Dict[str, List[str]],
    *,
    k: int = 60,
    weights: Optional[Dict[str, float]] = None,
) -> Dict[str, float]:
    weights = weights or {}
    fused: Dict[str, float] = defaultdict(float)
    for source, ids in (ranked_lists or {}).items():
        w = float(weights.get(source, 1.0))
        for rank, item_id in enumerate(ids, start=1):
            if not item_id:
                continue
            fused[item_id] += w / float(int(k) + rank)
    return dict(fused)


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b:
        return 0.0
    if len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += float(x) * float(y)
        na += float(x) * float(x)
        nb += float(y) * float(y)
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return float(dot / (math.sqrt(na) * math.sqrt(nb)))


@dataclass(frozen=True)
class MMRPick:
    item_id: str
    mmr_score: float
    relevance: float
    max_sim_to_selected: float


def mmr_select(
    candidates: List[str],
    relevance: Dict[str, float],
    vectors: Dict[str, Sequence[float]],
    *,
    top_k: int,
    lambda_mult: float = 0.5,
    similarity_threshold: Optional[float] = None,
) -> Tuple[List[str], Dict[str, MMRPick]]:
    """MMR selection (diversify while keeping relevance).

    Returns (selected_ids, pick_info_by_id).
    """
    if not candidates or top_k <= 0:
        return [], {}

    lam = float(lambda_mult)
    lam = max(0.0, min(1.0, lam))
    threshold = float(similarity_threshold) if similarity_threshold is not None else None

    remaining = [c for c in candidates if c]
    selected: List[str] = []
    picks: Dict[str, MMRPick] = {}

    def max_sim(cid: str) -> float:
        if not selected:
            return 0.0
        v = vectors.get(cid)
        if v is None:
            return 0.0
        best = 0.0
        for sid in selected:
            sv = vectors.get(sid)
            if sv is None:
                continue
            best = max(best, cosine_similarity(v, sv))
        return float(best)

    while remaining and len(selected) < int(top_k):
        best_id: Optional[str] = None
        best_mmr = -1e30
        best_rel = 0.0
        best_sim = 0.0

        for cid in remaining:
            rel = float(relevance.get(cid, 0.0))
            sim = max_sim(cid)
            if threshold is not None and selected and sim >= threshold:
                continue
            mmr = lam * rel - (1.0 - lam) * sim
            if mmr > best_mmr:
                best_id = cid
                best_mmr = mmr
                best_rel = rel
                best_sim = sim

        if best_id is None:
            # If threshold filtered everything, stop early.
            break

        selected.append(best_id)
        remaining = [c for c in remaining if c != best_id]
        picks[best_id] = MMRPick(
            item_id=best_id,
            mmr_score=float(best_mmr),
            relevance=float(best_rel),
            max_sim_to_selected=float(best_sim),
        )

    return selected, picks

