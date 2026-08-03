from __future__ import annotations

import importlib.util
import stat
import subprocess
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SKILLS_ROOT = (
    ROOT
    / "apps"
    / "assistant-service"
    / "src"
    / "assistant_service"
    / "core"
    / "skills"
)


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_soffice_shim_is_compiled_in_a_private_process_directory(monkeypatch):
    module = _load_module(
        "test_docx_soffice_security",
        SKILLS_ROOT / "docx" / "scripts" / "office" / "soffice.py",
    )

    def fake_compile(argv, **_kwargs):
        output_path = Path(argv[argv.index("-o") + 1])
        output_path.write_bytes(b"compiled-for-test")
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(module.subprocess, "run", fake_compile)
    shim = module._ensure_shim()

    try:
        shared_temp_shim = Path(tempfile.gettempdir()) / "lo_socket_shim.so"
        assert shim != shared_temp_shim
        assert shim.parent.name.startswith("lo_socket_shim_")
        assert stat.S_IMODE(shim.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(shim.stat().st_mode) == 0o700
        assert module._ensure_shim() == shim
    finally:
        module._cleanup_shim()


def test_all_office_shims_avoid_the_shared_predictable_path():
    for skill in ("docx", "xlsx", "pptx"):
        source = (
            SKILLS_ROOT / skill / "scripts" / "office" / "soffice.py"
        ).read_text()
        assert 'Path(tempfile.gettempdir()) / "lo_socket_shim.so"' not in source
        assert 'tempfile.mkdtemp(prefix="lo_socket_shim_")' in source


def test_accept_changes_reports_timeout_and_uses_ephemeral_profile(
    monkeypatch,
    tmp_path,
):
    office_package = types.ModuleType("office")
    office_soffice = types.ModuleType("office.soffice")
    office_soffice.get_soffice_env = lambda: {}
    monkeypatch.setitem(sys.modules, "office", office_package)
    monkeypatch.setitem(sys.modules, "office.soffice", office_soffice)
    module = _load_module(
        "test_accept_changes_security",
        SKILLS_ROOT / "docx" / "scripts" / "accept_changes.py",
    )

    profiles: list[Path] = []

    def record_profile(profile_dir: Path) -> bool:
        profiles.append(profile_dir)
        return True

    def time_out(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="soffice", timeout=30)

    monkeypatch.setattr(module, "_setup_libreoffice_macro", record_profile)
    monkeypatch.setattr(module.subprocess, "run", time_out)
    input_file = tmp_path / "input.docx"
    output_file = tmp_path / "output.docx"
    input_file.write_bytes(b"test-docx")

    _, message = module.accept_changes(str(input_file), str(output_file))

    assert message == "Error: LibreOffice timed out while accepting tracked changes"
    assert len(profiles) == 1
    assert profiles[0].name.startswith("libreoffice_docx_profile_")
    assert not profiles[0].exists()
