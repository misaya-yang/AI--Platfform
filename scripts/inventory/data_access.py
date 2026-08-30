"""Data-access inventory baseline.

Produces ``data-access-inventory.json``:

* PostgreSQL objects parsed from ``database/schema.sql`` and every file under
  ``database/migrations/`` (tables, views, sequences, functions, types,
  indexes, schemas, grants) with their source evidence.
* Static writer/reader attribution per table, from an AST scan of SQL string
  literals in every first-party Python unit. This is a heuristic: it records
  which unit *contains SQL touching the table*, with the evidence files, not
  a runtime proof. It is the input ARC-03 uses to generate least-privilege
  grants, so false negatives are the failure mode to watch.
* Qdrant, Redis, and object-store namespace ownership with evidence.

The inventory never invents owners. Where ownership is a target rather than a
fact (e.g. per-service PostgreSQL roles), it is marked ``target`` and points
at the work package that creates it.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from _common import (
    REPO_ROOT,
    base_envelope,
    extract_sql_table_refs,
    unit_for_path,
    walk_files,
)

_SCHEMA_OBJECT = re.compile(
    r"^\s*CREATE\s+(?:OR\s+REPLACE\s+)?"
    r"(?P<kind>TABLE|VIEW|MATERIALIZED\s+VIEW|SEQUENCE|FUNCTION|TYPE|SCHEMA|INDEX|UNIQUE\s+INDEX)\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?(?P<name>[A-Za-z_][\w.]*)",
    re.IGNORECASE,
)

_GRANT = re.compile(
    r"^\s*GRANT\s+(?P<privs>[A-Z, ]+?)\s+ON\s+(?P<what>[A-Z ]+?)\s+(?P<obj>[\w.\"]+)\s+TO\s+(?P<role>[\w\"]+)",
    re.IGNORECASE,
)

_SQL_VERB = {
    "writer": re.compile(r"\b(INSERT\s+INTO|UPDATE|DELETE\s+FROM|TRUNCATE(?:\s+TABLE)?|MERGE\s+INTO)\b", re.IGNORECASE),
    "reader": re.compile(r"\b(SELECT|FROM|JOIN)\b", re.IGNORECASE),
}

_DDL_NOISE = {"schema_migrations", "schema_migrations_meta"}


def _sql_files() -> list[Path]:
    root = REPO_ROOT / "database"
    found = [root / "schema.sql"]
    for path in sorted(root.rglob("*.sql")):
        if path.is_file():
            found.append(path)
    return sorted(set(found))


def parse_schema_objects() -> tuple[dict[str, dict], list[dict]]:
    """Parse CREATE statements and GRANTs from schema.sql + migrations."""
    objects: dict[str, dict] = {}
    grants: list[dict] = []
    for path in _sql_files():
        rel = str(path.relative_to(REPO_ROOT))
        text = path.read_text(encoding="utf-8")
        for match in _SCHEMA_OBJECT.finditer(text):
            kind = re.sub(r"\s+", "_", match.group("kind").upper())
            name = match.group("name").strip('"').lower()
            record = objects.setdefault(
                name,
                {
                    "name": name,
                    "kinds": set(),
                    "first_defined_in": rel,
                    "defined_in": set(),
                    "schema_qualified": "." in name,
                },
            )
            record["kinds"].add(kind)
            record["defined_in"].add(rel)
        for match in _GRANT.finditer(text):
            grants.append(
                {
                    "privileges": re.sub(r"\s+", " ", match.group("privs").strip()),
                    "on": match.group("what").strip().lower(),
                    "object": match.group("obj").strip('"').lower(),
                    "role": match.group("role").strip('"').lower(),
                    "source": rel,
                }
            )
    for record in objects.values():
        record["kinds"] = sorted(record["kinds"])
        record["defined_in"] = sorted(record["defined_in"])
    grants.sort(key=lambda g: (g["object"], g["role"], g["privileges"], g["source"]))
    return objects, grants


def _python_string_literals(path: Path):
    """Yield string constant fragments from a Python file (incl. f-string parts)."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value


def _looks_like_sql(text: str) -> bool:
    return bool(re.search(r"\b(SELECT|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|CREATE\s+TABLE)\b", text, re.IGNORECASE))


