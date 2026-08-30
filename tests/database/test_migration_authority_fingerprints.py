from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from database.authority import commands
from database.authority.fingerprint import (
    FingerprintError,
    _canonical_row,
    _canonical_value,
    _like_prefix_pattern,
    acl_lines,
    compute_fingerprints,
    reference_data_lines,
    structural_lines,
)
from database.authority.manifest import BaselineManifest, ReferenceDataSet
from database.authority.runner import AuthorityError, AuthorityPaths


def _baseline() -> BaselineManifest:
    return BaselineManifest(
        baseline_id="2026_08_post_kb_v1",
        schema_revision="112",
        source_git_sha="a" * 40,
        last_legacy_change="112_last.sql",
        structural_sha256="1" * 64,
        acl_sha256="2" * 64,
        extensions_sha256="3" * 64,
        reference_data_sha256="4" * 64,
        generator="test",
        generated_at="2026-08-30T00:00:00Z",
        postgres_version="16",
    )


def _marker(baseline: BaselineManifest, manifest_sha: str = "5" * 64) -> dict[str, str]:
    return {
        "baseline_id": baseline.baseline_id,
        "manifest_sha256": manifest_sha,
        "structural_sha256": baseline.structural_sha256,
        "acl_sha256": baseline.acl_sha256,
        "extensions_sha256": baseline.extensions_sha256,
        "reference_data_sha256": baseline.reference_data_sha256,
        "source_git_sha": baseline.source_git_sha,
        "adopted_at": "2026-08-30T00:00:00Z",
    }


class EmptyConnection:
    def __init__(self) -> None:
        self.queries: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.queries.append((query, args))
        return []


class AclConnection(EmptyConnection):
    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.queries.append((query, args))
        if "pg_get_userbyid(c.relowner) AS owner" in query:
            return [{"schema": "public", "name": "widgets", "owner": "ai_platform_owner"}]
        if "pg_get_userbyid(n.nspowner) AS owner" in query:
            return [{"schema": "public", "owner": "ai_platform_owner"}]
        if "pg_get_userbyid(p.proowner) AS owner" in query:
            return [
                {
                    "schema": "public",
                    "name": "run_widget",
                    "identity_args": "uuid",
                    "owner": "ai_platform_owner",
                }
            ]
        if "pg_get_userbyid(t.typowner) AS owner" in query:
            return [
                {
                    "schema": "public",
                    "name": "widget_state",
                    "type": "e",
                    "owner": "ai_platform_owner",
                }
            ]
        if "aclexplode(COALESCE(c.relacl" in query:
            return [
                {
                    "schema": "public",
                    "name": "widgets",
                    "grantee": "ai_platform_gateway",
                    "grant_option": False,
                    "privileges": "SELECT",
                }
            ]
        if "aclexplode(COALESCE(p.proacl" in query:
            return [
                {
                    "schema": "public",
                    "name": "run_widget",
                    "identity_args": "uuid",
                    "grantee": "ai_platform_runtime",
                    "grant_option": False,
                    "privileges": "EXECUTE",
                }
            ]
        if "aclexplode(COALESCE(n.nspacl" in query:
            return [
                {
                    "schema": "public",
                    "grantee": "ai_platform_gateway",
                    "grant_option": False,
                    "privileges": "USAGE",
                }
            ]
        if "aclexplode(COALESCE(t.typacl" in query:
            return [
                {
                    "schema": "public",
                    "name": "widget_state",
                    "type": "e",
                    "grantee": "ai_platform_gateway",
                    "grant_option": False,
                    "privileges": "USAGE",
                }
            ]
        if "FROM pg_default_acl AS d" in query:
            return [
                {
                    "grantor": "ai_platform_owner",
                    "object_type": "table",
                    "schema": "public",
                    "grantee": "ai_platform_gateway",
                    "grant_option": False,
                    "privileges": "SELECT",
                }
            ]
        if "FROM pg_roles AS r" in query:
            return [
                {
                    "name": "ai_platform_gateway",
                    "rolsuper": False,
                    "rolcreaterole": False,
                    "rolcreatedb": False,
                    "rolcanlogin": True,
                    "rolinherit": True,
                    "config": "",
                }
            ]
        raise AssertionError(f"unexpected ACL query: {query}")


