"""Four-class schema fingerprints.

Classes (PRD ARC-03 §3B.6):

1. structural — tables/columns/constraints/indexes/sequences/functions/types,
   normalized and environment-independent.
2. acl — object owners, ACL entries and default privileges, mapped to the
   logical principal ids of the baseline manifest so that configurable LOGIN
   role prefixes never change an equivalent-permission hash.
3. extensions — allowlisted extension name/version pairs.
4. reference_data — exact hash over declared system-owned immutable rows only.

Ledger/history tables and catalog schemas are excluded by construction.  User
data and credentials never enter a fingerprint.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from uuid import UUID

from .constants import (
    CATALOG_SCHEMAS,
    EXTENSION_ALLOWLIST,
)
from .manifest import ReferenceDataSet

_FINGERPRINT_CLASSES = ("structural", "acl", "extensions", "reference_data")

# pg_class.relkind values the structural fingerprint covers.
_RELKIND_NAMES = {
    "r": "table",
    "p": "partitioned_table",
    "v": "view",
    "m": "materialized_view",
    "S": "sequence",
    "f": "foreign_table",
}

# Ledger and history tables differ between a fresh install and an adopted
# legacy database by design; they never enter a fingerprint.
_EXCLUDED_RELATION_FILTER = """
    AND NOT (n.nspname = 'public' AND c.relname IN (
        'platform_schema_baselines',
        'platform_schema_changes',
        'platform_schema_change_attempts',
        'schema_migrations',
        'schema_migrations_meta'
    ))
"""

_SCHEMA_FILTER = """
    AND n.nspname <> ALL($1)
    AND n.nspname NOT LIKE 'pg_temp_%'
    AND n.nspname NOT LIKE 'pg_toast_temp_%'
