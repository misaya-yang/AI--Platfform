"""
Assistant Service — standalone FastAPI app for AI assistant functionality.

This is the microservice entry point. It exposes the same /api/v1/assistant/*
endpoints as the Gateway, but runs independently on port 8093.

In Phase 3 deployment:
  Gateway (port 8080) → RemoteAssistantClient → this service (port 8093)

Environment Variables:
  DATABASE_URL    — PostgreSQL connection string
  REDIS_URL       — Redis connection string (optional)
  QDRANT_URL      — Qdrant vector store URL (for KB retrieval)
  TAVILY_API_KEY  — Web search API key (optional)
  LLM_PROVIDERS   — JSON config for LLM providers

This file is a thin wrapper that reuses the existing assistant module.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time

# Add the gateway src to path so we can reuse its modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

logger = logging.getLogger("assistant-service")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")

app = FastAPI(
    title="AI Assistant Service",
    description="Extracted assistant microservice from AI Gateway",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "assistant"}


@app.get("/api/v1/health")
async def api_health():
    return {"status": "ok", "service": "assistant"}


@app.on_event("startup")
async def startup():
    """Initialize assistant service components.

    This mirrors the Gateway's _init_assistant_service() but runs standalone.
    Connects to the same PostgreSQL, Redis, and LLM providers.
    """
    logger.info("Assistant Service starting up...")

    # This is a placeholder — full initialization will be implemented
    # when Phase 3 deployment is activated. For now, the service
    # just runs health checks to prove the container works.
    #
    # Full init requires:
    # 1. Database connection (shared PostgreSQL)
    # 2. Redis connection
    # 3. ModelRegistry with LLM providers
    # 4. KnowledgeService or KB proxy client
    # 5. AssistantService instantiation
    # 6. ToolRegistry setup

    logger.info("Assistant Service ready (stub mode — full init pending Phase 3 activation)")


@app.on_event("shutdown")
async def shutdown():
    logger.info("Assistant Service shutting down...")


# Error handler
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled error: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"error": {"message": "Internal server error", "service": "assistant"}},
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8093)
