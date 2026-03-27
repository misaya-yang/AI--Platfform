const fs = require("fs");
const { Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
        Header, Footer, AlignmentType, HeadingLevel, BorderStyle, WidthType,
        ShadingType, PageNumber, PageBreak, LevelFormat, TabStopType, TabStopPosition } = require("docx");

const border = { style: BorderStyle.SINGLE, size: 1, color: "CCCCCC" };
const borders = { top: border, bottom: border, left: border, right: border };
const cellMargins = { top: 80, bottom: 80, left: 120, right: 120 };

function heading(text, level = HeadingLevel.HEADING_1) {
  return new Paragraph({ heading: level, children: [new TextRun(text)] });
}

function para(text, opts = {}) {
  const runs = [];
  if (typeof text === "string") {
    runs.push(new TextRun({ text, ...opts }));
  } else {
    runs.push(...text);
  }
  return new Paragraph({ spacing: { after: 120 }, children: runs });
}

function bold(text) {
  return new TextRun({ text, bold: true });
}

function severityTag(severity) {
  const colors = { Critical: "FF0000", Major: "E67E22", Minor: "3498DB" };
  return new TextRun({ text: `[${severity}] `, bold: true, color: colors[severity] || "000000" });
}

function bugEntry(severity, title, evidence, explanation) {
  return [
    new Paragraph({ spacing: { before: 200, after: 60 }, children: [severityTag(severity), bold(title)] }),
    para(evidence, { italics: true, size: 20, color: "555555" }),
    para(explanation),
  ];
}

function tableRow(cells, header = false) {
  return new TableRow({
    children: cells.map((text, i) => new TableCell({
      borders,
      width: { size: Math.floor(9360 / cells.length), type: WidthType.DXA },
      shading: header ? { fill: "2C3E50", type: ShadingType.CLEAR } : undefined,
      margins: cellMargins,
      children: [new Paragraph({ children: [new TextRun({ text, bold: header, color: header ? "FFFFFF" : "000000", size: header ? 22 : 20 })] })],
    })),
  });
}

function makeTable(headers, rows, colWidths) {
  const totalWidth = 9360;
  const widths = colWidths || headers.map(() => Math.floor(totalWidth / headers.length));
  return new Table({
    width: { size: totalWidth, type: WidthType.DXA },
    columnWidths: widths,
    rows: [
      new TableRow({
        children: headers.map((h, i) => new TableCell({
          borders, width: { size: widths[i], type: WidthType.DXA },
          shading: { fill: "2C3E50", type: ShadingType.CLEAR }, margins: cellMargins,
          children: [new Paragraph({ children: [new TextRun({ text: h, bold: true, color: "FFFFFF", size: 22 })] })],
        })),
      }),
      ...rows.map(row => new TableRow({
        children: row.map((cell, i) => new TableCell({
          borders, width: { size: widths[i], type: WidthType.DXA }, margins: cellMargins,
          children: [new Paragraph({ spacing: { after: 40 }, children: typeof cell === "string" ? [new TextRun({ text: cell, size: 20 })] : cell })],
        })),
      })),
    ],
  });
}

function bulletList(items, ref = "bullets") {
  return items.map(item => new Paragraph({
    numbering: { reference: ref, level: 0 },
    spacing: { after: 60 },
    children: typeof item === "string" ? [new TextRun({ text: item, size: 22 })] : item,
  }));
}

