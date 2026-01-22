# -*- coding: utf-8 -*-
"""
Comprehensive Image Sync Test

Tests the full image sync pipeline with various scenarios:
1. Download image from Confluence
2. Generate VLM description
3. Test filename sanitization for S3
4. Create text embedding
5. Store in Qdrant
6. Store in database with position handling
7. Search and verify retrieval
8. Test specific fee table queries
"""
import asyncio
import sys
import os
sys.stdout.reconfigure(encoding='utf-8')
os.chdir("C:/Projects/Agent_Gateway")
sys.path.insert(0, "C:/Projects/Agent_Gateway")

# Test data based on the fee table image
FEE_TABLE_TEST_QUERIES = [
    ("Legal fee 是多少", "550", "Legal Fee"),
    ("Application Fee for Personal use", "330", "Application Fee - Personal use"),
    ("Application Fee for Business use", "440", "Application Fee - Business use"),
    ("Establishment fee Personal Use", "650", "Establishment fee - Personal Use"),
    ("Establishment fee Business Use", "800", "Establishment fee - Business Use"),
    ("Administration Account keeping fee", "15.00", "Administration/Account keeping fee"),
    ("Discharge fee是多少", "50.00", "Discharge fee"),
    ("Early exit fee", "700", "Early exit fee"),
    ("Default fee是什么", "150", "Default fee"),
    ("Dishonour fee", "35", "Dishonour fee"),
    ("Verification and valuation fee", "70", "Verification and valuation"),
]


async def test_filename_sanitization():
    """Test 1: Filename sanitization for S3 metadata"""
    print("\n" + "="*60)
    print("TEST 1: Filename Sanitization")
    print("="*60)

    from src.services.storage.image_storage import _sanitize_for_s3_metadata

    test_cases = [
        # (input, expected_output_contains)
        ("Screenshot 2025-01-16 at 11.56.16\u202fam.png", "Screenshot 2025-01-16 at 11.56.16 am.png"),  # narrow no-break space
        ("normal_file.png", "normal_file.png"),
        ("文件名.png", ".png"),  # Chinese characters removed
        ("file\u00a0name.png", "file name.png"),  # non-breaking space
        ("", ""),  # empty string
    ]

    all_passed = True
    for input_str, expected in test_cases:
        result = _sanitize_for_s3_metadata(input_str)
        # Check if ASCII only
        is_ascii = all(ord(c) < 128 for c in result)
        passed = is_ascii and (expected in result or result == expected)
        status = "PASS" if passed else "FAIL"
        print(f"  [{status}] Input: {repr(input_str)}")
        print(f"         Output: {repr(result)}")
        if not passed:
            all_passed = False

    return all_passed


async def test_image_download():
    """Test 2: Image download from Confluence"""
    print("\n" + "="*60)
    print("TEST 2: Image Download from Confluence")
    print("="*60)

    from src.config.settings import Settings
    from src.persistence.database import DatabaseStorage
    from src.services.knowledge.confluence.client import ConfluenceClient, ConfluenceCredentials

    settings = Settings()
    db = DatabaseStorage(dsn=settings.database.dsn, enabled=True, auto_init=False)
    await db.connect()

    # Get connection
    conn = await db._pool.fetchrow(
        "SELECT domain, email, api_token FROM confluence_connections LIMIT 1"
    )
    credentials = ConfluenceCredentials(
        domain=conn["domain"],
        email=conn["email"],
        api_token=conn["api_token"]
    )
    client = ConfluenceClient(credentials)

    # Get attachments
    page_id = "449347589"
    attachments = await client.get_page_image_attachments(page_id=page_id, embeddable_only=True)
    print(f"  Found {len(attachments)} images")

    # Download first image
    if attachments:
        att = attachments[0]
        content = await client.download_attachment(att)
        print(f"  Downloaded: {att.filename} ({len(content)} bytes)")
        passed = len(content) > 0
    else:
        passed = False
        print("  FAIL: No attachments found")

    await client.close()
    await db.close()

    return passed, content if passed else None, att if passed else None


