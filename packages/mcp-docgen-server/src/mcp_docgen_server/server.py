"""MCP server wrapping docgen.

# TODO(agent-A): ensure pyproject.toml [project.dependencies] includes:
#   "mcp>=1.0"
#   "starlette>=0.37"   (for SSE transport + /artifacts/{id} route)
#   "uvicorn>=0.30"
#
# These are listed as hard deps because the entrypoint imports them
# unconditionally. Keeping them out of ``[optional-dependencies]`` means
# ``pip install hejaz-docgen`` brings up a working MCP server, which is
# what the Dockerfile and stdio test both assume.

Transport is chosen by the ``MCP_TRANSPORT`` env var:
  - ``stdio`` (default): local dev + the smoke test in tests/mcp/.
    Artifacts are emitted as ``file://`` URLs — the client has to live
    on the same machine. Fine for debugging; unsuitable for deployment.
  - ``sse``: binds a Starlette app on ``0.0.0.0:${PORT:-8765}``.
    Serves MCP over ``/mcp`` and artifacts via ``/artifacts/{id}``
    (HMAC-signed via docgen.storage.signing).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any, Optional

from .llm_caller import build_default_llm
from .resource_provider import (
    DESIGN_SYSTEMS_URI,
    SKILLS_URI_PREFIX,
    TEMPLATES_URI_PREFIX,
    list_static_resources,
    parse_uri,
    read_design_systems,
    read_skill,
    read_templates,
)
from .tool_schema import GenerateDocumentInput, input_json_schema

logger = logging.getLogger("mcp_docgen")


# ---------------------------------------------------------------------------
# Sampling adapter
#
# docgen's LLM planner expects an ``LLMCaller`` with a ``generate_json``
# method. When running under MCP, we should not make outbound LLM calls
# ourselves — we delegate back to the client via MCP sampling so docgen
# uses the client's model + token budget. If the client doesn't advertise
# sampling capability, we fall back to ``llm=None`` (deterministic planner).


class SamplingLLMCaller:
    """Adapter: docgen's ``LLMCaller`` protocol → MCP ``session.create_message``.

    Only the ``generate_json`` method is required by docgen's planner;
    we shim ``system`` + ``user`` into an MCP sampling request and parse
    the first text block as JSON.
    """

    def __init__(self, session: Any) -> None:
        self._session = session

    async def generate_json(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 4000,
    ) -> dict:
        # Local import keeps mcp an optional runtime dep at module load time.
        from mcp import types as mcp_types  # type: ignore

        messages = [
            mcp_types.SamplingMessage(
                role="user",
                content=mcp_types.TextContent(type="text", text=user),
            )
        ]
        result = await self._session.create_message(
            messages=messages,
            system_prompt=system,
            max_tokens=max_tokens,
        )
        # ``result.content`` is a single Content block; we assume text.
        text = ""
        content = getattr(result, "content", None)
        if content is not None and getattr(content, "type", "") == "text":
            text = getattr(content, "text", "") or ""
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            # Planner will treat a RuntimeError as "LLM unavailable" and
            # fall back to the deterministic planner. Better than
            # propagating a half-parsed dict downstream.
            raise RuntimeError(f"sampling result was not JSON: {exc}") from exc


# ---------------------------------------------------------------------------
# Helpers


def _tmp_artifact_root() -> Path:
    """Directory for generated artifacts.

    In production ``DOCGEN_ARTIFACT_ROOT`` is set (Dockerfile sets
    ``/var/lib/docgen/artifacts``, mounted as a volume). Locally we fall
    back to a temp directory so tests / dev runs leave no traces.
    """
    override = os.environ.get("DOCGEN_ARTIFACT_ROOT")
    root = Path(override) if override else Path(tempfile.gettempdir()) / "mcp_docgen_artifacts"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _default_service(base_download_url: str = "file://") -> Any:
    """Build a DocgenService wired to a LocalArtifactStore + default LLM.

    Imports are local so ``import mcp_docgen_server`` works even when
    docgen hasn't been installed yet (Agent A territory).

    ``llm`` is pulled from the environment (``DASHSCOPE_API_KEY``); when
    absent we fall back to ``None`` so the deterministic planner still
    produces *something* — but output quality drops to template-level.
    See ``build_default_llm`` for the full env contract.
    """
    from docgen.service import DocgenService  # type: ignore
    from docgen.storage import LocalArtifactStore  # type: ignore

    store = LocalArtifactStore(_tmp_artifact_root(), base_download_url=base_download_url)
    return DocgenService(artifact_store=store, llm=build_default_llm())


async def _run_generate(
    service: Any,
    payload: dict,
    *,
    tenant_id: str = "default",
    session_id: Optional[str] = None,
    turn_id: Optional[str] = None,
    progress_cb: Optional[Any] = None,
) -> dict:
    """Translate MCP tool args → ``GenerateRequest`` → dict for the tool."""
    from docgen.service import GenerateRequest  # type: ignore

    validated = GenerateDocumentInput.model_validate(payload)

    req = GenerateRequest(
        tenant_id=tenant_id,
        session_id=session_id or f"mcp-{uuid.uuid4().hex[:8]}",
        turn_id=turn_id or f"t-{uuid.uuid4().hex[:8]}",
        doc_type=validated.format,
        title=validated.title,
        goal=validated.goal,
        locale=validated.locale,
        body_markdown=validated.body_markdown,
        template_name=validated.template_name,
        style_hints=(
            {"design_system": validated.design_system}
            if validated.design_system
            else None
        ),
    )

    if progress_cb is not None:
        await progress_cb("plan", 0.1)

    resp = await service.generate(req)

    if progress_cb is not None:
        await progress_cb("render", 0.9)

    artifact = resp.artifact
    return {
        "artifact_id": artifact.artifact_id,
        "download_url": resp.download_url,
        "filename": artifact.filename,
        "size_bytes": artifact.size_bytes,
        "sha256": artifact.sha256,
        "plan_outline": resp.plan_text or "",
        "critic_passed": bool(resp.passed),
    }


# ---------------------------------------------------------------------------
# Server construction


def build_server(service: Any) -> Any:
    """Create the MCP ``Server`` with tools + resources registered.

    Kept as a function (not module-level) so the stdio and sse entry
    points can inject different service wiring (e.g. different artifact
    stores and download URL bases).
    """
    from mcp.server import Server  # type: ignore
    from mcp import types as mcp_types  # type: ignore

    server = Server("mcp-docgen")

    @server.list_tools()
    async def _list_tools() -> list:
        return [
            mcp_types.Tool(
                name="generate_document",
                description=(
                    "Plan + render a document (docx / pptx / xlsx / pdf). "
                    "Returns a download URL plus artifact metadata. "
                    "Inspect docgen://design-systems for palette options."
                ),
                inputSchema=input_json_schema(),
            )
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict) -> list:
        if name != "generate_document":
            raise ValueError(f"unknown tool: {name!r}")
        result = await _run_generate(service, arguments or {})
        return [
            mcp_types.TextContent(
                type="text",
                text=json.dumps(result, ensure_ascii=False, indent=2),
            )
        ]

    @server.list_resources()
    async def _list_resources() -> list:
        descriptors = list_static_resources()
        return [
            mcp_types.Resource(
                uri=d.uri,
                name=d.name,
                description=d.description,
                mimeType=d.mime_type,
            )
            for d in descriptors
        ]

    @server.read_resource()
    async def _read_resource(uri: str) -> str:
        kind, tail = parse_uri(str(uri))
        if kind == "design-systems":
            _mime, text = read_design_systems(service)
            return text
        if kind == "templates":
            assert tail is not None
            _mime, text = read_templates(service, tail)
            return text
        if kind == "skills":
            assert tail is not None
            _mime, text = read_skill(service, tail)
            return text
        raise ValueError(f"unhandled resource kind: {kind!r}")

    return server


# ---------------------------------------------------------------------------
# stdio entrypoint


async def main_stdio() -> None:
    """Run the MCP server over stdin/stdout JSON-RPC."""
    from mcp.server.stdio import stdio_server  # type: ignore

    service = _default_service(base_download_url="file://")
    server = build_server(service)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


# ---------------------------------------------------------------------------
# sse entrypoint
#
# Binds an SSE transport for MCP PLUS a ``/artifacts/{id}`` route that
# serves artifacts out of the LocalArtifactStore. The route verifies
# signed URLs from docgen.storage.signing — unsigned or expired requests
# get a 403.


async def main_sse() -> None:
    """Run the MCP server over HTTP+SSE with artifact hosting."""
    import uvicorn  # type: ignore
    from mcp.server.sse import SseServerTransport  # type: ignore
    from starlette.applications import Starlette  # type: ignore
    from starlette.requests import Request  # type: ignore
    from starlette.responses import FileResponse, JSONResponse, Response  # type: ignore
    from starlette.routing import Mount, Route  # type: ignore

    port = int(os.environ.get("PORT", "8765"))
    public_base = os.environ.get(
        "DOCGEN_PUBLIC_URL",
        f"http://127.0.0.1:{port}",
    ).rstrip("/")

    # Wire the store with a URL base pointing at our /artifacts route so
    # the signed URLs returned by the tool are directly fetchable.
    from docgen.service import DocgenService  # type: ignore
    from docgen.storage import LocalArtifactStore  # type: ignore

    store_root = _tmp_artifact_root()
    store = LocalArtifactStore(
        store_root,
        base_download_url=f"{public_base}/artifacts",
    )
    service = DocgenService(artifact_store=store, llm=None)
    server = build_server(service)

    sse = SseServerTransport("/messages/")

    async def handle_sse(request: Request) -> Response:  # type: ignore[override]
        async with sse.connect_sse(
            request.scope, request.receive, request._send
        ) as (reader, writer):
            await server.run(reader, writer, server.create_initialization_options())
        # connect_sse handles the response lifecycle
        return Response(status_code=200)

    async def health(_request: Request) -> Response:
        return JSONResponse({"status": "ok", "name": "mcp-docgen"})

    async def artifact(request: Request) -> Response:
        """See ``main_http.artifact`` — LocalArtifactStore signs the full
        4-segment key_path, so the route must capture slashes."""
        from docgen.storage.signing import verify_url  # type: ignore

        key_path = request.path_params["path"]
        full_url = str(request.url)
        if not verify_url(full_url):
            return JSONResponse({"error": "invalid or expired signature"}, status_code=403)
        target = (store_root / key_path).resolve()
        try:
            target.relative_to(store_root.resolve())
        except ValueError:
            return JSONResponse({"error": "invalid path"}, status_code=400)
        if target.is_file():
            return FileResponse(
                str(target),
                filename=target.name,
                media_type="application/octet-stream",
            )
        return JSONResponse({"error": "not found"}, status_code=404)

    app = Starlette(
        routes=[
            Route("/health", health),
            Route("/mcp", handle_sse),
            Mount("/messages/", app=sse.handle_post_message),
            Route("/artifacts/{path:path}", artifact),
        ]
    )

    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    await uvicorn.Server(config).serve()


# ---------------------------------------------------------------------------
# streamable-http (simplified)
#
# MCP's official transports — SSE and the StreamableHTTP session manager —
# assume a spec-compliant client (Accept negotiation, Mcp-Session-Id round-
# tripping, version headers). The ai-gateway MCPClient is intentionally
# simpler: it POSTs JSON-RPC 2.0 to ``/mcp`` and expects an immediate JSON
# response, no SSE, no session state.
#
# Rather than force that client to implement the full streamable-http spec
# (or add compat shims on both sides), we serve the same JSON-RPC surface
# directly: ``initialize``, ``tools/list``, ``tools/call``, plus the
# ``notifications/initialized`` fire-and-forget. This keeps the single-tool
# server simple and still speaks MCP at the wire-format level so other
# clients (Claude Desktop, etc.) can consume it via the stdio or sse
# entrypoints when a real session is desired.


async def main_http() -> None:
    """Run the MCP server as a plain JSON-RPC-over-HTTP endpoint.

    Suited for in-cluster consumption by our ai-gateway, where TLS is
    terminated at the ingress and client ↔ server runs over a trusted
    docker network. Serves:
      - ``POST /mcp``           — JSON-RPC 2.0 single-request/response
      - ``GET  /health``        — liveness probe
      - ``GET  /artifacts/{id}``— signed-URL artifact download
    """
    import uvicorn  # type: ignore
    from starlette.applications import Starlette  # type: ignore
    from starlette.requests import Request  # type: ignore
    from starlette.responses import FileResponse, JSONResponse, Response  # type: ignore
    from starlette.routing import Route  # type: ignore

    port = int(os.environ.get("PORT", "8765"))
    public_base = os.environ.get(
        "DOCGEN_PUBLIC_URL",
        f"http://127.0.0.1:{port}",
    ).rstrip("/")

    from docgen.service import DocgenService  # type: ignore
    from docgen.storage import LocalArtifactStore  # type: ignore

    store_root = _tmp_artifact_root()
    store = LocalArtifactStore(
        store_root,
        base_download_url=f"{public_base}/artifacts",
    )
    service = DocgenService(artifact_store=store, llm=build_default_llm())

    # JSON-RPC dispatch ------------------------------------------------------

    def _error(request_id: Any, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    def _result(request_id: Any, result: Any) -> dict:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    async def _dispatch(payload: dict) -> Optional[dict]:
        """Return a JSON-RPC response, or None for notifications."""
        method = payload.get("method")
        params = payload.get("params") or {}
        req_id = payload.get("id")

        # Notifications (id absent) — fire-and-forget, respond 204-ish.
        if req_id is None:
            return None

        if method == "initialize":
            return _result(req_id, {
                "protocolVersion": params.get("protocolVersion", "2025-11-25"),
                "capabilities": {"tools": {}, "resources": {}},
                "serverInfo": {"name": "mcp-docgen", "version": "0.1.0"},
            })

        if method == "tools/list":
            return _result(req_id, {
                "tools": [
                    {
                        "name": "generate_document",
                        "description": (
                            "Plan + render a document (docx / pptx / xlsx / pdf). "
                            "Returns a download URL plus artifact metadata."
                        ),
                        "inputSchema": input_json_schema(),
                    }
                ]
            })

        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if name != "generate_document":
                return _error(req_id, -32601, f"unknown tool: {name!r}")
            try:
                result = await _run_generate(service, arguments)
            except Exception as exc:  # noqa: BLE001
                logger.exception("generate_document failed")
                return _result(req_id, {
                    "content": [{"type": "text", "text": f"Tool call failed: {exc}"}],
                    "isError": True,
                })
            return _result(req_id, {
                "content": [{
                    "type": "text",
                    "text": json.dumps(result, ensure_ascii=False, indent=2),
                }],
                "isError": False,
            })

        return _error(req_id, -32601, f"method not found: {method!r}")

    async def mcp_endpoint(request: Request) -> Response:
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json"}, status_code=400)
        response = await _dispatch(payload)
        if response is None:
            # Notification — empty 204.
            return Response(status_code=204)
        return JSONResponse(response)

    async def health(_request: Request) -> Response:
        return JSONResponse({"status": "ok", "name": "mcp-docgen"})

    async def artifact(request: Request) -> Response:
        """Serve artifact via 4-segment key_path: tenant/session/turn/filename.

        LocalArtifactStore.download_url signs the full ``{base}/{key_path}``
        URL, so the route has to accept all four segments. ``{path:path}``
        captures slashes, letting us resolve the file directly off disk
        without walking meta.json files.
        """
        from docgen.storage.signing import verify_url  # type: ignore

        key_path = request.path_params["path"]
        full_url = str(request.url)
        if not verify_url(full_url):
            return JSONResponse({"error": "invalid or expired signature"}, status_code=403)

        target = (store_root / key_path).resolve()
        # Defense in depth: prevent ``..`` escape out of the store root.
        try:
            target.relative_to(store_root.resolve())
        except ValueError:
            return JSONResponse({"error": "invalid path"}, status_code=400)
        if target.is_file():
            return FileResponse(
                str(target),
                filename=target.name,
                media_type="application/octet-stream",
            )
        return JSONResponse({"error": "not found"}, status_code=404)

    app = Starlette(
        routes=[
            Route("/health", health),
            Route("/mcp", mcp_endpoint, methods=["POST"]),
            Route("/artifacts/{path:path}", artifact),
        ]
    )

    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    await uvicorn.Server(config).serve()


# ---------------------------------------------------------------------------
# module-runnable


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport == "stdio":
        asyncio.run(main_stdio())
    elif transport == "sse":
        asyncio.run(main_sse())
    elif transport == "http":
        asyncio.run(main_http())
    else:
        raise SystemExit(f"unknown MCP_TRANSPORT: {transport!r}")
