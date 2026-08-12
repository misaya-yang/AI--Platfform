from __future__ import annotations

from assistant_service.core.turn_contract import TurnKernel, TurnState


def test_nonterminal_stream_projection_is_constant_size_and_auditable() -> None:
    kernel = TurnKernel(run_id="run-1", request_id="request-1")
    kernel.transition(TurnState.PREPARING, reason="prepare")
    kernel.transition(TurnState.MODEL_RUNNING, reason="model")

    projection = kernel.stream_snapshot()

    assert "transitions" not in projection
    assert projection["transition_count"] == 2
    assert projection["last_transition"] == {
        "sequence_no": 2,
        "from": "preparing",
        "to": "model_running",
        "reason": "model",
    }
    assert len(json_bytes(projection)) < len(json_bytes(kernel.snapshot()))


def test_terminal_snapshot_keeps_complete_transition_audit() -> None:
    kernel = TurnKernel(run_id="run-1", request_id="request-1")
    kernel.transition(TurnState.PREPARING, reason="prepare")
    terminal = kernel.finish(TurnState.FAILED, reason="provider_error")

    assert terminal["terminal"] is True
    assert [item["to"] for item in terminal["transitions"]] == ["preparing", "failed"]


def json_bytes(value: object) -> bytes:
    import json

    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