async def test_vlm_description(image_bytes):
    """Test 3: VLM description generation"""
    print("\n" + "="*60)
    print("TEST 3: VLM Description Generation")
    print("="*60)

    from src.config.settings import Settings
    from src.services.knowledge.vlm_service import DashScopeVLMService

    settings = Settings()
    vlm = DashScopeVLMService(
        api_key=settings.knowledge.dashscope.api_key,
        model="qwen-vl-max"
    )

    result = await vlm.describe_image(
        image_bytes=image_bytes,
        image_type="table",
        context="Auto Finance FAQs"
    )

    print(f"  Description length: {len(result.description)} characters")

    # Check for key fee values
    checks = [
        ("Legal Fee $550", "550" in result.description),
        ("Application Fee $330", "330" in result.description),
        ("Discharge fee $50", "50" in result.description),
    ]

    for name, found in checks:
        status = "PASS" if found else "FAIL"
        print(f"  [{status}] {name}")

    all_passed = all(c[1] for c in checks)
    return all_passed, result.description


async def test_embedding_and_storage(description):
    """Test 4: Embedding generation and Qdrant storage"""
    print("\n" + "="*60)
    print("TEST 4: Embedding and Vector Storage")
    print("="*60)

    from src.config.settings import Settings
    from src.services.knowledge.embedding import DashScopeEmbedding
    from src.services.knowledge.vector_store import VectorStore
    from qdrant_client import models as qmodels
    import uuid

    settings = Settings()

    # Create embedding
    embedder = DashScopeEmbedding(
        model="text-embedding-v4",
        api_key=settings.knowledge.dashscope.api_key,
    )
    vectors = await embedder.embed_documents([description])
    print(f"  Embedding dimension: {len(vectors[0])}")

    # Store in Qdrant
    vector_store = VectorStore(url=settings.knowledge.qdrant.url)
    segment_id = str(uuid.uuid4())

    payload = {
        "dataset_id": "test_dataset",
        "document_id": "test_doc",
        "segment_id": segment_id,
        "text": description,
        "content_type": "image",
        "image_filename": "fee_table_test.png",
    }

    await vector_store.upsert(
        collection_name="kb_kb_48cbe2b9f033_1024",
        points=[qmodels.PointStruct(id=segment_id, vector=vectors[0], payload=payload)],
    )
    print(f"  Stored vector: {segment_id}")

    await embedder.close()
    await vector_store.close()

    return True, embedder, segment_id


async def test_search_queries():
    """Test 5: Search queries for fee table values"""
    print("\n" + "="*60)
    print("TEST 5: Search Query Validation")
    print("="*60)

    from src.config.settings import Settings
    from src.services.knowledge.embedding import DashScopeEmbedding
    from src.services.knowledge.vector_store import VectorStore

    settings = Settings()

    embedder = DashScopeEmbedding(
        model="text-embedding-v4",
        api_key=settings.knowledge.dashscope.api_key,
    )
    vector_store = VectorStore(url=settings.knowledge.qdrant.url)

    results_summary = []

    for query, expected_value, fee_name in FEE_TABLE_TEST_QUERIES[:5]:  # Test first 5
        query_vec = await embedder.embed_query(query)
        results = await vector_store.search(
            collection_name="kb_kb_48cbe2b9f033_1024",
            query_vector=query_vec,
            top_k=3,
        )

        # Check if image content is in top results
        has_image_result = any(r.payload.get("content_type") == "image" for r in results)
        # Check if expected value is in results
        has_expected = any(expected_value in r.payload.get("text", "") for r in results)

        passed = has_image_result and has_expected
        status = "PASS" if passed else "FAIL"
        print(f"  [{status}] Query: {query}")
        print(f"         Top result score: {results[0].score:.4f}, type: {results[0].payload.get('content_type')}")

        results_summary.append((query, passed, results[0].score if results else 0))

    await embedder.close()
    await vector_store.close()

    all_passed = all(r[1] for r in results_summary)
    return all_passed, results_summary