def scan_table_access(known_tables: set[str]) -> dict[str, dict]:
    """Attribute table references in Python SQL strings to source units."""
    access: dict[str, dict] = {
        table: {"readers": set(), "writers": set(), "reader_files": set(), "writer_files": set()}
        for table in known_tables
    }
    for rel in walk_files((".py",)):
        if str(rel).startswith("tests/"):
            continue  # tests pin behaviour; they are not data-access owners
        unit = unit_for_path(rel)
        literals = [text for text in _python_string_literals(REPO_ROOT / rel) if _looks_like_sql(text)]
        if not literals:
            continue
        for text in literals:
            refs = extract_sql_table_refs(text)
            is_writer = bool(_SQL_VERB["writer"].search(text))
            is_reader = bool(_SQL_VERB["reader"].search(text))
            for ref in refs:
                leaf = ref.rsplit(".", 1)[-1]
                table = leaf if leaf in known_tables else (ref if ref in known_tables else None)
                if table is None:
                    continue
                if is_writer:
                    access[table]["writers"].add(unit)
                    access[table]["writer_files"].add(str(rel))
                if is_reader or is_writer:
                    access[table]["readers"].add(unit)
                    access[table]["reader_files"].add(str(rel))
    return access


def build() -> dict:
    objects, grants = parse_schema_objects()
    known_tables = {
        name for name, record in objects.items() if "TABLE" in record["kinds"]
    }
    access = scan_table_access(known_tables)

    tables = []
    for name in sorted(known_tables):
        record = objects[name]
        tables.append(
            {
                "table": name,
                "schema_qualified_in_ddl": record["schema_qualified"],
                "defined_in": record["defined_in"],
                "static_readers": sorted(access[name]["readers"]),
                "static_writers": sorted(access[name]["writers"]),
                "reader_files": sorted(access[name]["reader_files"]),
                "writer_files": sorted(access[name]["writer_files"]),
                "cross_unit_write": sorted(
                    {w for w in access[name]["writers"]} - {"database-tools"}
                ),
            }
        )

    functions = sorted(
        name for name, record in objects.items() if "FUNCTION" in record["kinds"]
    )
    views = sorted(
        name for name, record in objects.items()
        if "VIEW" in record["kinds"] or "MATERIALIZED_VIEW" in record["kinds"]
    )
    sequences = sorted(
        name for name, record in objects.items() if "SEQUENCE" in record["kinds"]
    )
    types = sorted(name for name, record in objects.items() if "TYPE" in record["kinds"])
    schemas = sorted(name for name, record in objects.items() if "SCHEMA" in record["kinds"])

    cross_unit_writes = sorted(
        {
            f"{row['table']}: {'+'.join(w for w in row['static_writers'] if w != 'database-tools')}"
            for row in tables
            if len({w for w in row["static_writers"]} - {"database-tools"}) > 1
        }
    )

    return {
        **base_envelope("data-access-inventory"),
        "purpose": (
            "Input for ARC-03 least-privilege grants and ARC-04 persistence owner separation. "
            "Static writer/reader attribution is a heuristic over SQL string literals; "
            "verify against runtime queries before narrowing any grant."
        ),
        "postgresql": {
            "ddl_sources": [str(p.relative_to(REPO_ROOT)) for p in _sql_files()],
            "object_counts": {
                "tables": len(tables),
                "functions": len(functions),
                "views": len(views),
                "sequences": len(sequences),
                "types": len(types),
                "schemas_declared": len(schemas),
                "grants_found": len(grants),
            },
            "schemas_declared": schemas,
            "functions": functions,
            "views": views,
            "sequences": sequences,
            "types": types,
            "grants": grants,
            "tables": tables,
            "tables_with_multiple_python_unit_writers": cross_unit_writes,
            "role_model": {
                "current": (
                    "All application services share one PostgreSQL user with a wide search_path; "
                    "schemas are named apart but data privileges are not separated (PRD §1)."
                ),
                "target": "ARC-03 per-service roles (ai_gateway_gateway/runtime/capability_worker/knowledge_api/knowledge_worker) generated from this inventory.",
            },
        },
        "qdrant": {
            "namespaces": [
                {
                    "namespace": "kb_{base}_{dimension} (+_summary / _sections suffixes)",
                    "owner": "knowledge-service",
                    "kind": "dataset collections",
                    "evidence": [
                        "apps/knowledge-service/src/knowledge_service/services/knowledge/vector_store.py (make_collection_name)",
                        "apps/knowledge-service/src/knowledge_service/services/knowledge/hierarchical_indexer.py (SUMMARY_COLLECTION_SUFFIX, SECTION_COLLECTION_SUFFIX)",
                    ],
                },
                {
                    "namespace": "kb_bm25_v2_canary_*",
                    "owner": "knowledge-service",
                    "kind": "BM25 v2 canary collections",
                    "evidence": [
                        "apps/knowledge-service/src/knowledge_service/services/knowledge/vector_store.py (bm25 v2 canary)"
                    ],
                },
                {
                    "namespace": "collection metadata keys knowledge_lexical / knowledge_scope",
                    "owner": "knowledge-service",
                    "kind": "collection metadata contract",
                    "evidence": [
                        "apps/knowledge-service/src/knowledge_service/services/knowledge/lexical_config.py (COLLECTION_METADATA_KEY, COLLECTION_SCOPE_METADATA_KEY)"
                    ],
                },
                {
                    "namespace": "agent memory vector collections (dynamic names from cleanup metadata vector_collections)",
                    "owner": "gateway data governance (Agent memory namespace)",
                    "kind": "memory vectors",
                    "evidence": [
                        "src/services/agent_runtime_cleanup.py (memory_vector_* cleanup over /collections/... endpoints)",
                        "packages/ai-gateway-core/src/ai_gateway_core/agents/runtime.py (agent_memory_principal am_* identity)",
                        "packages/ai-gateway-core/src/ai_gateway_core/persistence/repositories/agent_repository.py (agent memory principals)",
                    ],
                },
            ],
            "target": (
                "ARC-00/ARC-03: every collection/alias/prefix gets exactly one owner plus a "
                "negative-deletion test; cross-namespace reads/writes need a named API/function."
            ),
        },
        "redis": {
            "note": "Cache/counter only; no durable system of record lives in Redis.",
            "keyspaces": [
                {"prefix": "session:{session_id}", "owner": "ai-gateway-core (session cache)", "evidence": "packages/ai-gateway-core/src/ai_gateway_core/persistence/redis.py"},
                {"prefix": "cache:{service_id}:{input_hash}", "owner": "ai-gateway-core (response cache)", "evidence": "packages/ai-gateway-core/src/ai_gateway_core/persistence/redis.py"},
                {"prefix": "health:{service_id}", "owner": "ai-gateway-core (health cache)", "evidence": "packages/ai-gateway-core/src/ai_gateway_core/persistence/redis.py"},
                {"prefix": "service:{service_id}", "owner": "gateway service registry", "evidence": "src/services/registry/database_storage.py"},
                {"prefix": "task:{task_id}", "owner": "gateway task storage", "evidence": "src/services/task/database_task_storage.py"},
                {"prefix": "guest_session:{session_id}", "owner": "gateway guest sessions", "evidence": "src/services/session/guest_session_manager.py"},
                {"prefix": "ratelimit:global|ip|user|tenant|assistant|op", "owner": "gateway rate limiter", "evidence": "src/core/gateway/multi_dimension_rate_limiter.py"},
            ],
        },
        "object_storage": {
            "note": "Local path or S3/OSS bucket selected by config; prefixes below are the logical namespaces.",
            "namespaces": [
                {"prefix": "uploads (FileStorage.KEY_PREFIX)", "owner": "gateway user uploads", "evidence": "packages/ai-gateway-core/src/ai_gateway_core/storage/file_storage.py"},
                {"prefix": "configurable key_prefix (StorageConfig)", "owner": "gateway artifacts", "evidence": "packages/ai-gateway-core/src/ai_gateway_core/storage/artifact_storage.py"},
                {"prefix": "GATEWAY_STORAGE__KEY_PREFIX env prefix", "owner": "gateway image storage", "evidence": "packages/ai-gateway-core/src/ai_gateway_core/storage/image_storage.py"},
                {"prefix": "StorageSettings.key_prefix (default dev), local ./data/files or S3 bucket", "owner": "knowledge-service uploads + extracted images", "evidence": "apps/knowledge-service/src/knowledge_service/config/__init__.py (StorageSettings)"},
            ],
        },
    }


if __name__ == "__main__":
    from _common import write_json

    path = write_json("data-access-inventory.json", build())
    print(f"wrote {path}")
