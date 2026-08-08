#!/usr/bin/env python3
"""Validate one local Agent Plugins 1.0.0 directory without executing it."""

from __future__ import annotations

import argparse
import json

from ai_gateway_core.agent_plugins import AgentPluginLoadError, load_agent_plugin


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate Agent Plugins 1.0.0 skills-only compatibility."
    )
    parser.add_argument("plugin_dir")
    args = parser.parse_args()

    try:
        package = load_agent_plugin(args.plugin_dir)
    except AgentPluginLoadError as exc:
        print(json.dumps({"valid": False, "code": exc.code}, sort_keys=True))
        return 1

    print(
        json.dumps(
            {
                "valid": True,
                "spec": "1.0.0",
                "client_mode": "skills-only",
                "plugin": package.manifest.name,
                "skills": [skill.name for skill in package.skills],
                "mcp_present": package.mcp_present,
                "mcp_supported": False,
                "diagnostics": [item.to_dict() for item in package.diagnostics],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
