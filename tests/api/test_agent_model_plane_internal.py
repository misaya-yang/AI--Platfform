from __future__ import annotations

from fastapi import Request

from src.api.internal.agent_model_plane import _turn_metadata_header


def _request(headers: list[tuple[bytes, bytes]]) -> Request:
    return Request({"type": "http", "method": "POST", "path": "/", "headers": headers})


def test_turn_metadata_header_prefers_platform_name() -> None:
    request = _request(
        [
            (b"x-runtime-turn-metadata", b'{"source":"runtime"}'),
            (b"x-agent-turn-metadata", b'{"source":"platform"}'),
        ]
    )

    assert _turn_metadata_header(request) == '{"source":"platform"}'


def test_turn_metadata_header_accepts_one_runtime_alias() -> None:
    request = _request([(b"x-runtime-turn-metadata", b'{"lease":"signed"}')])

    assert _turn_metadata_header(request) == '{"lease":"signed"}'


def test_turn_metadata_header_rejects_ambiguous_runtime_aliases() -> None:
    request = _request(
        [
            (b"x-first-turn-metadata", b'{"lease":"one"}'),
            (b"x-second-turn-metadata", b'{"lease":"two"}'),
        ]
    )

    assert _turn_metadata_header(request) == ""
