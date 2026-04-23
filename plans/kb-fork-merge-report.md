# Phase K5b — KB Fork Merge Report

> Reconciles `src/services/knowledge/` (gateway fork) against
> `apps/knowledge-service/src/knowledge_service/services/knowledge/` (kb-service fork).
> Polaris item #2 (源码单一权威, KB) closes by collapsing the two trees onto
> kb-service as the single source of truth. Gateway side is reduced to a
> thin HTTP client (`kb_proxy_client.py`) plus a few pure-utility hoists.

## Background: why the gateway fork was already dead code

`src/main.py:463` gates the entire knowledge-service initialization on
`if False and getattr(settings, "knowledge", None) and settings.knowledge.enabled`.
Every gateway-side import of a business module
(`KnowledgeService`, `KnowledgeWorker`, `UnifiedMultimodalEmbedding`,
`DashScopeVLMService`, `VisionPDFProcessor`, `DocumentTypeDetector`,
`HierarchicalIndexer`, `SummaryGenerator`, `VLMOCRService`) lives inside
that dead branch. The single live path is:

```
src/main.py:753  from .services.knowledge.kb_proxy_client import KBProxyClient
```

…plus one pure-utility import in `src/api/v1/assistant.py`:

```
src/api/v1/assistant.py:56  from ...services.knowledge.embedding import is_multimodal_embedding_model
```

The deletion is therefore mechanically safe; we just need to (a) keep
the proxy client, (b) hoist the one pure utility, (c) leave `confluence/`
alone, and (d) make sure the kb-service side absorbs any unique logic
that lived only on the gateway side.

## Per-file decision table

> **Diff line counts** are computed via `diff -u … | wc -l`, which
> includes the unified-diff header (3 lines) and trailing newline. The
> raw diff content is therefore `count − 4` lines on average; we report
> the raw `wc -l` number for reproducibility.
>
> **Decision shorthand:**
> - `kb-service retained` — keep apps/knowledge-service file as-is, delete gateway-side file.
> - `kb-service + ported gateway logic` — start from kb-service, hand-merge specific gateway-only logic.
> - `gateway retained / promoted` — pure-utility hoisted to `ai_gateway_core`.
> - `confluence subdir` — out of scope, deferred to K5c.

