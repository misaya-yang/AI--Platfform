"""Shared FastAPI dependencies for route handlers."""

from __future__ import annotations

from fastapi import HTTPException, Request

from ..core import AssistantService


def get_assistant_service(request: Request) -> AssistantService:
    svc = getattr(request.app.state, "assistant_service", None)
    if svc is None:
        raise HTTPException(503, "Assistant service not ready")
    return svc


def get_database(request: Request):
    return getattr(request.app.state, "database", None)


def get_session_manager(request: Request):
    return getattr(request.app.state, "session_manager", None)


def get_model_registry(request: Request):
    return getattr(request.app.state, "model_registry", None)
