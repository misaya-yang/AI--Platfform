"""Generate the published Gateway OpenAPI snapshot from the actual FastAPI app."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    from src.main import create_app

    spec = create_app().openapi()
    output = ROOT / "sdk" / "openapi.json"
    output.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n")
    print(f"Wrote Gateway OpenAPI snapshot to {output} ({len(spec.get('paths', {}))} paths)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
