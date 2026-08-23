#!/usr/bin/env python3
"""Generate and validate the pinned Agent Harness supply-chain receipt."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

LOCK_SCHEMA = "ai-platform/agent-runtime-source-lock/v2"
RECEIPT_SCHEMA = "ai-platform/agent-runtime-source-receipt/v1"
ARTIFACT_BINARIES = {
    "app_server": "codex-app-server",
    "agent_runtime": "ai-platform-agent-runtime",
}
_HEX_40 = re.compile(r"^[0-9a-f]{40}$")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")


class ContractError(ValueError):
    """Raised when an immutable Harness identity is missing or inconsistent."""


def _run(command: list[str], *, cwd: Path) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bundle_identity(root: Path) -> dict[str, Any]:
    files = sorted(path for path in root.rglob("*") if path.is_file())
    if not files:
        raise ContractError(f"schema bundle is empty: {root}")
    digest = hashlib.sha256()
    total_bytes = 0
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        payload = path.read_bytes()
        total_bytes += len(payload)
        digest.update(relative)
        digest.update(b"\0")
        digest.update(payload)
        digest.update(b"\0")
    return {
        "sha256": digest.hexdigest(),
        "file_count": len(files),
        "total_bytes": total_bytes,
    }


def overlay_identity(root: Path) -> dict[str, Any]:
    """Hash the platform overlay without including its self-describing manifest."""
    files = sorted(
        path for path in root.rglob("*") if path.is_file() and path.name != "manifest.json"
    )
    if not files:
        raise ContractError(f"Agent Runtime overlay is empty: {root}")
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        payload = path.read_bytes()
        digest.update(relative)
        digest.update(b"\0")
        digest.update(payload)
        digest.update(b"\0")
    return {"sha256": digest.hexdigest(), "file_count": len(files)}


def _cargo_components(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    components: list[dict[str, Any]] = []
    for package in metadata.get("packages", []):
        name = str(package["name"])
        version = str(package["version"])
        component: dict[str, Any] = {
            "type": "library",
            "bom-ref": f"pkg:cargo/{name}@{version}",
            "name": name,
            "version": version,
            "purl": f"pkg:cargo/{name}@{version}",
        }
        license_value = package.get("license")
        if isinstance(license_value, str) and license_value:
            component["licenses"] = [{"expression": license_value}]
        source = package.get("source")
        if isinstance(source, str) and source:
            component["properties"] = [{"name": "cargo:source", "value": source}]
        components.append(component)
    return sorted(components, key=lambda item: (item["name"], item["version"], item["bom-ref"]))


def generate_receipt(*, fork: Path, schema_dir: Path, receipt_path: Path, sbom_path: Path) -> None:
    fork = fork.resolve()
    schema_dir = schema_dir.resolve()
    if _run(["git", "status", "--porcelain"], cwd=fork):
        raise ContractError("Agent Harness fork must be clean before generating a source receipt")

    fork_sha = _run(["git", "rev-parse", "HEAD"], cwd=fork)
    upstream_sha = _run(["git", "merge-base", "HEAD", "upstream/main"], cwd=fork)
    tree_sha = _run(["git", "rev-parse", "HEAD^{tree}"], cwd=fork)
    branch = _run(["git", "branch", "--show-current"], cwd=fork)
    upstream_url = _run(["git", "remote", "get-url", "upstream"], cwd=fork)
    cargo_dir = fork / "codex-rs"
    metadata = json.loads(
        _run(["cargo", "metadata", "--locked", "--format-version", "1"], cwd=cargo_dir)
    )
    cargo_version = _run(["cargo", "--version"], cwd=cargo_dir)
    rustc_version = _run(["rustc", "--version"], cwd=cargo_dir)

    components = _cargo_components(metadata)
    # The controlled fork revision is the runnable product artifact.  Seeding the
    # SBOM from the upstream merge-base would make distinct fork releases share
    # an identity even when their source and dependency graph differ.
    serial = uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"ai-platform-agent-runtime:{fork_sha}:{tree_sha}",
    )
    sbom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{serial}",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "bom-ref": f"urn:git:{fork_sha}",
                "name": "ai-platform-agent-runtime-source",
                "version": fork_sha,
                "properties": [
                    {"name": "ai-platform:upstream.url", "value": upstream_url},
                    {"name": "ai-platform:upstream.sha", "value": upstream_sha},
                    {"name": "ai-platform:git.tree", "value": tree_sha},
                ],
            }
        },
        "components": components,
    }
    sbom_path.parent.mkdir(parents=True, exist_ok=True)
    sbom_path.write_text(
        json.dumps(sbom, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    schema = bundle_identity(schema_dir)
    receipt = {
        "schema_version": RECEIPT_SCHEMA,
        "source": {
            "upstream_url": upstream_url,
            "upstream_sha": upstream_sha,
            "fork_sha": fork_sha,
            "git_tree_sha": tree_sha,
            "branch": branch,
        },
        "toolchain": {
            "cargo": cargo_version,
            "rustc": rustc_version,
            "rust_toolchain_sha256": sha256_file(cargo_dir / "rust-toolchain.toml"),
            "cargo_lock_sha256": sha256_file(cargo_dir / "Cargo.lock"),
        },
        "schema_bundle": schema,
        "license": {
            "spdx": "Apache-2.0",
            "license_sha256": sha256_file(fork / "LICENSE"),
            "upstream_notice_sha256": sha256_file(fork / "NOTICE"),
        },
        "sbom": {
            "format": "CycloneDX-1.5",
            "component_count": len(components),
            "sha256": sha256_file(sbom_path),
        },
    }
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _load_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"{label} is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise ContractError(f"{label} must be a JSON object")
    return value


def _write_object_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def _require_hex(value: Any, *, length: int, label: str) -> str:
    pattern = _HEX_40 if length == 40 else _HEX_64
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ContractError(f"{label} must be {length} lowercase hexadecimal characters")
    return value


def _validate_oci_artifact(
    *,
    artifact_id: str,
    artifact: dict[str, Any],
    release_state: str,
) -> bool:
    expected_binary = ARTIFACT_BINARIES[artifact_id]
    if artifact.get("binary") != expected_binary:
        raise ContractError(f"oci.artifacts.{artifact_id}.binary must be {expected_binary}")
    if not isinstance(artifact.get("protocol"), str) or not artifact["protocol"]:
        raise ContractError(f"oci.artifacts.{artifact_id}.protocol is required")
    platforms = artifact.get("platforms")
    if not isinstance(platforms, list) or not all(
        isinstance(platform, str) and platform for platform in platforms
    ):
        raise ContractError(f"oci.artifacts.{artifact_id}.platforms must be a string array")

    candidate_start_allowed = artifact.get("candidate_start_allowed") is True
    digest = artifact.get("image_digest")
    image_ref = artifact.get("image_ref")
    if not candidate_start_allowed:
        if digest is not None or image_ref is not None:
            raise ContractError(
                f"unlocked oci artifact {artifact_id} must not carry an image identity"
            )
        return False

    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ContractError(f"oci.artifacts.{artifact_id}.image_digest must be immutable")
    if release_state == "local_image_locked":
        if image_ref != f"local-image://{digest}":
            raise ContractError(
                f"local oci.artifacts.{artifact_id}.image_ref must equal local-image://<digest>"
            )
    elif release_state == "published":
        if not isinstance(image_ref, str) or not image_ref.endswith(f"@{digest}"):
            raise ContractError(
                f"published oci.artifacts.{artifact_id}.image_ref must be digest-pinned"
            )
    else:
        raise ContractError("a runnable image cannot use local_source_locked release state")
    return True


def validate_lock(
    *,
    repo_root: Path,
    lock_path: Path,
    require_runnable: bool = False,
    required_artifact: str | None = None,
) -> None:
    lock = _load_object(lock_path, label="Agent Harness lock")
    if lock.get("schema_version") != LOCK_SCHEMA:
        raise ContractError(f"unsupported lock schema: {lock.get('schema_version')!r}")

    release_state = lock.get("release_state")
    if release_state not in {"local_source_locked", "local_image_locked", "published"}:
        raise ContractError(
            "release_state must be local_source_locked, local_image_locked, or published"
        )

    source = lock.get("source")
    build = lock.get("build")
    license_info = lock.get("license")
    oci = lock.get("oci")
    if not all(isinstance(value, dict) for value in (source, build, license_info, oci)):
        raise ContractError("source, build, license, and oci sections must be objects")

    upstream_sha = _require_hex(source.get("upstream_sha"), length=40, label="source.upstream_sha")
    fork_sha = _require_hex(source.get("fork_sha"), length=40, label="source.fork_sha")
    if source.get("upstream_url") != "https://github.com/openai/codex.git":
        raise ContractError("source.upstream_url must identify the canonical HTTPS upstream")
    if not isinstance(source.get("fork_repository"), str) or not source["fork_repository"]:
        raise ContractError("source.fork_repository is required")

    receipt_rel = build.get("source_receipt")
    sbom_rel = build.get("sbom")
    notice_rel = license_info.get("notice")
    if not all(isinstance(value, str) and value for value in (receipt_rel, sbom_rel, notice_rel)):
        raise ContractError("source receipt, SBOM, and notice paths are required")
    receipt_path = repo_root / receipt_rel
    sbom_path = repo_root / sbom_rel
    notice_path = repo_root / notice_rel
    receipt = _load_object(receipt_path, label="Agent Harness source receipt")

    if receipt.get("schema_version") != RECEIPT_SCHEMA:
        raise ContractError("source receipt schema does not match the supported contract")
    overlay = receipt.get("overlay") or {}
    actual_overlay = None
    if build.get("overlay_manifest"):
        overlay_root = repo_root / "rust/agent-runtime-overlay"
        actual_overlay = overlay_identity(overlay_root)
        manifest = _load_object(
            overlay_root / "manifest.json", label="Agent Runtime overlay manifest"
        )
        if manifest.get("sha256") != actual_overlay["sha256"] or manifest.get("file_count") != actual_overlay["file_count"]:
            raise ContractError("Agent Runtime overlay manifest does not match its files")
        if manifest.get("source_revision") != source.get("fork_sha") or manifest.get("upstream_sha") != source.get("upstream_sha"):
            raise ContractError("Agent Runtime overlay source identity does not match the lock")
        if overlay.get("sha256") != actual_overlay["sha256"] or overlay.get("file_count") != actual_overlay["file_count"]:
            raise ContractError("source receipt overlay identity does not match the overlay")
    receipt_source = receipt.get("source") or {}
    if (
        receipt_source.get("upstream_sha") != upstream_sha
        or receipt_source.get("fork_sha") != fork_sha
    ):
        raise ContractError("lock source SHAs do not match the source receipt")
    receipt_tree_sha = _require_hex(
        receipt_source.get("git_tree_sha"), length=40, label="receipt git tree SHA"
    )
    if receipt_tree_sha != source.get("git_tree_sha"):
        raise ContractError("lock git tree SHA does not match the source receipt")
    if receipt_source.get("upstream_url") != source.get("upstream_url"):
        raise ContractError("lock upstream URL does not match the source receipt")

    expected_receipt_sha = _require_hex(
        build.get("source_receipt_sha256"), length=64, label="build.source_receipt_sha256"
    )
    if sha256_file(receipt_path) != expected_receipt_sha:
        raise ContractError("source receipt digest does not match the lock")
    expected_sbom_sha = _require_hex(build.get("sbom_sha256"), length=64, label="build.sbom_sha256")
    if not sbom_path.is_file() or sha256_file(sbom_path) != expected_sbom_sha:
        raise ContractError("SBOM is missing or its digest does not match the lock")
    receipt_sbom = receipt.get("sbom") or {}
    if receipt_sbom.get("sha256") != expected_sbom_sha:
        raise ContractError("source receipt SBOM digest does not match the lock")
    if build.get("overlay_manifest"):
        if build.get("overlay_manifest") != "rust/agent-runtime-overlay/manifest.json":
            raise ContractError("build.overlay_manifest must point to the Agent Runtime overlay manifest")
        if build.get("overlay_sha256") != actual_overlay["sha256"]:
            raise ContractError("build.overlay_sha256 does not match the Agent Runtime overlay")

    schema = receipt.get("schema_bundle") or {}
    _require_hex(schema.get("sha256"), length=64, label="schema bundle SHA")
    if not isinstance(schema.get("file_count"), int) or schema["file_count"] <= 0:
        raise ContractError("schema bundle file_count must be positive")
    if schema.get("sha256") != build.get("app_server_schema_sha256"):
        raise ContractError("App Server schema digest does not match the source receipt")
    if schema.get("file_count") != build.get("app_server_schema_file_count"):
        raise ContractError("App Server schema file count does not match the source receipt")

    toolchain = receipt.get("toolchain") or {}
    for field in ("cargo", "rustc", "rust_toolchain_sha256", "cargo_lock_sha256"):
        if toolchain.get(field) != build.get(field):
            raise ContractError(f"build.{field} does not match the source receipt")

    if license_info.get("spdx") != "Apache-2.0":
        raise ContractError("Agent Harness license must remain Apache-2.0")
    expected_notice_sha = _require_hex(
        license_info.get("notice_sha256"), length=64, label="license.notice_sha256"
    )
    if not notice_path.is_file() or sha256_file(notice_path) != expected_notice_sha:
        raise ContractError("Apache notice is missing or its digest does not match the lock")
    receipt_license = receipt.get("license") or {}
    if receipt_license.get("license_sha256") != license_info.get("upstream_license_sha256"):
        raise ContractError("upstream LICENSE digest does not match the source receipt")
    if receipt_license.get("upstream_notice_sha256") != license_info.get("upstream_notice_sha256"):
        raise ContractError("upstream NOTICE digest does not match the source receipt")

    artifacts = oci.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != set(ARTIFACT_BINARIES):
        raise ContractError("oci.artifacts must define app_server and agent_runtime")
    runnable = {
        artifact_id: _validate_oci_artifact(
            artifact_id=artifact_id,
            artifact=artifacts[artifact_id],
            release_state=release_state,
        )
        for artifact_id in ARTIFACT_BINARIES
        if isinstance(artifacts.get(artifact_id), dict)
    }
    if len(runnable) != len(ARTIFACT_BINARIES):
        raise ContractError("every oci artifact must be an object")
    if release_state == "local_source_locked" and any(runnable.values()):
        raise ContractError("local_source_locked cannot contain runnable OCI artifacts")
    if release_state in {"local_image_locked", "published"} and not any(runnable.values()):
        raise ContractError(f"{release_state} requires at least one runnable OCI artifact")

    required = required_artifact or ("app_server" if require_runnable else None)
    if required is not None:
        if required not in ARTIFACT_BINARIES:
            raise ContractError(f"unknown required OCI artifact: {required}")
        if not runnable[required]:
            raise ContractError(f"OCI artifact {required} is not locked to a runnable image")


def refresh_source_lock(*, repo_root: Path, lock_path: Path) -> None:
    lock = _load_object(lock_path, label="Agent Harness lock")
    if lock.get("schema_version") != LOCK_SCHEMA:
        raise ContractError("source lock must be upgraded before refresh")
    build = lock.get("build") or {}
    license_info = lock.get("license") or {}
    receipt_rel = build.get("source_receipt")
    sbom_rel = build.get("sbom")
    notice_rel = license_info.get("notice")
    if not all(isinstance(value, str) and value for value in (receipt_rel, sbom_rel, notice_rel)):
        raise ContractError("lock paths must be present before source refresh")
    receipt_path = repo_root / receipt_rel
    sbom_path = repo_root / sbom_rel
    notice_path = repo_root / notice_rel
    receipt = _load_object(receipt_path, label="Agent Harness source receipt")
    if receipt.get("schema_version") != RECEIPT_SCHEMA:
        raise ContractError("source receipt schema does not match the supported contract")
    source = receipt.get("source") or {}
    toolchain = receipt.get("toolchain") or {}
    schema = receipt.get("schema_bundle") or {}
    receipt_license = receipt.get("license") or {}
    receipt_sbom = receipt.get("sbom") or {}
    _require_hex(source.get("upstream_sha"), length=40, label="source upstream SHA")
    _require_hex(source.get("fork_sha"), length=40, label="source fork SHA")
    _require_hex(source.get("git_tree_sha"), length=40, label="source tree SHA")
    _require_hex(schema.get("sha256"), length=64, label="schema bundle SHA")
    if not sbom_path.is_file() or sha256_file(sbom_path) != receipt_sbom.get("sha256"):
        raise ContractError("generated SBOM does not match the source receipt")
    if not notice_path.is_file():
        raise ContractError("platform Apache notice is missing")

    fork_repository = (lock.get("source") or {}).get("fork_repository")
    if not isinstance(fork_repository, str) or not fork_repository:
        raise ContractError("source.fork_repository is required")
    lock["source"] = {
        "upstream_url": source.get("upstream_url"),
        "upstream_sha": source.get("upstream_sha"),
        "fork_repository": fork_repository,
        "fork_sha": source.get("fork_sha"),
        "git_tree_sha": source.get("git_tree_sha"),
        "branch": source.get("branch"),
    }
    lock["build"] = {
        "rustc": toolchain.get("rustc"),
        "cargo": toolchain.get("cargo"),
        "local_image_profile": build.get("local_image_profile", "dev-small"),
        "rust_toolchain_sha256": toolchain.get("rust_toolchain_sha256"),
        "cargo_lock_sha256": toolchain.get("cargo_lock_sha256"),
        "app_server_schema_sha256": schema.get("sha256"),
        "app_server_schema_file_count": schema.get("file_count"),
        "source_receipt": receipt_rel,
        "source_receipt_sha256": sha256_file(receipt_path),
        "sbom": sbom_rel,
        "sbom_sha256": sha256_file(sbom_path),
    }
    if build.get("overlay_manifest"):
        lock["build"]["overlay_manifest"] = build["overlay_manifest"]
        lock["build"]["overlay_sha256"] = build.get("overlay_sha256")
    lock["license"] = {
        "spdx": "Apache-2.0",
        "notice": notice_rel,
        "notice_sha256": sha256_file(notice_path),
        "upstream_license_sha256": receipt_license.get("license_sha256"),
        "upstream_notice_sha256": receipt_license.get("upstream_notice_sha256"),
    }
    lock["release_state"] = "local_source_locked"
    artifacts = (lock.get("oci") or {}).get("artifacts") or {}
    for artifact_id, binary in ARTIFACT_BINARIES.items():
        artifact = artifacts.get(artifact_id) or {}
        artifacts[artifact_id] = {
            "binary": binary,
            "protocol": artifact.get("protocol"),
            "candidate_start_allowed": False,
            "image_ref": None,
            "image_digest": None,
            "platforms": artifact.get("platforms", ["linux/arm64"]),
        }
    lock["oci"] = {"artifacts": artifacts}
    _write_object_atomic(lock_path, lock)
    validate_lock(repo_root=repo_root, lock_path=lock_path)


def record_local_image(
    *,
    repo_root: Path,
    lock_path: Path,
    artifact_id: str,
    image: str,
) -> None:
    if artifact_id not in ARTIFACT_BINARIES:
        raise ContractError(f"unknown OCI artifact: {artifact_id}")
    validate_lock(repo_root=repo_root, lock_path=lock_path)
    lock = _load_object(lock_path, label="Agent Harness lock")
    inspected = json.loads(_run(["docker", "image", "inspect", image], cwd=repo_root))
    if not isinstance(inspected, list) or len(inspected) != 1 or not isinstance(inspected[0], dict):
        raise ContractError("Docker image inspection returned an unexpected shape")
    image_info = inspected[0]
    labels = (image_info.get("Config") or {}).get("Labels") or {}
    source = lock["source"]
    build = lock["build"]
    expected_binary = ARTIFACT_BINARIES[artifact_id]
    expected_revision = source["fork_sha"]
    if build.get("overlay_sha256"):
        expected_revision = f"{source['upstream_sha']}+{build['overlay_sha256'][:12]}"
    expected_labels = {
        "org.opencontainers.image.revision": expected_revision,
        "com.misaya.ai-platform.agent-runtime.schema-sha256": build["app_server_schema_sha256"],
        "com.misaya.ai-platform.agent-runtime.artifact": artifact_id,
        "com.misaya.ai-platform.agent-runtime.binary": expected_binary,
    }
    if any(labels.get(key) != value for key, value in expected_labels.items()):
        raise ContractError("Docker image labels do not match the locked source/artifact identity")
    image_digest = image_info.get("Id")
    if not isinstance(image_digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest):
        raise ContractError("Docker image ID must be an immutable sha256 digest")
    os_name = image_info.get("Os")
    architecture = image_info.get("Architecture")
    if not all(isinstance(value, str) and value for value in (os_name, architecture)):
        raise ContractError("Docker image platform is missing")

    artifact = lock["oci"]["artifacts"][artifact_id]
    artifact.update(
        {
            "candidate_start_allowed": True,
            "image_ref": f"local-image://{image_digest}",
            "image_digest": image_digest,
            "platforms": [f"{os_name}/{architecture}"],
        }
    )
    lock["release_state"] = "local_image_locked"
    _write_object_atomic(lock_path, lock)
    validate_lock(
        repo_root=repo_root,
        lock_path=lock_path,
        required_artifact=artifact_id,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser(
        "generate", help="generate source receipt and deterministic SBOM"
    )
    generate.add_argument("--fork", type=Path, required=True)
    generate.add_argument("--schema-dir", type=Path, required=True)
    generate.add_argument("--receipt", type=Path, required=True)
    generate.add_argument("--sbom", type=Path, required=True)

    refresh = subparsers.add_parser(
        "refresh-source-lock",
        help="refresh source/build/license identity and invalidate every OCI artifact",
    )
    refresh.add_argument("--repo-root", type=Path, required=True)
    refresh.add_argument("--lock", type=Path, required=True)

    record = subparsers.add_parser(
        "record-local-image",
        help="record one locally built, label-verified OCI artifact",
    )
    record.add_argument("--repo-root", type=Path, required=True)
    record.add_argument("--lock", type=Path, required=True)
    record.add_argument("--artifact", choices=sorted(ARTIFACT_BINARIES), required=True)
    record.add_argument("--image", required=True)

    validate = subparsers.add_parser("validate", help="validate the committed immutable lock")
    validate.add_argument("--repo-root", type=Path, required=True)
    validate.add_argument("--lock", type=Path, required=True)
    validate.add_argument("--require-runnable", action="store_true")
    validate.add_argument("--require-artifact", choices=sorted(ARTIFACT_BINARIES))
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "generate":
            generate_receipt(
                fork=args.fork,
                schema_dir=args.schema_dir,
                receipt_path=args.receipt,
                sbom_path=args.sbom,
            )
        elif args.command == "refresh-source-lock":
            refresh_source_lock(
                repo_root=args.repo_root.resolve(),
                lock_path=args.lock.resolve(),
            )
        elif args.command == "record-local-image":
            record_local_image(
                repo_root=args.repo_root.resolve(),
                lock_path=args.lock.resolve(),
                artifact_id=args.artifact,
                image=args.image,
            )
        else:
            validate_lock(
                repo_root=args.repo_root.resolve(),
                lock_path=args.lock.resolve(),
                require_runnable=args.require_runnable,
                required_artifact=args.require_artifact,
            )
    except (ContractError, subprocess.CalledProcessError) as exc:
        print(f"agent-runtime-source-contract: {exc}", file=sys.stderr)
        return 1
    print("agent-runtime-source-contract: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
