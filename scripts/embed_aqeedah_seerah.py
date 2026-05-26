#!/usr/bin/env python3
r"""Embed Aqeedah + Seerah PDFs into the active KB collection.

One-step pipeline: OCR -> chunk -> contextual-prefix -> embed+BM25 -> upsert.
Target: kb_imam_v2_1024_ctx_gemini_embedding_2_preview (the live collection).

Usage:
    GOOGLE_API_KEY=... SILICONFLOW_API_KEYS=... \
    python3 scripts/embed_aqeedah_seerah.py \
        --pdf-dir "/opt/deploy/islamic-pdfs/extracted/For Ai Imam/" \
        --vlm-ocr
    python3 scripts/embed_aqeedah_seerah.py --pdf-dir ./pdfs --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("embed-aqeedah-seerah")

# Constants
COLLECTION = "kb_imam_v2_1024_ctx_gemini_embedding_2_preview"
EMBEDDING_MODEL = "gemini-embedding-2-preview"
EMBEDDING_DIM = 1024
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"

SILICONFLOW_URL = "https://api.siliconflow.cn/v1/chat/completions"
SILICONFLOW_MODEL = "deepseek-ai/DeepSeek-OCR"

EMBED_BATCH = 50
EMBED_CONCURRENCY = 25

_NS = uuid.UUID("b1c2d3e4-f5a6-7890-abcd-ef1234567890")

VLM_OCR_PROMPT = (
    "OCR this page. Extract ALL text preserving Arabic and English exactly as written. "
    "Maintain RTL order for Arabic. Preserve paragraph breaks. Output text only."
)


# Inline tokenizer + BM25 (from reindex_with_context.py)

_RE_LATIN_WORD = re.compile(r"[A-Za-z0-9\-_]+")
_RE_CJK_RUN = re.compile(r"[一-鿿]+")
_RE_ARABIC_RUN = re.compile(
    r"[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]+"
)
_RE_ARABIC_DIACRITICS = re.compile(r"[ً-ْٰ]")
_RE_ALEF = re.compile(r"[أإآٱ]")


def _normalize_arabic(text: str) -> str:
    t = _RE_ARABIC_DIACRITICS.sub("", text)
    t = _RE_ALEF.sub("ا", t)
    return t.replace("ة", "ه").replace("ى", "ي")


def tokenize(text: str) -> list[str]:
    t = (text or "").strip()
    if not t:
        return []
    tokens: list[str] = []
    t_clean = t.lower()
    tokens.extend(_RE_LATIN_WORD.findall(t_clean))
    for run in _RE_CJK_RUN.findall(t_clean):
        if len(run) >= 2:
            tokens.append(run)
            for i in range(len(run) - 1):
                tokens.append(run[i : i + 2])
        tokens.extend(list(run))
    normalized = _normalize_arabic(t_clean)
    for run in _RE_ARABIC_RUN.findall(normalized):
        for word in run.split():
            word = word.strip()
            if not word or len(word) < 2:
                continue
            tokens.append(word)
            if len(word) > 3:
                for prefix in ["و", "ف", "ب", "ل", "ك", "ال", "وال", "فال", "بال", "لل"]:
                    if word.startswith(prefix):
                        stem = word[len(prefix):]
                        if len(stem) >= 2:
                            tokens.append(stem)
                        break
    seen: set[str] = set()
    return [t for t in tokens if t and not (t in seen or seen.add(t))]


def _token_hash(token: str) -> int:
    h = 0x81C9DC5
    for b in token.encode("utf-8"):
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h


def text_to_sparse(text: str) -> tuple[list[int], list[float]]:
    tokens = tokenize(text)
    if not tokens:
        return [], []
    tf: dict[int, float] = {}
    for token in tokens:
        idx = _token_hash(token)
        tf[idx] = tf.get(idx, 0.0) + 1.0
    indices = sorted(tf.keys())
    return indices, [tf[i] for i in indices]


# Contextual prefix (from reindex_with_context.py)

def generate_context_prefix(text: str, metadata: dict[str, Any]) -> str:
    source_type = metadata.get("source_type") or ""
    doc_title = metadata.get("book_title") or metadata.get("document_title") or ""

    if source_type == "aqeedah":
        if doc_title:
            return f"This Islamic theology text '{doc_title}' discusses creed and belief. "
        return "This is Islamic theology (Aqeedah) text discussing creed and belief. "

    if source_type == "seerah":
        if doc_title:
            return f"This biography of Prophet Muhammad from '{doc_title}'. "
        return "This is from the Seerah (biography of Prophet Muhammad). "

    if doc_title:
        return f"From '{doc_title}'. "
    return ""


# PDF classification

def classify_pdf(filename: str) -> tuple[str, str, str]:
    name = Path(filename).stem
    low = name.lower()

    if any(k in low for k in ["tafsir", "atlas", "stories"]):
        return "tafseer", name, ""
    if any(k in low for k in ["imaan", "tawheed", "creed", "aqeedah", "wasitiyah", "islam", "jesus"]):
        return "aqeedah", name, ""
    if any(k in low for k in ["prophet", "sealed", "raheeq", "muhammad", "seerah"]):
        return "seerah", name, ""
    if any(k in low for k in ["prayer", "fiqh", "inheritance", "purification", "fasting", "marriage"]):
        madhab = "hanafi" if "hanafi" in low else "general"
        return "fiqh", name, madhab
    return "general_islamic", name, ""


def chunk_text(text: str, chunk_size: int = 800, overlap: int = 150) -> list[str]:
    words = text.split()
    if len(words) <= chunk_size:
        return [text] if text.strip() else []
    chunks: list[str] = []
    start = 0
    while start < len(words):
        end = min(start + chunk_size, len(words))
        chunk = " ".join(words[start:end])
        if chunk.strip():
            chunks.append(chunk)
        start += chunk_size - overlap
    return chunks


# VLM OCR

class VLMPageOCR:
    def __init__(self, api_keys: list[str], model: str = SILICONFLOW_MODEL,
                 base_url: str = SILICONFLOW_URL):
        if not api_keys:
            raise ValueError("At least one API key required")
        self.api_keys = api_keys
        self.model = model
        self.base_url = base_url
        self._key_index = 0
        self._lock = asyncio.Lock()
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(120.0, connect=10.0),
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=25),
        )
        concurrency = 5 * len(api_keys)
        self._sem = asyncio.Semaphore(concurrency)
        log.info("OCR pool: %d keys, concurrency=%d", len(api_keys), concurrency)

    async def _next_key(self) -> str:
        async with self._lock:
            key = self.api_keys[self._key_index % len(self.api_keys)]
            self._key_index += 1
            return key

    async def close(self) -> None:
        await self._client.aclose()

    async def ocr_page(self, image_bytes: bytes) -> str:
        async with self._sem:
            return await self._call_with_retry(image_bytes)

    async def _call_with_retry(self, image_bytes: bytes, retries: int = 3) -> str:
        b64 = base64.b64encode(image_bytes).decode()
        mime = "image/png" if image_bytes[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                {"type": "text", "text": VLM_OCR_PROMPT},
            ]}],
            "max_tokens": 4096,
        }
        for attempt in range(retries):
            key = await self._next_key()
            try:
                resp = await self._client.post(
                    self.base_url, json=payload,
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                )
                if resp.status_code == 429:
                    delay = 1 if len(self.api_keys) > 1 else 2 ** (attempt + 1)
                    log.warning("OCR 429 key ..%s, rotate (attempt %d/%d)", key[-6:], attempt + 1, retries)
                    await asyncio.sleep(delay)
                    continue
                if resp.status_code >= 400:
                    log.warning("OCR error %d: %s", resp.status_code, resp.text[:200])
                    if attempt < retries - 1:
                        await asyncio.sleep(2 ** attempt)
                    continue
                data = resp.json()
                return data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            except Exception as exc:
                if attempt < retries - 1:
                    await asyncio.sleep(2 ** attempt)
                else:
                    log.error("OCR failed after %d attempts: %s", retries, exc)
        return ""


def render_page_to_image(page: Any, dpi: int = 200, max_bytes: int = 3_000_000) -> bytes:
    import fitz
    for d in [dpi, 150, 120]:
        mat = fitz.Matrix(d / 72, d / 72)
        pix = page.get_pixmap(matrix=mat)
        img_bytes = pix.tobytes("png")
        if len(img_bytes) <= max_bytes:
            return img_bytes
    mat = fitz.Matrix(0.8, 0.8)
    pix = page.get_pixmap(matrix=mat)
    return pix.tobytes("jpeg")


# OCR + chunk PDFs

async def build_pdf_segments(
    pdf_dir: str,
    vlm_ocr: VLMPageOCR | None,
    target_types: set[str] | None = None,
) -> list[dict]:
    try:
        import fitz  # noqa: F401
    except ImportError:
        log.error("PyMuPDF not installed. pip install PyMuPDF")
        return []

    pdf_path = Path(pdf_dir)
    if not pdf_path.exists():
        log.error("PDF directory not found: %s", pdf_dir)
        return []

    pdf_files = sorted(pdf_path.glob("**/*.pdf"))
    log.info("Found %d PDF files in %s", len(pdf_files), pdf_dir)

    cache_dir = pdf_path / ".ocr_cache"
    cache_dir.mkdir(exist_ok=True)
    segments: list[dict] = []

    for pdf_file in pdf_files:
        source_type, book_title, madhab = classify_pdf(pdf_file.name)

        # Skip __MACOSX junk
        if "__MACOSX" in str(pdf_file):
            continue
        # Skip if not in target types
        if target_types and source_type not in target_types:
            log.info("  Skipping %s (type=%s, not in target)", pdf_file.name, source_type)
            continue

        log.info("  Processing: %s -> %s", pdf_file.name, source_type)

        # Check OCR cache
        cache_file = cache_dir / f"{pdf_file.stem}.json"
        if cache_file.exists() and vlm_ocr:
            try:
                cached = json.loads(cache_file.read_text())
                page_texts = [(p["page"], p["text"]) for p in cached]
                log.info("  %s: loaded %d pages from cache", pdf_file.name, len(page_texts))
            except Exception:
                page_texts = []
                cache_file.unlink(missing_ok=True)
        else:
            page_texts = []

        if not page_texts:
            try:
                doc = fitz.open(str(pdf_file))
            except Exception as exc:
                log.error("  Failed to open %s: %s", pdf_file.name, exc)
                continue
            total_pages = len(doc)

            if vlm_ocr:
                batch_size = 5 * len(vlm_ocr.api_keys)
                for batch_start in range(0, total_pages, batch_size):
                    batch_end = min(batch_start + batch_size, total_pages)
                    tasks = []
                    for pn in range(batch_start, batch_end):
                        page = doc[pn]
                        img_bytes = render_page_to_image(page)
                        tasks.append((pn + 1, vlm_ocr.ocr_page(img_bytes)))
                    results = await asyncio.gather(*[t[1] for t in tasks], return_exceptions=True)
                    for (pn, _), result in zip(tasks, results, strict=True):
                        if isinstance(result, str) and result.strip():
                            page_texts.append((pn, result))
                        elif isinstance(result, Exception):
                            log.warning("  Page %d OCR failed: %s", pn, result)
                    if batch_end % 50 == 0 or batch_end == total_pages:
                        log.info("    %s: OCR %d/%d pages (%d with text)",
                                 pdf_file.name, batch_end, total_pages, len(page_texts))
                    num_keys = len(vlm_ocr.api_keys)
                    await asyncio.sleep(max(1, 4 // num_keys))
                # Save cache
                cache_data = [{"page": p, "text": t} for p, t in page_texts]
                cache_file.write_text(json.dumps(cache_data, ensure_ascii=False))
                log.info("  %s: cached %d pages", pdf_file.name, len(page_texts))
            else:
                for pn in range(total_pages):
                    text = doc[pn].get_text("text")
                    if text.strip():
                        page_texts.append((pn + 1, text))
            doc.close()

        if not page_texts:
            log.warning("  Empty PDF: %s", pdf_file.name)
            continue

        # Combine page texts
        full_text = ""
        page_breaks: list[tuple[int, int]] = []
        for pn, text in page_texts:
            page_breaks.append((len(full_text), pn))
            full_text += text + "\n\n"

        # Chunk
        chunks = chunk_text(full_text, chunk_size=800, overlap=150)
        log.info("  %s: %d/%d pages -> %d chunks",
                 pdf_file.name, len(page_texts),
                 max((pn for pn, _ in page_texts), default=0), len(chunks))

        for idx, chunk in enumerate(chunks):
            chunk_offset = full_text.find(chunk[:50])
            page_num = 1
            for offset, pn in page_breaks:
                if offset <= chunk_offset:
                    page_num = pn
                else:
                    break

            seg_payload: dict[str, Any] = {
                "source_type": source_type,
                "citation_text": book_title,
                "book_title": book_title,
                "file_name": pdf_file.name,
                "page_number": page_num,
                "chunk_index": idx,
                "level": 3,
                "authority_rank": 4,
                "language": "en",
            }
            if madhab:
                seg_payload["madhab"] = madhab

            embed_text = f"{book_title} (p.{page_num})\n\n{chunk}"
            seg_payload["text"] = embed_text
            segments.append({
                "id": str(uuid.uuid5(_NS, f"pdf-{hashlib.sha256(pdf_file.name.encode()).hexdigest()[:16]}-{idx}")),
                "text": embed_text,
                "payload": seg_payload,
            })

    log.info("Total segments: %d", len(segments))
    return segments


# Gemini Embedding

async def embed_batch(
    client: httpx.AsyncClient,
    texts: list[str],
    api_key: str,
    sem: asyncio.Semaphore,
    model: str = EMBEDDING_MODEL,
    dim: int = EMBEDDING_DIM,
) -> list[list[float]]:
    async with sem:
        requests = [
            {
                "model": f"models/{model}",
                "content": {"parts": [{"text": t[:8000]}]},
                "outputDimensionality": dim,
                "taskType": "RETRIEVAL_DOCUMENT",
            }
            for t in texts
        ]
        for attempt in range(4):
            try:
                resp = await client.post(
                    f"{GEMINI_BASE}/models/{model}:batchEmbedContents",
                    json={"requests": requests},
                    params={"key": api_key},
                    headers={"Content-Type": "application/json"},
                    timeout=60,
                )
                if resp.status_code == 429:
                    delay = 2 ** (attempt + 1)
                    m = re.search(r'"retryDelay"\s*:\s*"(\d+)s"', resp.text)
                    if m:
                        delay = max(delay, int(m.group(1)))
                    log.warning("Embed 429, wait %ds (attempt %d/4)", delay, attempt + 1)
                    await asyncio.sleep(delay)
                    continue
                resp.raise_for_status()
                data = resp.json()
                embeddings = data.get("embeddings", [])
                if len(embeddings) != len(texts):
                    raise RuntimeError(f"Expected {len(texts)} embeddings, got {len(embeddings)}")
                return [e["values"] for e in embeddings]
            except (httpx.TimeoutException, httpx.ConnectError):
                if attempt < 3:
                    await asyncio.sleep(2 ** attempt)
                else:
                    raise
        raise RuntimeError("Embed failed after all retries")


# Main

async def run(args: argparse.Namespace) -> None:
    from qdrant_client import AsyncQdrantClient
    from qdrant_client.http import models as qm

    t_start = time.time()

    # Connect to Qdrant
    log.info("Connecting to Qdrant: %s", args.qdrant_url)
    qdrant = AsyncQdrantClient(url=args.qdrant_url, timeout=60)

    info = await qdrant.get_collection(COLLECTION)
    log.info("Target collection: %s (%d existing points)", COLLECTION, info.points_count)

    # Target source types
    target_types = {"aqeedah", "seerah"}

    # Init VLM OCR
    vlm: VLMPageOCR | None = None
    if args.vlm_ocr:
        keys = [k.strip() for k in args.siliconflow_keys.split(",") if k.strip()]
        if not keys:
            log.error("--siliconflow-keys required for --vlm-ocr")
            sys.exit(1)
        vlm = VLMPageOCR(api_keys=keys, model=args.vlm_model)

    # Build segments
    try:
        segments = await build_pdf_segments(args.pdf_dir, vlm_ocr=vlm, target_types=target_types)
    finally:
        if vlm:
            await vlm.close()

    if not segments:
        log.warning("No segments generated, exiting")
        return

    # Count by source_type
    by_type: dict[str, int] = {}
    for seg in segments:
        st = seg["payload"]["source_type"]
        by_type[st] = by_type.get(st, 0) + 1
    log.info("Segments by type: %s", by_type)

    if args.dry_run:
        log.info("DRY RUN - not embedding")
        return

    # Embed + upsert
    embed_sem = asyncio.Semaphore(EMBED_CONCURRENCY)
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(60, connect=10),
        limits=httpx.Limits(max_connections=EMBED_CONCURRENCY * 2),
    ) as http_client:
        total = 0
        for batch_start in range(0, len(segments), EMBED_BATCH):
            batch = segments[batch_start : batch_start + EMBED_BATCH]

            # Build embed texts with contextual prefix
            embed_texts: list[str] = []
            raw_texts: list[str] = []
            point_ids: list[str] = []
            payloads: list[dict] = []
            sparse_vecs: list[tuple[list[int], list[float]]] = []

            for seg in batch:
                text = seg["text"]
                meta = seg["payload"]
                prefix = generate_context_prefix(text, meta)
                embed_text = f"{prefix}{text}" if prefix else text

                embed_texts.append(embed_text)
                raw_texts.append(text)
                point_ids.append(seg["id"])
                payloads.append(seg["payload"])
                sparse_vecs.append(text_to_sparse(text))

            # Embed
            embeddings = await embed_batch(http_client, embed_texts, args.google_api_key, embed_sem)

            # Build Qdrant points
            points: list[qm.PointStruct] = []
            for pid, payload, emb, (si, sv) in zip(
                point_ids,
                payloads,
                embeddings,
                sparse_vecs,
                strict=True,
            ):
                if not emb:
                    continue
                vector: dict[str, Any] = {"": emb}
                if si:
                    vector["bm25"] = qm.SparseVector(indices=si, values=sv)
                points.append(qm.PointStruct(id=pid, vector=vector, payload=payload))

            if points:
                await qdrant.upsert(collection_name=COLLECTION, points=points)

            total += len(batch)
            elapsed = time.time() - t_start
            rate = total / elapsed if elapsed > 0 else 0
            log.info("  Embedded %d/%d (%.1f/s)", total, len(segments), rate)

    # Final stats
    info = await qdrant.get_collection(COLLECTION)
    elapsed = time.time() - t_start
    log.info("=== DONE ===")
    log.info("  Collection: %s", COLLECTION)
    log.info("  New points: %d", total)
    log.info("  Total points: %d", info.points_count)
    log.info("  Elapsed: %.1fs", elapsed)
    log.info("  By type: %s", by_type)

    await qdrant.close()


def main() -> None:
    p = argparse.ArgumentParser(description="Embed Aqeedah + Seerah PDFs")
    p.add_argument("--qdrant-url", default=os.environ.get("QDRANT_URL", "http://127.0.0.1:6333"))
    p.add_argument("--google-api-key", default=os.environ.get("GOOGLE_API_KEY", ""))
    p.add_argument("--pdf-dir", required=True, help="Path to PDF directory to scan recursively")
    p.add_argument("--vlm-ocr", action="store_true", help="Use VLM OCR (DeepSeek-OCR via SiliconFlow)")
    p.add_argument("--vlm-model", default=SILICONFLOW_MODEL)
    p.add_argument("--siliconflow-keys", default=os.environ.get("SILICONFLOW_API_KEYS",
                   os.environ.get("SILICONFLOW_API_KEY", "")),
                   help="SiliconFlow API keys, comma-separated for pool")
    p.add_argument("--dry-run", action="store_true", help="Count segments without embedding")
    args = p.parse_args()

    if not args.google_api_key and not args.dry_run:
        log.error("--google-api-key required (or GOOGLE_API_KEY env)")
        sys.exit(1)

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
