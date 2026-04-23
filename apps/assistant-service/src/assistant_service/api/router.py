"""Assistant Service API Router — aggregates all route modules."""

from fastapi import APIRouter

from .routes.chat import router as chat_router
from .routes.sessions import router as sessions_router
from .routes.models import router as models_router
from .routes.tools import router as tools_router
from .routes.runs_approvals import router as runs_approvals_router

router = APIRouter()

router.include_router(chat_router, tags=["Chat"])
router.include_router(sessions_router, tags=["Sessions"])
router.include_router(models_router, tags=["Models"])
router.include_router(tools_router, tags=["Tools"])
router.include_router(runs_approvals_router, tags=["Runs+Approvals"])
