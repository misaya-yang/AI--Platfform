#!/usr/bin/env python3
"""Run the CHR-04 tool side-effect lifecycle contract tests.

The gate is deliberately source-level: it runs the small platform lifecycle
ledger in the controlled Codex fork and does not call a provider or mutate the
developer's Compose stack.  The Docker/runtime acceptance gate is separate.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


class GateError(RuntimeError):
    pass


def _fork_path(value: str | None) -> Path:
    path = Path(value or "../AI--Platfform-codex-harness").expanduser().resolve()
    if not (path / "justfile").is_file() or not (path / "codex-rs").is_dir():
        raise GateError(f"controlled Codex fork is not a source checkout: {path}")
    return path


def run_gate(fork: Path) -> int:
    command = ["just", "test", "-p", "ai-platform-agent-runtime"]
    env = os.environ.copy()
    env.setdefault("CARGO_TERM_COLOR", "never")
    completed = subprocess.run(command, cwd=fork, env=env, check=False)
    if completed.returncode:
        raise GateError(f"tool lifecycle contract failed with exit {completed.returncode}")
    print("CODEX_RUNTIME_WRITE_GATE_OK")
    print("invariants=approval,dispatch_fence,idempotency,recovery,pairing")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fork", default=os.environ.get("CODEX_HARNESS_FORK"))
    args = parser.parse_args()
    try:
        return run_gate(_fork_path(args.fork))
    except (GateError, OSError) as error:
        print(f"codex-runtime-write-gate: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
