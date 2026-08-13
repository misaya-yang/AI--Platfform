from __future__ import annotations

import importlib.util
import stat
import subprocess
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SKILLS_ROOT = ROOT / "packages" / "mcp-docgen-server" / "src" / "docgen" / "_skills_data"


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
    sources = []
    for skill in ("docx", "xlsx", "pptx"):
        source = (
            SKILLS_ROOT / skill / "scripts" / "office" / "soffice.py"
        ).read_text()
        sources.append(source)
        assert 'Path(tempfile.gettempdir()) / "lo_socket_shim.so"' not in source
        assert 'tempfile.mkdtemp(prefix="lo_socket_shim_")' in source
    assert len(set(sources)) == 1


def test_all_soffice_launchers_use_a_closed_bootstrap_environment(monkeypatch):
    source_environment = {
        "HOME": "/tmp/office-home",
        "LANG": "C.UTF-8",
        "LANGUAGE": "en_US:en",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "TMPDIR": "/tmp",
        "TZ": "UTC",
        "OPENAI_API_KEY": "provider-secret-must-not-reach-soffice",
        "GATEWAY_ASSISTANT_SHARED_SECRET": "gateway-secret-must-not-reach-soffice",
        "TENANT_ID": "tenant-context-must-not-reach-soffice",
        "ASSISTANT_DEFAULT_MEMORY_MODE": "platform-config-must-not-reach-soffice",
        "LD_PRELOAD": "/tmp/untrusted-preload.so",
        "PYTHONPATH": "/tmp/untrusted-pythonpath",
    }
    expected = {
        name: source_environment[name]
        for name in ("HOME", "LANG", "LANGUAGE", "LC_ALL", "PATH", "TMPDIR", "TZ")
    }
    expected["SAL_USE_VCLPLUGIN"] = "svp"

    for skill in ("docx", "pptx", "xlsx"):
        module = _load_module(
            f"test_{skill}_soffice_environment",
            SKILLS_ROOT / skill / "scripts" / "office" / "soffice.py",
        )
        monkeypatch.setattr(module.os, "environ", source_environment)
        monkeypatch.setattr(module, "_needs_shim", lambda: False)

        assert module.get_soffice_env() == expected


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
