"""Test image processing through the sync service directly"""

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
    from knowledge_service.services.knowledge.knowledge_service import KnowledgeService
    from src.services.knowledge.vlm_service import DashScopeVLMService
    from src.services.storage.image_storage import (
        ImageStorageService,
        StorageBackend,
        StorageConfig,
    )

    settings = Settings()

    print("=== 1. Initializing components ===")

    # Database
    db = DatabaseStorage(dsn=settings.database.dsn, enabled=True, auto_init=False)
    await db.connect()
    print("  Database connected")

    # Storage service
    storage_config = StorageConfig(
        backend=StorageBackend.S3,
        s3_bucket=settings.storage.s3.bucket,
        s3_region=settings.storage.s3.region,
        s3_access_key=settings.storage.s3.access_key,
        s3_secret_key=settings.storage.s3.secret_key,
    )
    storage_service = ImageStorageService(storage_config)
    print("  Storage service created")

    # VLM service
    vlm_service = DashScopeVLMService(
        api_key=settings.knowledge.dashscope.api_key, model="qwen-vl-max"
    )
    print("  VLM service created")

    # Knowledge service (needed by sync service)
    knowledge_service = KnowledgeService(
        settings=settings,
        database=db,
        multimodal_embedding=None,
        image_storage_service=storage_service,
    )
    print("  Knowledge service created")

    # Sync service
    sync_service = ConfluenceSyncService(
        settings=settings,
        database=db,
        knowledge_service=knowledge_service,
        knowledge_worker=None,
        image_storage_service=storage_service,
        multimodal_embedding=None,
        vlm_service=vlm_service,
    )
    print("  Sync service created")

    # Get binding info
    binding = await db.get_confluence_binding("4ba00182-a8e3-4042-b14d-ee1678821ed4")
    print(f"\n=== 2. Binding: {binding['space_key']} ===")
    print(f"  sync_images: {binding.get('sync_images')}")
    print(f"  tenant_id: {binding.get('tenant_id')}")

    # Get document
    document_id = "4069a9d6-0ae8-489b-be13-b8ece03c9d66"
    page_id = "449347589"
    connection_id = binding["connection_id"]
    dataset_id = binding["dataset_id"]

    # Get page from Confluence
    print(f"\n=== 3. Getting page {page_id} ===")
    client = await sync_service._get_client(connection_id)
    page = await client.get_page(page_id)
    print(f"  Title: {page.title}")

    # Test _get_image_processor
    print("\n=== 4. Getting image processor ===")
    img_processor = await sync_service._get_image_processor(connection_id)
    if img_processor:
        print("  Image processor created successfully!")
        print(f"    VLM enabled: {img_processor.vlm_service is not None}")
    else:
        print("  ERROR: Image processor is None!")
        print(f"    _image_storage_service: {sync_service._image_storage_service}")
        print(f"    _vlm_service: {sync_service._vlm_service}")
        await db.close()
        return

    # Call _reprocess_document_images directly
    print("\n=== 5. Calling _reprocess_document_images ===")
    try:
        count = await sync_service._reprocess_document_images(
            document_id=document_id,
            connection_id=connection_id,
            page=page,
            dataset_id=dataset_id,
            tenant_id=binding.get("tenant_id", ""),
            binding_id=binding["binding_id"],
        )
        print(f"  Processed {count} images")
    except Exception as e:
        print(f"  Error: {e}")
        import traceback

        traceback.print_exc()

    # Check database for image segments
    print("\n=== 6. Checking database ===")
    segments = await db._pool.fetch(
        """
        SELECT segment_id, image_filename, LEFT(text, 100) as preview
        FROM segments WHERE content_type = 'image' AND document_id = $1
    """,
        document_id,
    )
    print(f"  Image segments in DB: {len(segments)}")
    for s in segments:
        print(f"    - {s['image_filename']}")

    await db.close()
    print("\n=== Done ===")


if __name__ == "__main__":
    asyncio.run(main())
