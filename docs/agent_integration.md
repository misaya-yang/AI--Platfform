# Knowledge Base Integration for LangGraph Agents

This guide explains how to integrate the Knowledge Base with LangGraph agents.

## Quick Start

### HTTP API (Recommended)

The simplest way for agents to access the KB is via the HTTP API:

```python
import httpx

async def search_kb(query: str, dataset_id: str, top_k: int = 5) -> dict:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "http://localhost:8000/v1/kb/search",
            json={
                "query": query,
                "dataset_id": dataset_id,
                "top_k": top_k,
            },
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
        return response.json()

# Example usage
results = await search_kb("What is our refund policy?", "company-docs")
print(results["formatted_context"])  # Pre-formatted for LLM
```

### SDK Tool (For Python Agents)

For agents running in the same process as the gateway:

```python
from agent_gateway.services.knowledge import create_kb_tool
from langgraph.prebuilt import ToolNode
from langgraph.graph import StateGraph, MessagesState

# Create KB tool
kb_tool = create_kb_tool(
    kb_service,
    "company-docs",
    user_context,
    top_k=5,
    mode="hybrid",
)

# Use with LangGraph
tool_node = ToolNode([kb_tool])

# Build your graph
graph = StateGraph(MessagesState)
graph.add_node("agent", call_model)
graph.add_node("tools", tool_node)
# ... add edges
```

## API Reference

### POST /v1/kb/search

Search a single knowledge base.

**Request:**
```json
{
    "query": "What is our refund policy?",
    "dataset_id": "company-docs",
    "top_k": 5,
    "mode": "auto",
    "rerank": false,
    "mmr": false
}
```

**Response:**
```json
{
    "results": [
        {
            "content": "Our refund policy allows...",
            "score": 0.85,
            "segment_id": "seg_123",
            "document_id": "doc_456",
            "dataset_id": "company-docs",
            "content_type": "text",
            "metadata": {}
        }
    ],
    "formatted_context": "[1] (score: 0.850)\nOur refund policy allows...",
    "query": "What is our refund policy?",
    "dataset_id": "company-docs",
    "total_results": 1
}
```

### POST /v1/kb/multi-search

Search across multiple knowledge bases.

**Request:**
```json
{
    "query": "How do I reset my password?",
    "dataset_ids": ["docs", "wiki", "faq"],
    "top_k": 5,
    "merge_strategy": "score"
}
```

### GET /v1/kb/datasets

List available datasets.

### GET /v1/kb/tool-definition/{dataset_id}

Get OpenAI function calling definition for a dataset.

## SDK Reference

### create_kb_tool()

Create a knowledge base search tool for LangGraph agents.

```python
from agent_gateway.services.knowledge import create_kb_tool

kb_tool = create_kb_tool(
    knowledge_service=kb_service,
    dataset_id="company-docs",
    user_context=user_context,
    name="search_docs",              # Optional custom name
    description="Search docs...",     # Optional custom description
    top_k=5,                         # Default results count
    mode="hybrid",                   # hybrid | dense | bm25
    rerank=False,                    # Enable reranking
    mmr=False,                       # Enable MMR diversity
)
```

### create_multi_kb_tool()

Create a tool that searches multiple knowledge bases.

```python
from agent_gateway.services.knowledge import create_multi_kb_tool

kb_tool = create_multi_kb_tool(
    knowledge_service=kb_service,
    dataset_ids=["docs", "wiki", "faq"],
    user_context=user_context,
)
```

## LangGraph Integration Examples

### Basic RAG Agent

```python
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, MessagesState, START, END
from langgraph.prebuilt import ToolNode, tools_condition
from agent_gateway.services.knowledge import create_kb_tool

# 1. Create KB tool
kb_tool = create_kb_tool(kb_service, "docs", user_context)

# 2. Create LLM with tool binding
llm = ChatOpenAI(model="gpt-4o").bind_tools([kb_tool])

# 3. Define agent node
async def call_model(state: MessagesState):
    response = await llm.ainvoke(state["messages"])
    return {"messages": [response]}

# 4. Build graph
graph = StateGraph(MessagesState)
graph.add_node("agent", call_model)
graph.add_node("tools", ToolNode([kb_tool]))

graph.add_edge(START, "agent")
graph.add_conditional_edges("agent", tools_condition)
graph.add_edge("tools", "agent")

# 5. Compile and run
agent = graph.compile()
result = await agent.ainvoke({
    "messages": [{"role": "user", "content": "What is our refund policy?"}]
})
```

### Multi-Dataset Agent

```python
from agent_gateway.services.knowledge import create_multi_kb_tool

# Create tool that searches multiple KBs
kb_tool = create_multi_kb_tool(
    kb_service,
    ["company-docs", "product-wiki", "support-faq"],
    user_context,
)

# The agent can now search all datasets or specify one
# LLM will receive:
# - dataset_id enum with available options
# - Can search all by omitting dataset_id
```

### HTTP Client Integration

For agents not running in the same process:

```python
import httpx
from langchain.tools import tool

@tool
async def search_knowledge_base(query: str, dataset_id: str = "docs") -> str:
    """Search the knowledge base for relevant information."""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "http://gateway:8000/v1/kb/search",
            json={"query": query, "dataset_id": dataset_id, "top_k": 5},
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
        data = response.json()
        return data.get("formatted_context", "No results found")

# Use in LangGraph
tools = [search_knowledge_base]
```

## Best Practices

1. **Use `formatted_context`**: The API returns pre-formatted context strings optimized for LLM consumption. Use these directly in your prompts.

2. **Choose appropriate `mode`**:
   - `auto`: Let the system decide (recommended)
   - `hybrid`: Best for most queries
   - `dense`: Better for semantic/conceptual queries
   - `bm25`: Better for exact keyword matching

3. **Enable `rerank` for quality**: If response quality is critical and latency is acceptable, enable reranking.

4. **Use `mmr` for diversity**: If you need diverse results (not just the top-scoring similar results), enable MMR.

5. **Multi-dataset strategy**: Use `create_multi_kb_tool()` when your agent needs to search across multiple knowledge sources.

## Authentication

All KB API endpoints require authentication:

- **API Key**: Pass in `X-API-Key` header
- **JWT**: Pass in `Authorization: Bearer <token>` header

For SDK tools, pass the `user_context` from the request handler.
