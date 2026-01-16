#!/usr/bin/env python
"""
Script to deduplicate segments in knowledge bases.

Usage:
    python scripts/dedupe_segments.py <dataset_id> [--dry-run]

Example:
    python scripts/dedupe_segments.py kb_4fb7649a86bd --dry-run
    python scripts/dedupe_segments.py kb_4fb7649a86bd
"""

import asyncio
import sys
from collections import defaultdict

# Add parent to path
sys.path.insert(0, ".")

async def dedupe_dataset(dataset_id: str, dry_run: bool = True):
    """Remove duplicate segments from a dataset."""
    from src.persistence.database import DatabaseStorage
    from src.services.knowledge.vector_store import VectorStore
    from src.config.settings import Settings

    settings = Settings()
    db = DatabaseStorage(settings)
    await db.connect()

    # Get Qdrant config from settings
    qdrant_url = settings.knowledge.qdrant.url
    qdrant_api_key = getattr(settings.knowledge.qdrant, 'api_key', None)
    vector_store = VectorStore(url=qdrant_url, api_key=qdrant_api_key)

    try:
        # Get dataset info
        dataset = await db.get_dataset(dataset_id)
        if not dataset:
            print(f"Dataset {dataset_id} not found")
            return

        collection_name = dataset.get("collection_name")
        print(f"Dataset: {dataset.get('name')} ({dataset_id})")
        print(f"Collection: {collection_name}")
        print()

        # Get all segments
        async with db._pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT id, document_id, content_hash, created_at, content
                FROM knowledge_segments
                WHERE dataset_id = $1
                ORDER BY content_hash, created_at ASC
            """, dataset_id)

        print(f"Total segments: {len(rows)}")

        # Group by content_hash
        hash_to_segments = defaultdict(list)
        for row in rows:
            hash_to_segments[row['content_hash']].append(row)

        # Find duplicates
        duplicates_to_delete = []
        for content_hash, segments in hash_to_segments.items():
            if len(segments) > 1:
                # Keep the oldest one, delete the rest
                segments.sort(key=lambda x: x['created_at'])
                keep = segments[0]
                to_delete = segments[1:]
                duplicates_to_delete.extend(to_delete)

                if len(duplicates_to_delete) <= 10:  # Print first 10
                    print(f"\nDuplicate group (keeping {keep['id'][:8]}...):")
                    for d in to_delete:
                        print(f"  Delete: {d['id'][:8]}... ({d['content'][:60]}...)")

        print(f"\n{'='*60}")
        print(f"Unique content: {len(hash_to_segments)}")
        print(f"Duplicates to delete: {len(duplicates_to_delete)}")
        print(f"{'='*60}")

        if dry_run:
            print("\n[DRY RUN] No changes made. Run without --dry-run to delete.")
            return

        if not duplicates_to_delete:
            print("\nNo duplicates found.")
            return

        # Delete duplicates
        print(f"\nDeleting {len(duplicates_to_delete)} duplicate segments...")

        segment_ids = [row['id'] for row in duplicates_to_delete]

        # Delete from database
        async with db._pool.acquire() as conn:
            deleted = await conn.execute("""
                DELETE FROM knowledge_segments
                WHERE id = ANY($1)
            """, segment_ids)
            print(f"Deleted from database: {deleted}")

        # Delete from vector store
        if collection_name:
            try:
                await vector_store.delete_points(collection_name, segment_ids)
                print(f"Deleted from vector store: {len(segment_ids)} points")
            except Exception as e:
                print(f"Warning: Failed to delete from vector store: {e}")

        print("\n✅ Deduplication complete!")

    finally:
        await db.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    dataset_id = sys.argv[1]
    dry_run = "--dry-run" in sys.argv

    asyncio.run(dedupe_dataset(dataset_id, dry_run))
