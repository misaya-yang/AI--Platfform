"""Top-level authority operations (the only sanctioned schema writers).

Every write entrypoint acquires the one session advisory lock, keeps
transactions in the runner's hands, and fails closed instead of guessing.

Flow of ``command_migrate`` for a database without a baseline marker:

1. frozen baseline + empty database: install the baseline directly; legacy
   ``schema.sql`` and the historical chain are never replayed;
2. non-empty database guard: an unknown database without any platform object is
   refused (the authority never mistakes foreign data for a legacy install);
3. legacy base schema: ``database/schema.sql`` is applied when the required objects
   are missing (same rule as ``ensure_base_schema`` in migrate.sh);
4. legacy chain: the historical ``002…112`` files are executed by the
   authority's native executor, recorded in whichever legacy ledger shape
   the database already carries (filename ledger created when none exists);
5. per-service track: topped up ONLY on databases that already carry
   ``public.schema_migrations_meta``;
6. cutover + adoption (when the baseline files are frozen and adoption is
   allowed): roles → extensions → convergence change → grants → ledger →
   fingerprint comparison → ONE adoption marker.  Legacy ledgers are then
   frozen evidence, never written again.

Databases that already carry the marker only receive pending epoch changes
(``database/migrations/<baseline_id>/``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import ledger, legacy
from .adoption import (
    adopt_baseline,
    detect_legacy_state,
    legacy_missing_files,
)
from .bootstrap import (
    baseline_manifest_sha256,
    bootstrap_extensions,
    bootstrap_roles,
    database_empty,
    fresh_install,
    run_baseline_sql_file,
    startup_schema_check,
    verify_baseline_sql_file,
)
from .constants import DEFAULT_BASELINE_ID, EPOCH_MANIFEST_NAME, PLATFORM_SCHEMAS
from .discovery import LEGACY_MANIFEST_NAME, discover_legacy_migrations
from .manifest import (
    BaselineManifest,
    load_baseline_manifest,
    load_epoch_manifest,
    load_legacy_manifest,
)
from .numeric_reconciliation import (
    NumericReconciliationBlocked,
    NumericReconciliationReceipt,
    reconcile_numeric_legacy_history,
)
from .runner import AuthorityBlockedError, AuthorityError, AuthorityPaths, MigrationAuthority

SUPPORTED_BASELINES = frozenset({DEFAULT_BASELINE_ID})
# Applications built against the frozen baseline support epoch revisions up
# to this sequence number.  Bump together with the compatibility manifest.
MAX_SUPPORTED_EPOCH_SEQUENCE = 0


@dataclass(frozen=True)
class MigrationCommandResult:
    """Programmatic migrate result; CLI callers consume ``exit_code``."""

    exit_code: int
    reconciliation_receipt: NumericReconciliationReceipt | None = None

    def to_json(self) -> str:
        return json.dumps(
            {
                "exit_code": self.exit_code,
                "numeric_reconciliation": (
                    self.reconciliation_receipt.as_dict()
                    if self.reconciliation_receipt is not None
                    else {"verdict": "not_applicable"}
                ),
            },
            indent=2,
            sort_keys=True,
        )


def _write_migration_evidence(result: MigrationCommandResult, evidence_out: Path | None) -> None:
    """Write evidence only when the operator explicitly names an output path."""
    if evidence_out is None:
        return
    evidence_out.parent.mkdir(parents=True, exist_ok=True)
    evidence_out.write_text(result.to_json() + "\n", encoding="utf-8")


def default_paths() -> AuthorityPaths:
    database_dir = Path(__file__).resolve().parents[1]
    return AuthorityPaths(database_dir=database_dir)


def load_baseline(paths: AuthorityPaths, baseline_id: str) -> tuple[BaselineManifest, str]:
    baseline_dir = paths.baseline_dir(baseline_id)
    manifest_path = baseline_dir / "manifest.json"
    baseline = load_baseline_manifest(manifest_path)
    legacy_manifest = load_legacy_manifest(paths.migrations_root / LEGACY_MANIFEST_NAME)
    if baseline.last_legacy_change != legacy_manifest.freeze_point:
        raise AuthorityError(
            f"baseline {baseline.baseline_id} freezes legacy change "
            f"{baseline.last_legacy_change!r}, but the immutable legacy manifest "
            f"freezes {legacy_manifest.freeze_point!r}"
        )
    return baseline, baseline_manifest_sha256(manifest_path)


def baseline_ready(paths: AuthorityPaths, baseline_id: str = DEFAULT_BASELINE_ID) -> bool:
    """A baseline is usable only when all of its frozen files exist."""
    baseline_dir = paths.baseline_dir(baseline_id)
    required = (
        "manifest.json",
        "init.sql",
        "reference_data.sql",
        "grants.sql",
        "verify.sql",
        "cutover_convergence.sql",
    )
    return all((baseline_dir / name).exists() for name in required)


def _validate_adoption_marker(
    adopted: dict[str, Any],
    baseline: BaselineManifest,
    manifest_sha256: str,
) -> None:
    """Prove that an existing marker names this exact frozen baseline."""
    expected = {
        "baseline_id": baseline.baseline_id,
        "manifest_sha256": manifest_sha256,
        "structural_sha256": baseline.structural_sha256,
        "acl_sha256": baseline.acl_sha256,
        "extensions_sha256": baseline.extensions_sha256,
        "reference_data_sha256": baseline.reference_data_sha256,
        "source_git_sha": baseline.source_git_sha,
    }
    drift = [
        f"{field}: marker={adopted.get(field)!r}, manifest={value!r}"
        for field, value in expected.items()
        if str(adopted.get(field, "")) != str(value)
    ]
    if drift:
        raise AuthorityError(
            "adopted baseline marker does not match the frozen manifest; " + "; ".join(drift)
        )


# ----------------------------------------------------------------------
# migrate: the single write path
# ----------------------------------------------------------------------


async def _guard_known_database(conn: Any) -> None:
    """Refuse to treat an unknown non-empty database as a legacy install."""
    if await database_empty(conn):
        return
    if await legacy.base_schema_present(conn):
        return
    raise AuthorityBlockedError(
        "database is not empty and carries none of the required platform "
        "objects (services/datasets/documents/segments); the authority does "
        "not apply schema.sql or migrations to an unknown database"
    )


async def command_migrate(
    authority: MigrationAuthority,
    *,
    baseline_id: str = DEFAULT_BASELINE_ID,
    allow_adoption: bool = True,
    reconciliation_evidence_out: Path | None = None,
    log: Any = print,
) -> MigrationCommandResult:
    """Bring one database to the current schema revision.

    * baseline adopted  -> apply pending epoch changes only;
    * no baseline, baseline files ready, empty database -> install the frozen
      baseline and then apply its epoch;
    * no baseline, baseline files ready, existing database -> complete the
      legacy chain, cut over ownership/grants, adopt the baseline;
    * no baseline, baseline files not ready -> compatibility mode: apply the
      legacy chain only (pre-freeze transition behaviour).

    The structured result carries any numeric reconciliation receipt. JSON is
    written only when ``reconciliation_evidence_out`` is explicitly supplied.
    """
    paths = authority.paths

    if baseline_ready(paths, baseline_id):
        baseline, manifest_sha = load_baseline(paths, baseline_id)
    else:
        baseline = None
        manifest_sha = ""
        log(
            "authority: baseline files not frozen yet — compatibility mode "
            "(legacy chain only, no adoption)"
        )

    reconciliation_receipt: NumericReconciliationReceipt | None = None
    lock_conn = await authority.connect()
    await authority.acquire_lock(lock_conn)
    try:
        conn = await authority.connect()
        try:
            adopted = await authority.adopted_baseline(conn)

            if adopted is None:
                if baseline is not None and await database_empty(
                    conn,
                    allowed_empty_schemas=tuple(PLATFORM_SCHEMAS),
                ):
                    await fresh_install(
                        conn,
                        paths,
                        baseline,
                        manifest_sha,
                        role_prefix=authority.role_prefix,
                    )
                    epoch_dir = paths.epoch_dir(baseline_id)
                    manifest_path = epoch_dir / EPOCH_MANIFEST_NAME
                    if manifest_path.exists():
                        epoch_manifest = load_epoch_manifest(manifest_path)
                        for line in await authority.apply_epoch(conn, epoch_manifest, epoch_dir):
                            log(f"authority: {line}")
                    log(f"authority: fresh install on baseline {baseline.baseline_id} complete")
                    result = MigrationCommandResult(0)
                    _write_migration_evidence(result, reconciliation_evidence_out)
                    return result
                await _guard_known_database(conn)
                try:
                    _, _, reconciliation_receipt = await legacy.apply_legacy_chain(
                        conn, paths, log=log
                    )
                except NumericReconciliationBlocked as exc:
                    log("authority: numeric reconciliation BLOCKED:\n" + exc.receipt.to_json())
                    _write_migration_evidence(
                        MigrationCommandResult(1, exc.receipt),
                        reconciliation_evidence_out,
                    )
                    raise
                await legacy.apply_per_service_chain(conn, paths, log=log)
                if baseline is None or not allow_adoption:
                    result = MigrationCommandResult(0, reconciliation_receipt)
                    _write_migration_evidence(result, reconciliation_evidence_out)
                    return result
                try:
                    await _cutover_and_adopt(
                        conn,
                        authority,
                        baseline,
                        manifest_sha,
                        reconciliation_receipt=reconciliation_receipt,
                        log=log,
                    )
                except NumericReconciliationBlocked as exc:
                    log("authority: numeric reconciliation BLOCKED:\n" + exc.receipt.to_json())
                    _write_migration_evidence(
                        MigrationCommandResult(1, exc.receipt),
                        reconciliation_evidence_out,
                    )
                    raise
                result = MigrationCommandResult(0, reconciliation_receipt)
                _write_migration_evidence(result, reconciliation_evidence_out)
                return result

            # Phase 2: epoch changes under the frozen baseline.
            if baseline is None:
                raise AuthorityError(
                    f"database carries adopted baseline {adopted['baseline_id']!r}, but "
                    f"the frozen local manifest for {baseline_id!r} is unavailable"
                )
            _validate_adoption_marker(adopted, baseline, manifest_sha)
            epoch_dir = paths.epoch_dir(baseline_id)
            manifest_path = epoch_dir / EPOCH_MANIFEST_NAME
            if manifest_path.exists():
                epoch_manifest = load_epoch_manifest(manifest_path)
                for line in await authority.apply_epoch(conn, epoch_manifest, epoch_dir):
                    log(f"authority: {line}")
                if not epoch_manifest.changes:
                    log("authority: no post-baseline epoch changes declared")
            else:
                log("authority: no epoch manifest; nothing to apply")
        finally:
            await conn.close()
    finally:
        await authority.release_lock(lock_conn)
    result = MigrationCommandResult(0, reconciliation_receipt)
    _write_migration_evidence(result, reconciliation_evidence_out)
    return result


async def _cutover_and_adopt(
    conn: Any,
    authority: MigrationAuthority,
    baseline: BaselineManifest,
    manifest_sha: str,
    *,
    reconciliation_receipt: NumericReconciliationReceipt | None = None,
    log: Any = print,
) -> None:
    """Cut a completed legacy database over to the baseline, then adopt.

    Runs under the caller's advisory lock.  Order matters: roles and
    extensions first (grants reference the roles), then the convergence
    change, then least-privilege grants, then the ledger + marker.
    """
    existing = await authority.adopted_baseline(conn)
    if existing is not None:
        _validate_adoption_marker(existing, baseline, manifest_sha)
        log(f"authority: baseline already adopted ({existing['baseline_id']})")
        return

    paths = authority.paths
    baseline_dir = paths.baseline_dir(baseline.baseline_id)
    cutover_path = baseline_dir / "cutover_convergence.sql"
    grants_path = baseline_dir / "grants.sql"

    state = await detect_legacy_state(conn)
    if not state.has_any:
        raise AuthorityBlockedError(
            "no legacy ledger present at adoption time; the compatibility "
            "runner must complete the chain before the baseline marker"
        )

    migrations = discover_legacy_migrations(paths.migrations_root)
    missing = legacy_missing_files(state, migrations)
    if missing:
        raise AuthorityBlockedError(
            "legacy chain incomplete; missing files: "
            + ", ".join(m.path.name for m in missing)
            + " — run the compatibility runner first"
        )

    if state.numeric_ledger and not state.filename_ledger:
        receipt = reconciliation_receipt
        if receipt is None:
            receipt = await reconcile_numeric_legacy_history(
                conn,
                load_legacy_manifest(paths.migrations_root / LEGACY_MANIFEST_NAME),
            )
        if receipt.verdict != "proven":
            raise NumericReconciliationBlocked(
                "numeric ledger reconciliation BLOCKED; receipt:",
                receipt,
            )
        log("authority: numeric-ledger reconciliation receipt proven")

    # Roles/extensions are idempotent; an adopted database must carry the
    # same role set and search_path configuration as a fresh install.
    await bootstrap_roles(conn, paths, authority.role_prefix)
    await bootstrap_extensions(conn, paths, authority.role_prefix)

    await run_baseline_sql_file(conn, cutover_path, role_prefix=authority.role_prefix)
    await run_baseline_sql_file(conn, grants_path, role_prefix=authority.role_prefix)
    await verify_baseline_sql_file(conn, baseline_dir / "verify.sql")

    await conn.execute(ledger.LEDGER_DDL)
    computed = await adopt_baseline(
        conn,
        baseline,
        manifest_sha256=manifest_sha,
        role_prefix=authority.role_prefix,
    )
    if "already_adopted" in computed:
        adopted = await authority.adopted_baseline(conn)
        if adopted is None:
            raise AuthorityError(
                "adoption reported an existing marker, but no marker row can be read"
            )
        _validate_adoption_marker(adopted, baseline, manifest_sha)
        log(f"authority: baseline already adopted ({computed['already_adopted']})")
    else:
        log(
            f"authority: baseline {baseline.baseline_id} adopted; "
            "legacy ledgers frozen as historical evidence"
        )


# ----------------------------------------------------------------------
# init-fresh: empty database -> baseline
# ----------------------------------------------------------------------


async def command_init_fresh(
    authority: MigrationAuthority,
    *,
    baseline_id: str = DEFAULT_BASELINE_ID,
    log: Any = print,
) -> int:
    paths = authority.paths
    if not baseline_ready(paths, baseline_id):
        raise AuthorityError(
            f"baseline {baseline_id} is not frozen (missing files under "
            f"{paths.baseline_dir(baseline_id)}); refusing fresh install"
        )
    baseline, manifest_sha = load_baseline(paths, baseline_id)

    lock_conn = await authority.connect()
    await authority.acquire_lock(lock_conn)
    try:
        conn = await authority.connect()
        try:
            await fresh_install(
                conn, paths, baseline, manifest_sha, role_prefix=authority.role_prefix
            )
            epoch_dir = paths.epoch_dir(baseline_id)
            manifest_path = epoch_dir / EPOCH_MANIFEST_NAME
            if manifest_path.exists():
                epoch_manifest = load_epoch_manifest(manifest_path)
                for line in await authority.apply_epoch(conn, epoch_manifest, epoch_dir):
                    log(f"authority: {line}")
            log(f"authority: fresh install on baseline {baseline.baseline_id} complete")
        finally:
            await conn.close()
    finally:
        await authority.release_lock(lock_conn)
    return 0


# ----------------------------------------------------------------------
# read-only surfaces
# ----------------------------------------------------------------------


async def command_status(authority: MigrationAuthority, *, log: Any = print) -> int:
    """Absolutely read-only status over baseline, ledger and legacy tables."""
    conn = await authority.connect(read_only=True)
    try:
        state = await detect_legacy_state(conn)
        adopted = await authority.adopted_baseline(conn)
        if adopted:
            log(f"baseline: {adopted['baseline_id']} (adopted {adopted['adopted_at']})")
            baseline_id = adopted["baseline_id"]
            rows = await conn.fetch(ledger.SELECT_APPLIED_CHANGES, baseline_id)
            if rows:
                for row in rows:
                    log(f"  epoch change {row['sequence']}: {row['checksum_sha256']}")
            else:
                log("  no post-baseline epoch changes applied")
            attempts_present = await conn.fetchval(
                "SELECT to_regclass($1) IS NOT NULL", f"public.{ledger.ATTEMPTS_TABLE}"
            )
            if attempts_present:
                open_attempts = await conn.fetch(
                    f"SELECT attempt_id, sequence, state, checkpoint "
                    f"FROM public.{ledger.ATTEMPTS_TABLE} "
                    "ORDER BY started_at DESC LIMIT 5"
                )
                for attempt in open_attempts:
                    log(
                        f"  attempt {attempt['attempt_id']}: seq={attempt['sequence']} "
                        f"state={attempt['state']} checkpoint={attempt['checkpoint']}"
                    )
        else:
            log("baseline: none adopted")
        if state.filename_ledger:
            log(f"legacy filename ledger rows: {len(state.applied_filenames)}")
        if state.numeric_ledger:
            log(f"legacy numeric ledger rows: {len(state.applied_versions)}")
        if state.per_service_ledger:
            log(f"per-service ledger rows: {len(state.per_service_keys)}")
        pending_legacy = (
            legacy_missing_files(state, authority.discover_legacy())
            if state.has_any
            else authority.discover_legacy()
        )
        log(f"legacy chain pending files: {len(pending_legacy)}")
    finally:
        await conn.close()
    return 0


async def command_verify(
    authority: MigrationAuthority,
    *,
    baseline_id: str = DEFAULT_BASELINE_ID,
    log: Any = print,
) -> int:
    """Absolutely read-only fingerprint verification against the baseline."""
    paths = authority.paths
    if not baseline_ready(paths, baseline_id):
        raise AuthorityError(f"baseline {baseline_id} is not frozen; cannot verify")
    baseline, manifest_sha = load_baseline(paths, baseline_id)

    conn = await authority.connect(read_only=True)
    try:
        from .fingerprint import compute_fingerprints

        adopted = await authority.adopted_baseline(conn)
        if adopted is not None:
            _validate_adoption_marker(adopted, baseline, manifest_sha)

        computed = await compute_fingerprints(
            conn, role_prefix=authority.role_prefix, reference_sets=baseline.reference_data
        )
        failures = []
        for name in ("structural", "acl", "extensions", "reference_data"):
            expected = baseline.fingerprints[name]
            actual = computed[name]
            status = "match" if expected == actual else "DRIFT"
            log(f"fingerprint {name}: {status}")
            if expected != actual:
                failures.append(name)

        checks = await verify_baseline_sql_file(
            conn, paths.baseline_dir(baseline_id) / "verify.sql"
        )
        for check in checks:
            log(f"verify check {check['check_name']}: match")

        if failures:
            raise AuthorityError(f"verification failed for: {failures}")
        log("verify: baseline fingerprints match; database is read-only-verified")
    finally:
        await conn.close()
    return 0


async def command_startup_check(
    authority: MigrationAuthority,
    *,
    log: Any = print,
) -> int:
    """Application startup gate: supported revision + required objects."""
    conn = await authority.connect(read_only=True)
    try:
        result = await startup_schema_check(
            conn,
            SUPPORTED_BASELINES,
            max_epoch_sequence=MAX_SUPPORTED_EPOCH_SEQUENCE,
        )
    finally:
        await conn.close()
    if not result["ok"]:
        reason = result.get("reason") or (
            "missing required objects: " + ", ".join(result["missing_objects"])
        )
        raise AuthorityError(f"startup schema check failed: {reason}")
    log(f"startup schema check passed (epoch={result['epoch']})")
    return 0
