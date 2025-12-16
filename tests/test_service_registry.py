import pytest

from src.services.registry.service_registry import MemoryRegistryStorage, ServiceRegistry
from src.models.enums import ConnectorType, ContentType, InvocationMode, ServiceType


@pytest.mark.asyncio
async def test_service_registry_from_dict():
    storage = MemoryRegistryStorage()
    registry = ServiceRegistry(storage)
    data = {
        "service_id": "test-svc",
        "name": "Test",
        "service_type": "processing",
        "supported_modes": ["sync", "async"],
        "connector_type": "http",
        "connector_config": {"base_url": "http://example"},
        "accepted_content_types": ["text"],
        "output_content_types": ["text"],
        "metadata": {"adapter_type": "generic_rest"},
    }
    service = registry._service_from_dict(data)
    assert service.service_id == "test-svc"
    assert service.service_type == ServiceType.PROCESSING
    assert service.supported_modes == [
        InvocationMode.SYNC,
        InvocationMode.ASYNC,
    ]
    assert service.connector_type == ConnectorType.HTTP
    assert service.accepted_content_types == [ContentType.TEXT]


@pytest.mark.asyncio
async def test_service_registry_register_and_get():
    storage = MemoryRegistryStorage()
    registry = ServiceRegistry(storage)
    data = {
        "service_id": "test-svc",
        "name": "Test",
        "service_type": "processing",
        "supported_modes": ["sync"],
        "connector_type": "http",
        "connector_config": {"base_url": "http://example"},
        "accepted_content_types": ["text"],
        "output_content_types": ["text"],
        "metadata": {"adapter_type": "generic_rest"},
    }
    service = registry._service_from_dict(data)
    await registry.register(service)
    got = await registry.get("test-svc")
    assert got is not None
    assert got.service_id == "test-svc"
