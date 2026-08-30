"""Manifest models for the migration authority.

Two machine carriers:

* ``database/migrations/<baseline_id>/manifest.yml`` — one epoch. Declares per
  change the full SHA-256, owner, transaction mode, rollback class, pre/post
  conditions, timeout/lock budget and resume/repair handler.  The runner and
  CI read this file only; they never infer intent from report prose.
* ``database/baselines/<baseline_id>/manifest.json`` — one frozen baseline.
  Declares baseline id, source Git SHA, last legacy change, the four
  fingerprints, generator identity and the reference-data policy.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import yaml

from .constants import DEFAULT_BASELINE_ID, LOGICAL_PRINCIPALS


class AuthorityManifestError(RuntimeError):
    """A manifest failed validation; the runner must fail closed."""


class TransactionMode(str, Enum):
    TRANSACTIONAL = "transactional"
    NON_TRANSACTIONAL = "non_transactional"


class RollbackClass(str, Enum):
    OLD_BINARY_COMPATIBLE = "old-binary-compatible"
    FORWARD_FIX_ONLY = "forward-fix-only"
    RESTORE_REQUIRED = "restore-required"


_CHANGE_FILE_RE = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")
_REQUIRED_CHANGE_FIELDS = (
    "sequence",
    "name",
    "file",
    "sha256",
    "owner",
    "transaction_mode",
    "rollback_class",
)


@dataclass(frozen=True)
class ChangeSpec:
    """One immutable, manifest-declared migration change."""

    sequence: int
    name: str
    file: str
    sha256: str
    owner: str
    transaction_mode: TransactionMode
    rollback_class: RollbackClass
    preconditions: tuple[str, ...] = ()
    postconditions: tuple[str, ...] = ()
    timeout_seconds: int = 300
    lock_budget_seconds: int = 30
    resume_handler: str | None = None
    repair_handler: str | None = None
    notes: str = ""


@dataclass(frozen=True)
class EpochManifest:
    baseline_id: str
    epoch: int
    changes: tuple[ChangeSpec, ...] = ()
    extra: dict = field(default_factory=dict)

    def by_sequence(self) -> dict[int, ChangeSpec]:
        return {change.sequence: change for change in self.changes}


@dataclass(frozen=True)
class ReferenceDataSet:
    """One system-owned immutable table slice admitted to the exact hash."""

    table: str
    natural_key: tuple[str, ...]
    immutable_columns: tuple[str, ...]
    where: str = ""


@dataclass(frozen=True)
class BaselineManifest:
    baseline_id: str
    schema_revision: str
    source_git_sha: str
    last_legacy_change: str
    structural_sha256: str
    acl_sha256: str
    extensions_sha256: str
    reference_data_sha256: str
    generator: str
    generated_at: str
    postgres_version: str
    reference_data: tuple[ReferenceDataSet, ...] = ()

    @property
    def fingerprints(self) -> dict[str, str]:
        return {
            "structural": self.structural_sha256,
            "acl": self.acl_sha256,
            "extensions": self.extensions_sha256,
            "reference_data": self.reference_data_sha256,
        }


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_identifier(value: str, field_name: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", str(value)):
        raise AuthorityManifestError(
            f"manifest field {field_name!r} is not a safe identifier: {value!r}"
        )
    return str(value)


def _validate_relation_name(value: str, field_name: str) -> str:
    """A table name, optionally schema-qualified (``schema.table``).

    Reference-data tables are always addressed schema-qualified so the
    fingerprint never depends on the session search_path.
    """
    if not re.fullmatch(
        r"[a-z][a-z0-9_]{0,62}(\.[a-z][a-z0-9_]{0,62})?", str(value)
    ):
        raise AuthorityManifestError(
            f"manifest field {field_name!r} is not a safe relation name: {value!r}"
        )
    return str(value)


def _as_str_tuple(value: object, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise AuthorityManifestError(f"manifest field {field_name!r} must be a list of SQL strings")
    return tuple(value)


def load_epoch_manifest(path: Path) -> EpochManifest:
    """Parse and validate one epoch manifest.yml, fail closed."""
    if not path.exists():
        raise AuthorityManifestError(f"epoch manifest not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise AuthorityManifestError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise AuthorityManifestError(f"epoch manifest {path} must be a mapping")

    baseline_id = _validate_identifier(raw.get("baseline_id", ""), "baseline_id")
    epoch = raw.get("epoch")
    if not isinstance(epoch, int) or epoch < 1:
        raise AuthorityManifestError(f"epoch manifest {path} needs an integer epoch >= 1")

    raw_changes = raw.get("changes") or []
    if not isinstance(raw_changes, list):
        raise AuthorityManifestError(f"epoch manifest {path}: changes must be a list")

    changes: list[ChangeSpec] = []
    seen_sequences: set[int] = set()
    for entry in raw_changes:
        if not isinstance(entry, dict):
            raise AuthorityManifestError(f"epoch manifest {path}: change entry must be a mapping")
        missing = [key for key in _REQUIRED_CHANGE_FIELDS if key not in entry]
        if missing:
            raise AuthorityManifestError(
                f"epoch manifest {path}: change entry missing fields {sorted(missing)}"
            )
        sequence = entry["sequence"]
        if not isinstance(sequence, int) or sequence < 1:
            raise AuthorityManifestError(f"epoch manifest {path}: sequence must be an integer >= 1")
        if sequence in seen_sequences:
            raise AuthorityManifestError(
                f"epoch manifest {path}: duplicate sequence {sequence}"
            )
        seen_sequences.add(sequence)
        try:
            mode = TransactionMode(entry["transaction_mode"])
        except ValueError as exc:
            raise AuthorityManifestError(
                f"epoch manifest {path}: unknown transaction_mode {entry['transaction_mode']!r}"
            ) from exc
        try:
            rollback = RollbackClass(entry["rollback_class"])
        except ValueError as exc:
            raise AuthorityManifestError(
                f"epoch manifest {path}: unknown rollback_class {entry['rollback_class']!r}"
            ) from exc
        owner = entry["owner"]
        if owner not in LOGICAL_PRINCIPALS:
            raise AuthorityManifestError(
                f"epoch manifest {path}: owner {owner!r} is not a logical principal "
                f"(one of {LOGICAL_PRINCIPALS})"
            )
        sha = str(entry["sha256"])
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise AuthorityManifestError(
                f"epoch manifest {path}: change {sequence} sha256 must be a full 64-char digest"
            )
        filename = str(entry["file"])
        match = _CHANGE_FILE_RE.fullmatch(filename)
        if not match or int(match.group(1)) != sequence:
            raise AuthorityManifestError(
                f"epoch manifest {path}: file {filename!r} must be named "
                f"{sequence:03d}_<snake_case>.sql"
            )
        changes.append(
            ChangeSpec(
                sequence=sequence,
                name=_validate_identifier(entry["name"], "name"),
                file=filename,
                sha256=sha,
                owner=owner,
                transaction_mode=mode,
                rollback_class=rollback,
                preconditions=_as_str_tuple(entry.get("preconditions"), "preconditions"),
                postconditions=_as_str_tuple(entry.get("postconditions"), "postconditions"),
                timeout_seconds=int(entry.get("timeout_seconds", 300)),
                lock_budget_seconds=int(entry.get("lock_budget_seconds", 30)),
                resume_handler=entry.get("resume_handler"),
                repair_handler=entry.get("repair_handler"),
                notes=str(entry.get("notes", "")),
            )
        )

    ordered = sorted(seen_sequences)
    if ordered and ordered != list(range(1, len(ordered) + 1)):
        raise AuthorityManifestError(
            f"epoch manifest {path}: sequences must be contiguous from 1, got {ordered}"
        )

    return EpochManifest(
        baseline_id=baseline_id,
        epoch=epoch,
        changes=tuple(sorted(changes, key=lambda change: change.sequence)),
        extra={key: value for key, value in raw.items() if key not in ("baseline_id", "epoch", "changes")},
    )


def load_baseline_manifest(path: Path) -> BaselineManifest:
    """Parse and validate one frozen baseline manifest.json, fail closed."""
    if not path.exists():
        raise AuthorityManifestError(f"baseline manifest not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AuthorityManifestError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise AuthorityManifestError(f"baseline manifest {path} must be an object")

    required = (
        "baseline_id",
        "schema_revision",
        "source_git_sha",
        "last_legacy_change",
        "structural_sha256",
        "acl_sha256",
        "extensions_sha256",
        "reference_data_sha256",
        "generator",
        "generated_at",
        "postgres_version",
    )
    missing = [key for key in required if not raw.get(key)]
    if missing:
        raise AuthorityManifestError(
            f"baseline manifest {path} missing fields: {sorted(missing)}"
        )

    digest_fields = (
        "structural_sha256",
        "acl_sha256",
        "extensions_sha256",
        "reference_data_sha256",
    )
    for digest_field in digest_fields:
        if not re.fullmatch(r"[0-9a-f]{64}", str(raw[digest_field])):
            raise AuthorityManifestError(
                f"baseline manifest {path}: {digest_field} must be a full 64-char digest"
            )

    reference_sets: list[ReferenceDataSet] = []
    for entry in raw.get("reference_data", []):
        if not isinstance(entry, dict):
            raise AuthorityManifestError(
                f"baseline manifest {path}: reference_data entries must be objects"
            )
        natural_key = entry.get("natural_key")
        immutable_columns = entry.get("immutable_columns")
        if not natural_key or not immutable_columns:
            raise AuthorityManifestError(
                f"baseline manifest {path}: reference_data entry for "
                f"{entry.get('table')!r} needs natural_key and immutable_columns"
            )
        reference_sets.append(
            ReferenceDataSet(
                table=_validate_relation_name(entry["table"], "reference_data.table"),
                natural_key=tuple(_validate_identifier(c, "natural_key") for c in natural_key),
                immutable_columns=tuple(
                    _validate_identifier(c, "immutable_columns") for c in immutable_columns
                ),
                where=str(entry.get("where", "")),
            )
        )

    return BaselineManifest(
        baseline_id=str(raw["baseline_id"]),
        schema_revision=str(raw["schema_revision"]),
        source_git_sha=str(raw["source_git_sha"]),
        last_legacy_change=str(raw["last_legacy_change"]),
        structural_sha256=str(raw["structural_sha256"]),
        acl_sha256=str(raw["acl_sha256"]),
        extensions_sha256=str(raw["extensions_sha256"]),
        reference_data_sha256=str(raw["reference_data_sha256"]),
        generator=str(raw["generator"]),
        generated_at=str(raw["generated_at"]),
        postgres_version=str(raw["postgres_version"]),
        reference_data=tuple(reference_sets),
    )


def default_baseline_dir(database_dir: Path, baseline_id: str = DEFAULT_BASELINE_ID) -> Path:
    return database_dir / "baselines" / baseline_id
