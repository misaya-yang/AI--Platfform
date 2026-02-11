"""Test sync service with image processing"""

import asyncio
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
os.chdir("C:/Projects/Agent_Gateway")
sys.path.insert(0, "C:/Projects/Agent_Gateway")


async def main():
    from src.config.settings import Settings
    from src.persistence.database import DatabaseStorage
    from src.services.knowledge.confluence.sync_service import ConfluenceSyncService
    from src.services.knowledge.embedding import DashScopeEmbedding
    from src.services.knowledge.vector_store import VectorStore
    from src.services.knowledge.vlm_service import DashScopeVLMService
    from src.services.storage.image_storage import (
        ImageStorageService,
        StorageBackend,
        StorageConfig,
    )

    settings = Settings()

    # Initialize database
    db = DatabaseStorage(dsn=settings.database.dsn, enabled=True, auto_init=False)
    await db.connect()

    # Initialize vector store
    vector_store = VectorStore(url=settings.knowledge.qdrant.url)

    # Initialize embedding service
    embedder = DashScopeEmbedding(
        model="text-embedding-v4",
        api_key=settings.knowledge.dashscope.api_key,
    )

    # Initialize VLM service
    vlm_service = DashScopeVLMService(
        api_key=settings.knowledge.dashscope.api_key, model="qwen-vl-max"
    )

    # Initialize storage service (S3)
    storage_config = StorageConfig(
        backend=StorageBackend.S3,
        s3_bucket=settings.storage.s3.bucket,
        s3_region=settings.storage.s3.region,
        s3_access_key=settings.storage.s3.access_key,
        s3_secret_key=settings.storage.s3.secret_key,
    )
    storage_service = ImageStorageService(storage_config)

    print("=== All services initialized ===")

    # Get the HFDSH binding
    bindings = await db.list_confluence_bindings()
    binding = None
    for b in bindings:
        if b["space_key"] == "HFDSH":
            binding = b
            break

    if not binding:
        print("HFDSH binding not found!")
        return

    print(f"Found binding: {binding['binding_id']}")
    print(f"  space_key: {binding['space_key']}")
    print(f"  sync_images: {binding.get('sync_images')}")
    print(f"  dataset_id: {binding['dataset_id']}")
    print(f"  tenant_id: {binding.get('tenant_id')}")

    # Initialize sync service with all dependencies
    sync_service = ConfluenceSyncService(
        db=db,
        vector_store=vector_store,
        embedding=embedder,
        vlm_service=vlm_service,
        image_storage_service=storage_service,
    )

    print("\n=== Starting sync for specific page ===")

    # Get connection
    connections = await db.list_confluence_connections()
    for c in connections:
        if c["connection_id"] == binding["connection_id"]:
            break

    # Sync a specific page (Auto Finance FAQs)
    page_id = "449347589"

    try:
        result = await sync_service.sync_page(
            connection_id=binding["connection_id"],
            page_id=page_id,
            dataset_id=binding["dataset_id"],
            binding_id=binding["binding_id"],
            sync_images=True,  # Enable image sync!
            tenant_id=binding.get("tenant_id"),
        )

        print("\n=== Sync Result ===")
        print(f"Status: {result.get('status')}")
        print(f"Document ID: {result.get('document_id')}")
        print(f"Segment count: {result.get('segment_count')}")
        print(f"Image count: {result.get('image_count', 0)}")

        if result.get("error"):
            print(f"Error: {result.get('error')}")

    except Exception as e:
        print(f"Sync failed: {e}")
        import traceback

        traceback.print_exc()

    # Check image segments in database
    print("\n=== Checking image segments ===")
    img_segments = await db._pool.fetch("""
        SELECT segment_id, image_filename, content_type, LEFT(text, 100) as preview
        FROM segments
        WHERE content_type = 'image'
        ORDER BY created_at DESC
        LIMIT 5
    """)

    print(f"Image segments found: {len(img_segments)}")
    for s in img_segments:
        print(f"  - {s['image_filename']}: {s['preview'][:50]}...")

    # Cleanup
    await embedder.close()
    await vector_store.close()
    await db.close()
    print("\n=== Test Complete ===")


if __name__ == "__main__":
    asyncio.run(main())
