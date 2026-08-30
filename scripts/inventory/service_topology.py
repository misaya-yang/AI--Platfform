"""Service topology baseline.

Produces ``service-topology.json``: every deployment unit and infrastructure
dependency with bounded context, process role, exposure, scale facts, health
contract, image artifact, and evidence pointers.

Facts come from the Compose files, the Rust overlay manifest, and the route
surfaces (published OpenAPI snapshot for Gateway; static AST scan for the
Knowledge service). Nothing here is copied from plan documents: every number
is recomputed from the tree at the pinned Git revision.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from _common import REPO_ROOT, base_envelope, sha256_file, walk_files

COMPOSE_FILES = (
    "docker-compose.yml",
    "docker-compose.dev.yml",
    "docker-compose.build.yml",
    "docker-compose.kbms.yml",
    "docker-compose.capability.yml",
)

HTTP_METHOD_DECORATORS = {"get", "post", "put", "delete", "patch", "options", "head", "websocket"}


# -- Compose parsing ----------------------------------------------------------


def parse_compose_services(path: Path) -> dict[str, dict]:
    """Minimal structural parse of a Compose file's ``services:`` block.

    Extracts service name, image default, container_name, and build presence.
    Deliberately dependency-free; the Compose files in this repository use a
    regular two-space layout that this parser covers.
    """
    services: dict[str, dict] = {}
    if not path.is_file():
        return services
    in_services = False
    current: str | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        stripped = raw.strip()
        if indent == 0:
            in_services = stripped == "services:"
            current = None
            continue
        if not in_services:
            continue
        if indent == 2 and stripped.endswith(":") and not stripped.startswith("-"):
            current = stripped[:-1]
            services[current] = {"image": None, "container_name": None, "build": False}
            continue
        if current is None or indent <= 2:
            continue
        if stripped.startswith("image:"):
            services[current]["image"] = stripped.split(":", 1)[1].strip().strip('"\'')
        elif stripped.startswith("container_name:"):
            services[current]["container_name"] = stripped.split(":", 1)[1].strip().strip('"\'')
        elif stripped.startswith("build:"):
            services[current]["build"] = True
    return services


# -- Route surfaces -----------------------------------------------------------


def gateway_route_surface() -> dict:
    """Route facts from the published OpenAPI snapshot (sdk/openapi.json)."""
    snapshot = REPO_ROOT / "sdk" / "openapi.json"
    if not snapshot.is_file():
        return {"available": False, "source": "sdk/openapi.json"}
    spec = json.loads(snapshot.read_text(encoding="utf-8"))
    paths = spec.get("paths", {})
    operations = 0
    operation_ids: list[str] = []
    for methods in paths.values():
        for method, op in methods.items():
            if method.startswith("x-") or not isinstance(op, dict):
                continue
            operations += 1
            if op.get("operationId"):
                operation_ids.append(op["operationId"])
    return {
        "available": True,
        "source": "sdk/openapi.json",
        "sha256": sha256_file(snapshot),
        "openapi_version": spec.get("openapi"),
        "info_version": (spec.get("info") or {}).get("version"),
        "path_count": len(paths),
        "operation_count": operations,
        "operation_id_count": len(set(operation_ids)),
        "route_authority": "published snapshot; live diff via tests/integration/test_gateway_openapi_contract.py",
    }


def _router_decorators(tree: ast.AST) -> list[tuple[str, str, str]]:
    """Collect (method, path, function) from @<router>.<method>(...) decorators."""
    found: list[tuple[str, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            if not isinstance(deco, ast.Call):
                continue
            func = deco.func
            if isinstance(func, ast.Attribute) and func.attr in HTTP_METHOD_DECORATORS:
                path_arg = ""
                if deco.args and isinstance(deco.args[0], ast.Constant):
                    path_arg = str(deco.args[0].value)
                found.append((func.attr.upper(), path_arg, node.name))
    return found


def knowledge_route_surface() -> dict:
    """Static scan of Knowledge API route decorators (AST, offline)."""
    route_files = sorted(
        walk_files((".py",), roots=("apps/knowledge-service/src/knowledge_service/api",))
    )
    routes: list[dict] = []
    for rel in route_files:
        path = REPO_ROOT / rel
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for method, route_path, fn in _router_decorators(tree):
            routes.append(
                {
                    "method": method,
                    "path_template": route_path,
                    "function": fn,
                    "file": str(rel),
                }
            )
    routes.sort(key=lambda item: (item["file"], item["path_template"], item["method"]))
    files_with_routes = sorted({item["file"] for item in routes})
    return {
        "source": "static AST scan of apps/knowledge-service/src/knowledge_service/api/**",
        "note": (
            "Decorators counted before prefix assembly; knowledge-service mounts its "
            "routers under /api/v1 (see apps/knowledge-service/src/knowledge_service/main.py)."
        ),
        "route_file_count": len(files_with_routes),
        "route_decorator_count": len(routes),
        "route_files": files_with_routes,
    }


# -- Topology assembly --------------------------------------------------------


def build() -> dict:
    compose = {name: parse_compose_services(REPO_ROOT / name) for name in COMPOSE_FILES}
    main = compose["docker-compose.yml"]

    bounded_contexts = {
        "agent-execution": {
            "definition": "Agent Runtime + Capability Worker (Rust); one version unit, two isolated processes.",
            "decision": "ADR-006, ADR-007, successor ADR-008",
        },
        "gateway-control": {
            "definition": "Public API, auth, tenant, model routing, quota, billing, admin plane (Python).",
            "decision": "successor ADR-008 + PRD platform-architecture-convergence §1",
        },
        "infrastructure": {
            "definition": "PostgreSQL, Redis, Qdrant, object storage, observability stores.",
            "decision": "Shared physical infrastructure with logical namespaces, not ownerless buckets (PRD §5).",
        },
        "knowledge": {
            "definition": "Knowledge API + Knowledge Worker (Python); one code unit, two runtime roles.",
            "decision": "successor ADR-008; knowledge schema + Qdrant dataset collection contract",
        },
        "product-surface": {
            "definition": "Frontend console; no persistent data of its own.",
            "decision": "PRD §5",
        },
    }

    services: list[dict] = []

    def compose_image(service: str) -> str | None:
        info = main.get(service) or {}
        return info.get("image")

    def entry(
        service_id: str,
        context: str,
        process_role: str,
        exposure: str,
        scale_support: str,
        image_artifact,
        state_owner: str,
        health_contract: str,
        evidence: list[str],
        required_deps: list[str] | None = None,
        optional_deps: list[str] | None = None,
        notes: str = "",
        scale_class: str = "other",
        helm_values_key: str | None = None,
    ) -> None:
        hpa_allowed = scale_class in {"stateless", "worker"}
        record = {
            "service_id": service_id,
            "bounded_context": context,
            "process_role": process_role,
            "exposure": exposure,
            "scale_support": scale_support,
            "scale_contract": {
                "class": scale_class,
                "max_replicas": 1 if scale_class == "single-instance" else None,
                "hpa_allowed": hpa_allowed,
                **({"helm_values_key": helm_values_key} if helm_values_key else {}),
            },
            "image_artifact": image_artifact,
            "state_owner": state_owner,
            "health_contract": health_contract,
            "required_deps": sorted(required_deps or []),
            "optional_deps": sorted(optional_deps or []),
            "notes": notes,
            "evidence": sorted(evidence),
        }
        info = main.get(service_id)
        if info and info.get("container_name"):
            record["container_name"] = info["container_name"]
            record["fixed_container_name"] = True
        services.append(record)

    entry(
        "frontend",
        "product-surface",
        "static console (nginx)",
        "public",
        "yes (stateless)",
        compose_image("frontend"),
        "none",
        "static asset delivery; no readiness probe of its own",
        ["docker-compose.yml:frontend"],
        required_deps=["gateway"],
        scale_class="stateless",
        helm_values_key="frontend",
    )
    entry(
        "gateway",
        "gateway-control",
        "FastAPI application (src/main.py)",
        "public",
        "currently-no: background scheduler has no leader lease; in-process SSE/thread ownership (PRD §5.1)",
        compose_image("gateway"),
        "gateway schema objects, sessions, quota/billing, assistant facades",
        "/health/live + /health/ready (src/api/v1/health.py)",
        ["docker-compose.yml:gateway", "src/main.py", "src/api/router.py"],
        required_deps=["postgres", "redis"],
        optional_deps=["agent-runtime", "knowledge-service", "qdrant (via knowledge proxy)"],
        notes="Fixed container_name blocks multi-replica compose scaling until removed (ARC-06).",
        scale_class="single-instance",
        helm_values_key="gateway",
    )
    entry(
        "gateway-init",
        "gateway-control",
        "one-shot init job (not a long-running service)",
        "private",
        "no",
        compose_image("gateway-init"),
        "none",
        "runs to completion before gateway",
        ["docker-compose.yml:gateway-init"],
        required_deps=["postgres"],
        scale_class="one-shot",
    )
    entry(
        "migrate",
        "infrastructure",
        "one-shot migration job (not a long-running service)",
        "private",
        "no (single database.authority writer, serialized by PostgreSQL advisory lock)",
        compose_image("migrate"),
        "platform_schema_baselines/changes/change_attempts; legacy ledgers are read-only adoption evidence",
        "runs to completion; failure blocks dependent services",
        [
            "docker-compose.yml:migrate",
            "database/authority/",
            "database/migrations/legacy-manifest.yml",
        ],
        required_deps=["postgres"],
        scale_class="one-shot",
    )
    entry(
        "agent-runtime",
        "agent-execution",
        "Rust Agent Runtime kernel (Thread/Turn/Item, leases, snapshots)",
        "private",
        "currently-no: thread affinity and cross-instance event notification unsolved (PRD §5.1)",
        compose_image("agent-runtime"),
        "assistant_runtime_threads/items/snapshots/leases (ADR-007)",
        "Runtime readiness reports kernel+store; optional capability loss must not unready it (ARC-05 target)",
        [
            "docker-compose.yml:agent-runtime",
            "rust/agent-runtime-overlay/manifest.json",
            "docs/architecture/ADR-006-agent-runtime-single-kernel.md",
            "docs/architecture/ADR-007-agent-runtime-data-boundaries.md",
        ],
        required_deps=["gateway (private model plane)", "postgres"],
        optional_deps=["agent-capability-worker"],
        notes="Fixed container_name blocks multi-replica compose scaling until removed (ARC-06).",
        scale_class="single-instance",
        helm_values_key="agentRuntime",
    )
    entry(
        "agent-capability-worker",
        "agent-execution",
        "Rust capability executor (tool workspaces, provider-free execution)",
        "private",
        "conditional: durable claim/recovery/cancel/side-effect-unknown proof required first (PRD §5.1)",
        compose_image("agent-capability-worker"),
        "agent_capability_executions + event ledger (migration 096)",
        "readiness checks execution store; per-capability downstream health (ARC-05 target)",
        [
            "docker-compose.yml:agent-capability-worker",
            "rust/agent-runtime-overlay/manifest.json",
            "database/migrations/096_agent_capability_executions.sql",
        ],
        required_deps=["agent-runtime", "postgres"],
        optional_deps=["gateway brokers", "knowledge-service"],
        notes="Fixed container_name blocks multi-replica compose scaling until removed (ARC-06).",
        scale_class="worker",
        helm_values_key="agentCapabilityWorker",
    )
    entry(
        "knowledge-service",
        "knowledge",
        "Knowledge API role of knowledge_service (KNOWLEDGE_RUNTIME_ROLE selects api/worker/all)",
        "private (Gateway-proxied public surface)",
        "yes for API role (stateless request handling)",
        compose_image("knowledge-service"),
        "knowledge schema objects; Qdrant dataset collection contract",
        "/health endpoints in knowledge_service/api",
        [
            "docker-compose.yml:knowledge-service",
            "apps/knowledge-service/src/knowledge_service/main.py",
        ],
        required_deps=["postgres", "qdrant"],
        optional_deps=["object storage", "redis"],
        notes="Fixed container_name blocks multi-replica compose scaling until removed (ARC-06).",
        scale_class="stateless",
        helm_values_key="knowledgeService",
    )
    entry(
        "knowledge-worker",
        "knowledge",
        "Knowledge Worker role of the same knowledge_service code unit (no separate schema)",
        "private",
        "conditional: durable claim/generation recovery must be proven (PRD §5.1)",
        compose_image("knowledge-worker"),
        "same knowledge domain as knowledge-service; no separate schema",
        "worker loop liveness; job claim/recovery semantics are ARC-05 targets",
        [
            "docker-compose.yml:knowledge-worker",
            "apps/knowledge-service/src/knowledge_service/services/knowledge/worker.py",
        ],
        required_deps=["postgres", "qdrant"],
        optional_deps=["embedding providers", "object storage"],
        notes="Fixed container_name blocks multi-replica compose scaling until removed (ARC-06).",
        scale_class="worker",
        helm_values_key="knowledgeWorker",
    )
    entry(
        "local-node",
        "agent-execution",
        "host-side daemon (apps/local-node), started by the end user, not by Compose",
        "host-local (connects outbound to gateway)",
        "per-user-host by design",
        None,
        "local ledger/outbox under user control (migration 098)",
        "local doctor/identity checks (apps/local-node/src/local_node/doctor.py)",
        ["apps/local-node/", "database/migrations/098_local_node_control_plane.sql"],
        required_deps=["gateway"],
        notes="Not a Compose service: runs on the user's machine; compose files define no local-node entry.",
        scale_class="per-host",
    )
    entry(
        "postgres",
        "infrastructure",
        "PostgreSQL 16 (single cluster; per-service schema/role split is ARC-03)",
        "private",
        "vertical only in this program (no physical split planned)",
        compose_image("postgres"),
        "all relational state; database.authority + frozen baseline/epoch manifests are the DDL authority",
        "pg_isready via compose healthcheck",
        ["docker-compose.yml:postgres", "database/schema.sql"],
        scale_class="stateful",
    )
    entry(
        "redis",
        "infrastructure",
        "Redis 7 (caches, rate-limit counters, session/task caches)",
        "private",
        "single instance; keyspace namespaced per owner (see data-access-inventory.json)",
        compose_image("redis"),
        "cache only: sessions, service/task caches, rate limits; no durable system of record",
        "compose healthcheck",
        ["docker-compose.yml:redis"],
        scale_class="stateful",
    )
    entry(
        "qdrant",
        "infrastructure",
        "Qdrant vector store shared by knowledge dataset collections and agent memory vectors",
        "private",
        "single instance; collection namespaces owned per data-access-inventory.json",
        compose_image("qdrant"),
        "dataset vectors (knowledge), agent memory vectors (gateway data governance)",
        "compose healthcheck",
        ["docker-compose.yml:qdrant"],
        scale_class="stateful",
    )
    entry(
        "tempo",
        "infrastructure",
        "Grafana Tempo trace store (observability)",
        "private",
        "single instance",
        compose_image("tempo"),
        "trace retention only",
        "compose healthcheck",
        ["docker-compose.yml:tempo"],
        optional_deps=["gateway", "knowledge-service"],
        scale_class="stateful",
    )

    overlay = REPO_ROOT / "rust" / "agent-runtime-overlay" / "manifest.json"
    rust_identity = json.loads(overlay.read_text(encoding="utf-8")) if overlay.is_file() else {}

    return {
        **base_envelope("service-topology"),
        "bounded_contexts": bounded_contexts,
        "services": sorted(services, key=lambda item: item["service_id"]),
        "compose_files": {
            name: {
                "exists": (REPO_ROOT / name).is_file(),
                "services": sorted((compose.get(name) or {}).keys()),
                "role": (
                    "base runtime topology"
                    if name == "docker-compose.yml"
                    else (
                        "explicitly empty local-live capability override (opt-in Docker socket trust)"
                        if name == "docker-compose.capability.yml"
                        else "override/build overlay — not an additional service set"
                    )
                ),
            }
            for name in COMPOSE_FILES
        },
        "rust_overlay_identity": rust_identity,
        "gateway_routes": gateway_route_surface(),
        "knowledge_routes": knowledge_route_surface(),
        "methodology": [
            "Compose services parsed structurally from docker-compose*.yml by scripts/inventory/service_topology.py.",
            "Gateway route surface taken from the published snapshot sdk/openapi.json; it is the contract authority frozen in contract-freeze.json.",
            "Knowledge route surface is a static AST decorator scan; it excludes mounted prefixes but is stable for drift comparison.",
            "scale_support values are current facts per PRD §5.1, not promises.",
            "scale_contract is the machine authority consumed by the fail-closed single-instance guard; Gateway and Agent Runtime cannot be reclassified by Helm values.",
        ],
    }


if __name__ == "__main__":
    from _common import write_json

    path = write_json("service-topology.json", build())
    print(f"wrote {path}")
