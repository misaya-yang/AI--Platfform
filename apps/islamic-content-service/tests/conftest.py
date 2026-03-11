from __future__ import annotations

import sys
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

SERVICE_ROOT = Path(__file__).resolve().parents[1]
SRC = SERVICE_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from islamic_content_service.main import create_app


@pytest_asyncio.fixture
async def test_app():
    return create_app(enable_runtime=False)


@pytest_asyncio.fixture
async def async_client(test_app) -> AsyncGenerator[AsyncClient, None]:
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
