"""
Diagnose why the model can't see Confluence tools.

Run ON THE SERVER (where the gateway process is):
    python3 scripts/diagnose_confluence_tools.py

or over ssh:
    ssh ... "cd /opt/deploy/ai-gateway && python3 scripts/diagnose_confluence_tools.py"

Checks:
1. Is register_confluence_tools importable in this deployed code?
2. Does the tool_registry have confluence_read / confluence_write currently?
3. If not: is the old 8-tool code path still present (= stale deploy)?
4. What are ALL tools registered right now?

This talks directly to the in-process registry — but the registry is
PROCESS-LOCAL, so you must run this inside the same process to see the
runtime state. For the running gateway, expose an endpoint that calls
`list_registered_tools()` below and curl it instead (see bottom).
"""

from __future__ import annotations

import sys


def run_local_check() -> None:
    """Verify the CODE deployed on disk has the new meta-tools."""
    print("=" * 60)
    print("1. CODE check — is the new meta-tool code deployed?")
    print("=" * 60)
    try:
        from src.services.assistant.tools.confluence_tool import (
            CONFLUENCE_READ_DEFINITION,
            CONFLUENCE_WRITE_DEFINITION,
            register_confluence_tools,
        )
        print("✅ New meta-tool definitions importable")
        print(f"   confluence_read actions : "
              f"{[p.enum for p in CONFLUENCE_READ_DEFINITION.parameters if p.name == 'action'][0]}")
        print(f"   confluence_write actions: "
              f"{[p.enum for p in CONFLUENCE_WRITE_DEFINITION.parameters if p.name == 'action'][0]}")
    except ImportError as e:
        print(f"❌ NEW meta-tool code is NOT on this server — {e}")
        print("   → Deploy is stale. Rebuild the gateway image.")
        return
    except Exception as e:
        print(f"❌ Import error: {e}")
        return

    # Check for leftover old code.
    try:
        from src.services.assistant.tools import confluence_tool as ct
        for old_name in (
            "ConfluenceSearchExecutor",
            "ConfluenceReadExecutor",
            "SEARCH_CONFLUENCE_DEFINITION",
            "READ_CONFLUENCE_PAGE_DEFINITION",
        ):
            if hasattr(ct, old_name) and old_name not in ("ConfluenceReadExecutor",):
                # ConfluenceReadExecutor is the new name too — skip
                print(f"⚠️  Legacy symbol still present: {old_name}")
    except Exception:
        pass

    print()
    print("=" * 60)
    print("2. REGISTRY check — are the tools actually registered NOW?")
    print("=" * 60)
    from src.services.assistant.tools.tool_registry import get_tool_registry
    reg = get_tool_registry()
    all_tools = reg.list_tools()
    confluence_tools = [t.name for t in all_tools if "confluence" in t.name]

    if not confluence_tools:
        print("❌ NO confluence tools in the in-process registry.")
        print("   → The tenant has NOT called `start_confluence()` in this")
        print("     process. `register_confluence_tools()` is runtime state;")
        print("     it evaporates on restart. The frontend 'activate' button")
        print("     likely only updates DB state, not the live registry.")
        print()
        print("   FIX OPTIONS:")
        print("   a) Have the frontend re-call the 'activate' API endpoint")
        print("      after every backend restart.")
        print("   b) On server startup, scan the connectors DB table and")
        print("      auto-call start_confluence for each tenant that has a")
        print("      saved Confluence connection.")
    else:
        print(f"✅ Confluence tools registered: {confluence_tools}")
        if "confluence_read" in confluence_tools and "confluence_write" in confluence_tools:
            print("   → This is the NEW meta-tool setup. Correct.")
        elif "search_confluence" in confluence_tools:
            print("   ⚠️  Old 8-tool setup still registered. Deploy is partly stale")
            print("      OR activation happened before the code was updated.")
            print("      Run stop_connector + start_confluence to re-register.")

    print()
    print("=" * 60)
    print("3. FULL tool inventory")
    print("=" * 60)
    print(f"Total tools in registry: {len(all_tools)}")
    for t in all_tools:
        print(f"  - {t.name}  ({t.category.value if t.category else '?'})")


def run_live_check_hint() -> None:
    print()
    print("=" * 60)
    print("If you can't run Python directly on the server, add a debug endpoint:")
    print("=" * 60)
    print("""
# In apps/assistant-service/.../api/routes/debug.py (or similar):

from fastapi import APIRouter
from src.services.assistant.tools.tool_registry import get_tool_registry

router = APIRouter(prefix="/debug")

@router.get("/tools")
async def list_tools():
    reg = get_tool_registry()
    return {
        "count": len(reg.list_tools()),
        "tools": [{"name": t.name, "category": t.category.value} for t in reg.list_tools()],
        "confluence": [t.name for t in reg.list_tools() if "confluence" in t.name],
    }

# Then:
curl https://api.your-domain/debug/tools
""")


if __name__ == "__main__":
    run_local_check()
    run_live_check_hint()