async def test_database_segment_storage():
    """Test 6: Database segment storage with position"""
    print("\n" + "="*60)
    print("TEST 6: Database Segment Storage")
    print("="*60)

    from src.config.settings import Settings
    from src.persistence.database import DatabaseStorage

    settings = Settings()
    db = DatabaseStorage(dsn=settings.database.dsn, enabled=True, auto_init=False)
    await db.connect()

    # Check image segments
    segments = await db._pool.fetch("""
        SELECT segment_id, document_id, position, content_type, image_filename
        FROM segments
        WHERE content_type = 'image'
        ORDER BY position
        LIMIT 10
    """)

    print(f"  Total image segments: {len(segments)}")

    # Verify position offset
    positions_valid = True
    for s in segments:
        if s["position"] < 100000:
            positions_valid = False
            print(f"  WARN: Segment {s['segment_id']} has low position {s['position']}")
        else:
            print(f"  [OK] Segment pos={s['position']}: {s['image_filename']}")

    await db.close()

    return len(segments) > 0 and positions_valid


async def test_sync_service_image_processor():
    """Test 7: Sync service image processor creation"""
    print("\n" + "="*60)
    print("TEST 7: Sync Service Image Processor")
    print("="*60)

    from src.config.settings import Settings
    from src.persistence.database import DatabaseStorage
    from src.services.knowledge.confluence.sync_service import ConfluenceSyncService
    from src.services.knowledge.knowledge_service import KnowledgeService
    from src.services.knowledge.vlm_service import DashScopeVLMService
    from src.services.storage.image_storage import ImageStorageService, StorageConfig, StorageBackend

    settings = Settings()

    db = DatabaseStorage(dsn=settings.database.dsn, enabled=True, auto_init=False)
    await db.connect()

    storage_config = StorageConfig(
        backend=StorageBackend.S3,
        s3_bucket=settings.storage.s3.bucket,
        s3_region=settings.storage.s3.region,
        s3_access_key=settings.storage.s3.access_key,
        s3_secret_key=settings.storage.s3.secret_key,
    )
    storage_service = ImageStorageService(storage_config)

    vlm_service = DashScopeVLMService(
        api_key=settings.knowledge.dashscope.api_key,
        model="qwen-vl-max"
    )

    knowledge_service = KnowledgeService(
        settings=settings,
        database=db,
        multimodal_embedding=None,
        image_storage_service=storage_service,
    )

    sync_service = ConfluenceSyncService(
        settings=settings,
        database=db,
        knowledge_service=knowledge_service,
        knowledge_worker=None,
        image_storage_service=storage_service,
        multimodal_embedding=None,
        vlm_service=vlm_service,
    )

    # Get connection ID
    binding = await db.get_confluence_binding("4ba00182-a8e3-4042-b14d-ee1678821ed4")
    connection_id = binding["connection_id"]

    # Test image processor creation
    img_processor = await sync_service._get_image_processor(connection_id)

    if img_processor:
        print(f"  [PASS] Image processor created")
        print(f"         VLM enabled: {img_processor.vlm_service is not None}")
        print(f"         Storage: S3/OSS configured")
        passed = True
    else:
        print(f"  [FAIL] Image processor is None")
        passed = False

    await db.close()
    return passed


async def main():
    print("="*60)
    print("COMPREHENSIVE IMAGE SYNC TEST SUITE")
    print("="*60)

    test_results = {}

    # Test 1: Filename sanitization
    test_results["sanitization"] = await test_filename_sanitization()

    # Test 2: Image download
    passed, image_bytes, attachment = await test_image_download()
    test_results["download"] = passed

    if image_bytes:
        # Test 3: VLM description
        passed, description = await test_vlm_description(image_bytes)
        test_results["vlm"] = passed

        if description:
            # Test 4: Embedding and storage
            passed, _, _ = await test_embedding_and_storage(description)
            test_results["embedding"] = passed

    # Test 5: Search queries
    passed, _ = await test_search_queries()
    test_results["search"] = passed

    # Test 6: Database storage
    test_results["database"] = await test_database_segment_storage()

    # Test 7: Sync service
    test_results["sync_service"] = await test_sync_service_image_processor()

    # Summary
    print("\n" + "="*60)
    print("TEST SUMMARY")
    print("="*60)
    all_passed = True
    for name, result in test_results.items():
        status = "PASS" if result else "FAIL"
        if not result:
            all_passed = False
        print(f"  [{status}] {name}")

    print("\n" + "="*60)
    if all_passed:
        print("ALL TESTS PASSED!")
    else:
        print("SOME TESTS FAILED - Review output above")
    print("="*60)

    return all_passed


if __name__ == "__main__":
    asyncio.run(main())
