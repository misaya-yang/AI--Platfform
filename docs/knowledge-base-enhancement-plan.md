# Knowledge Base Enhancement Plan
## Multimodal RAG + LangGraph Integration + GPT-like Web Experience

---

## Executive Summary

This plan addresses three interconnected enhancements to the Knowledge Base module:
1. **Multimodal RAG Optimization** - Enhanced ingestion, cross-modal retrieval, unified embeddings
2. **LangGraph Agent Access** - Clean interfaces via HTTP API, SDK wrapper, and MCP server
3. **GPT-like Web Experience** - Internal ChatGPT-style interface with model/KB selection

---

## Implementation Status

| Phase | Description | Status |
|-------|-------------|--------|
| Phase 1 | LangGraph Agent Access (HTTP API, SDK) | ✅ Completed |
| Phase 2 | Multimodal RAG Enhancement | ✅ Completed |
| Phase 3 | GPT-like Assistant Experience | 🔲 Pending |
| Phase 4 | MCP Server & Polish | 🔲 Pending |

---

## Part 1: Multimodal RAG Optimization

### Current State
Your codebase already has foundational multimodal support:
- `DashScopeMultimodalEmbedding` - image embedding (1024D)
- `DashScopeVLMService` - generates image descriptions
- `MultimodalReranker` - VLM-based relevance scoring
- `RetrievalCandidate` - tracks `content_type`, `image_url`, `associated_images`
- Confluence sync with image extraction pipeline

### Dify 1.11 Key Insights
- **Unified Semantic Space**: Text + images embedded in same vector space
- **Auto-Image Extraction**: Automatically extracts images from documents (JPG, PNG, GIF ≤ 2MB)
- **Image-Text Association**: Links images to nearby text chunks via proximity scoring
- **Knowledge Pipeline**: Plugin architecture for modular RAG components

### Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    MULTIMODAL INGESTION PIPELINE                │
└─────────────────────────────────────────────────────────────────┘
     Document (PDF/DOCX/HTML/Confluence)
                    │
     ┌──────────────┼──────────────┐
     ▼              ▼              ▼
┌─────────┐  ┌───────────┐  ┌────────────┐
│  Text   │  │  Images   │  │  Tables    │
│ Chunks  │  │ Extracted │  │ Structured │
└────┬────┘  └─────┬─────┘  └─────┬──────┘
     │             │              │
     │    ┌────────┴────────┐     │
     │    ▼                 │     │
     │  VLM Description     │     │
     │  (qwen-vl-max)       │     │
     │                      │     │
     └──────────┬───────────┴─────┘
                ▼
┌─────────────────────────────────────────────────────────────────┐
│              UNIFIED MULTIMODAL EMBEDDING                       │
│         (tongyi-embedding-vision-plus / 1024D)                  │
│    Text and images in SAME vector space for cross-modal search  │
└─────────────────────────────────────────────────────────────────┘
                │
                ▼
┌─────────────────────────────────────────────────────────────────┐
│                    QDRANT HYBRID COLLECTION                     │
│  - Dense vectors (unified embedding)                            │
│  - BM25 sparse index (text only)                                │
│  - Payload: content_type, image_url, vlm_description            │
└─────────────────────────────────────────────────────────────────┘
                │
     ┌──────────┼──────────┬──────────┐
     ▼          ▼          ▼          ▼
  Text→Text  Text→Image  Image→Text  Image→Image
  (standard) (cross)     (cross)     (visual)
```

### Implemented Components

**1. UnifiedMultimodalEmbedding** ✅
```python
# src/services/knowledge/embedding.py
class UnifiedMultimodalEmbedding(BaseEmbedding):
    """Embed text and images into unified vector space."""
    models = ["tongyi-embedding-vision-plus", "multimodal-embedding-v1"]

    async def embed_texts(self, texts: List[str]) -> List[List[float]]
    async def embed_images(self, images: List[bytes]) -> List[List[float]]
    async def embed_mixed_batch(self, items: List[Dict]) -> List[UnifiedEmbeddingResult]
```

**2. DocumentImageExtractor** ✅
```python
# src/services/knowledge/ingestion/document_image_extractor.py
class DocumentImageExtractor:
    """Auto-extract images from PDF, DOCX, HTML."""
    SUPPORTED_TYPES = {"image/jpeg", "image/png", "image/gif"}
    MAX_SIZE = 2 * 1024 * 1024  # 2MB (Dify standard)

    async def extract_from_pdf(self, pdf_bytes) -> List[ExtractedImage]
    async def extract_from_docx(self, docx_bytes) -> List[ExtractedImage]
    async def extract_from_html(self, html_content) -> List[ExtractedImage]
