from fastapi import APIRouter

from .v1.config import router as config_router
from .v1.conversations import router as conversations_router
from .v1.health import router as health_router
from .v1.invoke import router as invoke_router
from .v1.langgraph import router as langgraph_router
from .v1.knowledge import router as knowledge_router
from .v1.proxy import router as proxy_router
from .v1.services import router as services_router
from .v1.sessions import router as sessions_router
from .v1.stream import router as stream_router
from .v1.submit import router as submit_router
from .v1.tasks import router as tasks_router

api_router = APIRouter()
api_router.include_router(invoke_router)
api_router.include_router(stream_router)
api_router.include_router(submit_router)
api_router.include_router(tasks_router)
api_router.include_router(sessions_router)
api_router.include_router(services_router)
api_router.include_router(health_router)
api_router.include_router(config_router)
api_router.include_router(conversations_router)  # 简化的对话 API
api_router.include_router(langgraph_router)  # LangGraph 官方 API 代理（向后兼容）
api_router.include_router(proxy_router)  # 透明代理路由
api_router.include_router(knowledge_router)  # KBMS Knowledge Base
