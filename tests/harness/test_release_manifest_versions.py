from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from ai_gateway_core.release_manifest import (
    ReleaseManifestUnavailable,
    service_version,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from knowledge_service.api.version import version_router

from scripts.release import compatibility_manifest, version_agreement_gate
from src.api.version import router as gateway_version_router


def _manifest(path: Path, *, status: str = "release_candidate") -> dict:
    git_sha = "1" * 40
    release_id = "platform-123456789012-1234567890abcdef"
    services = {}
    for service in ("gateway", "knowledge-service"):
        image = f"sha256:{'2' * 64}"
        services[service] = {
            "image_digest": image,
            "reported_version": {
                "service_id": service,
                "release_id": release_id,
                "git_sha": git_sha,
                "image_digest": image,
            },
        }
    payload = {
        "schema_version": "ai-platform/compatibility-manifest/v1",
        "status": status,
        "release_id": release_id,
        "source": {"git_sha": git_sha, "git_tree_sha": "3" * 40},
        "services": services,
    }
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return payload


def test_services_project_the_same_manifest_identity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "compatibility.json"
    payload = _manifest(path)
    monkeypatch.setenv("AI_PLATFORM_COMPATIBILITY_MANIFEST_PATH", str(path))
    expected_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    gateway_app = FastAPI()
    gateway_app.include_router(gateway_version_router)
    knowledge_app = FastAPI()
    knowledge_app.include_router(version_router("knowledge-service"))

    gateway = TestClient(gateway_app).get("/version")
    knowledge = TestClient(knowledge_app).get("/version")

    assert gateway.status_code == knowledge.status_code == 200
    assert gateway.json()["release_id"] == knowledge.json()["release_id"] == payload["release_id"]
    assert gateway.json()["manifest_sha256"] == knowledge.json()["manifest_sha256"] == expected_hash
    assert {gateway.json()["service_id"], knowledge.json()["service_id"]} == {
        "gateway",
        "knowledge-service",
    }


def test_draft_or_runtime_identity_drift_returns_unavailable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "compatibility.json"
    _manifest(path, status="draft")
    monkeypatch.setenv("AI_PLATFORM_COMPATIBILITY_MANIFEST_PATH", str(path))
    app = FastAPI()
    app.include_router(gateway_version_router)

    response = TestClient(app).get("/version")

    assert response.status_code == 503
    assert response.json()["code"] == "RELEASE_MANIFEST_UNAVAILABLE"
    _manifest(path)
    monkeypatch.setenv("AI_PLATFORM_GIT_SHA", "9" * 40)
    try:
        service_version("gateway", path)
    except ReleaseManifestUnavailable as exc:
        assert "runtime Git SHA" in str(exc)
    else:
        raise AssertionError("runtime identity drift was accepted")


def test_live_version_agreement_requires_every_service_and_same_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_id = "platform-release-12345678"
    git_sha = "1" * 40
    image = f"sha256:{'2' * 64}"
    services = {
        service: {
            "image_digest": image,
            "reported_version": {
                "service_id": service,
                "release_id": release_id,
                "git_sha": git_sha,
                "image_digest": image,
            },
        }
        for service in compatibility_manifest.SERVICES
    }
    manifest = {
        "schema_version": compatibility_manifest.SCHEMA,
        "status": "release_candidate",
        "release_id": release_id,
        "source": {"git_sha": git_sha},
        "services": services,
    }
    endpoints = {
        service: f"http://{service}.test/version"
        for service in compatibility_manifest.SERVICES
    }

    def fake_fetch(url: str) -> dict:
        service = next(name for name, endpoint in endpoints.items() if endpoint == url)
        return {
            "schema_version": "ai-platform/service-version/v1",
            **services[service]["reported_version"],
            "manifest_sha256": "3" * 64,
        }

    monkeypatch.setattr(version_agreement_gate, "_fetch", fake_fetch)
    assert version_agreement_gate.verify(manifest, endpoints)["result"] == "pass"

    endpoints.pop("migrator")
    with pytest.raises(version_agreement_gate.VersionAgreementError, match="endpoint set"):
        version_agreement_gate.verify(manifest, endpoints)
