from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from ..deps import get_quran_user_service
from ..schemas.quran_user import (
    QuranUserAuthConfigResponse,
    QuranUserAuthorizeUrlResponse,
    QuranUserInfoResponse,
    QuranUserProxyRequest,
    QuranUserProxyResponse,
    QuranUserRefreshRequest,
    QuranUserTokenRequest,
    QuranUserTokenResponse,
)
from ...domain.errors import NotReadyError, UpstreamAPIError
from ...services.quran_user_service import QuranUserService

router = APIRouter(prefix="/quran/user", tags=["Quran User"])


def _to_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, NotReadyError):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, UpstreamAPIError):
        return HTTPException(status_code=502, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


@router.get(
    "/auth/config",
    response_model=QuranUserAuthConfigResponse,
    summary="Get Quran user OAuth and API configuration metadata",
)
async def get_auth_config(
    service: QuranUserService = Depends(get_quran_user_service),
):
    try:
        return await service.get_auth_config()
    except Exception as exc:
        raise _to_http_error(exc) from exc


@router.get(
    "/auth/authorize-url",
    response_model=QuranUserAuthorizeUrlResponse,
    summary="Build Quran OAuth authorization URL for PKCE login",
)
async def get_authorize_url(
    redirect_uri: str | None = Query(default=None),
    state: str | None = Query(default=None),
    code_challenge: str | None = Query(default=None),
    code_challenge_method: str = Query(default="S256"),
    scopes: list[str] | None = Query(default=None),
    service: QuranUserService = Depends(get_quran_user_service),
):
    try:
        return await service.get_authorize_url(
            redirect_uri=redirect_uri,
            state=state,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            scopes=scopes,
        )
    except Exception as exc:
        raise _to_http_error(exc) from exc


@router.post(
    "/auth/token",
    response_model=QuranUserTokenResponse,
    summary="Exchange Quran OAuth authorization code for access token",
)
async def exchange_token(
    request: QuranUserTokenRequest,
    service: QuranUserService = Depends(get_quran_user_service),
):
    try:
        return await service.exchange_code(
            code=request.code,
            redirect_uri=request.redirect_uri,
            code_verifier=request.code_verifier,
        )
    except Exception as exc:
        raise _to_http_error(exc) from exc


@router.post(
    "/auth/refresh",
    response_model=QuranUserTokenResponse,
    summary="Refresh Quran OAuth access token",
)
async def refresh_token(
    request: QuranUserRefreshRequest,
    service: QuranUserService = Depends(get_quran_user_service),
):
    try:
        return await service.refresh_token(request.refresh_token)
    except Exception as exc:
        raise _to_http_error(exc) from exc


@router.get(
    "/userinfo",
    response_model=QuranUserInfoResponse,
    summary="Get Quran OAuth userinfo from access token",
)
async def get_userinfo(
    authorization: str = Header(..., description="Bearer access token"),
    service: QuranUserService = Depends(get_quran_user_service),
):
    try:
        token = authorization.replace("Bearer ", "", 1).strip()
        return await service.get_userinfo(token)
    except Exception as exc:
        raise _to_http_error(exc) from exc


@router.post(
    "/request",
    response_model=QuranUserProxyResponse,
    summary="Proxy a Quran user API request with an end-user bearer token",
    description="Use this for user features such as bookmarks, notes, collections, or progress APIs without coupling them to canonical Quran storage.",
)
async def proxy_user_request(
    request: QuranUserProxyRequest,
    service: QuranUserService = Depends(get_quran_user_service),
):
    try:
        return await service.proxy_request(
            method=request.method,
            path=request.path,
            access_token=request.access_token,
            query=request.query,
            body=request.body,
        )
    except Exception as exc:
        raise _to_http_error(exc) from exc
