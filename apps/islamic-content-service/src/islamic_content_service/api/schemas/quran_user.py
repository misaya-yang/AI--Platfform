from __future__ import annotations

from pydantic import BaseModel, Field


class QuranUserAuthConfigResponse(BaseModel):
    generated_at: str
    enabled: bool
    auth_url: str
    api_base_url: str
    redirect_uri: str | None = None
    post_logout_redirect_uri: str | None = None
    scopes: list[str] = Field(default_factory=list)


class QuranUserAuthorizeUrlResponse(BaseModel):
    generated_at: str
    authorize_url: str
    redirect_uri: str
    scopes: list[str] = Field(default_factory=list)


class QuranUserTokenRequest(BaseModel):
    code: str
    redirect_uri: str | None = None
    code_verifier: str | None = None


class QuranUserRefreshRequest(BaseModel):
    refresh_token: str


class QuranUserProxyRequest(BaseModel):
    method: str = Field(default="GET", description="HTTP method such as GET, POST, PATCH, DELETE")
    path: str = Field(description="Relative path under the configured Quran user API base URL")
    access_token: str = Field(description="End-user bearer token from the Quran OAuth flow")
    query: dict | None = None
    body: dict | None = None


class QuranUserTokenResponse(BaseModel):
    generated_at: str
    token: dict


class QuranUserInfoResponse(BaseModel):
    generated_at: str
    userinfo: dict


class QuranUserProxyResponse(BaseModel):
    generated_at: str
    method: str
    path: str
    payload: dict
