import asyncio
import os
import sys

sys.path.append(os.getcwd())


# Mock objects to simulate dependencies without running full server
class MockUser:
    user_id = "test_user"
    tenant_id = "test_tenant"
    roles = ["knowledge:manage"]
    tier = "standard"
    is_authenticated = True


class MockDatabase:
    async def get_dataset(self, dataset_id):
        return {
            "dataset_id": dataset_id,
            "tenant_id": "test_tenant",
            "index_config": {
                "chunking": {"mode": "automatic", "chunk_size": 100, "chunk_overlap": 10}
            },
        }

    async def get_dataset_permission(self, *args):
        return {"permission": "owner"}


class MockSettings:
    class KnowledgeSettings:
        enabled = True
        qdrant = type(
            "Qdrant",
            (),
            {
                "enabled": True,
                "url": "http://localhost:6333",
                "api_key": None,
                "timeout_seconds": 10,
                "prefer_grpc": False,
            },
        )()

    knowledge = KnowledgeSettings()


async def test_chunking_preview():
    # Import locally to avoid import errors if env not perfect
    try:
        # We need to mock VectorStore since we don't want real Qdrant connection
        from unittest.mock import MagicMock

        # Patch VectorStore
        import src.services.knowledge.knowledge_service
        from src.services.knowledge.knowledge_service import KnowledgeService

        src.services.knowledge.knowledge_service.VectorStore = MagicMock()

        svc = KnowledgeService(MockSettings(), MockDatabase())

        # Test Case 1: Automatic with markdown
        text = "# H1\n\nSection 1 content.\n\n## H2\n\nSection 2 content."
        print(f"Testing text: {text!r}")

        chunks = await svc.preview_chunking(
            MockUser(),
            "test_ds",
            text,
            config={"mode": "automatic", "chunk_size": 50, "chunk_overlap": 0},
        )

        print(f"\nResult chunks: {len(chunks)}")
        for i, c in enumerate(chunks):
            print(f"[{i}] {c['content']!r} (meta: {c['metadata']})")

        if len(chunks) == 0:
            print("ERROR: No chunks returned. Check process_document logic.")

        # Assertions
        assert len(chunks) > 0, "No chunks returned for header test"
        # Should detect headers if our robust chunker works

        # Test Case 2: Recursive fallback
        long_text = "A" * 60  # > 50 chunk size
        chunks_recursive = await svc.preview_chunking(
            MockUser(),
            "test_ds",
            long_text,
            config={"mode": "recursive", "chunk_size": 50, "chunk_overlap": 0},
        )
        print(f"\nRecursive check (len(A)*60, limit=50): {len(chunks_recursive)} chunks")
        assert len(chunks_recursive) == 2

        print("\n✅ Verification PASSED")

    except Exception as e:
        print(f"\n❌ Verification FAILED: {e}")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(test_chunking_preview())
