"""Legacy-ledger detection, reconciliation receipts and baseline adoption.

Existing installations are never deleted, rewritten or faked.  The authority:

1. detects which pre-authority ledgers exist (filename, numeric, per-service
   meta);
2. verifies the legacy chain is complete up to the baseline freeze point,
   topping up partial databases through the compatibility runner first;
3. for numeric ledgers, duplicate version prefixes (016/031) are ambiguous —
   a reconciliation receipt records per-file object evidence and anything
   that cannot be proven stays ``BLOCKED``;
4. computes the four fingerprints and writes ONE adoption marker when (and
   only when) they match the frozen baseline; otherwise it emits a drift
   report and fails closed;
5. freezes the legacy ledgers as historical evidence — readable, never
   written again.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from . import ledger
from .constants import DEFAULT_ROLE_PREFIX
from .discovery import (
    HISTORICAL_FILENAME_DUPLICATES,
    LEGACY_FILENAME_ALIASES,
    LegacyMigration,
)
from .fingerprint import compute_fingerprints
from .manifest import BaselineManifest
from .runner import AuthorityBlockedError, AuthorityError


@dataclass(frozen=True)
class LedgerState:
    """Which legacy ledgers a database carries and what they record."""

    filename_ledger: bool = False
    numeric_ledger: bool = False
    numeric_has_dirty: bool = False
    per_service_ledger: bool = False
    applied_filenames: frozenset[str] = frozenset()
    applied_versions: frozenset[str] = frozenset()
    per_service_keys: frozenset[str] = frozenset()

    @property
    def has_any(self) -> bool:
        return self.filename_ledger or self.numeric_ledger or self.per_service_ledger


@dataclass
class ReconciliationReceipt:
    """Machine evidence for ambiguous legacy ledger history."""

    ledger_kind: str
    duplicates: dict[str, dict[str, Any]] = field(default_factory=dict)
    verdict: str = "proven"  # proven | blocked
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(
            {
                "ledger_kind": self.ledger_kind,
                "verdict": self.verdict,
                "duplicates": self.duplicates,
                "notes": self.notes,
            },
            indent=2,
            sort_keys=True,
        )


# Object/constraint evidence per ambiguous historical filename.  Files without
# structural evidence (pure data updates) can never be proven from a numeric
# ledger and must stay unprovable — guessing is forbidden.
DUPLICATE_EVIDENCE: dict[str, tuple[tuple[str, str], ...]] = {
    "016_confluence_multi_root_pages.sql": (
        (
            "confluence_space_bindings.root_page_ids exists",
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'confluence_space_bindings' "
            "AND column_name = 'root_page_ids')",
        ),
        (
            "confluence_space_bindings.sync_images exists",
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'confluence_space_bindings' "
            "AND column_name = 'sync_images')",
        ),
    ),
    "016_usage_hourly_aggregates.sql": (
        (
            "usage_hourly_aggregates table exists",
            "SELECT to_regclass('public.usage_hourly_aggregates') IS NOT NULL",
        ),
    ),
    "031_hierarchical_segments.sql": (
        (
            "segments.level exists",
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'segments' "
            "AND column_name = 'level')",
        ),
        (
            "document_summaries table exists",
            "SELECT to_regclass('public.document_summaries') IS NOT NULL",
        ),
    ),
    # Data-only UPDATEs leave no structural trace: unprovable by design.
    "031_align_model_prices_20260211.sql": (),
}


async def detect_legacy_state(conn: Any) -> LedgerState:
    """Read every legacy ledger without writing anything."""
    columns = await conn.fetch(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = 'schema_migrations'
        """
    )
    column_names = {str(row["column_name"]) for row in columns}
    filename_ledger = "filename" in column_names
    numeric_ledger = "version" in column_names

    applied_filenames: set[str] = set()
    applied_versions: set[str] = set()
    numeric_has_dirty = False
    if filename_ledger:
        rows = await conn.fetch("SELECT filename FROM public.schema_migrations")
        applied_filenames = {str(row["filename"]) for row in rows}
    if numeric_ledger:
        numeric_has_dirty = "dirty" in column_names
        dirty_filter = " WHERE dirty = FALSE" if numeric_has_dirty else ""
        rows = await conn.fetch(
            f"SELECT version FROM public.schema_migrations{dirty_filter}"
        )
        applied_versions = {f"{int(row['version']):03d}" for row in rows}

    per_service = await conn.fetchval(
        "SELECT to_regclass('public.schema_migrations_meta') IS NOT NULL"
    )
    per_service_keys: set[str] = set()
    if per_service:
        rows = await conn.fetch("SELECT name FROM public.schema_migrations_meta")
        per_service_keys = {str(row["name"]) for row in rows}

    return LedgerState(
        filename_ledger=filename_ledger,
        numeric_ledger=numeric_ledger,
        numeric_has_dirty=numeric_has_dirty,
        per_service_ledger=bool(per_service),
        applied_filenames=frozenset(applied_filenames),
        applied_versions=frozenset(applied_versions),
        per_service_keys=frozenset(per_service_keys),
    )