```

### Files Modified/Created (Phase 2)
| File | Status | Changes |
|------|--------|---------|
| `src/services/knowledge/embedding.py` | ✅ Modified | Added `UnifiedMultimodalEmbedding` class (+409 lines) |
| `src/services/knowledge/ingestion/` | ✅ Created | New module for document image extraction |
| `src/api/v1/kb_tools.py` | ✅ Modified | Added multimodal routing |
| `src/api/schemas/kb_tools.py` | ✅ Modified | Added image search schemas |

---

## Part 2: LangGraph Agent Access (CRITICAL)

### Integration Strategy: Hybrid Approach

| Interface | Use Case | Implementation |
|-----------|----------|----------------|
| **HTTP API** | Universal, external agents, multi-language | Primary - `/api/v1/kb/search` |
| **SDK Wrapper** | Internal Python agents, minimal overhead | `create_kb_tool()` factory |
| **MCP Server** | Multi-server architectures, LangChain ecosystem | `mcp/kb_server.py` |

### Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                      LANGGRAPH AGENT                            │
│              (StateGraph + ToolNode pattern)                    │
└───────────────────────────┬─────────────────────────────────────┘
                            │
          ┌─────────────────┼─────────────────┐
          ▼                 ▼                 ▼
    ┌───────────┐     ┌───────────┐     ┌───────────┐
    │ HTTP API  │     │SDK Wrapper│     │MCP Client │
    │ (REST)    │     │ (Direct)  │     │ (SSE/HTTP)│
    └─────┬─────┘     └─────┬─────┘     └─────┬─────┘
          │                 │                 │
          │        ┌────────┴────────┐        │
          │        ▼                 │        ▼
          │  KnowledgeRetriever      │   MCP Server
          │  (enhanced)              │   (kb_server.py)
          │                          │        │
          └──────────┬───────────────┴────────┘
                     ▼
            ┌─────────────────┐
            │KnowledgeService │
            │ (unified layer) │
            └─────────────────┘
```

### 2.1 HTTP API (Primary Interface) ✅

```python
# src/api/v1/kb_tools.py

@router.post("/kb/search")
async def kb_search(request: KBSearchRequest) -> KBSearchResponse:
    """
    Universal KB search for LangGraph agents.

    Request:
        query: str              # Natural language query
        dataset_id: str         # Dataset to search
        top_k: int = 5          # Results count
        mode: str = "hybrid"    # hybrid | dense | bm25
        rerank: bool = False    # Enable reranking
        include_images: bool    # Include image results

    Response:
        results: List[KBSearchResult]
        formatted_context: str  # Pre-formatted for LLM
    """

@router.post("/kb/multi-search")
async def kb_multi_search(request: KBMultiSearchRequest) -> KBMultiSearchResponse:
    """Search across multiple datasets, merge and rank results."""
```

### 2.2 SDK Wrapper (LangChain-compatible) ✅

```python
# Enhanced src/services/knowledge/langgraph_tools.py

class KnowledgeRetrieverTool:
    """LangChain/LangGraph compatible tool."""

    name = "search_knowledge_base"
    description = """Search internal knowledge bases for relevant information.
    Use when you need facts, documentation, or context."""

    async def _arun(self, query: str) -> str:
        """LangChain async interface."""
        results = await self.retriever.retrieve(query)
        return self._format_results(results)

def create_kb_tool(
    kb_service: KnowledgeService,
    dataset_ids: List[str],
    user_context: UserContext,
) -> KnowledgeRetrieverTool:
    """Factory for creating KB tool."""
```

### Agent Usage Examples

**LangGraph Agent (SDK)**
```python
from agent_gateway.services.knowledge import create_kb_tool
from langgraph.graph import StateGraph, MessagesState
from langgraph.prebuilt import ToolNode

# Create KB tool
kb_tool = create_kb_tool(
    kb_service=kb_service,
    dataset_ids=["docs", "wiki"],
    user_context=user_context,
)

# Build graph
graph = StateGraph(MessagesState)
graph.add_node("agent", call_model)
graph.add_node("tools", ToolNode([kb_tool]))
graph.add_edge("__start__", "agent")
graph.add_conditional_edges("agent", should_continue)
graph.add_edge("tools", "agent")

agent = graph.compile()
result = await agent.ainvoke({"messages": [{"role": "user", "content": "What is our refund policy?"}]})
```

**External Agent (HTTP)**
```python
import httpx

async def search_kb(query: str, dataset_id: str) -> List[Dict]:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://gateway.internal/api/v1/kb/search",
            json={"query": query, "dataset_id": dataset_id, "top_k": 5},
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
        return response.json()["results"]
```

