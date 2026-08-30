from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from database.authority import bootstrap, commands
from database.authority.manifest import (
    AuthorityManifestError,
    load_baseline_manifest,
    verify_baseline_git_provenance,
)
from database.authority.runner import AuthorityError, AuthorityPaths

BASELINE_ID = "2026_08_post_kb_v1"
BASELINE_FILES = (
    "cutover_convergence.sql",
    "grants.sql",
    "init.sql",
    "reference_data.sql",
    "verify.sql",
)
POLICY_FILES = (
    "data-access-inventory.json",
    "ownership-policy.json",
    "grants-policy.json",
)


def _sha(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _write_baseline(root: Path, **overrides: object) -> tuple[AuthorityPaths, Path]:
    paths = AuthorityPaths(root / "database")
    baseline_dir = paths.baseline_dir(BASELINE_ID)
    baseline_dir.mkdir(parents=True, exist_ok=True)
    files_sha256: dict[str, str] = {}
    for filename in BASELINE_FILES:
        content = f"-- {filename}\n"
        (baseline_dir / filename).write_text(content, encoding="utf-8")
        files_sha256[filename] = _sha(content)
    policy_files_sha256: dict[str, str] = {}
    for filename in POLICY_FILES:
        content = "{}\n"
        (baseline_dir / filename).write_text(content, encoding="utf-8")
        policy_files_sha256[filename] = _sha(content)
    payload: dict[str, object] = {
        "schema": "migration-authority/baseline-manifest/v1",
        "state": "frozen",
        "baseline_id": BASELINE_ID,
        "schema_revision": "112",
        "source_git_sha": "a" * 40,
        "last_legacy_change": "112_kb_document_progress_retention.sql",
        "structural_sha256": "1" * 64,
        "acl_sha256": "2" * 64,
        "extensions_sha256": "3" * 64,
        "reference_data_sha256": "4" * 64,
        "generator": "test",
        "generated_at": "2026-08-30T00:00:00Z",
        "postgres_version": "16",
        "files_sha256": files_sha256,
        "policy_files_sha256": policy_files_sha256,
        "reference_data": [
            {
                "table": "public.system_values",
                "natural_key": ["key"],
                "immutable_columns": ["value"],
            }
        ],
    }
    payload.update(overrides)
    manifest = baseline_dir / "manifest.json"
    manifest.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return paths, manifest


def test_baseline_manifest_binds_every_required_sql_file(tmp_path: Path) -> None:
    paths, manifest = _write_baseline(tmp_path)

    baseline = load_baseline_manifest(manifest)

    assert dict(baseline.files_sha256) == {
        filename: hashlib.sha256(
            (paths.baseline_dir(BASELINE_ID) / filename).read_bytes()
        ).hexdigest()
        for filename in BASELINE_FILES
    }
    assert commands.baseline_ready(paths)


def test_baseline_manifest_rejects_checksum_drift_and_extra_sql(tmp_path: Path) -> None:
    paths, manifest = _write_baseline(tmp_path)
    init_sql = paths.baseline_dir(BASELINE_ID) / "init.sql"
    init_sql.write_text("-- changed\n", encoding="utf-8")

    with pytest.raises(AuthorityManifestError, match="checksum drift for init.sql"):
        load_baseline_manifest(manifest)

    _paths, second_manifest = _write_baseline(tmp_path / "second")
    (second_manifest.parent / "unreviewed.sql").write_text("SELECT 1;\n", encoding="utf-8")
    with pytest.raises(AuthorityManifestError, match="SQL coverage mismatch"):
        load_baseline_manifest(second_manifest)


def test_baseline_git_provenance_requires_a_prior_unchanged_source_commit(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "arc03@example.invalid")
    _git(root, "config", "user.name", "ARC03 Test")
    generator = root / "scripts/freeze_arc03.py"
    generator.parent.mkdir(parents=True)
    generator.write_text("GENERATOR = 1\n", encoding="utf-8")
    cutover = root / f"database/baselines/{BASELINE_ID}/cutover_convergence.sql"
    cutover.parent.mkdir(parents=True)
    cutover.write_text("-- cutover_convergence.sql\n", encoding="utf-8")
    _write_baseline(
        root,
        state="pending-live-freeze",
        source_git_sha=None,
        structural_sha256=None,
        acl_sha256=None,
        extensions_sha256=None,
        reference_data_sha256=None,
        generator="scripts/freeze_arc03.py",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "settled source")
    source_sha = _git(root, "rev-parse", "HEAD")

    _paths, manifest_path = _write_baseline(
        root,
        source_git_sha=source_sha,
        generator="scripts/freeze_arc03.py",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "frozen artifact")
    baseline = load_baseline_manifest(manifest_path)

    verify_baseline_git_provenance(manifest_path, baseline, repo_root=root)

    generator.write_text("GENERATOR = 2\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "drift generator")
    with pytest.raises(AuthorityManifestError, match="inputs differ"):
        verify_baseline_git_provenance(manifest_path, baseline, repo_root=root)


def test_baseline_git_provenance_rejects_fake_or_self_containing_source(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "arc03@example.invalid")
    _git(root, "config", "user.name", "ARC03 Test")
    generator = root / "scripts/freeze_arc03.py"
    generator.parent.mkdir(parents=True)
    generator.write_text("GENERATOR = 1\n", encoding="utf-8")
    cutover = root / f"database/baselines/{BASELINE_ID}/cutover_convergence.sql"
    cutover.parent.mkdir(parents=True)
    cutover.write_text("-- cutover_convergence.sql\n", encoding="utf-8")
    _write_baseline(
        root,
        state="pending-live-freeze",
        source_git_sha=None,
        structural_sha256=None,
        acl_sha256=None,
        extensions_sha256=None,
        reference_data_sha256=None,
        generator="scripts/freeze_arc03.py",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "settled source")
    source_sha = _git(root, "rev-parse", "HEAD")
    _paths, manifest_path = _write_baseline(
        root,
        source_git_sha=source_sha,
        generator="scripts/freeze_arc03.py",
    )
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "artifact")

    fake = load_baseline_manifest(manifest_path)
    object.__setattr__(fake, "source_git_sha", "0" * 40)
    with pytest.raises(AuthorityManifestError, match="not a resolvable"):
        verify_baseline_git_provenance(manifest_path, fake, repo_root=root)

    artifact_sha = _git(root, "rev-parse", "HEAD")
    object.__setattr__(fake, "source_git_sha", artifact_sha)
    with pytest.raises(AuthorityManifestError, match="pending-live-freeze"):
        verify_baseline_git_provenance(manifest_path, fake, repo_root=root)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schema_revision", "latest", "schema_revision must be numeric"),
        (
            "reference_data",
            [
                {
                    "table": "public.system_values",
                    "natural_key": "key",
                    "immutable_columns": ["value"],
                }
            ],
            "needs natural_key and immutable_columns",
        ),
        ("reference_data", {"table": "public.system_values"}, "must be a list"),
        (
            "reference_data",
            [
                {
                    "table": "public.system_values",
                    "natural_key": ["key"],
                    "immutable_columns": ["value"],
                    "where": 1,
                }
            ],
            "where must be a string",
        ),
        (
            "reference_data",
            [
                {
                    "table": "public.system_values",
                    "natural_key": ["key"],
                    "immutable_columns": ["value"],
                },
                {
                    "table": "public.system_values",
                    "natural_key": ["key"],
                    "immutable_columns": ["value"],
                },
            ],
            "duplicates table",
        ),
    ],
)
def test_baseline_manifest_rejects_ambiguous_metadata(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    _paths, manifest = _write_baseline(tmp_path, **{field: value})

    with pytest.raises(AuthorityManifestError, match=message):
        load_baseline_manifest(manifest)


def test_load_baseline_rejects_freeze_point_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths, _manifest = _write_baseline(tmp_path)
    monkeypatch.setattr(
        commands,
        "load_legacy_manifest",
        lambda _path: SimpleNamespace(freeze_point="111_wrong.sql"),
    )

    with pytest.raises(AuthorityError, match="immutable legacy manifest freezes"):
        commands.load_baseline(paths, BASELINE_ID)


def test_baseline_ready_requires_cutover_contract(tmp_path: Path) -> None:
    paths, _manifest = _write_baseline(tmp_path)
    (paths.baseline_dir(BASELINE_ID) / "cutover_convergence.sql").unlink()

    assert not commands.baseline_ready(paths)


def test_baseline_ready_rejects_pending_live_freeze(tmp_path: Path) -> None:
    paths, _manifest = _write_baseline(tmp_path, state="pending-live-freeze")

    assert not commands.baseline_ready(paths)


@pytest.mark.asyncio
async def test_verify_contract_rejects_writes_multiple_statements_and_zero_checks(
    tmp_path: Path,
) -> None:
    class VerifyConnection:
        async def fetch(self, _query: str) -> list[dict[str, object]]:
            return []

    verify_sql = tmp_path / "verify.sql"
    for sql in (
        "DELETE FROM widgets RETURNING 'deleted' AS check_name, TRUE AS ok;",
        "SELECT 'one' AS check_name, TRUE AS ok; SELECT TRUE;",
    ):
        verify_sql.write_text(sql, encoding="utf-8")
        with pytest.raises(AuthorityError, match="exactly one read-only SELECT"):
            await bootstrap.verify_baseline_sql_file(VerifyConnection(), verify_sql)

    verify_sql.write_text(
        "SELECT 'empty_source' AS check_name, TRUE AS ok WHERE FALSE;\n",
        encoding="utf-8",
    )
    with pytest.raises(AuthorityError, match="returned zero checks"):
        await bootstrap.verify_baseline_sql_file(VerifyConnection(), verify_sql)
