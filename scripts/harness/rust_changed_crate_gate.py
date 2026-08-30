#!/usr/bin/env python3
"""Rust overlay changed-crate gate (L1).

Maps the diff against a base SHA onto the Rust workspace under ``rust/`` and
runs ``cargo test`` only for the crates whose files changed. A change to a
workspace-level manifest (root Cargo.toml / Cargo.lock) tests the whole
workspace. No Rust changes at all => NOT APPLICABLE (exit 0, recorded in the
evidence — this is "nothing to gate", not a skipped gate).

Usage:
  python3 scripts/harness/rust_changed_crate_gate.py --base <sha>
  python3 scripts/harness/rust_changed_crate_gate.py --selftest   # crate mapping only

Exit codes: 0 pass/not-applicable, 1 test failure, 2 gate error (bad base,
missing cargo, unmappable change).
Evidence: tmp/gate-evidence/rust-changed-crate-gate.json
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUST_ROOT = "rust"
DEFAULT_EVIDENCE = ROOT / "tmp" / "gate-evidence" / "rust-changed-crate-gate.json"
PER_CRATE_TIMEOUT_S = 55 * 60


def changed_paths(base: str) -> list[str]:
    def run(*args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
        return result.stdout

    try:
        run("rev-parse", "--verify", "--quiet", base)
    except RuntimeError as exc:
        raise RuntimeError(f"invalid base SHA {base!r}: {exc}") from exc
    diff = run("diff", "--name-only", base)
    untracked = run("ls-files", "--others", "--exclude-standard")
    return sorted({p.strip() for p in (diff + untracked).splitlines() if p.strip()})


def crate_for_path(repo_root: Path, rel_path: str) -> tuple[str | None, Path | None]:
    """Return (crate name, crate manifest) for a changed file, walking up to the
    nearest Cargo.toml with a [package] section. Workspace-root manifests are
    reported as the special crate '@workspace'."""
    path = (repo_root / rel_path).resolve()
    if not rel_path.startswith(RUST_ROOT + "/") and rel_path != RUST_ROOT:
        return None, None
    current = path.parent if path.is_file() else path
    while True:
        manifest = current / "Cargo.toml"
        if manifest.is_file():
            name = package_name(manifest)
            if name:
                return name, manifest
            return "@workspace", manifest  # virtual manifest
        parent = current.parent
        if parent == current or not str(current).startswith(str(repo_root.resolve())):
            return None, None
        current = parent


def package_name(manifest: Path) -> str | None:
    """First `name = "..."` after the [package] header, if any."""
    try:
        text = manifest.read_text(encoding="utf-8")
    except OSError:
        return None
    in_package = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_package = stripped == "[package]"
            continue
        if in_package:
            match = re.match(r'^name\s*=\s*"([^"]+)"', stripped)
            if match:
                return match.group(1)
    return None


def workspace_root_for(manifest: Path) -> Path:
    """Walk up to the manifest that declares [workspace]."""
    current = manifest.parent
    while True:
        candidate = current / "Cargo.toml"
        if candidate.is_file() and "[workspace]" in candidate.read_text(encoding="utf-8"):
            return candidate
        parent = current.parent
        if parent == current:
            return manifest
        current = parent


def plan(repo_root: Path, changed: list[str]) -> tuple[dict[str, list[str]], list[str]]:
    """Return ({crate: [changed paths]}, unmapped rust paths)."""
    crates: dict[str, list[str]] = {}
    unmapped: list[str] = []
    for rel in changed:
        if not rel.startswith(RUST_ROOT + "/") and rel != RUST_ROOT:
            continue
        name, _manifest = crate_for_path(repo_root, rel)
        if name is None:
            unmapped.append(rel)
            continue
        crates.setdefault(name, []).append(rel)
    return crates, unmapped


def _selftest() -> int:
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "rust" / "ws").mkdir(parents=True)
        (root / "rust" / "ws" / "Cargo.toml").write_text(
            '[workspace]\nmembers = ["crate-a"]\n', encoding="utf-8"
        )
        (root / "rust" / "ws" / "crate-a").mkdir()
        (root / "rust" / "ws" / "crate-a" / "Cargo.toml").write_text(
            '[package]\nname = "crate-a"\nversion = "0.1.0"\n', encoding="utf-8"
        )
        (root / "rust" / "ws" / "crate-a" / "src").mkdir()
        src = root / "rust" / "ws" / "crate-a" / "src" / "lib.rs"
        src.write_text("// code\n", encoding="utf-8")

        def check(label: str, got, expected) -> None:
            nonlocal failures
            ok = got == expected
            print(f"[{'ok' if ok else 'FAIL'}] {label}")
            if not ok:
                failures += 1
                print(f"       expected {expected!r}, got {got!r}")

        name, manifest = crate_for_path(root, "rust/ws/crate-a/src/lib.rs")
        check("file maps to nearest package", name, "crate-a")
        name, _ = crate_for_path(root, "rust/ws/Cargo.toml")
        check("workspace manifest maps to @workspace", name, "@workspace")
        crates, unmapped = plan(root, ["rust/ws/crate-a/src/lib.rs", "src/main.py", "rust/unknown/notes.txt"])
        check("non-rust paths ignored", sorted(crates), ["crate-a"])
        check("rust paths without a manifest are unmapped", unmapped, ["rust/unknown/notes.txt"])
        check("package_name parses [package]", package_name(root / "rust" / "ws" / "crate-a" / "Cargo.toml"), "crate-a")
        check("package_name on virtual manifest", package_name(root / "rust" / "ws" / "Cargo.toml"), None)

    if failures:
        print(f"SELFTEST FAILED: {failures} check(s)", file=sys.stderr)
        return 1
    print("SELFTEST OK: crate mapping and workspace detection verified")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base")
    parser.add_argument("--evidence-out", type=Path, default=DEFAULT_EVIDENCE)
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)
    if args.selftest:
        return _selftest()
    if not args.base:
        print("ERROR: --base <sha> is required (make rust-changed-crate-gate BASE_SHA=<sha>)", file=sys.stderr)
        return 2

    try:
        changed = changed_paths(args.base)
    except (RuntimeError, OSError) as exc:
        print(f"GATE ERROR: {exc}", file=sys.stderr)
        return 2

    crates, unmapped = plan(ROOT, changed)

    def write_evidence(result: str, detail: dict) -> None:
        args.evidence_out.parent.mkdir(parents=True, exist_ok=True)
        args.evidence_out.write_text(
            json.dumps(
                {"gate": "rust-changed-crate-gate", "tier": "L1", "base": args.base,
                 "crates": detail, "result": result},
                ensure_ascii=False, indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    if not crates and not unmapped:
        print("NOT APPLICABLE: no rust/ paths in the diff; nothing to gate")
        write_evidence("not-applicable", {})
        return 0
    if unmapped:
        print(f"GATE ERROR: rust changes map to no crate manifest: {unmapped}", file=sys.stderr)
        write_evidence("fail", {"unmapped": unmapped})
        return 2

    cargo = shutil.which("cargo")
    if not cargo:
        print("GATE ERROR: cargo is required to test changed Rust crates", file=sys.stderr)
        return 2

    results: dict[str, dict] = {}
    exit_code = 0
    for crate in sorted(crates):
        if crate == "@workspace":
            manifest = ROOT / RUST_ROOT / "agent-runtime-overlay" / "kernel-rs" / "Cargo.toml"
            cmd = [cargo, "test", "--workspace", "--manifest-path", str(manifest)]
            label = "@workspace (manifest change tests the whole workspace)"
        else:
            _name, crate_manifest = crate_for_path(ROOT, crates[crate][0])
            ws_manifest = workspace_root_for(crate_manifest)
            cmd = [cargo, "test", "-p", crate, "--manifest-path", str(ws_manifest)]
            label = crate
        print(f"running: {' '.join(cmd)}  [{label}]")
        try:
            proc = subprocess.run(cmd, cwd=ROOT, timeout=PER_CRATE_TIMEOUT_S)
            status = "pass" if proc.returncode == 0 else "fail"
        except subprocess.TimeoutExpired:
            status = "timeout"
        results[crate] = {"status": status, "paths": crates[crate]}
        print(f"  {crate}: {status}")
        if status != "pass":
            exit_code = 1

    write_evidence("pass" if exit_code == 0 else "fail", results)
    if exit_code == 0:
        print(f"OK: {len(crates)} changed crate(s) tested (evidence: {args.evidence_out.relative_to(ROOT)})")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
