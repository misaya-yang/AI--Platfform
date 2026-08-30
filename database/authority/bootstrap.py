"""Fresh-install bootstrap, role creation and application startup checks.

Fresh install order (PRD ARC-03 §3B.7):

    empty preflight -> bootstrap/roles.sql -> bootstrap/extensions.sql ->
    baselines/<id>/init.sql -> reference_data.sql -> grants.sql ->
    fingerprint verification -> adoption marker

An existing, non-empty database NEVER receives init.sql; it can only go
through reconciliation/adoption.  Application startup performs a read-only
check of the supported baseline/epoch revision plus required objects; full
fingerprinting belongs to the migrator/status path.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from . import ledger
from .adoption import validate_existing_adoption_marker
from .constants import DEFAULT_ROLE_PREFIX, LEDGER_TABLES
from .fingerprint import compute_fingerprints
from .manifest import BaselineManifest
from .runner import AuthorityError, AuthorityPaths

REQUIRED_OBJECTS: tuple[tuple[str, ...], ...] = (
    ("public.services", "gateway.services"),
    ("public.datasets", "knowledge.datasets"),
    ("public.documents", "knowledge.documents"),
    ("public.segments", "knowledge.segments"),
    ("public.users",),
    ("public.permissions",),
    ("public.rbac_roles",),
)

_SUPPORTED_BASELINE_REVISIONS_QUERY = f"""
SELECT baseline_id FROM public.{ledger.BASELINES_TABLE}
"""


def render_role_sql(role_sql: str, role_prefix: str) -> str:
    """Substitute the configurable LOGIN/NOLOGIN role prefix.

    The checked-in file uses the canonical ``ai_gateway_`` prefix; managed
    deployments can namespace roles without changing the SQL's meaning.
    """
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,20}_", role_prefix):
        raise AuthorityError(f"unsafe role prefix: {role_prefix!r}")
    return role_sql.replace("ai_gateway_", role_prefix)


async def bootstrap_roles(conn: Any, paths: AuthorityPaths, role_prefix: str) -> None:
    roles_sql = (paths.bootstrap_dir / "roles.sql").read_text(encoding="utf-8")
    await conn.execute(render_role_sql(roles_sql, role_prefix))


async def bootstrap_extensions(conn: Any, paths: AuthorityPaths) -> None:
    extensions_sql = (paths.bootstrap_dir / "extensions.sql").read_text(encoding="utf-8")
    await conn.execute(extensions_sql)


async def run_baseline_sql_file(
    conn: Any, path: Path, *, role_prefix: str | None = None
) -> None:
    """Execute one baseline SQL file in a runner-owned transaction.

    Files that name platform roles (cutover, grants) are rendered with the
    deployment's configurable role prefix first; pure DDL/data files
    (init.sql, reference_data.sql) are executed verbatim.
    """
    if not path.exists():
        raise AuthorityError(f"required baseline file missing: {path}")
    sql = path.read_text(encoding="utf-8")
    if role_prefix is not None:
        sql = render_role_sql(sql, role_prefix)
    async with conn.transaction():
        await conn.execute(sql)


async def database_empty(conn: Any) -> bool:
    """True when the database has no user-created schema objects.

    Relations alone are insufficient: an enum, function, custom schema or
    preinstalled extension can collide with the frozen init while still
    making the old relation-only preflight report "empty".
    """
    count = await conn.fetchval(
        """
        SELECT sum(object_count)
        FROM (
            SELECT count(*) AS object_count
            FROM pg_class AS c
            JOIN pg_namespace AS n ON n.oid = c.relnamespace
            WHERE n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
              AND n.nspname NOT LIKE 'pg_temp%'
              AND n.nspname NOT LIKE 'pg_toast_temp%'
              AND c.relkind IN ('r', 'p', 'm', 'S', 'v', 'f', 'c')
            UNION ALL
            SELECT count(*)
            FROM pg_proc AS p
            JOIN pg_namespace AS n ON n.oid = p.pronamespace
            WHERE n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
              AND n.nspname NOT LIKE 'pg_temp%'
              AND n.nspname NOT LIKE 'pg_toast_temp%'
            UNION ALL
            SELECT count(*)
            FROM pg_type AS t
            JOIN pg_namespace AS n ON n.oid = t.typnamespace
            WHERE n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
              AND n.nspname NOT LIKE 'pg_temp%'
              AND n.nspname NOT LIKE 'pg_toast_temp%'
              AND t.typrelid = 0
              AND t.typelem = 0
              AND t.typtype IN ('b', 'c', 'd', 'e', 'm', 'r')
            UNION ALL
            SELECT count(*)
            FROM pg_namespace AS n
            WHERE n.nspname NOT IN ('public', 'pg_catalog', 'information_schema', 'pg_toast')
              AND n.nspname NOT LIKE 'pg_temp%'
              AND n.nspname NOT LIKE 'pg_toast_temp%'
            UNION ALL
            SELECT count(*)
            FROM pg_extension
            WHERE extname <> 'plpgsql'
        ) AS user_objects
        """
    )
    return not count


async def preflight_database_empty(conn: Any) -> None:
    """Fresh installs require a truly empty database."""
    if not await database_empty(conn):
        raise AuthorityError(
            "database is not empty; init.sql must never run against a "
            "non-empty database — use reconciliation/adoption"
        )


async def fresh_install(
    conn: Any,
    paths: AuthorityPaths,
    baseline: BaselineManifest,
    manifest_sha256: str,
    *,
    role_prefix: str = DEFAULT_ROLE_PREFIX,
) -> dict[str, str]:
    """Bootstrap one empty database atomically onto the frozen baseline.

    Roles, extensions, schema, reference rows, grants, fingerprints, ledger and
    marker share one outer transaction.  The per-file transactions below are
    savepoints when used with asyncpg.  Any failure therefore leaves no partial
    init that would make the next empty-database preflight permanently fail.
    Fresh install deliberately has no durable attempt row: rollback means
    "not started", while the atomic baseline marker means "complete".
    """
    ledger_presence = {
        table: bool(
            await conn.fetchval(
                "SELECT to_regclass($1) IS NOT NULL", f"public.{table}"
            )
        )
        for table in LEDGER_TABLES
    }
    ledger_present = ledger_presence[ledger.BASELINES_TABLE]
    if ledger_present:
        missing_ledger = sorted(
            table for table, present in ledger_presence.items() if not present
        )
        if missing_ledger:
            raise AuthorityError(
                "fresh-install marker ledger is incomplete; missing tables "
                f"{missing_ledger}"
            )
        existing = await conn.fetch(ledger.SELECT_BASELINE)
        if not existing:
            raise AuthorityError(
                "fresh-install ledger exists without an adoption marker; "
                "refusing to guess whether a partial or foreign schema is safe"
            )
        validate_existing_adoption_marker(
            existing, baseline, manifest_sha256=manifest_sha256
        )
        computed = await compute_fingerprints(
            conn, role_prefix=role_prefix, reference_sets=baseline.reference_data
        )
        drift = [
            f"{name}: expected {baseline.fingerprints[name]}, computed {computed[name]}"
            for name in ("structural", "acl", "extensions", "reference_data")
            if baseline.fingerprints[name] != computed[name]
        ]
        if drift:
            raise AuthorityError(
                "fresh-install marker exists but live fingerprints drifted: "
                + "; ".join(drift)
            )
        return computed

    await preflight_database_empty(conn)
    async with conn.transaction():
        await bootstrap_roles(conn, paths, role_prefix)
        await bootstrap_extensions(conn, paths)

        baseline_dir = paths.baseline_dir(baseline.baseline_id)
        # Objects created for the baseline are owned by the NOLOGIN owner; the
        # connecting session borrows that identity only inside this transaction.
        owner_role = f"{role_prefix}owner"
        await conn.execute(f'SET LOCAL ROLE "{owner_role}"')
        try:
            for file_name in ("init.sql", "reference_data.sql"):
                await run_baseline_sql_file(conn, baseline_dir / file_name)
        finally:
            await conn.execute("RESET ROLE")
        await run_baseline_sql_file(
            conn, baseline_dir / "grants.sql", role_prefix=role_prefix
        )

        computed = await compute_fingerprints(
            conn, role_prefix=role_prefix, reference_sets=baseline.reference_data
        )
        drift = [
            f"{name}: expected {baseline.fingerprints[name]}, computed {computed[name]}"
            for name in ("structural", "acl", "extensions", "reference_data")
            if baseline.fingerprints[name] != computed[name]
        ]
        if drift:
            raise AuthorityError(
                "fresh install fingerprint mismatch — baseline files do not "
                f"reproduce the frozen baseline: {'; '.join(drift)}"
            )

        await conn.execute(ledger.LEDGER_DDL)
        await conn.execute(
            ledger.INSERT_BASELINE_MARKER,
            baseline.baseline_id,
            manifest_sha256,
            baseline.structural_sha256,
            baseline.acl_sha256,
            baseline.extensions_sha256,
            baseline.reference_data_sha256,
            baseline.source_git_sha,
        )
        inserted = await conn.fetch(ledger.SELECT_BASELINE)
        validate_existing_adoption_marker(
            inserted, baseline, manifest_sha256=manifest_sha256
        )
    return computed


async def startup_schema_check(
    conn: Any,
    supported_baselines: frozenset[str],
    *,
    max_epoch_sequence: int,
) -> dict[str, Any]:
    """Read-only application startup validation.

    Checks the supported baseline/epoch revision and required objects only.
    Legacy (pre-authority) databases pass when their required objects exist;
    they are recognized as the legacy epoch and full fingerprinting is left
    to the migrator/status path.
    """
    result: dict[str, Any] = {"ok": True, "epoch": None, "missing_objects": []}

    for candidates in REQUIRED_OBJECTS:
        found = False
        for qualified in candidates:
            if await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", qualified):
                found = True
                break
        if not found:
            result["missing_objects"].append(" or ".join(candidates))

    ledger_present = await conn.fetchval(
        "SELECT to_regclass($1) IS NOT NULL", f"public.{ledger.BASELINES_TABLE}"
    )
    if not ledger_present:
        if result["missing_objects"]:
            result["ok"] = False
        else:
            result["epoch"] = "legacy"
        return result

    rows = await conn.fetch(_SUPPORTED_BASELINE_REVISIONS_QUERY)
    if not rows:
        result["ok"] = False
        result["reason"] = (
            "migration authority ledger exists but no baseline is adopted; "
            "run the migrator before starting the application"
        )
        return result
    baseline_id = str(rows[0]["baseline_id"])
    if baseline_id not in supported_baselines:
        result["ok"] = False
        result["reason"] = (
            f"adopted baseline {baseline_id!r} is not in the supported set "
            f"{sorted(supported_baselines)}; refusing to start"
        )
        return result

    max_sequence = await conn.fetchval(
        f"SELECT COALESCE(max(sequence), 0) FROM public.{ledger.CHANGES_TABLE} "
        "WHERE baseline_id = $1",
        baseline_id,
    )
    if int(max_sequence) > max_epoch_sequence:
        result["ok"] = False
        result["reason"] = (
            f"database schema revision {baseline_id}:{max_sequence} is newer "
            f"than this application supports (max {max_epoch_sequence}); "
            "refusing to start"
        )
        return result

    result["epoch"] = f"{baseline_id}:{max_sequence}"
    if result["missing_objects"]:
        result["ok"] = False
    return result


def baseline_manifest_sha256(manifest_path: Path) -> str:
    return hashlib.sha256(manifest_path.read_bytes()).hexdigest()