### Files Created/Modified (Phase 1)
| File | Status | Action |
|------|--------|--------|
| `src/api/v1/kb_tools.py` | ✅ Created | HTTP API endpoints |
| `src/api/schemas/kb_tools.py` | ✅ Created | Request/response schemas |
| `src/services/knowledge/langgraph_tools.py` | ✅ Modified | Enhanced with LangChain interface |
| `src/api/router.py` | ✅ Modified | Added kb_tools router |
| `docs/agent_integration.md` | ✅ Created | Documentation |
| `tests/api/test_kb_tools.py` | ✅ Created | Integration tests (31 tests) |

---

## Part 3: GPT-like Web Experience (Pending)

### Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    FRONTEND (React + TypeScript)                │
├─────────────────────────────────────────────────────────────────┤
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────────┐  │
│  │   Model     │  │    KB       │  │      Chat Interface     │  │
│  │  Selector   │  │  Selector   │  │  - Messages             │  │
│  │  ─────────  │  │  ─────────  │  │  - Streaming            │  │
│  │  GPT-4o     │  │  □ Docs     │  │  - Tool calls display   │  │
│  │  Claude 3.5 │  │  □ Wiki     │  │  - Context sources      │  │
│  │  DeepSeek   │  │  □ Policy   │  │  - Image attachments    │  │
│  │  Qwen       │  │  (multi)    │  │                         │  │
│  └─────────────┘  └─────────────┘  └─────────────────────────┘  │
├─────────────────────────────────────────────────────────────────┤
│                    CONFIGURATION PANEL                          │
│  [System Prompt] [Temperature: 0.7] [RAG: top_k=5, rerank=off]  │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼ SSE Streaming
┌─────────────────────────────────────────────────────────────────┐
│                      BACKEND API                                │
│  POST /assistant/chat/stream                                    │
│  GET  /assistant/models                                         │
│  GET  /assistant/sessions                                       │
│  POST /assistant/sessions                                       │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    ASSISTANT SERVICE                            │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │  1. Auto-retrieve KB context (if datasets selected)       │  │
│  │  2. Build messages: system prompt + context + history     │  │
│  │  3. Route to selected model (GPT/Claude/DeepSeek/Qwen)    │  │
│  │  4. Stream response with tool call handling               │  │
│  │  5. Persist to session                                    │  │
│  └───────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

### AssistantService

```python
# src/services/assistant/assistant_service.py

@dataclass
class AssistantConfig:
    model_provider: str = "openai"      # openai | anthropic | deepseek | dashscope
    model_name: str = "gpt-4o"
    temperature: float = 0.7
    kb_dataset_ids: List[str] = []      # Selected KBs
    kb_auto_retrieve: bool = True       # Auto-RAG on each message
    kb_top_k: int = 5
    system_prompt: Optional[str] = None
    tools_enabled: List[str] = []       # kb_search, web_search, etc.

class AssistantService:
    async def chat_stream(
        self,
        user: UserContext,
        session_id: str,
        message: str,
        config: AssistantConfig,
    ) -> AsyncIterator[AssistantStreamEvent]:
        """
        Stream events:
        - context_retrieved: KB results for display
        - text_delta: Streaming text chunk
        - tool_call: Tool invocation
        - tool_result: Tool response
        - done: Completion with usage
        """
```

### KB Selection Runtime Behavior

When user selects Knowledge Bases in the UI:
1. **Auto-RAG Mode** (default): Each message triggers KB retrieval, context injected into prompt
2. **Tool Mode**: KB exposed as callable tool, agent decides when to search
3. **Hybrid Mode**: Initial context retrieval + tool for follow-up queries

### Files to Create (Phase 3)
| File | Purpose |
|------|---------|
| `src/services/assistant/assistant_service.py` | Core assistant logic |
| `src/services/assistant/model_registry.py` | Multi-model routing |
| `src/api/v1/assistant.py` | API endpoints |
| `web/src/pages/assistant/AssistantPage.tsx` | Main UI |
| `web/src/components/assistant/ModelSelector.tsx` | Model dropdown |
| `web/src/components/assistant/DatasetSelector.tsx` | KB multi-select |

---

## Part 4: MCP Server (Pending)

### MCP Server (For External Agents)

```python
# src/mcp/kb_server.py
from mcp import Server, Tool

kb_server = Server("knowledge-base")

@kb_server.tool()
async def search_knowledge(
    query: str,
    dataset_id: str,
    top_k: int = 5,
) -> list[TextContent]:
    """Search the knowledge base for relevant information."""

@kb_server.tool()
async def list_datasets() -> list[TextContent]:
    """List available knowledge base datasets."""
```

