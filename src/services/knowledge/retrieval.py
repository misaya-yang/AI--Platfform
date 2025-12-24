from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


_RE_LATIN_WORD = re.compile(r"[A-Za-z0-9\-_]+")
_RE_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_RE_QUOTED = re.compile(r'["\']([^"\']+)["\']')


def tokenize(text: str, keep_original: bool = False) -> List[str]:
    """Tokenize text for lightweight lexical search.

    - Latin: lowercased alnum words (including hyphenated like Q-Flow)
    - CJK: single characters (for better exact matching)
    - Quoted phrases are kept intact
    
    Args:
        text: Input text to tokenize
        keep_original: If True, also include the original query as a token (for exact matching)
    """
    t = (text or "").strip()
    if not t:
        return []

    tokens: List[str] = []
    
    # Keep the original query for exact matching
    if keep_original and len(t) > 1:
        tokens.append(t.lower())
    
    # Extract quoted phrases first
    for match in _RE_QUOTED.finditer(t):
        phrase = match.group(1).strip()
        if phrase:
            tokens.append(phrase.lower())
    
    # Remove quotes for further processing
    t_clean = _RE_QUOTED.sub(' ', t).lower()
    
    # Extract Latin words (including hyphenated like Q-Flow)
    latin_words = _RE_LATIN_WORD.findall(t_clean)
    tokens.extend(latin_words)
    
    # For CJK, use single characters for better exact matching
    for run in _RE_CJK_RUN.findall(t_clean):
        # Add whole CJK phrase for exact matching
        if len(run) >= 2:
            tokens.append(run)
        # Also add individual characters
        tokens.extend(list(run))

    # Deduplicate while preserving order
    seen = set()
    result = []
    for tok in tokens:
        if tok and tok not in seen:
            seen.add(tok)
            result.append(tok)
    
    return result


def bm25_scores(
    query_tokens: Sequence[str],
    documents_tokens: Sequence[Sequence[str]],
    *,
    k1: float = 1.2,
    b: float = 0.75,
) -> List[float]:
    """Calculate BM25 scores for documents against query tokens."""
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


def compute_text_match_score(
    query: str,
    text: str,
    *,
    exact_match_boost: float = 5.0,  # Increased from 2.0 for bigger score gaps
    term_match_weight: float = 0.3,
) -> Tuple[float, Dict[str, Any]]:
    """Compute a text matching score based on exact and term matches.
    
    Returns:
        (score, debug_info) where score is 0.0-1.0 normalized
        
    Score interpretation:
    - 1.0: Exact query match found in text
    - 0.5-0.9: High term match ratio
    - 0.1-0.5: Partial term matches
    - 0.0: No matches
    """
    if not query or not text:
        return 0.0, {"exact_match": False, "term_matches": 0, "term_ratio": 0.0}
    
    query_lower = query.lower().strip()
    text_lower = text.lower()
    
    # Check exact query match (the whole query appears in the text)
    exact_match = query_lower in text_lower
    
    # Also check for partial exact match (each word matches exactly)
    query_words = query_lower.split()
    partial_exact = all(w in text_lower for w in query_words) if query_words else False
    
    # Extract meaningful terms from query (>1 char)
    query_terms = [t.strip() for t in re.split(r'[\s\"\'\-\(\)\[\]，。：；！？]+', query_lower) 
                   if t.strip() and len(t.strip()) > 1]
    
    if not query_terms:
        query_terms = [query_lower] if len(query_lower) > 1 else []
    
    # Count term matches
    term_matches = sum(1 for t in query_terms if t in text_lower)
    term_ratio = term_matches / max(len(query_terms), 1)
    
    # Calculate score with larger gaps between match types
    score = 0.0
    if exact_match:
        score = 1.0  # Perfect score for exact match
    elif partial_exact:
        score = 0.85  # High score for all words matching
    elif term_ratio >= 0.8:
        score = 0.7 + term_ratio * 0.15  # High term ratio
    elif term_ratio >= 0.5:
        score = 0.4 + term_ratio * 0.3  # Medium term ratio
    elif term_ratio > 0:
        score = term_ratio * 0.4  # Low term ratio
    else:
        score = 0.0  # No matches
    
    return score, {
        "exact_match": exact_match,
        "partial_exact": partial_exact,
        "term_matches": term_matches,
        "total_terms": len(query_terms),
        "term_ratio": term_ratio,
    }


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