"""


class FingerprintError(RuntimeError):
    """A fingerprint could not be computed or failed its allowlist checks."""


def canonical_digest(lines: Iterable[str]) -> str:
    """SHA-256 over newline-joined canonical lines."""
    material = "\n".join(lines)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _normalized_owner(
    owner: str, owner_map: dict[str, str] | None
) -> str:
    if owner_map and owner in owner_map:
        return owner_map[owner]
    return owner


def logical_principal_map(role_prefix: str) -> dict[str, str]:
    """Map physical role names to logical principal ids for ACL hashing."""
    return {
        f"{role_prefix}owner": "owner",
        f"{role_prefix}migrator": "migrator",
        f"{role_prefix}gateway": "gateway",
        f"{role_prefix}runtime": "runtime",
        f"{role_prefix}capability_worker": "capability_worker",
        f"{role_prefix}knowledge_api": "knowledge_api",
        f"{role_prefix}knowledge_worker": "knowledge_worker",
    }


def _like_prefix_pattern(prefix: str) -> str:
    """Literal SQL LIKE prefix; ``_``/``%`` in role names are not wildcards."""
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{escaped}%"


async def structural_lines(conn: Any) -> list[str]:
    """Deterministic structural description of every platform relation."""
    lines: list[str] = []

    schemas = await conn.fetch(
        """
        SELECT n.nspname
        FROM pg_namespace AS n
        WHERE n.nspname <> ALL($1)
          AND n.nspname NOT LIKE 'pg_temp_%'
          AND n.nspname NOT LIKE 'pg_toast_temp_%'
        ORDER BY 1
        """,
        list(CATALOG_SCHEMAS),
    )
    lines.extend(f"schema:{row['nspname']}" for row in schemas)

    relations = await conn.fetch(
        f"""
        SELECT n.nspname AS schema, c.relname AS name, c.relkind AS kind
        FROM pg_class AS c
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        WHERE c.relkind <> ALL(ARRAY['i', 'I', 't', 'c'])
        {_SCHEMA_FILTER}
        {_EXCLUDED_RELATION_FILTER}
        ORDER BY n.nspname, c.relname
        """,
        list(CATALOG_SCHEMAS),
    )
    for row in relations:
        kind = _RELKIND_NAMES.get(row["kind"], row["kind"])
        lines.append(f"relation:{row['schema']}.{row['name']}:{kind}")

    columns = await conn.fetch(
        f"""
        SELECT n.nspname AS schema, c.relname AS relation, a.attname AS column,
               format_type(a.atttypid, a.atttypmod) AS type,
               a.attnotnull AS not_null,
               COALESCE(pg_get_expr(d.adbin, d.adrelid), '') AS default_expr,
               a.attnum AS position
        FROM pg_class AS c
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        JOIN pg_attribute AS a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
        LEFT JOIN pg_attrdef AS d ON d.adrelid = c.oid AND d.adnum = a.attnum
        WHERE c.relkind IN ('r', 'p', 'f', 'm', 'v')
        {_SCHEMA_FILTER}
        {_EXCLUDED_RELATION_FILTER}
        ORDER BY n.nspname, c.relname, a.attnum
        """,
        list(CATALOG_SCHEMAS),
    )
    for row in columns:
        lines.append(
            "column:{schema}.{relation}.{column}:{position}:{type}:"
            "notnull={not_null}:default={default_expr}".format(
                schema=row["schema"],
                relation=row["relation"],
                column=row["column"],
                position=row["position"],
                type=row["type"],
                not_null="1" if row["not_null"] else "0",
                default_expr=row["default_expr"],
            )
        )

    constraints = await conn.fetch(
        f"""
        SELECT n.nspname AS schema, c.relname AS relation, con.conname AS name,
               con.contype AS type, pg_get_constraintdef(con.oid) AS definition
        FROM pg_constraint AS con
        JOIN pg_class AS c ON c.oid = con.conrelid
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        WHERE TRUE
        {_SCHEMA_FILTER}
        {_EXCLUDED_RELATION_FILTER}
        ORDER BY n.nspname, c.relname, con.conname
        """,
        list(CATALOG_SCHEMAS),
    )
    for row in constraints:
        lines.append(
            f"constraint:{row['schema']}.{row['relation']}.{row['name']}:"
            f"{row['type']}:{row['definition']}"
        )

    indexes = await conn.fetch(
        f"""
        SELECT n.nspname AS schema, c.relname AS name,
               pg_get_indexdef(c.oid) AS definition,
               indisunique AS is_unique, indisprimary AS is_primary
        FROM pg_class AS c
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        JOIN pg_index AS i ON i.indexrelid = c.oid
        WHERE c.relkind IN ('i', 'I')
        {_SCHEMA_FILTER}
        AND NOT (n.nspname = 'public' AND (
            SELECT rc.relname FROM pg_class AS rc WHERE rc.oid = i.indrelid
        ) IN (
            'platform_schema_baselines',
            'platform_schema_changes',
            'platform_schema_change_attempts',
            'schema_migrations',
            'schema_migrations_meta'
        ))
        ORDER BY n.nspname, c.relname
        """,
        list(CATALOG_SCHEMAS),
    )
    for row in indexes:
        flags = "u" if row["is_unique"] else ""
        flags += "p" if row["is_primary"] else ""
        lines.append(f"index:{row['schema']}.{row['name']}:{flags}:{row['definition']}")

    sequences = await conn.fetch(
        """
        SELECT schemaname AS schema, sequencename AS name, data_type,
               start_value, increment_by, cycle
        FROM pg_sequences
        WHERE schemaname <> ALL($1)
          AND schemaname NOT LIKE 'pg_temp_%'
          AND schemaname NOT LIKE 'pg_toast_temp_%'
        ORDER BY schemaname, sequencename
        """,
        list(CATALOG_SCHEMAS),
    )
    for row in sequences:
        lines.append(
            "sequence:{schema}.{name}:{data_type}:start={start}:by={by}:cycle={cycle}".format(
                schema=row["schema"],
                name=row["name"],
                data_type=row["data_type"],
                start=row["start_value"],
                by=row["increment_by"],
                cycle="1" if row["cycle"] else "0",
            )
        )

    functions = await conn.fetch(
        f"""
        SELECT n.nspname AS schema, p.proname AS name,
               pg_get_function_identity_arguments(p.oid) AS identity_args,
               pg_get_functiondef(p.oid) AS definition,
               p.prosecdef AS security_definer
        FROM pg_proc AS p
        JOIN pg_namespace AS n ON n.oid = p.pronamespace
        WHERE TRUE
        {_SCHEMA_FILTER}
        ORDER BY n.nspname, p.proname, pg_get_function_identity_arguments(p.oid)
        """,
        list(CATALOG_SCHEMAS),
    )
    for row in functions:
        security = "definer" if row["security_definer"] else "invoker"
        lines.append(
            f"function:{row['schema']}.{row['name']}({row['identity_args']}):"
            f"{security}:{row['definition']}"
        )

    types = await conn.fetch(
        f"""
        SELECT n.nspname AS schema, t.typname AS name, t.typtype AS type
        FROM pg_type AS t
        JOIN pg_namespace AS n ON n.oid = t.typnamespace
        WHERE t.typtype IN ('e', 'd')
        {_SCHEMA_FILTER}
        ORDER BY n.nspname, t.typname
        """,
        list(CATALOG_SCHEMAS),
    )
    for row in types:
        lines.append(f"type:{row['schema']}.{row['name']}:{row['type']}")
        if row["type"] == "e":
            labels = await conn.fetch(
                "SELECT enumlabel FROM pg_enum WHERE enumtypid = "
                "(SELECT oid FROM pg_type AS t2 JOIN pg_namespace AS n2 ON n2.oid = t2.typnamespace "
                "WHERE n2.nspname = $1 AND t2.typname = $2) ORDER BY enumsortorder",
                row["schema"],
                row["name"],
            )
            lines.extend(
                f"enum:{row['schema']}.{row['name']}:{label['enumlabel']}"
                for label in labels
            )

    return lines


async def acl_lines(conn: Any, *, role_prefix: str) -> list[str]:
    """Owners, ACLs and default privileges, normalized to logical principals."""
    principal_map = logical_principal_map(role_prefix)
    lines: list[str] = []

    owners = await conn.fetch(
        f"""
        SELECT n.nspname AS schema, c.relname AS name,
               pg_get_userbyid(c.relowner) AS owner
        FROM pg_class AS c
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        WHERE c.relkind <> ALL(ARRAY['i', 'I', 't', 'c'])
        {_SCHEMA_FILTER}
        {_EXCLUDED_RELATION_FILTER}
        ORDER BY n.nspname, c.relname
        """,
        list(CATALOG_SCHEMAS),
    )
    for row in owners:
        lines.append(
            f"owner:{row['schema']}.{row['name']}:"
            f"{_normalized_owner(row['owner'], principal_map)}"
        )

    schema_owners = await conn.fetch(
        """
        SELECT n.nspname AS schema, pg_get_userbyid(n.nspowner) AS owner
        FROM pg_namespace AS n
        WHERE n.nspname <> ALL($1)
          AND n.nspname NOT LIKE 'pg_temp_%'
          AND n.nspname NOT LIKE 'pg_toast_temp_%'
        ORDER BY n.nspname
        """,
        list(CATALOG_SCHEMAS),
    )
    for row in schema_owners:
        lines.append(
            f"schema_owner:{row['schema']}:"
            f"{_normalized_owner(row['owner'], principal_map)}"
        )

    function_owners = await conn.fetch(
        f"""
        SELECT n.nspname AS schema, p.proname AS name,
               pg_get_function_identity_arguments(p.oid) AS identity_args,
               pg_get_userbyid(p.proowner) AS owner
        FROM pg_proc AS p
        JOIN pg_namespace AS n ON n.oid = p.pronamespace
        WHERE TRUE
        {_SCHEMA_FILTER}
        ORDER BY n.nspname, p.proname, pg_get_function_identity_arguments(p.oid)
        """,
        list(CATALOG_SCHEMAS),
    )
    for row in function_owners:
        lines.append(
            f"function_owner:{row['schema']}.{row['name']}({row['identity_args']}):"
            f"{_normalized_owner(row['owner'], principal_map)}"
        )

    type_owners = await conn.fetch(
        f"""
        SELECT n.nspname AS schema, t.typname AS name, t.typtype AS type,
               pg_get_userbyid(t.typowner) AS owner
        FROM pg_type AS t
        JOIN pg_namespace AS n ON n.oid = t.typnamespace
        WHERE t.typtype IN ('e', 'd')
        {_SCHEMA_FILTER}
        ORDER BY n.nspname, t.typname
        """,
        list(CATALOG_SCHEMAS),
    )
    for row in type_owners:
        lines.append(
            f"type_owner:{row['schema']}.{row['name']}:{row['type']}:"
            f"{_normalized_owner(row['owner'], principal_map)}"
        )

    acls = await conn.fetch(
        f"""
        SELECT n.nspname AS schema, c.relname AS name,
               COALESCE(pg_get_userbyid(a.grantee), 'PUBLIC') AS grantee,
               a.grant_option, string_agg(a.privilege_type, ',' ORDER BY a.privilege_type) AS privileges,
               pg_get_userbyid(c.relowner) AS grantor_default
        FROM pg_class AS c
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        CROSS JOIN LATERAL aclexplode(COALESCE(c.relacl, acldefault(
            CASE c.relkind WHEN 'S' THEN 's' ELSE 'r' END, c.relowner))) AS a
        WHERE c.relkind <> ALL(ARRAY['i', 'I', 't', 'c'])
        {_SCHEMA_FILTER}
        {_EXCLUDED_RELATION_FILTER}
        GROUP BY n.nspname, c.relname, a.grantee, a.grant_option, c.relowner
        ORDER BY n.nspname, c.relname, grantee, a.grant_option
        """,
        list(CATALOG_SCHEMAS),
    )
    for row in acls:
        grantee = _normalized_owner(row["grantee"], principal_map)
        lines.append(
            f"acl:{row['schema']}.{row['name']}:{grantee}:"
            f"{row['privileges']}:grantopt={1 if row['grant_option'] else 0}"
        )

    function_acls = await conn.fetch(
        f"""
        SELECT n.nspname AS schema, p.proname AS name,
               pg_get_function_identity_arguments(p.oid) AS identity_args,
               COALESCE(pg_get_userbyid(a.grantee), 'PUBLIC') AS grantee,
               a.grant_option, string_agg(a.privilege_type, ',' ORDER BY a.privilege_type) AS privileges
        FROM pg_proc AS p
        JOIN pg_namespace AS n ON n.oid = p.pronamespace
        CROSS JOIN LATERAL aclexplode(COALESCE(p.proacl, acldefault('f', p.proowner))) AS a
        WHERE TRUE
        {_SCHEMA_FILTER}
        GROUP BY n.nspname, p.proname, p.oid, a.grantee, a.grant_option
        ORDER BY n.nspname, p.proname, identity_args, grantee
        """,
        list(CATALOG_SCHEMAS),
    )
    for row in function_acls:
        grantee = _normalized_owner(row["grantee"], principal_map)
        lines.append(
            f"function_acl:{row['schema']}.{row['name']}({row['identity_args']}):"
            f"{grantee}:{row['privileges']}:grantopt={1 if row['grant_option'] else 0}"
        )

    schema_acls = await conn.fetch(
        """
        SELECT n.nspname AS schema,
               COALESCE(pg_get_userbyid(a.grantee), 'PUBLIC') AS grantee,
               a.grant_option, string_agg(a.privilege_type, ',' ORDER BY a.privilege_type) AS privileges
        FROM pg_namespace AS n
        CROSS JOIN LATERAL aclexplode(COALESCE(n.nspacl, acldefault('n', n.nspowner))) AS a
        WHERE n.nspname <> ALL($1)
          AND n.nspname NOT LIKE 'pg_temp_%'
          AND n.nspname NOT LIKE 'pg_toast_temp_%'
        GROUP BY n.nspname, a.grantee, a.grant_option
        ORDER BY n.nspname, grantee
        """,
        list(CATALOG_SCHEMAS),
    )
    for row in schema_acls:
        grantee = _normalized_owner(row["grantee"], principal_map)
        lines.append(
            f"schema_acl:{row['schema']}:{grantee}:{row['privileges']}:"
            f"grantopt={1 if row['grant_option'] else 0}"
        )

    type_acls = await conn.fetch(
        f"""
        SELECT n.nspname AS schema, t.typname AS name, t.typtype AS type,
               COALESCE(pg_get_userbyid(a.grantee), 'PUBLIC') AS grantee,
               a.grant_option,
               string_agg(a.privilege_type, ',' ORDER BY a.privilege_type) AS privileges
        FROM pg_type AS t
        JOIN pg_namespace AS n ON n.oid = t.typnamespace
        CROSS JOIN LATERAL aclexplode(COALESCE(t.typacl, acldefault('T', t.typowner))) AS a
        WHERE t.typtype IN ('e', 'd')
        {_SCHEMA_FILTER}
        GROUP BY n.nspname, t.typname, t.typtype, a.grantee, a.grant_option
        ORDER BY n.nspname, t.typname, grantee, a.grant_option
        """,
        list(CATALOG_SCHEMAS),
    )
    for row in type_acls:
        grantee = _normalized_owner(row["grantee"], principal_map)
        lines.append(
            f"type_acl:{row['schema']}.{row['name']}:{row['type']}:{grantee}:"
            f"{row['privileges']}:grantopt={1 if row['grant_option'] else 0}"
        )

    default_privileges = await conn.fetch(
        """
        SELECT COALESCE(pg_get_userbyid(d.defaclrole), '?') AS grantor,
               CASE d.defaclobjtype WHEN 'r' THEN 'table' WHEN 'f' THEN 'function'
                    WHEN 'S' THEN 'sequence' WHEN 'n' THEN 'schema' WHEN 'T' THEN 'type'
                    ELSE d.defaclobjtype::text END AS object_type,
               COALESCE(n.nspname, '') AS schema,
               COALESCE(pg_get_userbyid(a.grantee), 'PUBLIC') AS grantee,
               a.grant_option,
               string_agg(a.privilege_type, ',' ORDER BY a.privilege_type) AS privileges
        FROM pg_default_acl AS d
        LEFT JOIN pg_namespace AS n ON n.oid = d.defaclnamespace
        CROSS JOIN LATERAL aclexplode(d.defaclacl) AS a
        WHERE pg_get_userbyid(d.defaclrole) LIKE $2 ESCAPE E'\\'
          AND (
              d.defaclnamespace = 0
              OR (
                  n.nspname <> ALL($1)
                  AND n.nspname NOT LIKE 'pg_temp_%'
                  AND n.nspname NOT LIKE 'pg_toast_temp_%'
              )
          )
        GROUP BY d.defaclrole, d.defaclobjtype, n.nspname,
                 a.grantee, a.grant_option
        ORDER BY grantor, object_type, schema, grantee
        """,
        list(CATALOG_SCHEMAS),
        _like_prefix_pattern(role_prefix),
    )
    for row in default_privileges:
        grantor = _normalized_owner(row["grantor"], principal_map)
        grantee = _normalized_owner(row["grantee"], principal_map)
        lines.append(
            f"default_privilege:{grantor}:{row['object_type']}:{row['schema']}:"
            f"{grantee}:{row['privileges']}:"
            f"grantopt={1 if row['grant_option'] else 0}"
        )

    # Only the platform's own roles enter the fingerprint.  The bootstrap
    # superuser differs between environments (``postgres`` locally, a DBA
    # role on managed PostgreSQL), so it must never influence the hash.
    roles = await conn.fetch(
        """
        SELECT r.rolname AS name, r.rolsuper, r.rolcreaterole, r.rolcreatedb,
               r.rolcanlogin, r.rolinherit,
               COALESCE((SELECT string_agg(option, ',' ORDER BY option)
                         FROM pg_options_to_table(r.rolconfig)), '') AS config
        FROM pg_roles AS r
        WHERE r.rolname LIKE $1 ESCAPE E'\\'
        ORDER BY r.rolname
        """,
        _like_prefix_pattern(role_prefix),
    )
    for row in roles:
        name = _normalized_owner(row["name"], principal_map)
        flags = "".join(
            letter
            for letter, present in (
                ("S", row["rolsuper"]),
                ("R", row["rolcreaterole"]),
                ("D", row["rolcreatedb"]),
                ("L", row["rolcanlogin"]),
                ("I", row["rolinherit"]),
            )
            if present
        )
        lines.append(f"role:{name}:{flags}:{row['config']}")

    return lines


async def extensions_lines(conn: Any) -> list[str]:
    """Allowlisted extensions only; anything else fails closed."""
    rows = await conn.fetch("SELECT extname, extversion FROM pg_extension ORDER BY extname")
    lines: list[str] = []
    for row in rows:
        if row["extname"] == "plpgsql":
            continue  # built-in, present everywhere
        if row["extname"] not in EXTENSION_ALLOWLIST:
            raise FingerprintError(
                f"extension {row['extname']!r} is not in the allowlist {EXTENSION_ALLOWLIST}"
            )
        lines.append(f"extension:{row['extname']}:{row['extversion']}")
    return lines


async def reference_data_lines(conn: Any, reference_sets: Iterable[ReferenceDataSet]) -> list[str]:
    """Exact rows of declared system-owned immutable tables."""
    lines: list[str] = []
    for ref in sorted(reference_sets, key=lambda item: item.table):
        columns = list(dict.fromkeys([*ref.natural_key, *ref.immutable_columns]))
        column_list = ", ".join(columns)
        where = f" WHERE {ref.where}" if ref.where else ""
        order_by = ", ".join(ref.natural_key)
        rows = await conn.fetch(
            f"SELECT {column_list} FROM {ref.table}{where} ORDER BY {order_by}"
        )
        lines.append(f"reference:{ref.table}:columns={','.join(columns)}:rows={len(rows)}")
        for row in rows:
            values = _canonical_row(row[column] for column in columns)
            lines.append(f"row:{ref.table}:{values}")
    return lines


def _canonical_value(value: Any) -> str:
    """Type-tagged canonical JSON for one PostgreSQL/JSON value."""
    return json.dumps(
        _canonical_value_tree(value),
        ensure_ascii=True,
        separators=(",", ":"),
    )


def _canonical_row(values: Iterable[Any]) -> str:
    return json.dumps(
        [_canonical_value_tree(value) for value in values],
        ensure_ascii=True,
        separators=(",", ":"),
    )


def _canonical_value_tree(value: Any) -> list[Any]:
    if value is None:
        return ["null"]
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, int):
        return ["int", str(value)]
    if isinstance(value, Decimal):
        if value.is_finite():
            normalized = format(value.normalize(), "f")
            if normalized == "-0":
                normalized = "0"
        else:
            normalized = str(value)
        return ["decimal", normalized]
    if isinstance(value, float):
        if math.isnan(value):
            rendered = "nan"
        elif math.isinf(value):
            rendered = "+inf" if value > 0 else "-inf"
        else:
            rendered = value.hex()
        return ["float", rendered]
    if isinstance(value, str):
        return ["str", value]
    if isinstance(value, (bytes, bytearray, memoryview)):
        return ["bytes", base64.b64encode(bytes(value)).decode("ascii")]
    if isinstance(value, datetime):
        return ["datetime", value.isoformat(timespec="microseconds")]
    if isinstance(value, date):
        return ["date", value.isoformat()]
    if isinstance(value, time):
        return ["time", value.isoformat(timespec="microseconds")]
    if isinstance(value, UUID):
        return ["uuid", str(value)]
    if isinstance(value, Mapping):
        items = [
            [_canonical_value_tree(key), _canonical_value_tree(item)]
            for key, item in value.items()
        ]
        items.sort(
            key=lambda pair: json.dumps(
                pair[0], ensure_ascii=True, separators=(",", ":")
            )
        )
        return ["map", items]
    if isinstance(value, list):
        return ["list", [_canonical_value_tree(item) for item in value]]
    if isinstance(value, tuple):
        return ["tuple", [_canonical_value_tree(item) for item in value]]
    raise FingerprintError(
        f"unsupported reference-data value type: {type(value).__module__}.{type(value).__qualname__}"
    )


async def compute_fingerprints(
    conn: Any,
    *,
    role_prefix: str,
    reference_sets: Iterable[ReferenceDataSet] = (),
) -> dict[str, str]:
    """Compute all four fingerprints in one read-only pass."""
    structural = canonical_digest(await structural_lines(conn))
    acl = canonical_digest(await acl_lines(conn, role_prefix=role_prefix))
    extensions = canonical_digest(await extensions_lines(conn))
    reference = canonical_digest(await reference_data_lines(conn, reference_sets))
    return {
        "structural": structural,
        "acl": acl,
        "extensions": extensions,
        "reference_data": reference,
    }


def fingerprint_classes() -> tuple[str, ...]:
    return _FINGERPRINT_CLASSES


def diff_line_lists(expected: list[str], actual: list[str]) -> list[str]:
    """Render drift between two canonical line lists (for drift reports)."""
    expected_set = set(expected)
    actual_set = set(actual)
    drift = [f"- {line}" for line in sorted(expected_set - actual_set)]
    drift.extend(f"+ {line}" for line in sorted(actual_set - expected_set))
    return drift
