"""
Assistant Service — standalone FastAPI microservice.

Runs independently on port 8093. Exposes:
  POST /api/v1/assistant/chat        — non-streaming chat
  POST /api/v1/assistant/chat/stream — SSE streaming chat
  GET  /api/v1/assistant/models      — list available models
  GET  /health                       — health check

Environment Variables (same as gateway):
  DATABASE_URL       — PostgreSQL (required)
  REDIS_URL          — Redis (optional)
  DASHSCOPE_API_KEY  — Qwen models
  OPENAI_API_KEY     — OpenAI models
  ANTHROPIC_API_KEY  — Claude models
  DEEPSEEK_API_KEY   — DeepSeek models
  GEMINI_API_KEY     — Google Gemini
  TAVILY_API_KEY     — Web search
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass
from typing import Any

# Ensure the gateway src is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

logger = logging.getLogger("assistant-service")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")

app = FastAPI(title="AI Assistant Service", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Request/Response schemas
# ---------------------------------------------------------------------------

class ChatMessage(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    model_id: str = "qwen3.5-plus"
    temperature: float = 0.7
    max_tokens: int | None = None
    kb_dataset_ids: list[str] | None = None
    kb_mode: str = "auto"
    web_search_enabled: bool = False
    system_prompt: str | None = None
    history: list[ChatMessage] | None = None
    enable_task_planning: bool = False
    memory_mode: str | None = None
    # User context forwarded from gateway
    _user_context: dict | None = None


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
@app.get("/api/v1/health")
async def health():
    ready = getattr(app.state, "_ready", False)
    return {"status": "ok" if ready else "starting", "service": "assistant"}


# ---------------------------------------------------------------------------
# Startup — initialize AssistantService
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup():
    logger.info("Assistant Service starting...")

    from src.services.assistant import AssistantService, ModelProvider, ModelRegistry
    from src.services.assistant.tools import (
        TavilySearchTool,
        get_tool_registry,
        register_builtin_tools,
        register_document_generation_tool,
        register_pptx_generation_tool,
    )

    # --- Database ---
    db_url = os.environ.get("DATABASE_URL", "")
    database = None
    session_manager = None
    if db_url:
        try:
            from src.persistence.database import DatabaseStorage
            database = DatabaseStorage(db_url, enabled=True, auto_init=False)
            await database.connect()
            logger.info("Database connected")

            from src.services.session.database_session_manager import DatabaseSessionManager
            session_manager = DatabaseSessionManager(database)
        except Exception as e:
            logger.warning(f"Database init failed: {e}")

    # --- Redis ---
    redis_client = None
    redis_url = os.environ.get("REDIS_URL", "")
    if redis_url:
        try:
            import redis.asyncio as aioredis
            redis_client = aioredis.from_url(redis_url, decode_responses=True)
            await redis_client.ping()
            logger.info("Redis connected")
        except Exception as e:
            logger.warning(f"Redis init failed: {e}")

    # --- Model Registry ---
    model_registry = ModelRegistry()
    PROVIDERS = {
        "openai": ("OPENAI_API_KEY", "OPENAI_BASE_URL", "https://api.openai.com"),
        "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
        "deepseek": ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        "dashscope": ("DASHSCOPE_API_KEY", None, "https://dashscope.aliyuncs.com/compatible-mode"),
        "google": ("GEMINI_API_KEY", None, "https://generativelanguage.googleapis.com"),
    }
    for pid, (env_key, env_url, default_url) in PROVIDERS.items():
        api_key = os.environ.get(env_key, "")
        if pid == "google" and not api_key:
            api_key = os.environ.get("GOOGLE_API_KEY", "")
        if api_key:
            try:
                base_url = os.environ.get(env_url) if env_url else None
                model_registry.configure_provider(
                    ModelProvider(pid),
                    api_key=api_key,
                    base_url=base_url or default_url,
                )
                logger.info(f"Provider {pid} configured")
            except Exception as e:
                logger.warning(f"Provider {pid} failed: {e}")

    # --- Tavily (web search) ---
    tavily_key = os.environ.get("TAVILY_API_KEY", "")
    tavily_tool = TavilySearchTool(api_key=tavily_key or None)

    # --- KB Proxy (connect to kb-service over HTTP) ---
    kb_proxy = None
    kb_url = os.environ.get("KB_SERVICE_URL", "http://knowledge-service:8092")
    try:
        from src.services.knowledge.kb_proxy_client import KBProxyClient
        kb_proxy = KBProxyClient(base_url=kb_url)
        logger.info(f"KB proxy → {kb_url}")
    except Exception as e:
        logger.warning(f"KB proxy init failed: {e}")

    # --- Memory Service ---
    memory_service = None
    if database:
        try:
            from src.services.assistant.memory_service import MemoryService
            memory_service = MemoryService(database)
            logger.info("Memory service initialized")
        except Exception as e:
            logger.warning(f"Memory service init failed: {e}")

    # --- AssistantService ---
    assistant_service = AssistantService(
        model_registry=model_registry,
        kb_service=None,
        kb_proxy=kb_proxy,
        tavily_api_key=tavily_key or None,
        session_manager=session_manager,
        redis_client=redis_client,
        memory_service=memory_service,
        db=database,
    )

    # --- Tool Registry ---
    tool_registry = get_tool_registry()
    register_builtin_tools(
        kb_service=kb_proxy, tavily_tool=tavily_tool, memory_service=memory_service,
    )
    register_document_generation_tool()
    try:
        register_pptx_generation_tool()
    except Exception:
        pass

    # Register quiz tool
    try:
        from src.services.assistant.tools.quiz_tool import register_quiz_tool
        register_quiz_tool(
            kb_service=None, model_registry=model_registry,
            database=database, kb_proxy=kb_proxy,
        )
    except Exception as e:
        logger.warning(f"Quiz tool registration failed: {e}")

    # --- Load models from database (bypass DatabaseStorage, use raw asyncpg) ---
    logger.info(f"DB pool check: database={database is not None}, has_pool={hasattr(database, '_pool') if database else 'N/A'}, pool={getattr(database, '_pool', None) is not None if database else 'N/A'}")
    if database and getattr(database, '_pool', None):
        try:
            async with database._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT model_id, display_name, provider_id, context_window, max_output_tokens, "
                    "access_level FROM llm_models WHERE tenant_id = $1 AND is_enabled = true",
                    "default",
                )
                if rows:
                    from src.services.assistant.models.model_registry import ModelInfo, ModelProvider
                    model_registry._models.clear()
                    for r in rows:
                        try:
                            provider = ModelProvider(r["provider_id"])
                            model_registry._models[r["model_id"]] = ModelInfo(
                                id=r["model_id"],
                                provider=provider,
                                display_name=r["display_name"] or r["model_id"],
                                context_window=r["context_window"] or 32000,
                                max_output_tokens=r["max_output_tokens"] or 4096,
                                access_level=r["access_level"] or "public",
                            )
                        except ValueError:
                            pass  # Unknown provider
                    logger.info(f"Loaded {len(rows)} models from DB: {list(model_registry._models.keys())}")
                else:
                    logger.info("No models in database, keeping defaults")
        except Exception as e:
            logger.warning(f"Failed to load models from DB: {e}")

    # Store on app state
    app.state.assistant_service = assistant_service
    app.state.model_registry = model_registry
    app.state.database = database
    app.state._ready = True
    logger.info("Assistant Service ready")


@app.on_event("shutdown")
async def shutdown():
    db = getattr(app.state, "database", None)
    if db:
        await db.close()
    logger.info("Assistant Service shut down")


# ---------------------------------------------------------------------------
# Helper: resolve user context from forwarded payload
# ---------------------------------------------------------------------------

@dataclass
class UserCtx:
    user_id: str = "anonymous"
    tenant_id: str = "default"
    roles: list[str] | None = None
    tier: str = "normal"
    is_authenticated: bool = True
    ip: str = ""


def _resolve_user(body: dict) -> UserCtx:
    uc = body.pop("_user_context", None) or {}
    return UserCtx(
        user_id=uc.get("user_id", "anonymous"),
        tenant_id=uc.get("tenant_id", "default"),
        roles=uc.get("roles", []),
        tier=uc.get("tier", "normal"),
    )


# ---------------------------------------------------------------------------
# Chat endpoints
# ---------------------------------------------------------------------------

@app.post("/api/v1/assistant/chat")
async def chat(request: Request):
    body = await request.json()
    user = _resolve_user(body)
    svc = app.state.assistant_service

    from src.services.assistant.assistant_service import AssistantConfig, RAGMode
    from src.services.assistant.models.model_registry import ModelProvider

    model_id = body.get("model_id", "qwen3.5-plus")
    kb_mode_str = body.get("kb_mode", "auto")
    kb_mode = RAGMode.TOOL if kb_mode_str == "tool" else RAGMode.DISABLED if kb_mode_str == "off" else RAGMode.AUTO

    model_provider = ModelProvider.OPENAI
    mr = app.state.model_registry
    mi = mr.get_model(model_id) if mr else None
    if mi:
        model_provider = mi.provider

    config = AssistantConfig(
        model_provider=model_provider,
        model_id=model_id,
        temperature=body.get("temperature", 0.7),
        max_tokens=body.get("max_tokens"),
        kb_dataset_ids=body.get("kb_dataset_ids"),
        kb_mode=kb_mode,
        web_search_enabled=body.get("web_search_enabled", False),
        system_prompt=body.get("system_prompt"),
        enable_task_planning=body.get("enable_task_planning", False),
        memory_mode=body.get("memory_mode"),
    )

    history = None
    if body.get("history"):
        history = [{"role": m["role"], "content": m["content"]} for m in body["history"]]

    session_id = body.get("session_id") or str(uuid.uuid4())

    try:
        result = await svc.chat(
            user=user, session_id=session_id, message=body["message"],
            config=config, history=history,
        )
        return {
            "content": result["content"],
            "usage": result.get("usage"),
            "contexts": result.get("contexts"),
            "duration_ms": result.get("duration_ms"),
            "model_id": result.get("model_id"),
            "session_id": session_id,
            "run_id": result.get("run_id"),
        }
    except Exception as e:
        logger.error(f"Chat failed: {e}", exc_info=True)
        raise HTTPException(500, f"Chat failed: {e}")


@app.post("/api/v1/assistant/chat/stream")
async def chat_stream(request: Request):
    body = await request.json()
    user = _resolve_user(body)
    svc = app.state.assistant_service

    from src.services.assistant.assistant_service import AssistantConfig, RAGMode
    from src.services.assistant.models.model_registry import ModelProvider

    model_id = body.get("model_id", "qwen3.5-plus")
    kb_mode_str = body.get("kb_mode", "auto")
    kb_mode = RAGMode.TOOL if kb_mode_str == "tool" else RAGMode.DISABLED if kb_mode_str == "off" else RAGMode.AUTO

    model_provider = ModelProvider.OPENAI
    mr = app.state.model_registry
    mi = mr.get_model(model_id) if mr else None
    if mi:
        model_provider = mi.provider

    config = AssistantConfig(
        model_provider=model_provider,
        model_id=model_id,
        temperature=body.get("temperature", 0.7),
        max_tokens=body.get("max_tokens"),
        kb_dataset_ids=body.get("kb_dataset_ids"),
        kb_mode=kb_mode,
        web_search_enabled=body.get("web_search_enabled", False),
        system_prompt=body.get("system_prompt"),
        enable_task_planning=body.get("enable_task_planning", False),
        memory_mode=body.get("memory_mode"),
    )

    history = None
    if body.get("history"):
        history = [{"role": m["role"], "content": m["content"]} for m in body["history"]]

    session_id = body.get("session_id") or str(uuid.uuid4())

    async def event_generator():
        try:
            async for event in svc.chat_stream(
                user=user, session_id=session_id, message=body["message"],
                config=config, history=history,
            ):
                event_data = {
                    "event_type": event.event_type,
                    "data": event.data,
                    "timestamp": event.timestamp,
                }
                yield f"data: {json.dumps(event_data, ensure_ascii=False)}\n\n"
        except Exception as e:
            logger.error(f"Stream error: {e}", exc_info=True)
            yield f"data: {json.dumps({'event_type': 'error', 'data': {'message': str(e)}, 'timestamp': time.time()})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@app.get("/api/v1/assistant/models")
async def list_models():
    svc = getattr(app.state, "assistant_service", None)
    if not svc:
        return {"models": []}
    return {"models": svc.get_available_models()}


# Error handler
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled: {exc}", exc_info=True)
    return JSONResponse(status_code=500, content={"error": {"message": "Internal server error"}})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8093)
