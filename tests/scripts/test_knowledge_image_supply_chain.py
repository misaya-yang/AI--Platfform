import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
KNOWLEDGE_DIR = ROOT / "apps/knowledge-service"

LOCK_EXPORTS = (
    (
        KNOWLEDGE_DIR / "build-requirements.lock.txt",
        (
            "--package",
            "knowledge-service",
            "--only-group",
            "image-build",
            "--no-annotate",
            "--no-header",
            "--no-emit-project",
            "--no-emit-workspace",
        ),
    ),
    (
        KNOWLEDGE_DIR / "requirements.lock.txt",
        (
            "--package",
            "knowledge-service",
            "--no-dev",
            "--no-group",
            "image-build",
            "--no-annotate",
            "--no-header",
            "--no-emit-package",
            "knowledge-service",
            "--no-emit-package",
            "ai-gateway-core",
        ),
    ),
)


def _logical_requirements(text: str) -> list[str]:
    requirements: list[str] = []
    current: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        continued = line.endswith("\\")
        current.append(line.removesuffix("\\").strip())
        if not continued:
            requirements.append(" ".join(current))
            current = []
    assert not current, "lock file ends with an incomplete requirement"
    return requirements


@pytest.mark.parametrize(("checked_in", "export_args"), LOCK_EXPORTS)
def test_knowledge_image_locks_match_uv_lock(
    tmp_path: Path,
    checked_in: Path,
    export_args: tuple[str, ...],
) -> None:
    generated = tmp_path / checked_in.name
    result = subprocess.run(
        [
            "uv",
            "export",
            "--locked",
            "--quiet",
            *export_args,
            "--output-file",
            str(generated),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert checked_in.read_bytes() == generated.read_bytes(), (
        f"{checked_in.relative_to(ROOT)} is stale; run make kb-image-lock-refresh"
    )


@pytest.mark.parametrize("lock_path", [lock_path for lock_path, _args in LOCK_EXPORTS])
def test_knowledge_image_locks_use_exact_versions_and_sha256_hashes(
    lock_path: Path,
) -> None:
    requirements = _logical_requirements(lock_path.read_text())

    assert requirements
    for requirement in requirements:
        assert re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*==[^ ;]+", requirement), requirement
        hashes = re.findall(r"--hash=sha256:([0-9a-f]{64})(?:\s|$)", requirement)
        assert hashes, requirement
        assert len(hashes) == requirement.count("--hash=sha256:"), requirement


def test_knowledge_dockerfile_has_no_floating_python_installs() -> None:
    dockerfile = (KNOWLEDGE_DIR / "Dockerfile").read_text()

    assert "pip install --upgrade" not in dockerfile
    assert dockerfile.count("--require-hashes") == 2
    assert dockerfile.count("--no-build-isolation") == 2
    assert "python -m pip install --no-deps --no-build-isolation" in dockerfile
    assert "build-requirements.lock.txt" in dockerfile
    assert "requirements.lock.txt" in dockerfile
    assert "/build/packages/ai-gateway-core /build/knowledge-service" in dockerfile
    assert "hatchling==1.27.0" in (KNOWLEDGE_DIR / "build-requirements.lock.txt").read_text()


def test_knowledge_image_gate_runs_in_pinned_ci_and_keeps_multiarch_publish() -> None:
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    publish = (ROOT / ".github/workflows/docker-publish.yml").read_text()

    assert 'UV_VERSION: "0.11.19"' in ci
    assert ci.count('python -m pip install "uv==${UV_VERSION}"') == 3
    assert "run: make kb-image-lock-gate" in ci
    assert "dockerfile: ./apps/knowledge-service/Dockerfile" in publish
    assert "platforms: linux/amd64,linux/arm64" in publish
