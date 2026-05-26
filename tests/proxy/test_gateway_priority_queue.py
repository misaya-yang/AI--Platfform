from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.api.deps import AuthContext
from src.api.v1.config import get_service_config
from src.config.settings import Settings
from src.models.service import ServiceDefinition


class _Registry:
    def __init__(self, service: ServiceDefinition):
        self.service = service

    async def get(self, service_id: str):
        return self.service if service_id == self.service.service_id else None


@pytest.mark.asyncio
async def test_service_config_marks_priority_not_enforced() -> None:
    request = SimpleNamespace(
        state=SimpleNamespace(),
        app=SimpleNamespace(
            state=SimpleNamespace(
                settings=Settings(),
                registry=_Registry(ServiceDefinition(service_id="imam", name="Imam")),
            )
        ),
    )
    auth = AuthContext(
        user_id="admin",
        tenant_id="default",
        roles=["developer"],
        permissions=["console:services:edit"],
        is_authenticated=True,
    )

    response = await get_service_config("imam", request=request, auth=auth)

    assert response["config"]["priority"]["enforced"] is False
    assert response["config"]["priority"]["scheduler"] == "not_configured"


def test_priority_controls_are_disabled_when_backend_is_not_enforcing() -> None:
    source = Path("web/src/components/ServiceConfigDialog.tsx").read_text()

    assert "priorityEnforced" in source
    assert "disabled={!priorityEnforced" in source
    assert "services.configDialog.priority.notEnforced" in source