| filename | status | diff lines | retained side / fate | rationale |
|---|---|---|---|---|
| `__init__.py` | identical | — | rewrite to thin re-export of `KBProxyClient` only | gateway-side `__init__` re-exports business types (`Chunk`, `chunk_text`, etc.) that are no longer present after deletion; rewriting avoids `ImportError` at gateway startup. |
| `cache_manager.py` | identical | — | delete from gateway | dup; only kb-service consumes it. |
| `chunking.py` | diverged | 60 | kb-service retained | kb-service inlines `_ARABIC_RANGE`/`_CJK_RANGE` (standalone image, no `.common` dependency); also fixes child-chunk indexing bug + applies min/max constraints to all modes (not just FIXED_SIZE). Gateway side imported these from `.common` but had the older bug. |
| `chunking_manager.py` | diverged | 13 | kb-service retained | pure import-path swap (`ai_gateway_core.exceptions`/`logging` → `...core.*`). |
| `citation_formatter.py` | diverged | 19 | kb-service retained | gateway side reads `metadata.get("islamic_source_type")` as fallback, but the kb-service `islamic_chunking.py` writes `source_type` (not `islamic_source_type`). Gateway behaviour was already broken in mixed-tenancy mode; kb-service is the consistent path. |
| `common.py` | identical | — | delete from gateway | only the gateway fork's own files imported `.common`; kb-service has inlined every helper into its consumers (intentional standalone design). After deletion, no live caller remains. |
| `constants.py` | identical | — | delete from gateway | only kb-service consumes it. |
| `contextual_retrieval.py` | identical | — | delete from gateway | only kb-service consumes it. |
| `dataset_service.py` | diverged | 13 | kb-service retained | pure import-path swap. |
| `document_detector.py` | diverged | 38 | kb-service retained | inlined `import pymupdf as fitz` fallback (was `import_pymupdf` from `.common`). Functional behaviour identical. |
| `document_processor.py` | diverged | 13 | kb-service retained | pure import-path swap. |
| `document_service.py` | diverged | 13 | kb-service retained | pure import-path swap. |
| `embedding.py` | diverged | 38 | kb-service retained | inlined `SensitiveDataFilter` (was imported from `.common`). Functional behaviour identical. **Pure utility `is_multimodal_embedding_model` + `MULTIMODAL_EMBEDDING_MODELS` hoisted to `ai_gateway_core.knowledge.utils`** (used by `src/api/v1/assistant.py`). |
| `embedding_manager.py` | diverged | 73 | kb-service retained | kb-service intentionally inlines `resolve_google` / `resolve_dashscope` from `ai_gateway_core.config` because the kb-service ships as a standalone image without the gateway-core dependency (commented in the file). |
| `enhanced_ingestion.py` | identical | — | delete from gateway | dup. |
| `hierarchical_indexer.py` | diverged | 29 | kb-service retained | pure import-path swap + tightened `zip(..., strict=True)`. |
| `hierarchical_retriever.py` | diverged | 11 | kb-service retained | pure import-path swap. |
| `image_processing_queue.py` | identical | — | delete from gateway | dup. |
| `ingestion_service.py` | diverged | 13 | kb-service retained | pure import-path swap. |
| `islamic_chunking.py` | diverged | 101 | kb-service retained | metadata key normalisation: gateway writes `islamic_source_type`, kb-service writes `source_type` (consistent with `citation_formatter.py` fix above). |
| `islamic_metadata.py` | diverged | 141 | **kb-service + ported `_clean_doc_title_for_citation` + `book_name`/`doc_title` fallbacks** | gateway-only enhancements (title-cleaner with corpus-specific mapping, `book_name` in hadith citation, doc_title-aware tafseer author detection) are real features that should survive. Hand-merged into kb-service to avoid regression. |
| `kb_proxy_client.py` | gateway-only | — | **gateway retained (already a thin HTTP client)** | this *is* the post-merge gateway entry point. No changes needed; verified against `src/api/v1/_proxy_utils.py` patterns. |
| `knowledge_service.py` | diverged | 13 | kb-service retained | pure import-path swap. |
| `langgraph_tools.py` | diverged | 11 | kb-service retained | pure import-path swap. |
| `metadata_extractor.py` | identical | — | delete from gateway | dup. |
| `multi_query.py` | identical | — | delete from gateway | dup. |
| `multilingual_embedding.py` | identical | — | delete from gateway | dup. |
| `multimodal_reranker.py` | identical | — | delete from gateway | dup. |
| `ocr_utils.py` | diverged | 52 | kb-service retained | inlined PyMuPDF import; `_process_completed_futures` rewritten to safely iterate completed futures (avoids list-mutation-during-iteration bug). Real bugfix on kb-service side. |
| `pdf_image_processor.py` | diverged | 35 | kb-service retained | inlined PyMuPDF import. Behaviour identical. |
| `pdf_splitter.py` | diverged | 24 | kb-service retained | inlined PyMuPDF import. Behaviour identical. |
| `processing_dispatcher.py` | identical | — | delete from gateway | dup. |
| `processing_mode.py` | identical | — | delete from gateway | dup. |
| `processor_factory.py` | diverged | 11 | kb-service retained | pure import-path swap. |
| `qa_service.py` | diverged | 49 | kb-service retained | default `LLMProvider.DEEPSEEK` → `LLMProvider.GEMINI`, default model `deepseek-chat` → `gemini-2.0-flash` (matches kb-service deployment defaults — kb-service uses Gemini for QA). |
| `retrieval.py` | diverged | 205 | kb-service retained | inlined Arabic/CJK range constants + inlined `detect_language` / `normalize_arabic`; cosine_similarity gets numpy fast-path; FNV-1a hash docs improved. Standalone design — no `.common` dep. |
| `retrieval_config.py` | diverged | 82 | kb-service retained | gateway side disabled rerank+multi_query for the islamic preset citing perf measurements; kb-service keeps them on with `gte-rerank-v2 top_n=15`. **Operational divergence — kb-service deployment owns this default. Documented here so the perf-tuning intent isn't lost; if needed, can be re-evaluated at runtime via per-dataset config override.** |
| `retrieval_service.py` | diverged | 62 | kb-service retained | kb-service adds segment-metadata hydration for citation/source_reference (real fix for dense-only payloads losing citation metadata). |
| `retrieval_v2.py` | diverged | 36 | kb-service retained | inlined `detect_query_language` (standalone design). |
| `section_extractor.py` | identical | — | delete from gateway | dup. |
| `streaming_loader.py` | diverged | 51 | kb-service retained | inlined PyMuPDF import. Behaviour identical. |
| `structured_document_parser.py` | diverged | 35 | kb-service retained | inlined PyMuPDF import. Behaviour identical. |
| `summary_generator.py` | diverged | 11 | kb-service retained | pure import-path swap. |
| `text_reranker.py` | diverged | 49 | kb-service retained | inlined `SensitiveDataFilter`; cache key `md5` → `sha256[:32]` (bandit B324 hardening). |
| `utils.py` | identical | — | delete from gateway | dup. |
| `vector_store.py` | identical | — | delete from gateway | dup. |
| `vision_pdf_processor.py` | diverged | 24 | kb-service retained | inlined PyMuPDF import. Behaviour identical. |
| `vlm_ocr_service.py` | diverged | 228 | kb-service retained | kb-service adds **SiliconFlow backend** with multi-key round-robin (genuine new feature, not in gateway side). |
| `vlm_service.py` | identical | — | delete from gateway | dup. |
| `worker.py` | diverged | 150 | kb-service retained | kb-service adds `_process_scanned_with_vlm_ocr` fallback path, OCR exception swallowing, closure-bug fix on `on_progress`. All net wins. |
| `confluence/` (subdir) | n/a | — | **deferred-to-K5c** | confluence subdir is out of scope per K5b; both sides untouched. |

