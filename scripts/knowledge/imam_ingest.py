#!/usr/bin/env python3
"""
Imam knowledge base batch ingest (manifest-driven).

Usage:
  python scripts/knowledge/imam_ingest.py \
    --dataset imam_v2 \
    --manifest data/knowledge/imam/source_manifest.yaml

Environment variables:
  GATEWAY_BASE_URL (default: http://localhost:8080)
  GATEWAY_API_KEY (required unless --api-key provided)
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx


ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md", ".html"}
DEFAULT_BASE_URL = os.getenv("GATEWAY_BASE_URL", "http://localhost:8080")
DEFAULT_API_KEY = os.getenv("GATEWAY_API_KEY", "")
DEFAULT_BEARER_TOKEN = os.getenv("GATEWAY_BEARER_TOKEN", "")


@dataclass
class ManifestEntry:
    file_path: str
    metadata: Dict[str, Any]
    title: Optional[str] = None


def _load_yaml(path: Path) -> Dict[str, Any]:
    try:
        import yaml  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "PyYAML is required. Install with: pip install pyyaml"
        ) from exc

    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ValueError("Manifest must be a YAML mapping")
    return data


def _expand_paths(base_dir: Path, file_path: str) -> List[Path]:
    raw = Path(file_path)
    if not raw.is_absolute():
        raw = (base_dir / raw).expanduser()
    raw_str = str(raw)

    if any(ch in raw_str for ch in "*?[]"):
        matches = [Path(p) for p in glob.glob(raw_str)]
    elif raw.is_dir():
        matches = [p for p in raw.rglob("*") if p.suffix.lower() in ALLOWED_EXTENSIONS]
    else:
        matches = [raw]

    return [p for p in matches if p.exists() and p.is_file()]


def _normalize_metadata(entry: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[str]]:
    metadata = {}
    title = None
    for key, value in entry.items():
        if key == "file_path":
            continue
        if key == "title":
            title = str(value) if value is not None else None
            continue
        if key == "metadata" and isinstance(value, dict):
            metadata.update(value)
            continue
        metadata[key] = value
    return metadata, title


def _load_manifest(path: Path) -> Tuple[Path, List[ManifestEntry]]:
    data = _load_yaml(path)
    base_dir_raw = data.get("base_dir") or "."
    base_dir = Path(str(base_dir_raw)).expanduser()

    entries: List[ManifestEntry] = []
    for item in data.get("documents", []) or []:
        if not isinstance(item, dict):
            continue
        file_path = item.get("file_path")
        if not file_path:
            continue
        metadata, title = _normalize_metadata(item)
        entries.append(ManifestEntry(file_path=str(file_path), metadata=metadata, title=title))

    return base_dir, entries


class ImamIngestor:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        dataset_id: str,
        processing_mode: str,
        bearer_token: str = "",
        timeout: float = 300.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.dataset_id = dataset_id
        self.processing_mode = processing_mode
        headers = {"Accept": "application/json"}
        if bearer_token:
            headers["Authorization"] = f"Bearer {bearer_token}"
        else:
            headers["X-API-Key"] = api_key
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=timeout,
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def resolve_dataset_id(self, dataset_ref: str) -> str:
        """Resolve dataset by id or by exact name."""
        if dataset_ref.startswith("kb_"):
            return dataset_ref

        candidates = [
            f"/api/v1/knowledge/datasets/{dataset_ref}",
            f"/api/v1/kb/datasets/{dataset_ref}",
        ]
        for endpoint in candidates:
            resp = await self.client.get(endpoint)
            if resp.status_code == 200:
                data = resp.json() or {}
                resolved = str(data.get("dataset_id") or "")
                if resolved:
                    return resolved

        list_endpoints = ["/api/v1/kb/datasets", "/api/v1/knowledge/datasets"]
        for endpoint in list_endpoints:
            resp = await self.client.get(endpoint)
            if resp.status_code != 200:
                continue
            payload = resp.json() or []
            items = []
            if isinstance(payload, list):
                items = payload
            elif isinstance(payload, dict):
                data = payload.get("datasets")
                if isinstance(data, list):
                    items = data
            for item in items:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "")
                if name == dataset_ref:
                    resolved = str(item.get("dataset_id") or "")
                    if resolved:
                        return resolved
        raise RuntimeError(f"Dataset not found: {dataset_ref}. Please use a valid dataset id (kb_...) or exact name.")

    async def upload_document(self, path: Path) -> Optional[str]:
        start_time = time.time()
        try:
            with path.open("rb") as f:
                files = {"file": (path.name, f, "application/octet-stream")}
                data = {"processing_mode": self.processing_mode}
                resp = await self.client.post(
                    f"/api/v1/knowledge/{self.dataset_id}/documents/upload",
                    files=files,
                    data=data,
                )
        except httpx.ReadTimeout:
            print(f"⏱️  Upload timeout: {path.name}")
            return None

        if resp.status_code != 200:
            print(f"❌ Upload failed: {path.name} ({resp.status_code}) {resp.text[:200]}")
            return None

        doc = resp.json()
        doc_id = doc.get("document_id")
        if not doc_id:
            print(f"❌ Upload response missing document_id for {path.name}")
            return None
        elapsed = time.time() - start_time
        print(f"✅ Uploaded {path.name} -> {doc_id[:8]}... ({elapsed:.1f}s)")
        return doc_id

    async def patch_metadata(self, document_id: str, metadata: Dict[str, Any], title: Optional[str]) -> bool:
        if not metadata and not title:
            return True

        # Merge with existing metadata to avoid wiping system fields
        existing_meta: Dict[str, Any] = {}
        if metadata:
            resp = await self.client.get(
                f"/api/v1/knowledge/{self.dataset_id}/documents/{document_id}"
            )
            if resp.status_code == 200:
                doc = resp.json() or {}
                existing_meta = doc.get("metadata") or {}
            else:
                print(
                    f"⚠️  Failed to fetch document for metadata merge {document_id[:8]}: "
                    f"{resp.status_code} {resp.text[:200]}"
                )

        merged_meta = {**existing_meta, **(metadata or {})}

        payload: Dict[str, Any] = {"metadata": merged_meta}
        if title:
            payload["title"] = title
        resp = await self.client.patch(
            f"/api/v1/knowledge/{self.dataset_id}/documents/{document_id}",
            json=payload,
        )
        if resp.status_code != 200:
            print(f"⚠️  Metadata patch failed for {document_id[:8]}: {resp.status_code} {resp.text[:200]}")
            return False
        return True

    async def wait_for_document(self, document_id: str, max_wait: int = 900) -> bool:
        start = time.time()
        while time.time() - start < max_wait:
            resp = await self.client.get(
                f"/api/v1/knowledge/{self.dataset_id}/documents/{document_id}"
            )
            if resp.status_code != 200:
                await asyncio.sleep(3)
                continue
            data = resp.json()
            status = data.get("status")
            if status == "completed":
                return True
            if status == "failed":
                print(f"❌ Processing failed for {document_id[:8]}: {data.get('error')}")
                return False
            await asyncio.sleep(3)
        print(f"⏱️  Timeout waiting for {document_id[:8]}")
        return False

    async def list_document_titles(self) -> List[str]:
        resp = await self.client.get(f"/api/v1/knowledge/{self.dataset_id}/documents")
        if resp.status_code != 200:
            print(f"⚠️  Failed to list documents: {resp.status_code} {resp.text[:200]}")
            return []
        data = resp.json()
        titles = []
        for doc in data or []:
            title = doc.get("title")
            if title:
                titles.append(title)
                continue
            meta = doc.get("metadata") or {}
            original = meta.get("original_filename")
            if original:
                titles.append(original)
        return titles


async def main() -> int:
    parser = argparse.ArgumentParser(description="Imam manifest-driven ingestion")
    parser.add_argument("--dataset", default="imam_v2", help="Dataset id (kb_...) or exact dataset name")
    parser.add_argument("--manifest", default="data/knowledge/imam/source_manifest.yaml")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--api-key", default=DEFAULT_API_KEY)
    parser.add_argument("--bearer-token", default=DEFAULT_BEARER_TOKEN)
    parser.add_argument("--processing-mode", default="auto", choices=["auto", "text_only", "scanned", "multimodal"])
    parser.add_argument("--timeout", type=float, default=300.0, help="HTTP timeout in seconds")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--wait", action="store_true", help="wait for ingestion completion")
    parser.add_argument("--dry-run", action="store_true", help="print files without uploading")
    parser.add_argument("--skip-existing", action="store_true", default=True, help="skip files already uploaded")
    args = parser.parse_args()

    if not args.api_key and not args.bearer_token:
        print("❌ Missing credentials. Set GATEWAY_API_KEY or GATEWAY_BEARER_TOKEN (or pass flags).")
        return 1

    manifest_path = Path(args.manifest)
    if not manifest_path.exists():
        print(f"❌ Manifest not found: {manifest_path}")
        return 1

    base_dir, entries = _load_manifest(manifest_path)
    if not entries:
        print("⚠️  Manifest has no documents. Add entries under 'documents:'")
        return 1

    resolved: List[Tuple[Path, Dict[str, Any], Optional[str]]] = []
    for entry in entries:
        paths = _expand_paths(base_dir, entry.file_path)
        if not paths:
            print(f"⚠️  No files matched: {entry.file_path}")
            continue
        for path in paths:
            if path.suffix.lower() not in ALLOWED_EXTENSIONS:
                continue
            resolved.append((path, entry.metadata, entry.title))

    if not resolved:
        print("❌ No valid files found to ingest")
        return 1

    print(f"📦 Files to ingest: {len(resolved)}")
    if args.dry_run:
        for path, meta, title in resolved:
            print(f"- {path}")
        return 0

    ingestor = ImamIngestor(
        args.base_url,
        args.api_key,
        args.dataset,
        args.processing_mode,
        bearer_token=args.bearer_token,
        timeout=args.timeout,
    )
    semaphore = asyncio.Semaphore(max(1, args.concurrency))

    try:
        resolved_dataset_id = await ingestor.resolve_dataset_id(args.dataset)
    except Exception as exc:
        print(f"❌ {exc}")
        await ingestor.close()
        return 1
    ingestor.dataset_id = resolved_dataset_id
    print(f"📚 Target dataset: {args.dataset} -> {resolved_dataset_id}")

    existing_titles: List[str] = []
    if args.skip_existing:
        existing_titles = await ingestor.list_document_titles()
        if existing_titles:
            print(f"ℹ️  Found {len(existing_titles)} existing documents, will skip duplicates.")

    async def process_one(path: Path, metadata: Dict[str, Any], title: Optional[str]) -> None:
        async with semaphore:
            if args.skip_existing and path.name in existing_titles:
                print(f"⏭️  Skip existing: {path.name}")
                return
            doc_id = await ingestor.upload_document(path)
            if not doc_id:
                return
            if metadata or title:
                await ingestor.patch_metadata(doc_id, metadata, title)
            if args.wait:
                await ingestor.wait_for_document(doc_id)

    try:
        await asyncio.gather(*(process_one(p, m, t) for p, m, t in resolved))
    finally:
        await ingestor.close()

    print("✅ Ingestion completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
