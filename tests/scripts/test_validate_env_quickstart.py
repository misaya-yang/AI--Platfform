from __future__ import annotations

import os
import secrets
import stat
import subprocess
from pathlib import Path


def test_validate_env_accepts_documented_local_bootstrap_password(tmp_path: Path) -> None:
    secret = secrets.token_hex(32)
    env_file = tmp_path / ".env"
    env_text = Path(".env.example").read_text()
    env_text = env_text.replace("change_me_generate_with_openssl_32_bytes_minimum", secret)
    env_text = env_text.replace("change_me_generate_with_openssl", secret)
    env_text = env_text.replace("change_me_embedding_provider_key", "test-embedding-key")
    env_text = env_text.replace("DASHSCOPE_API_KEY=", "DASHSCOPE_API_KEY=test-chat-key")
    env_file.write_text(env_text)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"compose\" ]; then\n"
        "  case \"$2\" in\n"
        "    version) exit 0 ;;\n"
        "    --env-file) exit 0 ;;\n"
        "  esac\n"
        "fi\n"
        "exit 1\n"
    )
    fake_docker.chmod(fake_docker.stat().st_mode | stat.S_IXUSR)

    env = {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
    }
    result = subprocess.run(
        ["bash", "scripts/new/validate-env.sh", "--env", str(env_file), "--config-only"],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "Configuration validation passed" in output
    assert "DEFAULT_USER_PASSWORD uses the documented local bootstrap default" in output
    assert secret not in output