**MCP Client (LangChain)**
```python
from langchain_mcp_adapters.client import MultiServerMCPClient

client = MultiServerMCPClient({
    "knowledge": {
        "transport": "http",
        "url": "http://gateway:8080/mcp",
    }
})
tools = await client.get_tools()
agent = create_agent("gpt-4o", tools)
```

---

## Implementation Phases Summary

> **User Decision**: Phase order is **P1 -> P2 -> P3 -> P4**
> - Primary interface: **HTTP API** for LangGraph agents
> - GPT UI: **New standalone /assistant page**

### Phase 1: LangGraph Agent Access ✅ COMPLETED
| Task | Files | Status |
|------|-------|--------|
| Create HTTP KB API (PRIMARY) | `api/v1/kb_tools.py`, `schemas/kb_tools.py` | ✅ |
| Enhance SDK wrapper | `langgraph_tools.py` | ✅ |
| Add LangChain tool interface | `langgraph_tools.py` | ✅ |
| Documentation & examples | `docs/agent_integration.md` | ✅ |
| Integration tests | `tests/api/test_kb_tools.py` | ✅ (31 tests) |

### Phase 2: Multimodal RAG Enhancement ✅ COMPLETED
| Task | Files | Status |
|------|-------|--------|
| Add unified embedding support | `embedding.py` | ✅ |
| DocumentImageExtractor | `ingestion/document_image_extractor.py` | ✅ |
| Cross-modal API routing | `kb_tools.py` | ✅ |
| API schema updates | `schemas/kb_tools.py` | ✅ |

### Phase 3: GPT-like Assistant Experience 🔲 PENDING
| Task | Files | Effort |
|------|-------|--------|
| AssistantService | `services/assistant/assistant_service.py` | 3 days |
| Model registry/routing | `services/assistant/model_registry.py` | 2 days |
| API endpoints | `api/v1/assistant.py` | 2 days |
| **New AssistantPage** | `web/src/pages/assistant/AssistantPage.tsx` | 4 days |
| Model & KB selectors | `web/src/components/assistant/` | 2 days |
| Session integration | Existing session system | 1 day |

### Phase 4: MCP Server & Polish 🔲 PENDING
| Task | Files | Effort |
|------|-------|--------|
| MCP server (secondary interface) | `mcp/kb_server.py` | 2 days |
| Integration testing | Tests | 2 days |
| Performance optimization | Various | 1 day |

---

## Key Recommendations

### 1. Embedding Strategy
- **Default to unified embedding** for new datasets (Dify 1.11 approach)
- Keep separate embedding option for legacy/specialized needs
- Model: `tongyi-embedding-vision-plus` (1024D, text+image)

### 2. LangGraph Integration Priority
- **HTTP API first** - Universal, works for all agent types
- **SDK wrapper** - For same-process Python agents
- **MCP server** - Lower priority, for multi-server scenarios

### 3. Multimodal Reranking
- Make VLM reranking **opt-in** (`multimodal_rerank=true`)
- High latency/cost trade-off for quality
- Default: text-only reranking

### 4. GPT Web UI
- Leverage existing `LangGraphAdapter` for model routing
- Align session management with existing `SessionManager`
- Use SSE for streaming (matches current patterns)

---

## Verification Plan

### Part 1: Multimodal RAG ✅
- [x] UnifiedMultimodalEmbedding class implemented
- [x] DocumentImageExtractor for PDF/DOCX/HTML
- [x] API routing for multimodal queries
- [ ] Upload PDF with embedded images, verify auto-extraction
- [ ] Query with text, receive relevant images in results
- [ ] Query with image, receive relevant text in results

### Part 2: LangGraph Access ✅
- [x] HTTP API endpoints created
- [x] SDK wrapper enhanced
- [x] 31 tests passing
- [x] Documentation created
- [ ] Create LangGraph agent using `create_kb_tool()`
- [ ] Agent successfully retrieves from KB during conversation

### Part 3: GPT-like Experience 🔲
- [ ] Select model, select KBs, send message
- [ ] Streaming response with context sources displayed
- [ ] Switch models mid-conversation
- [ ] Session persistence across page refresh

---

## Sources

- [Dify v1.11.1 Multimodal Knowledge Base](https://forum.dify.ai/t/dify-v1-11-1-multimodal-knowledge-base-is-live/371)
- [LangChain MCP Adapters](https://github.com/langchain-ai/langchain-mcp-adapters)
- [LangChain MCP Documentation](https://docs.langchain.com/oss/python/langchain/mcp)
- [LangGraph Agentic RAG Tutorial](https://docs.langchain.com/oss/python/langgraph/agentic-rag)
