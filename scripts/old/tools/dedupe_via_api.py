#!/usr/bin/env python
"""
Script to deduplicate segments via API.
Finds duplicate segments and deletes them through the knowledge service API.

Usage:
    python scripts/dedupe_via_api.py <dataset_id> [--dry-run]
"""

import asyncio
import sys
from collections import defaultdict
import httpx

API_BASE = "http://localhost:8080"


async def dedupe_dataset(dataset_id: str, dry_run: bool = True):
    """Find and remove duplicate segments via API."""

    async with httpx.AsyncClient(timeout=120.0) as client:
        # Get dataset info
        resp = await client.get(f"{API_BASE}/api/v1/knowledge/datasets/{dataset_id}")
        if resp.status_code != 200:
            print(f"Dataset {dataset_id} not found: {resp.status_code}")
            return

        dataset = resp.json()
        print(f"Dataset: {dataset.get('name')} ({dataset_id})")
        print()

        # Collect segments via multiple searches
        print("Scanning for segments...")
        content_to_segments = defaultdict(list)

        queries = [
            'loan finance home property',
            'Sharia Islamic compliance',
            'fee cost price',
            'application process',
            'SMSF super',
            'construction building',
            'settlement',
            'car auto vehicle',
        ]

        for query in queries:
            resp = await client.post(f"{API_BASE}/api/v1/kb/search", json={
                'query': query,
                'dataset_id': dataset_id,
                'mode': 'bm25',
                'rerank': False,
                'top_k': 50,
                'score_threshold': 0.0,
            })

            if resp.status_code == 200:
                data = resp.json()
                for r in data.get('results', []):
                    content_hash = r.get('content', '')[:200]  # Use first 200 chars as hash
                    seg_id = r.get('segment_id')
                    doc_id = r.get('document_id')

                    # Check if this segment is already tracked
                    existing_ids = [s['segment_id'] for s in content_to_segments[content_hash]]
                    if seg_id not in existing_ids:
                        content_to_segments[content_hash].append({
                            'segment_id': seg_id,
                            'document_id': doc_id,
                            'content_preview': r.get('content', '')[:80]
                        })

        # Find duplicates
        duplicates = {k: v for k, v in content_to_segments.items() if len(v) > 1}

        total_segments = sum(len(v) for v in content_to_segments.values())
        print(f"Found {total_segments} segments ({len(content_to_segments)} unique content)")
        print(f"Duplicate groups: {len(duplicates)}")

        if not duplicates:
            print("\n✅ No duplicates found!")
            return

        # Calculate segments to delete (keep first, delete rest)
        segments_to_delete = []
        for content_hash, segments in duplicates.items():
            # Keep the first one, delete the rest
            to_delete = segments[1:]
            segments_to_delete.extend(to_delete)

        print(f"Segments to delete: {len(segments_to_delete)}")
        print()

        # Show examples
        print("=== Examples ===")
        for content_hash, segments in list(duplicates.items())[:3]:
            print(f"Content: {segments[0]['content_preview']}...")
            print(f"  Keep: {segments[0]['segment_id'][:12]}...")
            for s in segments[1:]:
                print(f"  Delete: {s['segment_id'][:12]}...")
            print()

        if dry_run:
            print("[DRY RUN] No changes made. Run without --dry-run to delete.")
            return

        # Delete duplicates
        print(f"\nDeleting {len(segments_to_delete)} duplicate segments...")

        deleted_count = 0
        errors = 0

        for i, seg in enumerate(segments_to_delete):
            try:
                resp = await client.delete(
                    f"{API_BASE}/api/v1/knowledge/{dataset_id}/segments/{seg['segment_id']}"
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get('status') == 'success':
                        deleted_count += 1
                    else:
                        errors += 1
                        if errors <= 3:
                            print(f"  Not found: {seg['segment_id']}")
                else:
                    errors += 1
                    if errors <= 3:
                        print(f"  Failed to delete {seg['segment_id']}: {resp.status_code}")

                # Progress indicator
                if (i + 1) % 10 == 0:
                    print(f"  Progress: {i + 1}/{len(segments_to_delete)}")

            except Exception as e:
                errors += 1
                if errors <= 3:
                    print(f"  Error deleting {seg['segment_id']}: {e}")

        print(f"\nDeleted: {deleted_count}")
        if errors:
            print(f"Errors: {errors}")

        print("\n✅ Deduplication complete!")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    dataset_id = sys.argv[1]
    dry_run = "--dry-run" in sys.argv

    asyncio.run(dedupe_dataset(dataset_id, dry_run))
