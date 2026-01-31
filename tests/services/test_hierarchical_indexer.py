import pytest

from src.services.knowledge.hierarchical_indexer import HierarchicalIndexer, IndexLevel


class StubEmbedder:
    def __init__(self):
        self.dimension = 3

    async def embed_documents(self, texts):
        return [[0.1, 0.2, 0.3] for _ in texts]


class StubVectorStore:
    def __init__(self):
        self.collections = []
        self.upserts = []

    async def ensure_collection(self, dataset_id, dimension, collection_name=None, distance="cosine"):
        self.collections.append((dataset_id, dimension, collection_name))
        return collection_name or f"kb_{dataset_id}_{dimension}"

    async def upsert(self, collection_name, points):
        self.upserts.append((collection_name, points))


class StubDb:
    def __init__(self):
        self.inserted = []

    async def get_dataset(self, dataset_id):
        return {"dataset_id": dataset_id, "collection_name": None}

    async def insert_segments(self, segments):
        self.inserted.extend(segments)

    async def save_document_summary(self, data):
        return True


@pytest.mark.asyncio
async def test_indexer_sets_parent_segment_ids():
    text = "# Title\n\nSection one content.\n\n## Sub\n\nMore text."

    db = StubDb()
    indexer = HierarchicalIndexer(
        vector_store=StubVectorStore(),
        database=db,
        embedder=StubEmbedder(),
        summary_generator=None,
    )

    result = await indexer.index_document("doc1", "ds1", text)

    assert result.l2_count >= 1
    assert result.l3_count >= 1

    parents = {s["segment_id"] for s in db.inserted if s.get("level") == IndexLevel.SECTION}
    children = [s for s in db.inserted if s.get("level") == IndexLevel.PARAGRAPH]
    assert children[0]["parent_segment_id"] in parents
