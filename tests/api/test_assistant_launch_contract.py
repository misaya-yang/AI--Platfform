from __future__ import annotations

from types import SimpleNamespace

import pytest
from ai_gateway_contracts.agent_launch import ResolvedAgentLaunchV1

from src.api.schemas.assistant import AssistantChatRequest
from src.api.v1._assistant_routes.chat import _start_agent_runtime_turn
from src.core.auth.user_resolver import UserContext


class _ModelService:
    async def get_model(self, _tenant_id: str, model_id: str):
        return {
            "model_id": model_id,
            "provider_id": "provider-a",
            "is_enabled": True,
            "effective_capabilities": {
                "wire_protocols": {
                    "preferred": "responses_v1",
                    "supported": ["responses_v1"],
                }
            },
        }


@pytest.mark.asyncio
async def test_assistant_entry_resolves_launch_before_control_plane() -> None:
    captured = None

    class Control:
        async def start_turn(self, **kwargs):
            nonlocal captured
            captured = kwargs
            return SimpleNamespace(run_id="run-a")

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                agent_runtime_control=Control(),
                model_service=_ModelService(),
            )
        )
    )
    user = UserContext(
        user_id="user-a",
        tenant_id="tenant-a",
        tier="normal",
        is_authenticated=True,
    )

    await _start_agent_runtime_turn(
        request,
        user,
        AssistantChatRequest(message="hello"),
        session_id="session-a",
        model_id="model-a",
    )

    assert captured is not None
    launch = captured["resolved_agent_launch"]
    assert isinstance(launch, ResolvedAgentLaunchV1)
    assert launch.identity == {
        "agent_id": "__builtin_assistant__",
        "agent_version_id": None,
        "auth_mode": "private",
        "channel": "builtin",
        "draft_revision": None,
        "entrypoint": "assistant",
        "publication_id": None,
        "session_id": "session-a",
        "tenant_id": "tenant-a",
        "user_id": "user-a",
    }
