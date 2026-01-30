# -*- coding: utf-8 -*-
"""Directly trigger image reprocessing for a page"""
import asyncio
import sys
import os
sys.stdout.reconfigure(encoding='utf-8')
os.chdir("C:/Projects/Agent_Gateway")
sys.path.insert(0, "C:/Projects/Agent_Gateway")

async def main():
    from src.config.settings import Settings
    from src.persistence.database import DatabaseStorage
    from src.services.knowledge.confluence.client import ConfluenceClient, ConfluenceCredentials
    from src.services.knowledge.confluence.image_processor import ConfluenceImageProcessor
    from src.services.knowledge.vector_store import VectorStore
    from src.services.knowledge.embedding import DashScopeEmbedding
    from src.services.knowledge.vlm_service import DashScopeVLMService
    from src.services.storage.image_storage import ImageStorageService, StorageConfig, StorageBackend

    settings = Settings()

    # Initialize all services
    print("=== Initializing services ===")

    db = DatabaseStorage(dsn=settings.database.dsn, enabled=True, auto_init=False)
    await db.connect()

    vector_store = VectorStore(url=settings.knowledge.qdrant.url)

    embedder = DashScopeEmbedding(
        model="text-embedding-v4",
        api_key=settings.knowledge.dashscope.api_key,
    )

    vlm_service = DashScopeVLMService(
        api_key=settings.knowledge.dashscope.api_key,
        model="qwen-vl-max"
    )

    storage_config = StorageConfig(
        backend=StorageBackend.S3,
        s3_bucket=settings.storage.s3.bucket,
        s3_region=settings.storage.s3.region,
        s3_access_key=settings.storage.s3.access_key,
        s3_secret_key=settings.storage.s3.secret_key,
    )
    storage_service = ImageStorageService(storage_config)

    # Get connection credentials
    conn = await db._pool.fetchrow(
        "SELECT domain, email, api_token FROM confluence_connections WHERE connection_id = $1",
        "02e0f11a-1acd-4ad9-9c7a-f580b9f0d93b"
    )
    credentials = ConfluenceCredentials(
        domain=conn["domain"],
        email=conn["email"],
        api_token=conn["api_token"]
    )
    confluence_client = ConfluenceClient(credentials)

    # Create image processor (using VLM+text embedding approach)
    image_processor = ConfluenceImageProcessor(
        confluence_client=confluence_client,
        storage_service=storage_service,
        vlm_service=vlm_service,
        multimodal_embedding=None,  # Not using multimodal, we embed VLM text separately
    )

    print("All services initialized!")

    # Page info
    page_id = "449347589"
    document_id = "4069a9d6-0ae8-489b-be13-b8ece03c9d66"
    dataset_id = "kb_48cbe2b9f033"
    tenant_id = "default"

    # Get page content
    print(f"\n=== Getting page {page_id} ===")
    page = await confluence_client.get_page(page_id)
    print(f"Title: {page.title}")
    print(f"Version: {page.version}")

    # Get dataset info for collection name
    dataset = await db._pool.fetchrow(
        "SELECT collection_name FROM datasets WHERE dataset_id = $1",
        dataset_id
    )
    collection_name = dataset["collection_name"]
    print(f"Collection: {collection_name}")

    # Process images
    print("\n=== Processing images ===")
    result = await image_processor.process_page_images(
        page_id=page_id,
        document_id=document_id,
        tenant_id=tenant_id,
        page_content=page.body_storage,
        page_title=page.title,
        generate_embeddings=False,  # We'll do text embedding separately
    )

    print(f"\n=== Processing Result ===")
    print(f"Processed images: {result.processed_images}")
    print(f"Segments: {len(result.segments)}")
    print(f"Errors: {result.errors}")

    # Save segments to database and embed VLM descriptions
    if result.segments:
        print(f"\n=== Saving {len(result.segments)} image segments ===")
        from qdrant_client import models as qmodels

        for segment in result.segments:
            print(f"\nProcessing: {segment.filename}")
            print(f"  Storage URL: {segment.storage_url}")

            # Generate text embedding from VLM description
            if segment.vlm_description:
                print(f"  VLM description length: {len(segment.vlm_description)}")
                vectors = await embedder.embed_documents([segment.vlm_description])
                embedding = vectors[0]
                print(f"  Embedding dimension: {len(embedding)}")

                # Store vector in Qdrant
                payload = {
                    "dataset_id": dataset_id,
                    "document_id": document_id,
                    "segment_id": segment.segment_id,
                    "text": segment.vlm_description,
                    "content_type": "image",
                    "image_filename": segment.filename,
                    "image_url": segment.storage_url,
                }
                await vector_store.upsert(
                    collection_name=collection_name,
                    points=[qmodels.PointStruct(
                        id=segment.segment_id,
                        vector=embedding,
                        payload=payload
                    )],
                )
                print(f"  Stored vector in Qdrant")
            else:
                print(f"  WARNING: No VLM description!")

            # Save to database
            segment_data = {
                "segment_id": segment.segment_id,
                "dataset_id": dataset_id,
                "document_id": document_id,
                "position": 0,
                "text": segment.vlm_description or "",
                "content_type": "image",
                "image_url": segment.storage_url,
                "image_attachment_id": segment.attachment_id,
                "image_filename": segment.filename,
                "image_media_type": segment.media_type,
                "image_file_size": segment.file_size,
            }
            await db.save_image_segment(segment_data)
            print(f"  Saved to database")

    # Verify
    print("\n=== Verifying database ===")
    count = await db._pool.fetchval(
        "SELECT COUNT(*) FROM segments WHERE content_type = 'image' AND document_id = $1",
        document_id
    )
    print(f"Image segments for document: {count}")

    # Cleanup
    await confluence_client.close()
    await embedder.close()
    await vector_store.close()
    await db.close()
    print("\n=== Done ===")

if __name__ == "__main__":
    asyncio.run(main())
