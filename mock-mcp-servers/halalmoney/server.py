"""
Mock MCP Server: Halal Money Financial Platform
Implements MCP JSON-RPC 2.0 over HTTP for testing.
Tools: get_portfolio, check_halal_status, get_products
"""

import json
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="Halal Money MCP Server (Mock)")

TOOLS = [
    {
        "name": "get_portfolio",
        "description": "Get a user's investment portfolio with current holdings and values",
        "inputSchema": {
            "type": "object",
            "properties": {
                "user_id": {"type": "string", "description": "User ID (optional, defaults to current user)"},
            },
        },
    },
    {
        "name": "check_halal_status",
        "description": "Check if a stock or investment product is Halal-compliant according to Shariah standards",
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Stock ticker or product symbol (e.g., 'AAPL', 'BTC')"},
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "get_products",
        "description": "List available Halal investment products offered by Hejaz Financial Services",
        "inputSchema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": ["super", "etf", "home_loan", "auto_finance"], "description": "Product category filter"},
            },
        },
    },
]


def _handle_tool_call(name: str, args: dict) -> dict:
    if name == "get_portfolio":
        return {"content": [{"type": "text", "text": json.dumps({
            "holdings": [
                {"symbol": "HLAL", "name": "Wahed FTSE USA Shariah ETF", "units": 150, "value_aud": 8250.00, "return_pct": 12.3},
                {"symbol": "HEJAZ_SUPER", "name": "Hejaz Super Growth", "units": 1, "value_aud": 45000.00, "return_pct": 8.7},
                {"symbol": "IGLD", "name": "iShares Gold Trust", "units": 50, "value_aud": 3200.00, "return_pct": 5.1},
            ],
            "total_value_aud": 56450.00,
            "total_return_pct": 9.8,
            "last_updated": "2026-04-02T08:00:00Z",
        })}]}

    elif name == "check_halal_status":
        symbol = args.get("symbol", "").upper()
        # Mock compliance results
        compliant = {"AAPL": True, "MSFT": True, "HLAL": True, "BTC": False, "WINE": False}
        is_halal = compliant.get(symbol, None)
        return {"content": [{"type": "text", "text": json.dumps({
            "symbol": symbol,
            "halal_compliant": is_halal,
            "status": "Halal" if is_halal else "Not Halal" if is_halal is False else "Under Review",
            "reason": "Passes Shariah screening criteria" if is_halal else "Contains prohibited business activities" if is_halal is False else "Pending compliance review",
            "screened_by": "AAOIFI Shariah Standards",
            "last_screened": "2026-03-15",
        })}]}

    elif name == "get_products":
        category = args.get("category")
        products = [
            {"id": "super_growth", "name": "Hejaz Super Growth", "category": "super", "min_investment": 0, "return_pa": "8-12%", "shariah_certified": True},
            {"id": "super_balanced", "name": "Hejaz Super Balanced", "category": "super", "min_investment": 0, "return_pa": "6-9%", "shariah_certified": True},
            {"id": "hlal_etf", "name": "Halal ETF (HLAL)", "category": "etf", "min_investment": 100, "return_pa": "10-15%", "shariah_certified": True},
            {"id": "home_ijara", "name": "Home Ijara (Islamic Mortgage)", "category": "home_loan", "min_investment": 50000, "rate": "5.99%", "shariah_certified": True},
            {"id": "auto_murabaha", "name": "Auto Murabaha Finance", "category": "auto_finance", "min_investment": 5000, "rate": "6.49%", "shariah_certified": True},
        ]
        if category:
            products = [p for p in products if p["category"] == category]
        return {"content": [{"type": "text", "text": json.dumps({"products": products, "total": len(products)})}]}

    return {"content": [{"type": "text", "text": "Unknown tool"}], "isError": True}


@app.post("/mcp")
async def mcp_endpoint(request: Request):
    body = await request.json()
    method = body.get("method", "")
    req_id = body.get("id")
    params = body.get("params", {})

    if req_id is None:
        return JSONResponse(content={})

    if method == "initialize":
        return JSONResponse(content={
            "jsonrpc": "2.0", "id": req_id,
            "result": {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {"listChanged": True}},
                "serverInfo": {"name": "halalmoney-mcp-mock", "version": "1.0.0"},
            },
        })
    elif method == "tools/list":
        return JSONResponse(content={"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        result = _handle_tool_call(params.get("name", ""), params.get("arguments", {}))
        return JSONResponse(content={"jsonrpc": "2.0", "id": req_id, "result": result})

    return JSONResponse(content={"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method not found: {method}"}})


@app.get("/health")
async def health():
    return {"status": "ok", "service": "halalmoney-mcp-mock"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=3002)
