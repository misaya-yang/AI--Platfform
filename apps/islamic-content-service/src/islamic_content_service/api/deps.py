from __future__ import annotations

from fastapi import Request

from ..cache import RedisCache
from ..config import Settings
from ..services.bootstrap_service import BootstrapService
from ..services.dua_query_service import DuaQueryService
from ..services.hadith_query_service import HadithQueryService
from ..services.quran_query_service import QuranQueryService
from ..services.quran_user_service import QuranUserService


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_bootstrap_service(request: Request) -> BootstrapService:
    return request.app.state.bootstrap_service


def get_quran_query_service(request: Request) -> QuranQueryService:
    return request.app.state.quran_query_service


def get_hadith_query_service(request: Request) -> HadithQueryService:
    return request.app.state.hadith_query_service


def get_quran_user_service(request: Request) -> QuranUserService:
    return request.app.state.quran_user_service


def get_dua_query_service(request: Request) -> DuaQueryService:
    return request.app.state.dua_query_service


def get_cache(request: Request) -> RedisCache:
    return request.app.state.cache


def get_wahda_service(request: Request):
    from ..services.wahda_service import WahdaService
    return request.app.state.wahda_service