const doc = new Document({
  styles: {
    default: { document: { run: { font: "Arial", size: 22 } } },
    paragraphStyles: [
      { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 36, bold: true, font: "Arial", color: "1A5276" },
        paragraph: { spacing: { before: 360, after: 200 }, outlineLevel: 0 } },
      { id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 28, bold: true, font: "Arial", color: "2E86C1" },
        paragraph: { spacing: { before: 240, after: 160 }, outlineLevel: 1 } },
      { id: "Heading3", name: "Heading 3", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 24, bold: true, font: "Arial", color: "2C3E50" },
        paragraph: { spacing: { before: 200, after: 120 }, outlineLevel: 2 } },
    ],
  },
  numbering: {
    config: [
      { reference: "bullets", levels: [{ level: 0, format: LevelFormat.BULLET, text: "\u2022", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 720, hanging: 360 } } } }] },
      { reference: "numbers", levels: [{ level: 0, format: LevelFormat.DECIMAL, text: "%1.", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 720, hanging: 360 } } } }] },
      { reference: "actions", levels: [{ level: 0, format: LevelFormat.DECIMAL, text: "A%1.", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 720, hanging: 360 } } } }] },
    ],
  },
  sections: [{
    properties: {
      page: {
        size: { width: 12240, height: 15840 },
        margin: { top: 1440, right: 1440, bottom: 1440, left: 1440 },
      },
    },
    headers: {
      default: new Header({ children: [new Paragraph({
        children: [new TextRun({ text: "KB Server Deep Architecture Audit", italics: true, size: 18, color: "888888" })],
        alignment: AlignmentType.RIGHT,
      })] }),
    },
    footers: {
      default: new Footer({ children: [new Paragraph({
        alignment: AlignmentType.CENTER,
        children: [new TextRun({ text: "Page ", size: 18, color: "888888" }), new TextRun({ children: [PageNumber.CURRENT], size: 18, color: "888888" })],
      })] }),
    },
    children: [
      // ============ TITLE PAGE ============
      new Paragraph({ spacing: { before: 3000 } }),
      new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 200 }, children: [new TextRun({ text: "KB Server Module", size: 56, bold: true, color: "1A5276", font: "Arial" })] }),
      new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 100 }, children: [new TextRun({ text: "Deep Architecture Audit Report", size: 40, color: "2E86C1", font: "Arial" })] }),
      new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 600 }, children: [new TextRun({ text: "ai-gateway / src / services / knowledge", size: 24, color: "888888", font: "Arial" })] }),
      new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 100 }, border: { bottom: { style: BorderStyle.SINGLE, size: 6, color: "2E86C1", space: 1 } } }),
      new Paragraph({ spacing: { before: 400 } }),
      new Paragraph({ alignment: AlignmentType.CENTER, children: [new TextRun({ text: "Audit Date: March 26, 2026", size: 24, color: "555555" })] }),
      new Paragraph({ alignment: AlignmentType.CENTER, children: [new TextRun({ text: "Scope: Retrieval Pipeline, Fusion, Rerank, Vector Store, Ingestion", size: 22, color: "555555" })] }),
      new Paragraph({ alignment: AlignmentType.CENTER, children: [new TextRun({ text: "Auditor: Claude Opus 4.6 (AI Architecture Review)", size: 22, color: "555555" })] }),

      new Paragraph({ children: [new PageBreak()] }),

      // ============ EXECUTIVE SUMMARY ============
      heading("Executive Summary"),
      para("This report presents a rigorous, evidence-driven audit of the Knowledge Base (KB) server module inside the ai-gateway codebase. The KB module implements a multi-strategy retrieval system with hybrid search (dense + BM25), Reciprocal Rank Fusion (RRF), Maximal Marginal Relevance (MMR), cross-encoder reranking, hierarchical indexing, multimodal retrieval, and cross-language (Arabic-English) query expansion."),
      para("The system is architecturally ambitious and demonstrates strong domain awareness (Islamic knowledge retrieval). However, the audit identified several critical bugs, major architectural gaps, and areas where the system falls short of industry benchmarks set by Dify, OpenAI Retrieval, and LlamaIndex."),

      new Paragraph({ spacing: { before: 200 } }),
      makeTable(
        ["Category", "Critical", "Major", "Minor"],
        [
          ["Functional Correctness (Bugs)", [severityTag("Critical"), new TextRun({ text: "4", size: 20 })], "3", "5"],
          ["Retrieval & Ranking Quality", "1", "3", "2"],
          ["Architecture & Scalability", "1", "4", "3"],
          ["Industry Gaps", "2", "3", "2"],
        ],
        [4000, 1500, 1500, 1860]
      ),

      new Paragraph({ children: [new PageBreak()] }),

      // ============ SECTION 1: CRITICAL ISSUES ============
      heading("1. Critical Issues (Must Fix Before Production)"),
      para("These are bugs or design flaws that will cause incorrect results, silent failures, or data loss in production."),

      // Bug 1
      ...bugEntry("Critical",
        "BUG-01: MMR API Mismatch in retrieval_service.py (_apply_mmr / _apply_mmr_async)",
        "File: retrieval_service.py, lines 879-884 and 1020-1027",
        "The _apply_mmr and _apply_mmr_async methods call mmr_select() with positional arguments (query_embedding, result_embeddings, k=..., lambda_param=...), but the actual mmr_select() function in retrieval.py (line 778) expects a completely different signature: mmr_select(candidates: list[str], relevance: dict[str, float], vectors: dict[str, Sequence[float]], *, top_k: int, lambda_mult: float). This means MMR will ALWAYS throw a TypeError at runtime. Since the error is caught by a broad except clause, MMR silently fails and returns unmodified results. Users think they have diversity optimization but they do not."
      ),

      // Bug 2
      ...bugEntry("Critical",
        "BUG-02: BM25 Scoring on Raw Text Instead of Tokenized Documents",
        "File: retrieval_service.py, lines 643-644",
        "In _sparse_retrieval(), candidate_texts are raw strings passed directly to bm25_scores(), but bm25_scores() in retrieval.py expects pre-tokenized sequences (Sequence[Sequence[str]]). The function iterates over each character of the string rather than tokens. This produces completely meaningless BM25 scores. The sparse retrieval arm of hybrid search is effectively random noise."
      ),

      // Bug 3
      ...bugEntry("Critical",
        "BUG-03: RRF Fusion Loses Score Information in _fuse_results()",
        "File: retrieval_service.py, lines 896-955",
        "The _fuse_results() method uses O(N*M) linear scan to find ranks for each segment_id. More critically, it clips RRF scores to min(score, 1.0), but RRF scores from two lists with k=60 have a theoretical maximum of ~0.033 (1/61 + 1/61). Clipping to 1.0 is a no-op, but the real bug is the method doesn't handle the case where a segment appears only in one list correctly. Missing segments get score 0.0 from the absent list instead of a proper penalty, which can over-rank single-source results."
      ),

      // Bug 4
      ...bugEntry("Critical",
        "BUG-04: Language Detection Counts Matches, Not Characters",
        "File: retrieval.py, lines 213-229",
        "detect_language() divides arabic_count (number of regex matches/runs) by total (number of characters). These are fundamentally different units. A single long Arabic word is 1 match but could be 10+ characters. This makes the detection thresholds (0.2 for Arabic, 0.1 for CJK) unreliable. For short mixed queries like 'What is zakat?', the function may misclassify the language, causing incorrect weight adjustments in the entire hybrid pipeline."
      ),

      // Bug 5
      ...bugEntry("Critical",
        "BUG-05: Duplicate RetrievalConfig Class Definitions",
        "Files: retrieval_service.py (line 331) and retrieval_config.py (line 339)",
        "There are two completely different classes both named RetrievalConfig. The one in retrieval_service.py is a simple dataclass with 10 fields. The one in retrieval_config.py is a comprehensive configuration with nested sub-configs (VectorRetrievalConfig, KeywordRetrievalConfig, FusionConfig, RerankConfig, MMRConfig, etc.). The hybrid_retrieval method on line 762 references config.rerank.enabled, but the local RetrievalConfig has no rerank attribute. This will cause an AttributeError at runtime. The two config systems are not unified."
      ),

      new Paragraph({ children: [new PageBreak()] }),

      // ============ SECTION 2: MAJOR WEAKNESSES ============
      heading("2. Major Weaknesses"),

      ...bugEntry("Major",
        "WEAK-01: No True Sparse Index (BM25 Computed In-Memory on DB Results)",
        "File: retrieval_service.py, lines 624-664",
        "BM25 retrieval fetches candidates from the database using LIKE-matching (search_segments_like_any), then computes BM25 scores in Python. This is fundamentally wrong for a production system. The DB LIKE search may miss relevant documents that BM25 would surface (e.g., morphological variants). More importantly, BM25 IDF values are computed over the fetched candidate set rather than the entire corpus, making the IDF component statistically meaningless. Industry standard: use a proper inverted index (Elasticsearch, Qdrant sparse vectors, or Tantivy)."
      ),

      ...bugEntry("Major",
        "WEAK-02: retrieve_batch() Is Sequential, Not Parallel",
        "File: retrieval_service.py, lines 526-536",
        "retrieve_batch() processes queries sequentially in a for-loop. For 10 queries, this means 10x the latency. Should use asyncio.gather() for parallel execution, similar to how _hybrid_retrieval already parallelizes dense + sparse."
      ),

      ...bugEntry("Major",
        "WEAK-03: Rerank Cache Uses MD5 (Collision-Prone) and Module-Level Globals",
        "File: text_reranker.py, lines 81-109",
        "The rerank cache uses MD5 for hashing, which is cryptographically broken and has known collision attacks. While not a security vulnerability per se (it's a cache key), collisions can cause incorrect rerank results to be served. The cache is also unbounded in practice (200 entries with LRU, but the key space is infinite), and uses module-level globals with threading locks, making it incompatible with multi-process deployments (e.g., Gunicorn with workers)."
      ),

      ...bugEntry("Major",
        "WEAK-04: Dual Retrieval Pipeline (retrieval.py vs retrieval_v2.py) Creates Confusion",
        "Files: retrieval.py, retrieval_v2.py",
        "Two complete retrieval pipelines exist side by side. retrieval.py has comprehensive utilities (BM25, RRF, MMR, ScoreNormalization, language detection). retrieval_v2.py has a cleaner RetrievalPipeline class with RetrievalCandidate tracking. retrieval_service.py imports from both but primarily uses the functions from retrieval.py. retrieval_v2.py's RetrievalPipeline class appears to be unused in production. This creates maintenance burden and confusion about which pipeline is authoritative."
      ),

      ...bugEntry("Major",
        "WEAK-05: Vector Store hybrid_search() Ignores BM25 Entirely",
        "File: vector_store.py, lines 399-459",
        "The hybrid_search() method only does vector search and then applies a naive term-overlap lexical score. It doesn't use actual BM25 or any inverted index. The 'alpha' parameter blends vector similarity with simple substring matching. This is not true hybrid search. The method name is misleading and may give users false confidence in retrieval quality."
      ),

      ...bugEntry("Major",
        "WEAK-06: No Score Threshold Applied After RRF Fusion",
        "File: retrieval_service.py, _fuse_results() and _hybrid_retrieval()",
        "RRF produces scores in a very small range (typically 0.003-0.033). The config.score_threshold (default 0.5 in RetrievalConfig) is applied to dense/sparse results BEFORE fusion, but never after fusion. This means either all results pass (if threshold is below 0.033) or no results pass. The threshold should be applied to the final ranked output with appropriate scaling."
      ),

      new Paragraph({ children: [new PageBreak()] }),

      // ============ SECTION 3: MINOR IMPROVEMENTS ============
      heading("3. Minor Improvements"),

      ...bulletList([
        "tokenize() in retrieval.py calls detect_language() but discards the result (line 346). This is a dead call that wastes cycles.",
        "Arabic tokenization adds all prefix variants for every word, potentially inflating the token list and BM25 term frequencies. Consider limiting to the best-matching prefix strip.",
        "ScoreNormalization has 5 strategies but normalize_hybrid_scores() only uses 'robust' by default. The others (percentile, sigmoid, L2) are never exposed to the API layer.",
        "cosine_similarity() in retrieval.py is a pure-Python O(N) loop. For production with 768+ dimension vectors, use numpy or torch.",
        "The HTTP client pool in text_reranker.py uses a module-level asyncio.Lock which must be created within a running event loop. If the module is imported before the event loop starts (common in testing), this can cause RuntimeError.",
        "RerankConfig defaults model to 'gte-rerank' but the actual DashScope model is 'gte-rerank-v2'. This inconsistency may cause 404s on first call.",
        "The cross-language query expansion dictionary has ~75 terms. This is a good start but misses many common Islamic terms. Consider making it configurable via external JSON.",
      ]),

      new Paragraph({ children: [new PageBreak()] }),

      // ============ SECTION 4: INDUSTRY COMPARISON ============
      heading("4. Architectural Gaps vs Industry Benchmarks"),

      heading("4.1 Comparison with Dify", HeadingLevel.HEADING_2),
      makeTable(
        ["Capability", "Your System", "Dify", "Gap Analysis"],
        [
          ["Sparse Index", "In-memory BM25 on DB candidates", "Elasticsearch / Qdrant sparse", "Critical gap: no true inverted index"],
          ["Chunk Retrieval", "Flat single-level + optional hierarchical", "Multi-level with parent-child", "Hierarchical exists but not integrated into main pipeline"],
          ["Reranking", "4 providers (DashScope, BGE, Cohere, Local)", "Cohere + Jina", "Ahead: more reranker options"],
          ["Multi-tenancy", "Qdrant payload filter + DB validation", "Workspace-level isolation", "Comparable approach"],
          ["API Compatibility", "Custom API + LangGraph tools", "OpenAI-compatible + Dify workflow API", "Missing: OpenAI-compatible retrieval endpoint"],
          ["Metadata Filtering", "6 indexed fields in Qdrant", "Rich metadata + boolean filters", "Comparable, but missing compound filters"],
        ],
        [1800, 2200, 2200, 3160]
      ),

      heading("4.2 Comparison with OpenAI Retrieval (Assistants API)", HeadingLevel.HEADING_2),
      makeTable(
        ["Capability", "Your System", "OpenAI Retrieval", "Gap Analysis"],
        [
          ["Chunking", "9 strategies + hierarchical", "Automatic (768 tokens, 50% overlap)", "Ahead: more control and options"],
          ["Retrieval", "Hybrid (vector + BM25)", "Semantic only (vector)", "Ahead: hybrid is superior"],
          ["Reranking", "Cross-encoder pipeline", "Internal model (not exposed)", "Comparable"],
          ["File Support", "PDF, DOCX, TXT, HTML", "PDF, DOCX, TXT, CSV, JSON, MD", "Gap: missing CSV/JSON/MD native parsing"],
          ["Auto-tuning", "Manual configuration only", "Automatic parameter tuning", "Gap: no auto-tuning or feedback loop"],
          ["Observability", "Pipeline logging", "Run step events + usage tracking", "Gap: no per-stage latency metrics exposed to users"],
        ],
        [1800, 2200, 2200, 3160]
      ),

      heading("4.3 Comparison with LlamaIndex", HeadingLevel.HEADING_2),
      makeTable(
        ["Capability", "Your System", "LlamaIndex", "Gap Analysis"],
        [
          ["Retriever Abstraction", "Hardcoded in RetrievalService", "Pluggable BaseRetriever interface", "Gap: no retriever abstraction / plugin system"],
          ["Query Transforms", "Cross-language expansion only", "HyDE, Sub-question, Step-back", "Major gap: missing HyDE and sub-question decomposition"],
          ["Response Synthesis", "Not in KB module", "TreeSummarize, Refine, CompactRefine", "Gap: no built-in synthesis pipeline"],
          ["Evaluation", "QA service (basic)", "FaithfulnessEvaluator, RelevancyEvaluator", "Gap: no automated RAG evaluation framework"],
          ["Structured Outputs", "Not supported", "Pydantic output parsing", "Missing for structured extraction"],
          ["Caching", "Embedding + rerank cache", "Full response cache + embedding cache", "Partial: missing response-level cache"],
        ],
        [1800, 2200, 2200, 3160]
      ),

      heading("4.4 What You Are Missing", HeadingLevel.HEADING_2),
      ...bulletList([
        "HyDE (Hypothetical Document Embeddings): Generate a hypothetical answer, embed it, then search. This dramatically improves recall for question-type queries. LlamaIndex and many production systems use this.",
        "Sub-question decomposition: Break complex queries into atomic sub-queries, retrieve for each, then merge. Critical for multi-hop reasoning.",
        "Proper inverted index: Replace in-memory BM25 with Elasticsearch, Qdrant sparse vectors (SPLADE), or Tantivy. This is the single biggest quality gap.",
        "RAG evaluation framework: No automated way to measure retrieval quality (MRR, NDCG, Recall@K). You can't improve what you can't measure.",
        "Query routing: Automatically choose between vector/keyword/hybrid based on query characteristics (e.g., keyword queries should skip dense retrieval).",
        "Streaming retrieval: For long-context applications, stream results as they become available rather than waiting for the full pipeline.",
      ]),

      heading("4.5 Where You Are Over-Engineered", HeadingLevel.HEADING_2),
      ...bulletList([
        "5 normalization strategies in ScoreNormalization class, but only 'robust' is used. Remove unused strategies or expose them via config.",
        "Dual retrieval pipelines (retrieval.py + retrieval_v2.py) with overlapping functionality. Consolidate into one.",
        "Multiple redundant language detection functions across files (detect_language in retrieval.py, detect_query_language in retrieval_v2.py and retrieval_service.py).",
        "The multimodal reranker (VLM-based) is complex but the infrastructure to actually trigger it in the main pipeline is incomplete.",
      ]),

      new Paragraph({ children: [new PageBreak()] }),

      // ============ SECTION 5: ACTION PLAN ============
      heading("5. Concrete Action Plan"),

      heading("Phase 1: Critical Bug Fixes (Week 1)", HeadingLevel.HEADING_2),
      makeTable(
        ["Action", "File(s)", "Effort", "Impact"],
        [
          ["A1: Fix mmr_select() API mismatch. Build adapter that converts RetrieveResult list to the expected (candidates, relevance, vectors) format.", "retrieval_service.py", "4h", "Restores MMR functionality"],
          ["A2: Tokenize documents before passing to bm25_scores(). Add tokenize() call for each candidate text.", "retrieval_service.py L643", "2h", "Fixes BM25 scoring"],
          ["A3: Unify RetrievalConfig. Remove the local dataclass in retrieval_service.py, import from retrieval_config.py.", "retrieval_service.py, retrieval_config.py", "4h", "Eliminates config confusion"],
          ["A4: Fix language detection to count characters, not match count. Use sum(len(m) for m in matches) instead of len(matches).", "retrieval.py L213-229", "1h", "Correct weight adaptation"],
          ["A5: Add integration test for full hybrid pipeline: dense + sparse + RRF + rerank + MMR end-to-end.", "tests/knowledge/", "8h", "Prevents regression"],
        ],
        [3500, 2200, 800, 2860]
      ),

      heading("Phase 2: Architecture Improvements (Week 2-3)", HeadingLevel.HEADING_2),
      makeTable(
        ["Action", "File(s)", "Effort", "Impact"],
        [
          ["A6: Add Qdrant sparse vector support for BM25. Use SPLADE or BM42 embeddings instead of in-memory BM25.", "vector_store.py, retrieval_service.py", "3d", "True hybrid search"],
          ["A7: Consolidate retrieval.py and retrieval_v2.py into a single module. Keep RetrievalCandidate from v2, utilities from v1.", "retrieval.py, retrieval_v2.py", "2d", "Reduced confusion"],
          ["A8: Make retrieve_batch() parallel with asyncio.gather() and configurable concurrency.", "retrieval_service.py", "2h", "10x batch speedup"],
          ["A9: Replace MD5 with xxhash for cache keys. Add TTL-based eviction.", "text_reranker.py", "4h", "Cache safety"],
          ["A10: Add per-stage latency metrics (retrieval, fusion, rerank, MMR) exposed via /metrics endpoint.", "retrieval_service.py, metrics/", "1d", "Observability"],
        ],
        [3500, 2200, 800, 2860]
      ),

      heading("Phase 3: Quality & Feature Parity (Week 4-6)", HeadingLevel.HEADING_2),
      makeTable(
        ["Action", "File(s)", "Effort", "Impact"],
        [
          ["A11: Implement HyDE (Hypothetical Document Embeddings) as an optional query transform.", "new: hyde_transform.py", "2d", "Improved recall for questions"],
          ["A12: Add BaseRetriever abstraction for pluggable retrievers.", "new: base_retriever.py", "2d", "Extensibility"],
          ["A13: Build RAG evaluation framework with MRR, NDCG, Recall@K metrics.", "new: eval/", "3d", "Quality measurement"],
          ["A14: Add query router that selects optimal retrieval mode based on query analysis.", "new: query_router.py", "2d", "Adaptive retrieval"],
          ["A15: Implement proper score threshold after RRF with calibrated values.", "retrieval_service.py", "4h", "Correct filtering"],
        ],
        [3500, 2200, 800, 2860]
      ),

      new Paragraph({ children: [new PageBreak()] }),

      // ============ APPENDIX ============
      heading("Appendix: Module Inventory"),
      para("The following table lists all files in the KB server module and their primary responsibility."),
      makeTable(
        ["File", "Lines", "Responsibility"],
        [
          ["retrieval.py", "848", "Tokenization, BM25, RRF, MMR, ScoreNormalization, cosine similarity"],
          ["retrieval_v2.py", "806", "RetrievalCandidate, RetrievalPipeline, weighted/RRF fusion, text matching"],
          ["retrieval_service.py", "1094", "Main retrieval orchestration, cross-language expansion, hybrid pipeline"],
          ["retrieval_config.py", "570", "Configuration dataclasses, preset configs, DatasetIndexConfig"],
          ["text_reranker.py", "858", "4 reranker providers (DashScope, BGE, Cohere, Local), caching, factory"],
          ["multimodal_reranker.py", "518", "VLM-based multimodal reranking for image-text"],
          ["vector_store.py", "460", "Qdrant client wrapper, CRUD, search, hybrid search"],
          ["embedding.py", "200+", "Embedding service, Gemini integration, query caching"],
          ["knowledge_service.py", "200+", "Top-level service coordinator, composition of sub-services"],
          ["chunking.py", "700+", "9 chunking strategies, token counting, metadata extraction"],
          ["ingestion_service.py", "500+", "Document ingestion pipeline, batch embedding, progress tracking"],
          ["hierarchical_retriever.py", "200+", "3-level hierarchical retrieval (L1/L2/L3)"],
          ["hierarchical_indexer.py", "200+", "3-level indexing (summary/section/paragraph)"],
          ["multi_query.py", "95", "Islamic term expansion, zero-LLM query reformulation"],
          ["contextual_retrieval.py", "179", "Template-based contextual prefixes for Islamic content"],
          ["citation_formatter.py", "200+", "Islamic citation formatting, authority-based sorting"],
        ],
        [2800, 800, 5760]
      ),

      new Paragraph({ spacing: { before: 600 } }),
      new Paragraph({ border: { bottom: { style: BorderStyle.SINGLE, size: 6, color: "2E86C1", space: 1 } } }),
      new Paragraph({ spacing: { before: 200 }, alignment: AlignmentType.CENTER, children: [new TextRun({ text: "End of Audit Report", italics: true, color: "888888", size: 20 })] }),
    ],
  }],
});

Packer.toBuffer(doc).then(buffer => {
  fs.writeFileSync("/sessions/blissful-magical-edison/mnt/ai-gateway/KB-Server-Audit-Report.docx", buffer);
  console.log("Report generated successfully");
});