## Pure-utility hoist to `ai_gateway_core`

The single live gateway-side caller of a non-proxy KB symbol is
`src/api/v1/assistant.py:374` calling `is_multimodal_embedding_model`.
This is a stateless predicate over a static frozenset of model names,
i.e. exactly the kind of pure data the existing
`packages/ai-gateway-core/src/ai_gateway_core/knowledge/` package was
designed for (cf. `_authority.py`, `_synonyms.py`).

**New file:** `packages/ai-gateway-core/src/ai_gateway_core/knowledge/utils.py`
exporting `MULTIMODAL_EMBEDDING_MODELS` + `is_multimodal_embedding_model`.
Both gateway and kb-service import from here (kb-service's `embedding.py`
re-exports for backward compat — see migration note in that file).

## Final state of `src/services/knowledge/`

After this PR, the gateway tree contains exactly:

```
src/services/knowledge/
├── __init__.py                    # rewritten — only re-exports KBProxyClient
├── kb_proxy_client.py             # unchanged thin HTTP client to :8092
└── confluence/                    # untouched, deferred to K5c
    └── …
```

Anything else listed in the table above is **deleted** from the gateway
fork. The kb-service tree is **unchanged structurally** — every file
is now sourced from `apps/knowledge-service/src/knowledge_service/services/knowledge/`.

## Confluence subdir — deferred to K5c

The K5b prompt is explicit: do not touch `src/services/knowledge/confluence/`.
Both sides have a `confluence/` subdir; reconciling them is K5c's job.
This report makes no claims about Confluence beyond "out of scope".
