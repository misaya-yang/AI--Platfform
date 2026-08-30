from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from scripts.harness.rust_changed_crate_gate import cargo_environment, cargo_test_command

ROOT = Path(__file__).resolve().parents[2]


def _init_git(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "tests@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Rust Build Tests"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)


def _fixture_repo(tmp_path: Path, *, locked: bool = True) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    source = tmp_path / "source"
    (repo / "scripts").mkdir(parents=True)
    shutil.copytree(ROOT / "scripts/rust", repo / "scripts/rust")
    harness = repo / "scripts/harness"
    harness.mkdir()
    for name in (
        "build_agent_runtime_image.sh",
        "build_agent_capability_worker_image.sh",
        "build_agent_runtime_source_image.sh",
    ):
        path = harness / name
        path.write_text("#!/usr/bin/env bash\nexit 99\n", encoding="utf-8")
        path.chmod(0o755)
    (harness / "agent_runtime_supply_chain.py").write_text(
        "raise SystemExit(98)\n", encoding="utf-8"
    )
    deploy = repo / "deploy/agent-runtime-source"
    deploy.mkdir(parents=True)
    cargo_line = "RUN cargo build --locked -p fixture\n" if locked else "RUN cargo build -p fixture\n"
    for name in ("Dockerfile.runtime", "Dockerfile.capability-worker", "Dockerfile"):
        (deploy / name).write_text(cargo_line, encoding="utf-8")
    (deploy / "lock.json").write_text("{}\n", encoding="utf-8")
    _init_git(repo)

    source.mkdir(parents=True)
    (source / "README.md").write_text("controlled source\n", encoding="utf-8")
    _init_git(source)
    return repo, source


def _dry_run(repo: Path, source: Path, *extra: str, minimum_memory: str = "1"):
    environment = {
        **os.environ,
        "AI_PLATFORM_AGENT_RUNTIME_SOURCE": str(source),
        "AI_PLATFORM_RUST_MIN_AVAILABLE_MEMORY_MB": minimum_memory,
        "AI_PLATFORM_RUST_MIN_FREE_DISK_MB": "1",
    }
    return subprocess.run(
        ["bash", "scripts/rust/build-update.sh", "--dry-run", *extra],
        cwd=repo,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )


def test_changed_crate_command_is_locked_and_forces_one_job(tmp_path: Path) -> None:
    manifest = tmp_path / "Cargo.toml"

    package = cargo_test_command("cargo", "crate-a", manifest)
    workspace = cargo_test_command("cargo", "@workspace", manifest)

    assert package == [
        "cargo",
        "test",
        "--locked",
        "-p",
        "crate-a",
        "--manifest-path",
        str(manifest),
    ]
    assert workspace == [
        "cargo",
        "test",
        "--locked",
        "--workspace",
        "--manifest-path",
        str(manifest),
    ]
    assert cargo_environment()["CARGO_BUILD_JOBS"] == "1"


def test_all_local_build_scripts_default_to_one_job() -> None:
    expected = {
        "build_agent_runtime_image.sh": (
            'cargo_jobs="${AI_PLATFORM_AGENT_RUNTIME_CARGO_JOBS:-${CARGO_BUILD_JOBS:-1}}"'
        ),
        "build_agent_runtime_source_image.sh": (
            'cargo_jobs="${AI_PLATFORM_AGENT_RUNTIME_CARGO_JOBS:-${CARGO_BUILD_JOBS:-1}}"'
        ),
        "build_agent_capability_worker_image.sh": (
            'cargo_jobs="${AI_PLATFORM_CAPABILITY_WORKER_CARGO_JOBS:-${CARGO_BUILD_JOBS:-1}}"'
        ),
    }
    for name, declaration in expected.items():
        assert declaration in (ROOT / "scripts/harness" / name).read_text(encoding="utf-8")


def test_build_update_dry_run_is_locked_local_only_and_calls_real_entries(tmp_path: Path) -> None:
    repo, source = _fixture_repo(tmp_path)

    result = _dry_run(repo, source, "--artifact", "all")

    assert result.returncode == 0, result.stderr
    assert "jobs=1 cargo_mode=--locked" in result.stdout
    assert "LOCAL-ONLY" in result.stdout
    assert "does not publish or claim multi-arch artifacts" in result.stdout
    assert "build_agent_runtime_image.sh" in result.stdout
    assert "build_agent_capability_worker_image.sh" in result.stdout
    assert "--require-artifact agent_runtime" in result.stdout
    assert "--require-artifact capability_worker" in result.stdout
    assert "DRY RUN" in result.stdout
    assert not (repo / ".git/ai-gateway-locks/.low-memory").exists()
    assert not (repo / ".git/ai-gateway-locks/rust-build").exists()


def test_build_update_fails_closed_on_resource_and_locked_entrypoint_preflight(
    tmp_path: Path,
) -> None:
    low_resource_repo, low_resource_source = _fixture_repo(tmp_path / "low")
    low_resource = _dry_run(
        low_resource_repo,
        low_resource_source,
        "--artifact",
        "runtime",
        minimum_memory="999999999",
    )
    assert low_resource.returncode != 0
    assert "below required" in low_resource.stderr
    assert not (low_resource_repo / ".git/ai-gateway-locks/.low-memory").exists()

    unlocked_repo, unlocked_source = _fixture_repo(tmp_path / "unlocked", locked=False)
    unlocked = _dry_run(unlocked_repo, unlocked_source, "--artifact", "runtime")
    assert unlocked.returncode != 0
    assert "not Cargo --locked" in unlocked.stderr
    assert not (unlocked_repo / ".git/ai-gateway-locks/.low-memory").exists()


def test_rust_shell_entrypoints_are_syntax_valid() -> None:
    scripts = [
        ROOT / "scripts/rust/locks.sh",
        ROOT / "scripts/rust/build-update.sh",
        ROOT / "scripts/harness/build_agent_runtime_image.sh",
        ROOT / "scripts/harness/build_agent_runtime_source_image.sh",
        ROOT / "scripts/harness/build_agent_capability_worker_image.sh",
    ]
    for script in scripts:
        result = subprocess.run(
            ["bash", "-n", str(script)], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, f"{script}: {result.stderr}"
