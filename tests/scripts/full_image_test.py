# -*- coding: utf-8 -*-
"""Full end-to-end image sync test"""
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
    from src.services.knowledge.vlm_service import DashScopeVLMService

    settings = Settings()
    db = DatabaseStorage(dsn=settings.database.dsn, enabled=True, auto_init=False)
    await db.connect()

    # Setup
    connections = await db.list_confluence_connections()
    conn = None
    for c in connections:
        bindings = await db.list_confluence_bindings(connection_id=c["connection_id"])
        for b in bindings:
            if b["space_key"] == "HFDSH":
                conn = c
                break
        if conn:
            break

    credentials = ConfluenceCredentials(
        domain=conn["domain"], email=conn["email"], api_token=conn["api_token"]
    )
    client = ConfluenceClient(credentials)

    # Get attachments
    page_id = "449347589"
    attachments = await client.get_page_image_attachments(page_id=page_id, embeddable_only=True)
    print(f"=== {len(attachments)} images found ===\n")

    # Process first image (fee table)
    att = attachments[0]
    print(f"Processing: {att.filename}")
    content = await client.download_attachment(att)
    print(f"Downloaded: {len(content)} bytes")

    # Generate VLM description
    vlm = DashScopeVLMService(api_key=settings.knowledge.dashscope.api_key, model="qwen-vl-max")
    result = await vlm.describe_image(image_bytes=content, image_type="table", context="Auto Finance FAQs")

    # Check if "Legal" is mentioned
    if "legal" in result.description.lower() or "550" in result.description:
        print("\n*** Legal Fee ($550) FOUND in VLM description! ***")
    else:
        print("\n*** WARNING: Legal Fee NOT found in description! ***")

    # Now test embedding and vector storage
    print("\n=== Testing embedding and vector storage ===")

    # Get dataset
    datasets = await db._pool.fetch("SELECT * FROM datasets WHERE name LIKE '%Sales%' LIMIT 1")
    if not datasets:
        print("No Sales dataset found!")
        await client.close()
        await db.close()
        return

    dataset = dict(datasets[0])
    print(f"Dataset: {dataset['name']}, collection={dataset['collection_name']}")

    # Create embedding using DashScope text embedding
    from src.services.knowledge.embedding import DashScopeEmbedding
    embedder = DashScopeEmbedding(
        model=dataset["embedding_model"],
        api_key=settings.knowledge.dashscope.api_key,
    )

    # Embed the VLM description
    vectors = await embedder.embed_documents([result.description])
    print(f"Generated embedding: dimension={len(vectors[0])}")

    # Store in Qdrant
    from src.services.knowledge.vector_store import VectorStore
    from qdrant_client import models as qmodels

    import uuid
    vector_store = VectorStore(url=settings.knowledge.qdrant.url)
    segment_id = str(uuid.uuid4())

    payload = {
        "dataset_id": dataset["dataset_id"],
        "document_id": "test-doc-001",
        "segment_id": segment_id,
        "text": result.description,
        "content_type": "image",
        "image_filename": att.filename,
    }

    await vector_store.upsert(
        collection_name=dataset["collection_name"],
        points=[qmodels.PointStruct(id=segment_id, vector=vectors[0], payload=payload)],
    )
    print(f"Stored vector in Qdrant: {segment_id}")

    # Now test search
    print("\n=== Testing search for 'legal fee' ===")
    query_vec = await embedder.embed_query("legal fee 是多少")

    search_results = await vector_store.search(
        collection_name=dataset["collection_name"],
        query_vector=query_vec,
        top_k=5,
    )

    print(f"Search results: {len(search_results)}")
    for i, r in enumerate(search_results):
        print(f"\n  [{i+1}] Score: {r.score:.4f}")
        print(f"      content_type: {r.payload.get('content_type')}")
        text = r.payload.get("text", "")[:300]
        print(f"      Text preview: {text}...")

    # Cleanup
    await embedder.close()
    await vector_store.close()
    await client.close()
    await db.close()
    print("\n=== Test Complete! ===")

if __name__ == "__main__":
    asyncio.run(main())
