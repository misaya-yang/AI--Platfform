from __future__ import annotations

import pytest

from ai_gateway_core.enums import ConnectorType

from src.connectors.base import InProcessConnector
from src.models.service import ServiceDefinition


def test_in_process_connector_rejects_unapproved_module() -> None:
    service = ServiceDefinition(
        service_id="unsafe",
        name="Unsafe",
        connector_type=ConnectorType.IN_PROCESS,
        connector_config={"module": "os", "callable": "system"},
    )

    with pytest.raises(ValueError):
        InProcessConnector(service)
