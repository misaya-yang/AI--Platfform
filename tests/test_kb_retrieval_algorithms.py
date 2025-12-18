def test_tokenize_latin_and_cjk():
    from src.services.knowledge.retrieval import tokenize

    assert tokenize("Hello, world!") == ["hello", "world"]

    # CJK bigrams
    toks = tokenize("智能网关知识库")
    assert "智能" in toks
    assert "知识" in toks


def test_bm25_prefers_matching_docs():
    from src.services.knowledge.retrieval import bm25_scores, tokenize

    query = "vector search"
    q = tokenize(query)
    docs = [
        tokenize("this document talks about vector search and embeddings"),
        tokenize("nothing relevant here"),
        tokenize("search is important, but not about vectors"),
    ]
    scores = bm25_scores(q, docs)
    assert len(scores) == 3
    assert scores[0] > scores[1]
    assert scores[0] > scores[2]


def test_rrf_fusion_rank_order():
    from src.services.knowledge.retrieval import reciprocal_rank_fusion

    fused = reciprocal_rank_fusion(
        {"vector": ["a", "b", "c"], "keyword": ["b", "d", "a"]},
        k=60,
        weights={"vector": 1.0, "keyword": 1.0},
    )
    # 'a' appears in both lists, but with better ranks it should be competitive
    assert fused["a"] > 0.0
    assert fused["b"] > 0.0
    assert fused["d"] > 0.0
    assert fused["b"] > fused["d"]  # b appears high in both


def test_mmr_select_diversifies():
    from src.services.knowledge.retrieval import mmr_select

    # Two near-duplicates (a,b) and one different (c)
    candidates = ["a", "b", "c"]
    relevance = {"a": 1.0, "b": 0.99, "c": 0.8}
    vectors = {
        "a": [1.0, 0.0],
        "b": [0.999, 0.001],
        "c": [0.0, 1.0],
    }

    selected, picks = mmr_select(
        candidates,
        relevance,
        vectors,
        top_k=2,
        lambda_mult=0.5,
        similarity_threshold=0.95,
    )
    assert selected[0] == "a"
    assert "c" in selected  # diversify away from near-duplicate 'b'
    assert picks[selected[0]].mmr_score >= picks[selected[0]].mmr_score - 1e-9
