from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from scripts.deploy.topology_modes import TopologyModeError, resolve_mode

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("mode", ["compact", "full", "scale"])
def test_checked_in_topology_modes_resolve(mode: str) -> None:
    result = resolve_mode(mode)
    services = {row["service_id"]: row for row in result["services"]}

    assert result["mode"] == mode
    assert services["gateway"]["replicas"] == 1
    assert services["agent-runtime"]["replicas"] == 1
    assert services["gateway-init"]["lifecycle"] == "one-shot"
    assert services["migrate"]["lifecycle"] == "one-shot"
    if mode == "compact":
        assert services["knowledge-service"]["runtime_role"] == "all"
        assert "knowledge-worker" not in services
    if mode == "scale":
        assert services["knowledge-worker"]["replicas"] == 2
        assert services["agent-capability-worker"]["replicas"] == 2


def test_scale_overlay_cannot_claim_gateway_or_runtime_scale(tmp_path: Path) -> None:
    for relative in (
        "src/core/data/service_topology.json",
        "docker-compose.yml",
        "docker-compose.compact.yml",
        "docker-compose.full.yml",
        "docker-compose.scale.yml",
    ):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    manifest_path = tmp_path / "src/core/data/service_topology.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    gateway = next(row for row in payload["services"] if row["service_id"] == "gateway")
    gateway["modes"]["scale"]["replicas"] = 2
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(TopologyModeError, match="gateway must resolve to one replica"):
        resolve_mode("scale", root=tmp_path)


def test_compact_overlay_must_disable_dedicated_worker(tmp_path: Path) -> None:
    for relative in (
        "src/core/data/service_topology.json",
        "docker-compose.yml",
        "docker-compose.compact.yml",
    ):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    compact = tmp_path / "docker-compose.compact.yml"
    compact.write_text(
        compact.read_text(encoding="utf-8").replace(
            "profiles: [arc06-full-only]", "profiles: []"
        ),
        encoding="utf-8",
    )

    with pytest.raises(TopologyModeError, match="must disable"):
        resolve_mode("compact", root=tmp_path)
