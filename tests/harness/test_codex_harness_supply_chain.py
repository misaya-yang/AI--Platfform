from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.harness import codex_harness_supply_chain as supply_chain
from scripts.harness.codex_harness_supply_chain import (
    ContractError,
    record_local_image,
    refresh_source_lock,
    sha256_file,
    validate_lock,
)


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def _fixture(
    tmp_path: Path,
    *,
    app_server_runnable: bool = True,
    agent_runtime_runnable: bool = True,
) -> Path:
    receipt_path = tmp_path / "deploy/codex-harness/source-receipt.json"
    sbom_path = tmp_path / "deploy/codex-harness/sbom.cdx.json"
    notice_path = tmp_path / "deploy/codex-harness/NOTICE.md"
    sbom_path.parent.mkdir(parents=True, exist_ok=True)
    sbom_path.write_text("{}\n", encoding="utf-8")
    notice_path.write_text("OpenAI Codex notice\n", encoding="utf-8")
    sbom_sha = sha256_file(sbom_path)
    receipt = {
        "schema_version": "ai-platform/codex-harness-source-receipt/v1",
        "source": {
            "upstream_url": "https://github.com/openai/codex.git",
            "upstream_sha": "a" * 40,
            "fork_sha": "b" * 40,
            "git_tree_sha": "c" * 40,
        },
        "toolchain": {
            "cargo": "cargo 1.95.0",
            "rustc": "rustc 1.95.0",
            "rust_toolchain_sha256": "2" * 64,
            "cargo_lock_sha256": "3" * 64,
        },
        "schema_bundle": {"sha256": "d" * 64, "file_count": 2, "total_bytes": 10},
        "license": {
            "spdx": "Apache-2.0",
            "license_sha256": "e" * 64,
            "upstream_notice_sha256": "f" * 64,
        },
        "sbom": {"sha256": sbom_sha, "component_count": 1},
    }
    _write_json(receipt_path, receipt)
    digest = "sha256:" + "1" * 64
    any_runnable = app_server_runnable or agent_runtime_runnable
    lock = {
        "schema_version": "ai-platform/codex-harness-lock/v2",
        "release_state": "local_image_locked" if any_runnable else "local_source_locked",
        "source": {
            "upstream_url": "https://github.com/openai/codex.git",
            "upstream_sha": "a" * 40,
            "fork_sha": "b" * 40,
            "git_tree_sha": "c" * 40,
            "fork_repository": "local://ai-platform-codex-harness",
        },
        "build": {
            "cargo": "cargo 1.95.0",
            "rustc": "rustc 1.95.0",
            "rust_toolchain_sha256": "2" * 64,
            "cargo_lock_sha256": "3" * 64,
            "source_receipt": "deploy/codex-harness/source-receipt.json",
            "source_receipt_sha256": sha256_file(receipt_path),
            "sbom": "deploy/codex-harness/sbom.cdx.json",
            "sbom_sha256": sbom_sha,
            "app_server_schema_sha256": "d" * 64,
            "app_server_schema_file_count": 2,
        },
        "license": {
            "spdx": "Apache-2.0",
            "notice": "deploy/codex-harness/NOTICE.md",
            "notice_sha256": sha256_file(notice_path),
            "upstream_license_sha256": "e" * 64,
            "upstream_notice_sha256": "f" * 64,
        },
        "oci": {
            "artifacts": {
                "app_server": {
                    "binary": "codex-app-server",
                    "protocol": "codex-app-server-jsonrpc/v2",
                    "candidate_start_allowed": app_server_runnable,
                    "image_digest": digest if app_server_runnable else None,
                    "image_ref": f"local-image://{digest}" if app_server_runnable else None,
                    "platforms": ["linux/arm64"],
                },
                "agent_runtime": {
                    "binary": "ai-platform-agent-runtime",
                    "protocol": "ai-platform-agent-runtime-http-sse/v1",
                    "candidate_start_allowed": agent_runtime_runnable,
                    "image_digest": digest if agent_runtime_runnable else None,
                    "image_ref": f"local-image://{digest}" if agent_runtime_runnable else None,
                    "platforms": ["linux/arm64"],
                },
            }
        },
    }
    lock_path = tmp_path / "deploy/codex-harness/lock.json"
    _write_json(lock_path, lock)
    return lock_path


