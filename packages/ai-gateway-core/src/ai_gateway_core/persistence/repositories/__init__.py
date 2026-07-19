"""
数据仓库层

提供各实体的数据访问抽象，支持内存和数据库两种存储后端
"""

from .agent_repository import DatabaseAgentRepository
from .agent_resource_resolver import DatabaseAgentKnowledgeResolver
from .api_key_repository import APIKeyRepository, DatabaseAPIKeyRepository
from .base import BaseRepository
from .knowledge_repository import DatabaseKnowledgeRepository, KnowledgeRepository
from .mcp_repository import DatabaseMCPRepository
from .service_repository import DatabaseServiceRepository, ServiceRepository
from .session_repository import DatabaseSessionRepository, SessionRepository
from .task_repository import DatabaseTaskRepository, TaskRepository
from .user_repository import DatabaseUserRepository, UserRepository

__all__ = [
    "BaseRepository",
    "DatabaseAgentRepository",
    "DatabaseAgentKnowledgeResolver",
    "ServiceRepository",
    "DatabaseServiceRepository",
    "SessionRepository",
    "DatabaseSessionRepository",
    "TaskRepository",
    "DatabaseTaskRepository",
    "UserRepository",
    "DatabaseUserRepository",
    "APIKeyRepository",
    "DatabaseAPIKeyRepository",
    "KnowledgeRepository",
    "DatabaseKnowledgeRepository",
    "DatabaseMCPRepository",
]