def test_like_role_prefix_is_literal_not_a_wildcard() -> None:
    assert _like_prefix_pattern("ai_platform_") == r"ai\_platform\_%"
    assert _like_prefix_pattern("a_%\\") == r"a\_\%\\%"


async def test_structural_queries_filter_temporary_schemas() -> None:
    conn = EmptyConnection()

    fingerprints = await structural_lines(conn)

    assert fingerprints == []
    schema_query = next(query for query, _ in conn.queries if "FROM pg_namespace AS n" in query)
    sequence_query = next(query for query, _ in conn.queries if "FROM pg_sequences" in query)
    for query in (schema_query, sequence_query):
        assert "NOT LIKE 'pg_temp_%'" in query
        assert "NOT LIKE 'pg_toast_temp_%'" in query


async def test_acl_covers_all_owner_acl_classes_and_default_privileges() -> None:
    conn = AclConnection()

    lines = await acl_lines(conn, role_prefix="ai_platform_")

    assert "owner:public.widgets:owner" in lines
    assert "schema_owner:public:owner" in lines
    assert "function_owner:public.run_widget(uuid):owner" in lines
    assert "type_owner:public.widget_state:e:owner" in lines
    assert "acl:public.widgets:gateway:SELECT:grantopt=0" in lines
    assert "function_acl:public.run_widget(uuid):runtime:EXECUTE:grantopt=0" in lines
    assert "schema_acl:public:gateway:USAGE:grantopt=0" in lines
    assert "type_acl:public.widget_state:e:gateway:USAGE:grantopt=0" in lines
    assert "default_privilege:owner:table:public:gateway:SELECT:grantopt=0" in lines
    assert "role:gateway:LI:" in lines

    default_query, default_args = next(
        (query, args) for query, args in conn.queries if "FROM pg_default_acl AS d" in query
    )
    roles_query, roles_args = next(
        (query, args) for query, args in conn.queries if "FROM pg_roles AS r" in query
    )
    assert "GROUP BY d.defaclrole, d.defaclobjtype, n.nspname" in default_query
    assert "a.grantee, a.grant_option" in default_query
    assert "NOT LIKE 'pg_temp_%'" in default_query
    assert "ESCAPE E'\\'" in default_query
    assert default_args[1] == r"ai\_platform\_%"
    assert "ESCAPE E'\\'" in roles_query
    assert roles_args == (r"ai\_platform\_%",)


def test_reference_row_serialization_has_no_delimiter_or_type_collisions() -> None:
    assert _canonical_row(["a|b", "c"]) != _canonical_row(["a", "b|c"])
    assert _canonical_value(None) != _canonical_value("\x00null")
    assert _canonical_value(True) != _canonical_value("true")
    assert _canonical_value(1) != _canonical_value("1")
    assert _canonical_value({"b": [2], "a": 1}) == _canonical_value(
        {"a": 1, "b": [2]}
    )


def test_reference_serialization_rejects_unknown_driver_types() -> None:
    with pytest.raises(FingerprintError, match="unsupported reference-data value type"):
        _canonical_value(object())


class ReferenceConnection:
    def __init__(self, row: dict[str, Any]) -> None:
        self.row = row

    async def fetch(self, _query: str, *_args: Any) -> list[dict[str, Any]]:
        return [self.row]


async def test_reference_fingerprint_rows_preserve_column_boundaries() -> None:
    reference = ReferenceDataSet(
        table="public.system_values",
        natural_key=("key",),
        immutable_columns=("value",),
    )

    left = await reference_data_lines(
        ReferenceConnection({"key": "a|b", "value": "c"}),
        [reference],
    )
    right = await reference_data_lines(
        ReferenceConnection({"key": "a", "value": "b|c"}),
        [reference],
    )

    assert left != right


