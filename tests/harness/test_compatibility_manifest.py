from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts.release import compatibility_manifest as compatibility

ROOT = Path(__file__).resolve().parents[2]


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()


def test_checked_in_draft_is_structurally_valid_but_not_a_candidate() -> None:
    manifest = json.loads(compatibility.DEFAULT_MANIFEST.read_text())

    result = compatibility.validate(ROOT, manifest, level="draft")

    assert result["result"] == "draft"
    assert "release_id" in result["missing"]
    with pytest.raises(compatibility.ManifestError, match="candidate manifest is incomplete"):
        compatibility.validate(ROOT, manifest, level="candidate")


def test_offline_projection_covers_every_required_revision_family() -> None:
    offline = compatibility.build_offline(ROOT)

    assert set(offline) == {
        "runtime_overlay",
        "database",
        "contracts",
        "topology",
        "toolchains",
        "vector_contract",
    }
    assert offline["runtime_overlay"]["upstream_sha"]
    assert offline["contracts"]["openapi_sha256"]
    assert offline["topology"]["evidence_policy_revision"]
    assert offline["toolchains"] == {"python_major": "3", "node_major": "22", "rust": "1.95.0"}


def test_complete_candidate_requires_zero_skip_release_bound_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "release@example.invalid")
    _git(root, "config", "user.name", "Release Test")
    (root / "tracked").write_text("source", encoding="utf-8")
    _git(root, "add", "tracked")
    _git(root, "commit", "-qm", "source")
    source_sha = _git(root, "rev-parse", "HEAD")
    source_tree = _git(root, "rev-parse", "HEAD^{tree}")
    release_id = f"platform-{source_sha[:12]}-1234567890abcdef"
    image = f"sha256:{'1' * 64}"
    services = {
        service: {
            "image_digest": image,
            "reported_version": {
                "service_id": service,
                "release_id": release_id,
                "git_sha": source_sha,
                "image_digest": image,
            },
        }
        for service in compatibility.SERVICES
    }
    receipts: dict[str, str] = {}
    for name in compatibility.RECEIPTS:
        rel = f"tmp/gate-evidence/{name}.json"
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": "ai-platform/integration-gate-receipt/v1",
                    "gate": compatibility.RECEIPT_GATES[name],
                    "result": "pass",
                    "unexpected_skips": 0,
                    "release_id": release_id,
                    "source_git_sha": source_sha,
                    "steps": [
                        {
                            "command": ["make", f"{name}-gate"],
                            "exit_code": 0,
                            "skip_markers": 0,
                            "output_sha256": "5" * 64,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        receipts[name] = rel
    offline = {
        "runtime_overlay": {"upstream_sha": "2" * 40},
        "database": {"baseline_id": "b"},
        "contracts": {"openapi_sha256": "3" * 64},
        "topology": {"service_topology_revision": "4" * 64},
        "toolchains": {"python_major": "3", "node_major": "22", "rust": "1.95.0"},
        "vector_contract": {"bm25_revision": "bm25-v2"},
    }
    monkeypatch.setattr(compatibility, "build_offline", lambda _root: offline)
    manifest = {
        "schema_version": compatibility.SCHEMA,
        "status": "release_candidate",
        "release_id": release_id,
        "source": {"git_sha": source_sha, "git_tree_sha": source_tree},
        "services": services,
        "runtime_overlay": offline["runtime_overlay"],
        "database": offline["database"],
        "contracts": offline["contracts"],
        "topology": offline["topology"],
        "toolchains": offline["toolchains"],
        "vectors": {
            "qdrant_dataset_revision": "dataset-v1",
            "memory_namespace_revision": "memory-v1",
            "collection_or_alias": "kb-current",
            "embedding_provider": "provider",
            "embedding_model": "model",
            "embedding_dimension": 8,
            "bm25_revision": "bm25-v2",
        },
        "receipts": receipts,
    }

    assert compatibility.validate(root, manifest, level="candidate")["result"] == "pass"
    (root / receipts["knowledge"]).write_text(
        json.dumps(
            {
                "schema_version": "ai-platform/integration-gate-receipt/v1",
                "gate": compatibility.RECEIPT_GATES["knowledge"],
                "result": "pass",
                "unexpected_skips": 1,
                "release_id": release_id,
                "source_git_sha": source_sha,
                "steps": [
                    {
                        "command": ["make", "knowledge-gate"],
                        "exit_code": 0,
                        "skip_markers": 1,
                        "output_sha256": "5" * 64,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(compatibility.ManifestError, match="zero-skip pass"):
        compatibility.validate(root, manifest, level="candidate")
