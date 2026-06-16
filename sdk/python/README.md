# AI Gateway Assistant SDK

Python SDK for AI Gateway — chat, streaming, knowledge base, image generation, and more.

## Install

```bash
pip install ai-gateway-sdk
```

## Quick Start

```python
from ai_assistant import AssistantClient

async with AssistantClient(api_key="your-key", base_url="http://localhost:8080") as client:
    # Simple chat
    response = await client.chat.send("Summarize the onboarding checklist")
    print(response.content)

    # Streaming
    async for event in client.chat.stream("Explain our refund policy"):
        if event.event_type == "text_delta":
            print(event.data, end="", flush=True)

    # Knowledge base search + chat
    response = await client.knowledge.ask(
        "Compare the free and enterprise plan limits",
        dataset_ids=["product-docs"]
    )

    # Image generation
    result = await client.images.generate("a clean dashboard hero image")
```

## Features

- **Streaming SSE** — `async for event in client.chat.stream()` with 73 typed event types
- **Knowledge Base** — RAG-powered Q&A over your datasets
- **Sub-Agents** — Parallel explore/task/plan agents
- **Image Generation** — Gemini + DashScope with auto-routing
- **Artifacts** — File upload/download with S3 storage
- **Sessions** — Multi-turn conversation management
- **Tools** — 15+ built-in tools + MCP integration

## Requirements

- Python 3.11+
- `httpx` (auto-installed)
