"""
知识库检索算法测试

测试内容：
- 分词 (tokenize)
- BM25 评分
- RRF 融合
- MMR 多样化
"""

from src.services.knowledge.retrieval import (
    tokenize,
    bm25_scores,
    reciprocal_rank_fusion,
    mmr_select,
)


class TestTokenize:
    """分词测试"""

    def test_tokenize_latin(self):
        """测试拉丁文分词"""
        assert tokenize("Hello, world!") == ["hello", "world"]

    def test_tokenize_cjk(self):
        """测试中日韩文分词 (bigrams)"""
        toks = tokenize("智能网关知识库")
        assert "智能" in toks
        assert "知识" in toks

    def test_tokenize_mixed(self):
        """测试混合文本分词"""
        toks = tokenize("AI网关Gateway")
        assert len(toks) > 0


class TestBM25:
    """BM25 评分测试"""

    def test_bm25_prefers_matching_docs(self):
        """测试 BM25 优先匹配文档"""
        query = "vector search"
        q = tokenize(query)
        docs = [
            tokenize("this document talks about vector search and embeddings"),
            tokenize("nothing relevant here"),
            tokenize("search is important, but not about vectors"),
        ]

        scores = bm25_scores(q, docs)

        assert len(scores) == 3
        assert scores[0] > scores[1]  # 第一个文档得分更高
        assert scores[0] > scores[2]  # 第一个文档得分更高

    def test_bm25_empty_query(self):
        """测试空查询"""
        q = []
        docs = [tokenize("some document")]

        scores = bm25_scores(q, docs)
        assert len(scores) == 1


class TestRRF:
    """RRF 融合测试"""

    def test_rrf_fusion_rank_order(self):
        """测试 RRF 融合排序"""
        fused = reciprocal_rank_fusion(
            {"vector": ["a", "b", "c"], "keyword": ["b", "d", "a"]},
            k=60,
            weights={"vector": 1.0, "keyword": 1.0},
        )

        # 所有文档应该有得分
        assert fused["a"] > 0.0
        assert fused["b"] > 0.0
        assert fused["d"] > 0.0

        # b 在两个列表中排名都高，应该得分更高
        assert fused["b"] > fused["d"]

    def test_rrf_with_weights(self):
        """测试带权重的 RRF"""
        fused = reciprocal_rank_fusion(
            {"vector": ["a", "b"], "keyword": ["b", "a"]},
            k=60,
            weights={"vector": 2.0, "keyword": 1.0},  # vector 权重更高
        )

        # a 在 vector 中排第一，应该得分更高
        assert fused["a"] > fused["b"]


class TestMMR:
    """MMR 多样化测试"""

    def test_mmr_select_diversifies(self):
        """测试 MMR 选择多样化"""
        # 两个近似文档 (a,b) 和一个不同的 (c)
        candidates = ["a", "b", "c"]
        relevance = {"a": 1.0, "b": 0.99, "c": 0.8}
        vectors = {
            "a": [1.0, 0.0],
            "b": [0.99, 0.1],  # 与 a 非常相似
            "c": [0.0, 1.0],  # 与 a/b 不同
        }

        selected, picks = mmr_select(
            candidates=candidates,
            relevance=relevance,
            vectors=vectors,
            lambda_mult=0.5,
            top_k=2,
        )

        # 第一个应该是最相关的 a
        assert selected[0] == "a"
        # 第二个应该是多样化的 c（而不是相似的 b）
        assert selected[1] == "c"

    def test_mmr_with_high_lambda(self):
        """测试高 lambda 值（更重相关性）"""
        candidates = ["a", "b", "c"]
        relevance = {"a": 1.0, "b": 0.99, "c": 0.5}
        vectors = {
            "a": [1.0, 0.0],
            "b": [0.99, 0.1],
            "c": [0.0, 1.0],
        }

        selected, picks = mmr_select(
            candidates=candidates,
            relevance=relevance,
            vectors=vectors,
            lambda_mult=0.9,  # 高 lambda，更重相关性
            top_k=2,
        )

        # 高 lambda 时应该选择相关性高的
        assert "a" in selected
        assert "b" in selected  # b 相关性高于 c
