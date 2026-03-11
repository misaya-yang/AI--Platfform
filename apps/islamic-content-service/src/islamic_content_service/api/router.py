from fastapi import APIRouter

from .routes.hadith import router as hadith_router
from .routes.health import router as health_router
from .routes.meta import router as meta_router
from .routes.quran import router as quran_router
from .routes.quran_user import router as quran_user_router

api_router = APIRouter()
api_router.include_router(health_router)
api_router.include_router(meta_router, prefix="/api/v1")
api_router.include_router(quran_router, prefix="/api/v1")
api_router.include_router(quran_user_router, prefix="/api/v1")
api_router.include_router(hadith_router, prefix="/api/v1")
