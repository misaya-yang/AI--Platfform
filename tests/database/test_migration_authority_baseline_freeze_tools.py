from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BASELINE = ROOT / "database/baselines/2026_08_post_kb_v1"
sys.path.insert(0, str(ROOT / "scripts/inventory"))
sys.path.insert(0, str(ROOT / "scripts/database"))

import database_policy  # noqa: E402
import freeze_baseline  # noqa: E402
import generate_database_grants  # noqa: E402
import render_baseline_contract  # noqa: E402

from database.authority.commands import baseline_ready  # noqa: E402
from database.authority.runner import AuthorityPaths  # noqa: E402


def test_pending_baseline_is_deterministic_but_never_activates() -> None:
    manifest = json.loads((BASELINE / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["state"] == "pending-live-freeze"
    assert manifest["source_git_sha"] is None
    assert manifest["structural_sha256"] is None
    assert manifest["acl_sha256"] is None
    assert manifest["extensions_sha256"] is None
    assert manifest["reference_data_sha256"] is None
    assert not baseline_ready(AuthorityPaths(ROOT / "database"))
    for field, filenames in (
        ("files_sha256", freeze_baseline.SQL_FILES),
        ("policy_files_sha256", freeze_baseline.POLICY_FILES),
    ):
        assert set(manifest[field]) == set(filenames)
        for filename in filenames:
            assert manifest[field][filename] == hashlib.sha256(
                (BASELINE / filename).read_bytes()
            ).hexdigest()


def test_static_policy_and_sql_regenerate_exactly() -> None:
    inventory, ownership, grants_policy = database_policy.build(
        pending_live_freeze=True
    )
    generated = {
        "data-access-inventory.json": database_policy._serialized(inventory),
        "ownership-policy.json": database_policy._serialized(ownership),
        "grants-policy.json": database_policy._serialized(grants_policy),
    }
    for filename, content in generated.items():
        assert (BASELINE / filename).read_bytes() == content

    grants = generate_database_grants.generate_grants_sql(
        generated["data-access-inventory.json"],
        generated["grants-policy.json"],
    )
    assert (BASELINE / "grants.sql").read_text(encoding="utf-8") == grants
    objects = render_baseline_contract.load_policy_bytes(
        generated["data-access-inventory.json"],
        generated["ownership-policy.json"],
    )
    matrix = render_baseline_contract.load_grant_matrix(
        generated["data-access-inventory.json"],
        generated["grants-policy.json"],
    )
    cutover = (BASELINE / "cutover_convergence.sql").read_text(encoding="utf-8")
    assert render_baseline_contract.replace_relocation(
        cutover,
        render_baseline_contract.render_relocation(objects),
    ) == cutover
    assert render_baseline_contract.render_verify(objects, matrix) == (
        BASELINE / "verify.sql"
    ).read_text(encoding="utf-8")

    # Three authority/legacy ledgers are deliberately outside business-object
    # relocation, leaving 163 managed tables from the 166-table inventory.
    assert len([item for item in objects if item.kind == "table"]) == 163
    assert len([item for item in objects if item.kind == "function"]) == 45
    assert len([item for item in objects if item.kind == "sequence"]) == 26
    assert len([item for item in objects if item.kind == "view"]) == 2


def test_ownership_and_grant_inputs_fail_closed_on_drift() -> None:
    inventory = (BASELINE / "data-access-inventory.json").read_bytes()
    ownership = json.loads((BASELINE / "ownership-policy.json").read_bytes())
    ownership["objects"].pop()
    with pytest.raises(render_baseline_contract.RenderContractError, match="coverage mismatch"):
        render_baseline_contract.load_policy_bytes(
            inventory,
            (json.dumps(ownership, sort_keys=True) + "\n").encode(),
        )

    grants = json.loads((BASELINE / "grants-policy.json").read_bytes())
    grants["inventory_sha256"] = "0" * 64
    with pytest.raises(generate_database_grants.GrantContractError, match="does not bind"):
        generate_database_grants.generate_grants_sql(
            inventory,
            (json.dumps(grants, sort_keys=True) + "\n").encode(),
        )


def test_pg_dump_normalization_rejects_acl_and_ledger_bypass() -> None:
    normalized = freeze_baseline.normalize_pg_dump(
        "-- noise\n"
        "\\restrict random\n"
        "SET search_path = public;\n"
        "CREATE SCHEMA gateway;\n"
        "CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public;\n"
        "CREATE TABLE gateway.widgets (id integer);\n"
        "\\unrestrict random\n"
    )
    assert "random" not in normalized
    assert "CREATE SCHEMA" not in normalized
    assert "CREATE EXTENSION" not in normalized
    assert "CREATE TABLE gateway.widgets" in normalized

    for forbidden in (
        "CREATE TABLE gateway.widgets (id integer);\nGRANT SELECT ON gateway.widgets TO x;\n",
        "CREATE TABLE public.schema_migrations (version text);\n",
    ):
        with pytest.raises(freeze_baseline.FreezeError, match="forbidden"):
            freeze_baseline.normalize_pg_dump(forbidden)


def test_reference_policy_excludes_admin_editable_catalogs() -> None:
    tables = {spec.table for spec in freeze_baseline.REFERENCE_SPECS}

    assert tables == {
        "gateway.permissions",
        "gateway.rbac_roles",
        "gateway.role_permissions",
    }
    assert not any(
        name in table
        for table in tables
        for name in ("provider", "model", "rate_limit", "tenant", "user_roles")
    )