def test_valid_digest_pinned_lock_passes(tmp_path: Path) -> None:
    validate_lock(
        repo_root=tmp_path,
        lock_path=_fixture(tmp_path),
        required_artifact="agent_runtime",
    )


def test_source_only_lock_fails_runnable_gate(tmp_path: Path) -> None:
    lock_path = _fixture(
        tmp_path,
        app_server_runnable=True,
        agent_runtime_runnable=False,
    )
    with pytest.raises(ContractError, match="agent_runtime.*not locked"):
        validate_lock(
            repo_root=tmp_path,
            lock_path=lock_path,
            required_artifact="agent_runtime",
        )


@pytest.mark.parametrize("target", ["receipt", "sbom", "notice"])
def test_tampered_artifact_fails_closed(tmp_path: Path, target: str) -> None:
    lock_path = _fixture(tmp_path)
    paths = {
        "receipt": tmp_path / "deploy/codex-harness/source-receipt.json",
        "sbom": tmp_path / "deploy/codex-harness/sbom.cdx.json",
        "notice": tmp_path / "deploy/codex-harness/NOTICE.md",
    }
    paths[target].write_text("tampered\n", encoding="utf-8")
    with pytest.raises(ContractError):
        validate_lock(
            repo_root=tmp_path,
            lock_path=lock_path,
            required_artifact="app_server",
        )


def test_mismatched_local_image_reference_fails_closed(tmp_path: Path) -> None:
    lock_path = _fixture(tmp_path)
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["oci"]["artifacts"]["agent_runtime"]["image_ref"] = "registry.example/runtime:latest"
    _write_json(lock_path, lock)
    with pytest.raises(ContractError, match="local-image"):
        validate_lock(
            repo_root=tmp_path,
            lock_path=lock_path,
            required_artifact="agent_runtime",
        )


def test_artifact_binary_identity_is_mandatory(tmp_path: Path) -> None:
    lock_path = _fixture(tmp_path)
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["oci"]["artifacts"]["agent_runtime"]["binary"] = "codex-app-server"
    _write_json(lock_path, lock)
    with pytest.raises(ContractError, match="ai-platform-agent-runtime"):
        validate_lock(repo_root=tmp_path, lock_path=lock_path)


def test_source_refresh_invalidates_every_image(tmp_path: Path) -> None:
    lock_path = _fixture(tmp_path)

    refresh_source_lock(repo_root=tmp_path, lock_path=lock_path)

    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    assert lock["release_state"] == "local_source_locked"
    for artifact in lock["oci"]["artifacts"].values():
        assert artifact["candidate_start_allowed"] is False
        assert artifact["image_digest"] is None
        assert artifact["image_ref"] is None


def test_record_local_image_checks_labels_and_locks_only_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path = _fixture(
        tmp_path,
        app_server_runnable=False,
        agent_runtime_runnable=False,
    )
    image_digest = "sha256:" + "9" * 64
    inspected = [
        {
            "Id": image_digest,
            "Os": "linux",
            "Architecture": "arm64",
            "Config": {
                "Labels": {
                    "org.opencontainers.image.revision": "b" * 40,
                    "com.misaya.ai-platform.codex.schema-sha256": "d" * 64,
                    "com.misaya.ai-platform.codex.artifact": "agent_runtime",
                    "com.misaya.ai-platform.codex.binary": "ai-platform-agent-runtime",
                }
            },
        }
    ]
    monkeypatch.setattr(supply_chain, "_run", lambda *_args, **_kwargs: json.dumps(inspected))

    record_local_image(
        repo_root=tmp_path,
        lock_path=lock_path,
        artifact_id="agent_runtime",
        image="runtime:test",
    )

    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    runtime = lock["oci"]["artifacts"]["agent_runtime"]
    assert runtime["image_digest"] == image_digest
    assert runtime["image_ref"] == f"local-image://{image_digest}"
    assert runtime["platforms"] == ["linux/arm64"]
    assert lock["oci"]["artifacts"]["app_server"]["candidate_start_allowed"] is False
