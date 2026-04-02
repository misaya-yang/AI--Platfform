"""
Mock MCP Server: Wahda Social Platform
Implements MCP JSON-RPC 2.0 over HTTP for testing.
Tools: create_post, search_messages, send_message
"""

import json
import uuid
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="Wahda MCP Server (Mock)")

TOOLS = [
    {
        "name": "create_post",
        "description": "Create a new post on Wahda social platform",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "Post content text"},
                "visibility": {"type": "string", "enum": ["public", "friends", "private"], "description": "Post visibility"},
            },
            "required": ["content"],
        },
    },
    {
        "name": "search_messages",
        "description": "Search messages and posts on Wahda",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "limit": {"type": "integer", "description": "Max results", "default": 5},
            },
            "required": ["query"],
        },
    },
    {
        "name": "send_message",
        "description": "Send a direct message to a Wahda user",
        "inputSchema": {
            "type": "object",
            "properties": {
                "to_user": {"type": "string", "description": "Recipient username or ID"},
                "message": {"type": "string", "description": "Message content"},
            },
            "required": ["to_user", "message"],
        },
    },
]


def _handle_tool_call(name: str, args: dict) -> dict:
    if name == "create_post":
        post_id = f"post_{uuid.uuid4().hex[:8]}"
        return {
            "content": [{"type": "text", "text": json.dumps({
                "success": True,
                "post_id": post_id,
                "message": f"Post created successfully! ID: {post_id}",
                "url": f"https://wahda.app/post/{post_id}",
                "created_at": datetime.utcnow().isoformat(),
            })}],
        }
    elif name == "search_messages":
        query = args.get("query", "")
        return {
            "content": [{"type": "text", "text": json.dumps({
                "results": [
                    {"id": "msg_001", "author": "Ahmad", "content": f"Discussion about {query}", "timestamp": "2026-04-01T10:00:00Z"},
                    {"id": "msg_002", "author": "Fatima", "content": f"Great insights on {query}!", "timestamp": "2026-04-01T09:30:00Z"},
                    {"id": "msg_003", "author": "Omar", "content": f"Has anyone tried {query}?", "timestamp": "2026-04-01T09:00:00Z"},
                ],
                "total": 3,
                "query": query,
            })}],
        }
    elif name == "send_message":
        return {
            "content": [{"type": "text", "text": json.dumps({
                "success": True,
                "message_id": f"dm_{uuid.uuid4().hex[:8]}",
                "delivered": True,
            })}],
        }
    return {"content": [{"type": "text", "text": "Unknown tool"}], "isError": True}


@app.post("/mcp")
async def mcp_endpoint(request: Request):
    body = await request.json()
    method = body.get("method", "")
    req_id = body.get("id")
    params = body.get("params", {})

    # Notifications (no id)
    if req_id is None:
        return JSONResponse(content={})

    if method == "initialize":
        return JSONResponse(content={
            "jsonrpc": "2.0", "id": req_id,
            "result": {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {"listChanged": True}},
                "serverInfo": {"name": "wahda-mcp-mock", "version": "1.0.0"},
            },
        })
    elif method == "tools/list":
        return JSONResponse(content={
            "jsonrpc": "2.0", "id": req_id,
            "result": {"tools": TOOLS},
        })
    elif method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        result = _handle_tool_call(tool_name, arguments)
        return JSONResponse(content={
            "jsonrpc": "2.0", "id": req_id,
            "result": result,
        })

    return JSONResponse(content={
        "jsonrpc": "2.0", "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    })


@app.get("/health")
async def health():
    return {"status": "ok", "service": "wahda-mcp-mock"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=3001)