async def reconcile_numeric_duplicates(conn: Any) -> ReconciliationReceipt:
    """Prove or block each ambiguous duplicate version with object evidence."""
    receipt = ReconciliationReceipt(ledger_kind="numeric")
    for version, candidates in sorted(HISTORICAL_FILENAME_DUPLICATES.items()):
        version_entry: dict[str, Any] = {}
        for filename in sorted(candidates):
            checks = DUPLICATE_EVIDENCE.get(filename)
            if checks is None:
                raise AuthorityError(
                    f"no evidence registry entry for duplicate file {filename}"
                )
            results = []
            for description, query in checks:
                value = await conn.fetchval(query)
                results.append({"check": description, "passed": bool(value)})
            if not checks:
                verdict = "unprovable"
            else:
                verdict = "proven" if all(r["passed"] for r in results) else "not_proven"
            version_entry[filename] = {"evidence": results, "verdict": verdict}
            if verdict != "proven":
                receipt.verdict = "blocked"
        receipt.duplicates[version] = version_entry
    return receipt


def legacy_missing_files(
    state: LedgerState, migrations: list[LegacyMigration]
) -> list[LegacyMigration]:
    """Legacy files not covered by any legacy ledger row.

    Historical rename aliases count as applied: old deployments recorded the
    earlier ``codex_runtime`` filenames for the same changes.
    """
    missing: list[LegacyMigration] = []
    for migration in migrations:
        name = migration.path.name
        if state.filename_ledger:
            alias = LEGACY_FILENAME_ALIASES.get(name)
            if name in state.applied_filenames or (
                alias is not None and alias in state.applied_filenames
            ):
                continue
            missing.append(migration)
            continue
        if state.numeric_ledger and migration.version in state.applied_versions:
            continue
        missing.append(migration)
    return missing


async def adopt_baseline(
    conn: Any,
    baseline: BaselineManifest,
    *,
    manifest_sha256: str,
    role_prefix: str = DEFAULT_ROLE_PREFIX,
) -> dict[str, str]:
    """Write the adoption marker iff all four fingerprints match.

    Never executes init.sql; never repairs drift.  Returns the computed
    fingerprint dict on success and raises with a drift report otherwise.
    """
    existing = await conn.fetch(ledger.SELECT_BASELINE)
    if existing:
        current = str(existing[0]["baseline_id"])
        if current != baseline.baseline_id:
            raise AuthorityError(
                f"database already adopted baseline {current!r}; "
                f"refusing adoption marker for {baseline.baseline_id!r}"
            )
        return {"already_adopted": current}

    computed = await compute_fingerprints(
        conn, role_prefix=role_prefix, reference_sets=baseline.reference_data
    )
    drift: list[str] = []
    for fingerprint_class in ("structural", "acl", "extensions", "reference_data"):
        expected = baseline.fingerprints[fingerprint_class]
        actual = computed[fingerprint_class]
        if expected != actual:
            drift.append(
                f"{fingerprint_class}: expected {expected}, computed {actual}"
            )
    if drift:
        report = "; ".join(drift)
        raise AuthorityError(
            "baseline adoption refused — fingerprint drift detected; "
            f"drift report: {report}. No automatic repair is attempted."
        )

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
    return computed


def assert_legacy_ledgers_frozen(
    state: LedgerState, baseline_adopted: bool
) -> None:
    """After adoption the legacy ledgers are evidence, never writers."""
    if baseline_adopted and not state.has_any:
        raise AuthorityBlockedError(
            "baseline marker present but no legacy ledger found; "
            "the adoption history is incomplete"
        )
