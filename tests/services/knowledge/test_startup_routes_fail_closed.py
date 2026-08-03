"""Startup contracts for the knowledge-service API surface."""

from __future__ import annotations

import builtins

import pytest


def test_storage_signing_key_is_loaded_as_a_redacted_secret(monkeypatch) -> None:
    monkeypatch.setenv(
        "GATEWAY_ASSISTANT_SHARED_SECRET",
        "unit-test-shared-secret",
    )
    monkeypatch.setenv("KNOWLEDGE_APP__ALLOW_ANONYMOUS", "false")
    monkeypatch.setenv(
        "KNOWLEDGE_STORAGE__SIGNING_KEY",
        "unit-test-storage-signing-secret",
    )

    from knowledge_service import main

    settings = main.Settings(_env_file=None)

    assert (
        settings.storage.signing_key.get_secret_value()
        == "unit-test-storage-signing-secret"
    )
    assert "unit-test-storage-signing-secret" not in repr(settings)


@pytest.mark.parametrize("route_module", ["api.routes.eval", "api.routes.knowledge"])
def test_required_router_import_failure_aborts_app_creation(
    monkeypatch,
    route_module: str,
) -> None:
    """A broken required router must not produce a partially ready app."""
    monkeypatch.setenv("GATEWAY_ASSISTANT_SHARED_SECRET", "unit-test-shared-secret")
    monkeypatch.setenv("KNOWLEDGE_APP__ALLOW_ANONYMOUS", "false")

    from knowledge_service import main

    real_import = builtins.__import__

    def fail_required_router_import(name, globals=None, locals=None, fromlist=(), level=0):
        package = globals.get("__package__") if globals else None
        if name == route_module and level == 1 and package == "knowledge_service":
            raise ImportError("synthetic required-router failure")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fail_required_router_import)

    with pytest.raises(ImportError, match="synthetic required-router failure"):
        main.create_app(main.Settings())


@pytest.mark.asyncio
async def test_core_worker_startup_failure_never_marks_service_ready(
    monkeypatch,
) -> None:
    """A failed core worker must abort lifespan and release opened resources."""
    monkeypatch.setenv("GATEWAY_ASSISTANT_SHARED_SECRET", "unit-test-shared-secret")
    monkeypatch.setenv("KNOWLEDGE_APP__ALLOW_ANONYMOUS", "false")

    from ai_gateway_core import tracing
    from ai_gateway_core.proxy import drain
    from knowledge_service import main
    from knowledge_service.persistence import database as database_module
    from knowledge_service.services.knowledge import knowledge_service as service_module
    from knowledge_service.services.knowledge import worker as worker_module

    events: list[str] = []

    class FakeDatabasePool:
        async def init(self, *_args, **_kwargs) -> None:
            events.append("gateway-db-open")

        async def close(self) -> None:
            events.append("gateway-db-close")

    class FakeDatabaseStorage:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def connect(self) -> None:
            events.append("knowledge-db-open")

        async def close(self) -> None:
            events.append("knowledge-db-close")

    class FakeKnowledgeService:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def close(self) -> None:
            events.append("knowledge-service-close")

    class FailingKnowledgeWorker:
        is_running = False

        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def start(self) -> None:
            events.append("worker-start")
            raise RuntimeError("synthetic core startup failure")

        async def stop(self) -> None:
            events.append("worker-stop")

    class FakeQdrant:
        async def close(self) -> None:
            events.append("qdrant-close")

    async def fake_init_qdrant(_settings):
        events.append("qdrant-open")
        return FakeQdrant()

    monkeypatch.setattr(main, "DatabasePool", FakeDatabasePool)
    monkeypatch.setattr(main, "_init_qdrant", fake_init_qdrant)
    monkeypatch.setattr(database_module, "DatabaseStorage", FakeDatabaseStorage)
    monkeypatch.setattr(service_module, "KnowledgeService", FakeKnowledgeService)
    monkeypatch.setattr(worker_module, "KnowledgeWorker", FailingKnowledgeWorker)
    monkeypatch.setattr(tracing, "init_tracing", lambda _service: None)
    monkeypatch.setattr(drain, "install_signal_handlers", lambda _loop: None)

    app = main.create_app(main.Settings())

    with pytest.raises(RuntimeError, match="synthetic core startup failure"):
        async with app.router.lifespan_context(app):
            pytest.fail("lifespan yielded after failed core startup")

    assert getattr(app.state, "_ready", False) is False
    assert events == [
        "gateway-db-open",
        "qdrant-open",
        "knowledge-db-open",
        "worker-start",
        "worker-stop",
        "knowledge-service-close",
        "knowledge-db-close",
        "qdrant-close",
        "gateway-db-close",
    ]


@pytest.mark.asyncio
async def test_unreachable_qdrant_probe_aborts_startup_client_initialization(
    monkeypatch,
) -> None:
    """A client object is not readiness evidence when its bounded probe fails."""
    monkeypatch.setenv("GATEWAY_ASSISTANT_SHARED_SECRET", "unit-test-shared-secret")
    monkeypatch.setenv("KNOWLEDGE_APP__ALLOW_ANONYMOUS", "false")

    import qdrant_client
    from knowledge_service import main

    events: list[str] = []

    class UnreachableQdrant:
        def __init__(self, **_kwargs) -> None:
            events.append("created")

        async def get_collections(self):
            events.append("probe")
            raise ConnectionError("synthetic qdrant outage")

        async def close(self) -> None:
            events.append("closed")

    monkeypatch.setattr(qdrant_client, "AsyncQdrantClient", UnreachableQdrant)

    with pytest.raises(ConnectionError, match="synthetic qdrant outage"):
        await main._init_qdrant(main.Settings())

    assert events == ["created", "probe", "closed"]
