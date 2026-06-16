"""End-to-end visual test for StylePreset → Gemini image generation.

Generates one image per non-default style with the SAME subject prompt so
the stylistic differences are easy to eyeball. Writes PNGs to
STYLE_PRESET_OUT_DIR or tmp/image-styles named after each preset.

Usage:
    python scripts/test_style_presets_e2e.py
"""

from __future__ import annotations

import asyncio
import base64
import os
import sys
import time
from pathlib import Path

# Load .env so GOOGLE_API_KEY is picked up
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Ensure repo root is importable
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from src.services.assistant.tools.gemini_image_tool import GeminiImageGenerator
from src.services.assistant.tools.style_presets import (
    StylePreset,
    compose_styled_prompt,
)

OUTPUT_DIR = Path(os.getenv("STYLE_PRESET_OUT_DIR", REPO / "tmp/image-styles"))

# Fixed subject — simple enough to render consistently, rich enough to show
# stylistic differences (textures, lighting, composition, colour palette).
SUBJECT = "a cat sitting in a garden with flowers, looking towards the viewer"

# Skip DEFAULT — we specifically want to see what each preset does.
PRESETS_TO_TEST = [p for p in StylePreset if p is not StylePreset.DEFAULT]


async def generate_one(preset: StylePreset, generator: GeminiImageGenerator) -> dict:
    """Generate one image for one preset and save it to disk."""
    prompt = compose_styled_prompt(SUBJECT, preset)
    start = time.time()
    result = await generator.generate(
        prompt=prompt,
        n=1,
        aspect_ratio="1:1",
    )
    elapsed = time.time() - start

    if not result.success or not result.images:
        return {
            "preset": preset.value,
            "ok": False,
            "error": result.error or "no image returned",
            "elapsed": elapsed,
        }

    img = result.images[0]
    out_path = OUTPUT_DIR / f"{preset.value}.png"
    out_path.write_bytes(base64.b64decode(img["content_base64"]))
    return {
        "preset": preset.value,
        "ok": True,
        "path": str(out_path),
        "size_bytes": img.get("size_bytes"),
        "elapsed": elapsed,
    }


async def main() -> int:
    if not (os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")):
        print("ERROR: GOOGLE_API_KEY / GEMINI_API_KEY not set", file=sys.stderr)
        return 1

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    generator = GeminiImageGenerator()

    print(f"Subject: {SUBJECT}")
    print(f"Output:  {OUTPUT_DIR}")
    print(f"Presets: {len(PRESETS_TO_TEST)}\n")

    # Paid tier → high concurrency is safe.
    results = await asyncio.gather(
        *[generate_one(p, generator) for p in PRESETS_TO_TEST]
    )
    await generator.close()

    for r in sorted(results, key=lambda x: x["preset"]):
        if r["ok"]:
            print(f"  ✓ {r['preset']:12s}  {r['elapsed']:.1f}s  {r['size_bytes']:>8} bytes  → {r['path']}")
        else:
            print(f"  ✗ {r['preset']:12s}  {r['elapsed']:.1f}s  ERROR: {r['error']}")

    failures = [r for r in results if not r["ok"]]
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
