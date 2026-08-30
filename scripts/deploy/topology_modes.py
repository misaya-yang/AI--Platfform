#!/usr/bin/env python3
"""Resolve and validate ARC-06 compact/full/scale Compose modes."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "src/core/data/service_topology.json"


class TopologyModeError(RuntimeError):
    pass


def load_manifest(path: Path = MANIFEST) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TopologyModeError(f"cannot read topology manifest: {exc}") from exc
    if payload.get("schema") != "ai-gateway/service-topology/v1":
        raise TopologyModeError("unexpected topology schema")
    return payload


def resolve_mode(mode: str, *, root: Path = ROOT) -> dict[str, Any]:
    manifest = load_manifest(root / "src/core/data/service_topology.json")
    compose_modes = manifest.get("compose_modes") or {}
    if mode not in compose_modes:
        raise TopologyModeError(f"unknown topology mode {mode!r}")
    files = compose_modes[mode].get("files")
    if not isinstance(files, list) or len(files) != 2:
        raise TopologyModeError(f"topology mode {mode} must name base plus one overlay")
    for relative in files:
        if not (root / relative).is_file():
            raise TopologyModeError(f"topology mode {mode} file is missing: {relative}")

    services: list[dict[str, Any]] = []
    for service in manifest.get("services") or []:
        resolution = service.get("modes", {}).get(mode)
        if not isinstance(resolution, dict):
            raise TopologyModeError(f"{service.get('service_id')} has no {mode} resolution")
        if resolution.get("present"):
            services.append(
                {
                    "service_id": service["service_id"],
                    "lifecycle": service["lifecycle"],
                    "scale_support": service["scale_support"],
                    "replicas": resolution["replicas"],
                    **(
                        {"runtime_role": resolution["runtime_role"]}
                        if "runtime_role" in resolution
                        else {}
                    ),
                }
            )
    by_id = {service["service_id"]: service for service in services}
    for singleton in ("gateway", "agent-runtime"):
        if by_id.get(singleton, {}).get("replicas") != 1:
            raise TopologyModeError(f"{singleton} must resolve to one replica")
    if mode == "compact":
        if by_id["knowledge-service"].get("runtime_role") != "all":
            raise TopologyModeError("compact mode requires Knowledge all role")
        if "knowledge-worker" in by_id:
            raise TopologyModeError("compact mode cannot start knowledge-worker")
    if mode == "scale":
        for worker in ("knowledge-worker", "agent-capability-worker"):
            if by_id.get(worker, {}).get("replicas", 0) < 2:
                raise TopologyModeError(f"scale mode requires two {worker} replicas")
    for job in ("gateway-init", "migrate"):
        if by_id.get(job, {}).get("lifecycle") != "one-shot":
            raise TopologyModeError(f"{job} must resolve as one-shot")
    _validate_overlay(mode, root / files[-1])
    return {
        "schema": "ai-gateway/topology-resolution/v1",
        "mode": mode,
        "topology_revision": manifest["revision"],
        "compose_files": files,
        "services": sorted(services, key=lambda row: row["service_id"]),
    }


def _validate_overlay(mode: str, path: Path) -> None:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise TopologyModeError(f"cannot parse {path.name}: {exc}") from exc
    services = payload.get("services") if isinstance(payload, dict) else None
    if not isinstance(services, dict):
        raise TopologyModeError(f"{path.name} must declare services")
    gateway = services.get("gateway") or {}
    if (gateway.get("environment") or {}).get("AI_PLATFORM_TOPOLOGY_MODE") != mode:
        raise TopologyModeError(f"{path.name} does not stamp the Gateway topology mode")
    knowledge_api = services.get("knowledge-service") or {}
    role = (knowledge_api.get("environment") or {}).get("KNOWLEDGE_RUNTIME_ROLE")
    if role != ("all" if mode == "compact" else "api"):
        raise TopologyModeError(f"{path.name} has the wrong Knowledge API runtime role")
    worker = services.get("knowledge-worker") or {}
    if mode == "compact":
        if not worker.get("profiles"):
            raise TopologyModeError("compact overlay must disable the dedicated Knowledge worker")
        return
    if (worker.get("environment") or {}).get("KNOWLEDGE_RUNTIME_ROLE") != "worker":
        raise TopologyModeError(f"{path.name} has the wrong Knowledge worker runtime role")
    expected = 2 if mode == "scale" else 1
    if (worker.get("deploy") or {}).get("replicas") != expected:
        raise TopologyModeError(f"{path.name} must declare {expected} Knowledge worker replicas")
    capability = services.get("agent-capability-worker") or {}
    if (capability.get("deploy") or {}).get("replicas") != expected:
        raise TopologyModeError(f"{path.name} must declare {expected} capability worker replicas")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("compact", "full", "scale"), default="full")
    parser.add_argument("--check-all", action="store_true")
    args = parser.parse_args(argv)
    try:
        modes = ("compact", "full", "scale") if args.check_all else (args.mode,)
        resolved = [resolve_mode(mode) for mode in modes]
    except TopologyModeError as exc:
        print(f"TOPOLOGY ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(resolved if args.check_all else resolved[0], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
