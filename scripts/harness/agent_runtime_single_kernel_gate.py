#!/usr/bin/env python3
"""Fail-closed source contract for the platform's single Agent kernel."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    assignment = (ROOT / "src/services/assistant_runtime_assignment.py").read_text()
    gateway_v1 = (ROOT / "src/api/v1/assistant.py").read_text()
    gateway_v2 = (ROOT / "src/api/v2/agent.py").read_text()
    assistant_router = (
        ROOT / "apps/assistant-service/src/assistant_service/api/router.py"
    ).read_text()
    compose = (ROOT / "docker-compose.yml").read_text()

    assert 'RuntimeOwner = Literal["agent_runtime"]' in assignment
    assert "CANARY_PERCENT" not in assignment
    assert "python_control" not in assignment
    assert "codex_candidate" not in assignment
    assert "proxy_to_assistant_service(request, user, path=\"chat" not in gateway_v1
    assert "_start_agent_runtime_turn" in gateway_v1
    assert "agent_runtime_control" in gateway_v2
    assert 'route.path not in {"/chat", "/chat/stream"}' in assistant_router
    assert "\n  agent-runtime:\n" in compose
    runtime_block = compose.split("\n  agent-runtime:\n", 1)[1].split("\nnetworks:", 1)[0]
    assert "profiles:" not in runtime_block
    assert "container_name: ai-gateway-agent-runtime" in runtime_block
    assert "python_control" not in compose
    assert "codex_candidate" not in compose

    print(
        json.dumps(
            {
                "single_kernel": "agent_runtime",
                "v1_projection": "passed",
                "v2_native": "passed",
                "python_loop_route": "absent",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
