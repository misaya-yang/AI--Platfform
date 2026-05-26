from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.api.v1.connectors import oauth_callback


@pytest.mark.asyncio
async def test_oauth_callback_rejects_unissued_state_before_token_exchange() -> None:
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()), base_url="http://test/")

    with pytest.raises(HTTPException) as exc_info:
        await oauth_callback(
            provider="github",
            code="attacker-code",
            state="tenant:user:github:forgednonce",
            request=request,
        )

    assert exc_info.value.status_code == 400
    assert "state" in str(exc_info.value.detail).lower()
