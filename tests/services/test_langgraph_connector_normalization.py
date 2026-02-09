from __future__ import annotations

from src.api.v1.services import _normalize_langgraph_connector_config
from src.proxy.config_loader import ProxyConfigLoader


def test_normalize_langgraph_connector_config_syncs_urls_and_ids():
    definition = {
        "service_type": "langgraph",
        "metadata": {"adapter_type": "langgraph"},
        "connector_config": {
            "proxy_mode": "transparent",
            "base_url": "http://localhost:2025/",
            "upstream_url": "http://localhost:2024",
            "graph_id": "customer-agent",
        },
    }

    _normalize_langgraph_connector_config(definition)
    connector = definition["connector_config"]

    assert connector["base_url"] == "http://localhost:2025"
    assert connector["upstream_url"] == "http://localhost:2025"
    assert connector["assistant_id"] == "customer-agent"


def test_normalize_langgraph_connector_config_backfills_graph_id():
    definition = {
        "service_type": "langgraph",
        "metadata": {"adapter_type": "langgraph"},
        "connector_config": {
            "base_url": "http://localhost:2025",
            "assistant_id": "Imam",
        },
    }

    _normalize_langgraph_connector_config(definition)
    connector = definition["connector_config"]

    assert connector["graph_id"] == "Imam"
    assert connector["base_url"] == "http://localhost:2025"
    assert connector["upstream_url"] == "http://localhost:2025"


def test_proxy_config_loader_heals_transparent_url_mismatch():
    loader = ProxyConfigLoader(database=None)
    row = {
        "service_id": "customer-agent",
        "name": "Customer Agent",
        "connector_config": {
            "proxy_mode": "transparent",
            "base_url": "http://localhost:2025/",
            "upstream_url": "http://localhost:2024",
        },
        "service_config": {},
        "metadata": {},
        "status": "active",
    }

    config = loader._parse_service_row(row)

    assert config.upstream_url == "http://localhost:2025"

