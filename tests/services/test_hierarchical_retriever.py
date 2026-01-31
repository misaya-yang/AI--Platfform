import pytest
from qdrant_client.http import models as qmodels

from src.services.knowledge.hierarchical_retriever import HierarchicalRetriever
from src.services.knowledge.vector_store import VectorSearchHit


class StubEmbedder:
    async def embed_documents(self, texts):
        return [[0.1, 0.2, 0.3] for _ in texts]


class StubVectorStore:
    def __init__(self):
        self.calls = []

    async def search(self, collection_name, query_vector, top_k=5, query_filter=None, score_threshold=None, **kwargs):
        self.calls.append({"collection": collection_name, "query_filter": query_filter, "top_k": top_k})
        if collection_name.endswith("_summary"):
            return [VectorSearchHit(point_id="sum1", score=0.9, payload={
                "document_id": "doc1",
                "summary": "doc summary",
                "text": "doc summary",
                "level": 1,
            })]
        if collection_name.endswith("_sections"):
            return [VectorSearchHit(point_id="sec1", score=0.8, payload={
                "document_id": "doc1",
                "segment_id": "sec1",
                "text": "section text",
                "level": 2,
            })]
        return [VectorSearchHit(point_id="seg1", score=0.7, payload={
            "document_id": "doc1",
            "segment_id": "seg1",
            "text": "para",
            "level": 3,
            "parent_segment_id": "sec1",
        })]


class StubDb:
    async def get_document_summary(self, doc_id):
        return {"summary": "doc summary"}

    async def get_segment(self, seg_id):
        if seg_id == "sec1":
            return {"text": "section text"}
        return None


@pytest.mark.asyncio
async def test_retriever_uses_wrapper_and_enriches_context():
    retriever = HierarchicalRetriever(StubVectorStore(), StubEmbedder(), StubDb())

    results, meta = await retriever.retrieve(
        query="test",
        dataset_id="ds",
        top_k=1,
        include_context=True,
        base_collection="kb_ds_3",
    )

    assert results[0].segment_id == "seg1"
    assert results[0].document_summary == "doc summary"
    assert results[0].parent_context == "section text"

    l3_call = [c for c in retriever.vector_store.calls if c["collection"] == "kb_ds_3"][0]
    assert l3_call["query_filter"] is not None