async def test_compute_fingerprints_always_returns_the_four_named_classes() -> None:
    computed = await compute_fingerprints(EmptyConnection(), role_prefix="ai_platform_")

    assert set(computed) == {"structural", "acl", "extensions", "reference_data"}
    assert all(len(digest) == 64 for digest in computed.values())


@pytest.mark.parametrize(
    "field",
    [
        "baseline_id",
        "manifest_sha256",
        "structural_sha256",
        "acl_sha256",
        "extensions_sha256",
        "reference_data_sha256",
        "source_git_sha",
    ],
)
def test_existing_adoption_marker_must_match_every_frozen_field(field: str) -> None:
    baseline = _baseline()
    marker = _marker(baseline)
    commands._validate_adoption_marker(marker, baseline, "5" * 64)
    marker[field] = "drift"

    with pytest.raises(AuthorityError, match=field):
        commands._validate_adoption_marker(marker, baseline, "5" * 64)


class CommandConnection:
    def __init__(self) -> None:
        self.closed = False
        self.executed: list[str] = []

    async def close(self) -> None:
        self.closed = True

    async def execute(self, query: str, *_args: Any) -> None:
        self.executed.append(query)

    async def fetch(self, _query: str, *_args: Any) -> list[dict[str, Any]]:
        return []


class CommandAuthority:
    def __init__(self, tmp_path: Path, marker: dict[str, str]) -> None:
        self.paths = AuthorityPaths(database_dir=tmp_path / "database")
        self.role_prefix = "ai_platform_"
        self.marker = marker
        self.lock_conn = CommandConnection()
        self.conn = CommandConnection()
        self.read_only_flags: list[bool] = []
        self.epoch_called = False

    async def connect(self, *, read_only: bool = False) -> CommandConnection:
        self.read_only_flags.append(read_only)
        return self.conn if read_only or len(self.read_only_flags) > 1 else self.lock_conn

    async def acquire_lock(self, _conn: CommandConnection) -> None:
        return None

    async def release_lock(self, conn: CommandConnection) -> None:
        await conn.close()

    async def adopted_baseline(self, _conn: CommandConnection) -> dict[str, str]:
        return self.marker

    async def apply_epoch(self, *_args: Any, **_kwargs: Any) -> list[str]:
        self.epoch_called = True
        return []


async def test_migrate_rejects_existing_marker_manifest_drift_before_epoch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    baseline = _baseline()
    marker = _marker(baseline)
    marker["manifest_sha256"] = "bad"
    authority = CommandAuthority(tmp_path, marker)
    monkeypatch.setattr(commands, "baseline_ready", lambda *_args: True)
    monkeypatch.setattr(
        commands,
        "load_baseline",
        lambda *_args: (baseline, "5" * 64),
    )

    with pytest.raises(AuthorityError, match="manifest_sha256"):
        await commands.command_migrate(authority)

    assert not authority.epoch_called
    assert authority.conn.closed
    assert authority.lock_conn.closed


async def test_cutover_matching_existing_marker_is_a_read_only_noop(tmp_path: Path) -> None:
    baseline = _baseline()
    authority = CommandAuthority(tmp_path, _marker(baseline))
    messages: list[str] = []

    await commands._cutover_and_adopt(
        authority.conn,
        authority,
        baseline,
        "5" * 64,
        log=messages.append,
    )

    assert authority.conn.executed == []
    assert messages == [f"authority: baseline already adopted ({baseline.baseline_id})"]


async def test_verify_is_read_only_and_rejects_source_marker_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    baseline = _baseline()
    marker = _marker(baseline)
    marker["source_git_sha"] = "b" * 40
    authority = CommandAuthority(tmp_path, marker)
    monkeypatch.setattr(commands, "baseline_ready", lambda *_args: True)
    monkeypatch.setattr(
        commands,
        "load_baseline",
        lambda *_args: (baseline, "5" * 64),
    )

    with pytest.raises(AuthorityError, match="source_git_sha"):
        await commands.command_verify(authority)

    assert authority.read_only_flags == [True]
    assert authority.conn.executed == []
    assert authority.conn.closed
