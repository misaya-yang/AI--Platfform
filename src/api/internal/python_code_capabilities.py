"""Private Gateway boundary for Python code execution.

The Gateway brokers a separately managed sandbox service; it never evaluates
the submitted source itself.  The worker supplies a signed runtime lease and
the sandbox service owns process isolation, cgroups, no-network policy and
output collection.  If the broker response is lost, callers must reconcile
the execution as ``side_effect_unknown`` rather than retrying.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from fastapi import APIRouter, Body, Header, HTTPException, Request

router = APIRouter(prefix="/internal/v2/agent-capabilities", tags=["internal-agent-capabilities"])

MAX_CODE_BYTES = 2_000_000
MAX_INPUT_BYTES = 24 * 1024 * 1024
MAX_OUTPUT_BYTES = 24 * 1024 * 1024
MAX_STREAM_BYTES = 2_000_000
MAX_OUTPUT_FILES = 64
MAX_TIMEOUT_SECONDS = 300
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_FILENAME_BYTES = 255


class SandboxBrokerError(RuntimeError):
    """A sandbox request could not be completed or verified."""


@dataclass(frozen=True)
class PythonSandboxLimits:
    timeout_seconds: int = 30
    memory_bytes: int = 512 * 1024 * 1024
    cpu_millis: int = 500
    pids: int = 32
    stdout_bytes: int = MAX_STREAM_BYTES
    stderr_bytes: int = MAX_STREAM_BYTES
    output_bytes: int = MAX_OUTPUT_BYTES
    output_files: int = MAX_OUTPUT_FILES

    def validate(self) -> None:
        if not 1 <= self.timeout_seconds <= MAX_TIMEOUT_SECONDS:
            raise SandboxBrokerError("invalid sandbox timeout")
        if min(self.memory_bytes, self.cpu_millis, self.pids) <= 0:
            raise SandboxBrokerError("invalid sandbox resource limit")
        if self.stdout_bytes > MAX_STREAM_BYTES or self.stderr_bytes > MAX_STREAM_BYTES:
            raise SandboxBrokerError("stream limit exceeds host bound")
        if self.output_bytes <= 0 or self.output_bytes > MAX_OUTPUT_BYTES or self.output_files <= 0 or self.output_files > MAX_OUTPUT_FILES:
            raise SandboxBrokerError("output limit exceeds host bound")


class SandboxTransport(Protocol):
    async def execute(self, payload: dict[str, Any]) -> dict[str, Any]: ...


class HttpSandboxTransport:
    """One-shot transport to a dedicated sandbox service.

    No Docker SDK, subprocess, provider credential, or local execution fallback
    is present here by design.
    """

    def __init__(self, base_url: str, internal_token: str, client: httpx.AsyncClient | None = None):
        parsed = httpx.URL(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.host or parsed.username or parsed.password:
            raise ValueError("sandbox broker URL must be an http(s) origin")
        if len(internal_token) < 32:
            raise ValueError("sandbox broker token is too short")
        self._url = parsed.copy_with(path="/internal/v2/sandbox/python/execute", query=None, fragment=None)
        self._token = internal_token
        self._client = client or httpx.AsyncClient()

    async def execute(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.post(
                self._url,
                headers={"x-ai-platform-internal-token": self._token},
                json=payload,
                timeout=float(payload["limits"]["timeout_seconds"]) + 10,
            )
        except httpx.TimeoutException as exc:
            raise SandboxBrokerError("sandbox outcome is unknown") from exc
        except httpx.HTTPError as exc:
            raise SandboxBrokerError("sandbox is unavailable") from exc
        if response.status_code >= 500:
            raise SandboxBrokerError("sandbox outcome is unknown")
        if response.status_code >= 400:
            raise SandboxBrokerError("sandbox rejected execution")
        try:
            result = response.json()
        except ValueError as exc:
            raise SandboxBrokerError("sandbox returned malformed result") from exc
        if not isinstance(result, dict):
            raise SandboxBrokerError("sandbox returned malformed result")
        return result


class ArtifactSink(Protocol):
    async def put_code_output(
        self, *, tenant_id: str, user_id: str, session_id: str, execution_id: str,
        filename: str, mime_type: str | None, content_base64: str, sha256: str, size_bytes: int,
    ) -> dict[str, Any]: ...


def _arguments_hash(code: str, inputs: list[dict[str, Any]]) -> str:
    body = json.dumps({"code": code, "inputs": inputs}, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return "sha256:" + hashlib.sha256(body.encode()).hexdigest()


def _validate_filename(filename: Any) -> str:
    if not isinstance(filename, str) or not filename or len(filename.encode()) > _MAX_FILENAME_BYTES:
        raise SandboxBrokerError("artifact filename is invalid")
    if filename in {".", ".."} or "/" in filename or "\\" in filename:
        raise SandboxBrokerError("artifact filename is invalid")
    if any(ord(char) < 32 for char in filename):
        raise SandboxBrokerError("artifact filename is invalid")
    return filename


def _validate_input(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise SandboxBrokerError("input attachment is malformed")
    filename = _validate_filename(item.get("filename"))
    try:
        size = int(item["size_bytes"])
        raw = base64.b64decode(str(item["content_base64"]), validate=True)
    except (KeyError, TypeError, ValueError, binascii.Error) as exc:
        raise SandboxBrokerError("input attachment is malformed") from exc
    if size < 0 or len(raw) != size:
        raise SandboxBrokerError("input attachment size mismatch")
    return {**item, "filename": filename, "size_bytes": size}


class PythonCodeCapability:
    """Validate, broker and persist a single idempotent code execution."""

    def __init__(self, transport: SandboxTransport, artifacts: ArtifactSink):
        self._transport = transport
        self._artifacts = artifacts

    async def execute(self, request: dict[str, Any]) -> dict[str, Any]:
        lease = request.get("lease")
        if not isinstance(lease, dict):
            raise SandboxBrokerError("runtime lease is required")
        for field in ("tenant_id", "user_id", "session_id"):
            value = lease.get(field)
            if not isinstance(value, str) or not value or len(value) > 255 or any(ord(char) < 32 for char in value):
                raise SandboxBrokerError("runtime lease scope is invalid")
        if lease.get("capability_id") not in {None, "execute_python_code"}:
            raise SandboxBrokerError("runtime lease capability is invalid")
        code = request.get("code")
        inputs = request.get("inputs") or []
        if not isinstance(inputs, list):
            raise SandboxBrokerError("input attachments are malformed")
        inputs = [_validate_input(item) for item in inputs]
        try:
            limits = PythonSandboxLimits(**(request.get("limits") or {}))
        except (TypeError, ValueError) as exc:
            raise SandboxBrokerError("sandbox limits are malformed") from exc
        if not isinstance(code, str) or not code.strip():
            raise SandboxBrokerError("code is required")
        if len(code.encode()) > MAX_CODE_BYTES:
            raise SandboxBrokerError("code exceeds host bound")
        input_bytes = sum(item["size_bytes"] for item in inputs)
        if input_bytes > MAX_INPUT_BYTES:
            raise SandboxBrokerError("input attachments exceed host bound")
        limits.validate()
        hash_arguments: dict[str, Any] = {"code": code, "inputs": inputs}
        if "limits" in request:
            hash_arguments["limits"] = limits.__dict__
        expected_hash = "sha256:" + hashlib.sha256(
            json.dumps(hash_arguments, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()
        if request.get("arguments_hash") != expected_hash:
            raise SandboxBrokerError("arguments hash mismatch")
        payload = {"lease": lease, "arguments_hash": expected_hash, "code": code, "inputs": inputs, "limits": limits.__dict__}
        result = await self._transport.execute(payload)
        execution_id = result.get("execution_id")
        if not isinstance(execution_id, str) or not execution_id:
            raise SandboxBrokerError("sandbox result missing execution id")
        if result.get("side_effect_state") in {"side_effect_unknown", "unknown"}:
            raise SandboxBrokerError("sandbox outcome is unknown")
        if result.get("status") not in {"succeeded", "failed", "timeout", "cancelled"}:
            raise SandboxBrokerError("sandbox result status is invalid")
        output_files = result.get("output_files") or []
        if not isinstance(output_files, list) or len(output_files) > MAX_OUTPUT_FILES:
            raise SandboxBrokerError("too many output files")
        total_output_bytes = 0
        persisted: list[dict[str, Any]] = []
        for item in output_files:
            if not isinstance(item, dict):
                raise SandboxBrokerError("malformed output artifact")
            filename = _validate_filename(item.get("filename"))
            try:
                size_bytes = int(item["size_bytes"])
                encoded = str(item["content_base64"])
                content = base64.b64decode(encoded, validate=True)
            except (KeyError, TypeError, ValueError, binascii.Error) as exc:
                raise SandboxBrokerError("malformed output artifact") from exc
            if size_bytes < 0 or len(content) != size_bytes:
                raise SandboxBrokerError("output artifact exceeds host bound")
            total_output_bytes += size_bytes
            if total_output_bytes > MAX_OUTPUT_BYTES:
                raise SandboxBrokerError("output artifacts exceed host bound")
            sha256 = item.get("sha256")
            if not isinstance(sha256, str) or not _SHA256.fullmatch(sha256) or not hmac.compare_digest(hashlib.sha256(content).hexdigest(), sha256):
                raise SandboxBrokerError("output artifact hash mismatch")
            persisted.append(await self._artifacts.put_code_output(
                tenant_id=str(lease["tenant_id"]), user_id=str(lease["user_id"]),
                session_id=str(lease["session_id"]), execution_id=execution_id,
                filename=filename, mime_type=item.get("mime_type"),
                content_base64=encoded, sha256=sha256,
                size_bytes=size_bytes,
            ))
        result["artifacts"] = persisted
        result["side_effect_state"] = "known"
        return result


@router.post("/python/execute")
async def execute_python_capability(
    request: Request,
    payload: dict[str, Any] = Body(...),
    x_ai_platform_internal_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Gateway boundary for the capability worker's sandbox request."""

    expected = os.getenv("AI_PLATFORM_INTERNAL_TOKEN", "")
    if not expected or not x_ai_platform_internal_token or not hmac.compare_digest(
        expected, x_ai_platform_internal_token
    ):
        raise HTTPException(status_code=401, detail="internal authorization failed")
    capability = getattr(request.app.state, "python_code_capability", None)
    if capability is None or not callable(getattr(capability, "execute", None)):
        raise HTTPException(status_code=503, detail="python capability unavailable")
    try:
        return await capability.execute(payload)
    except SandboxBrokerError as exc:
        raise HTTPException(status_code=424, detail=str(exc)) from None
