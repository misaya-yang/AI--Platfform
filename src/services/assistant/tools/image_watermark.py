"""Image watermark overlay service.

Level 3 adaptive watermarking:
- Analyzes 4 corners to pick the smoothest region (lowest texture variance)
- Detects background brightness at that location
- Auto-selects contrasting color (white on dark, dark on light)
- Adds a subtle outline in the opposite color for cross-background readability
"""

import io
import logging
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

logger = logging.getLogger(__name__)

_WATERMARK_PATH = Path(__file__).resolve().parents[3] / "assets" / "watermark.png"
_watermark_cache: Image.Image | None = None


def _load_watermark() -> Image.Image | None:
    global _watermark_cache
    if _watermark_cache is not None:
        return _watermark_cache
    if not _WATERMARK_PATH.exists():
        logger.warning("[Watermark] watermark.png not found at %s", _WATERMARK_PATH)
        return None
    try:
        _watermark_cache = Image.open(_WATERMARK_PATH).convert("RGBA")
        logger.info("[Watermark] Loaded watermark: %dx%d", _watermark_cache.width, _watermark_cache.height)
        return _watermark_cache
    except Exception as e:
        logger.error("[Watermark] Failed to load: %s", e)
        return None


def _tint(img: Image.Image, rgb: tuple[int, int, int]) -> Image.Image:
    """Fill the RGB channels with a solid color, keep alpha."""
    r, g, b, a = img.split()
    rr = Image.new("L", img.size, rgb[0])
    gg = Image.new("L", img.size, rgb[1])
    bb = Image.new("L", img.size, rgb[2])
    return Image.merge("RGBA", (rr, gg, bb, a))


def _analyze_region(base: Image.Image, box: tuple[int, int, int, int]) -> tuple[float, float]:
    """Analyze a region: return (mean_brightness, variance).

    brightness in 0-255, variance is stddev of grayscale values.
    Lower variance = smoother region = better for watermark.
    """
    region = base.crop(box).convert("L")
    arr = np.asarray(region, dtype=np.float32)
    mean = float(arr.mean())
    std = float(arr.std())
    return mean, std


def _pick_best_corner(base: Image.Image, wm_w: int, wm_h: int, margin: int) -> tuple[int, int, float]:
    """Evaluate 4 corners, return (x, y, mean_brightness) of best one.

    Best corner = lowest texture variance (smoothest region).
    """
    W, H = base.width, base.height
    pad = 6  # analyze a slightly larger box than the watermark for more context

    corners = {
        "bl": (margin - pad, H - wm_h - margin - pad, margin + wm_w + pad, H - margin + pad),
        "br": (W - wm_w - margin - pad, H - wm_h - margin - pad, W - margin + pad, H - margin + pad),
        "tl": (margin - pad, margin - pad, margin + wm_w + pad, margin + wm_h + pad),
        "tr": (W - wm_w - margin - pad, margin - pad, W - margin + pad, margin + wm_h + pad),
    }

    results = {}
    for name, box in corners.items():
        # Clamp box to image bounds
        x0 = max(0, box[0])
        y0 = max(0, box[1])
        x1 = min(W, box[2])
        y1 = min(H, box[3])
        if x1 <= x0 or y1 <= y0:
            continue
        mean, std = _analyze_region(base, (x0, y0, x1, y1))
        results[name] = (mean, std)

    if not results:
        # Fallback: bottom-right with default brightness
        return W - wm_w - margin, H - wm_h - margin, 128.0

    # Pick corner with lowest variance (smoothest)
    best_name = min(results, key=lambda k: results[k][1])
    best_mean, best_std = results[best_name]

    # Compute top-left anchor (x, y) for the watermark at this corner
    positions = {
        "bl": (margin, H - wm_h - margin),
        "br": (W - wm_w - margin, H - wm_h - margin),
        "tl": (margin, margin),
        "tr": (W - wm_w - margin, margin),
    }
    x, y = positions[best_name]
    logger.info(
        "[Watermark] Best corner: %s (brightness=%.0f, variance=%.1f)",
        best_name, best_mean, best_std,
    )
    return x, y, best_mean


def apply_watermark(image_bytes: bytes, opacity: float = 0.75, margin: int = 24) -> bytes:
    """Apply adaptive watermark to image bytes.

    Args:
        image_bytes: Raw image file bytes.
        opacity: Watermark fill opacity (0.0-1.0).
        margin: Pixel margin from image edges.

    Returns:
        Watermarked image as PNG bytes.
    """
    wm = _load_watermark()
    if wm is None:
        return image_bytes

    try:
        base = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    except Exception as e:
        logger.warning("[Watermark] Cannot open image: %s", e)
        return image_bytes

    # Scale watermark to ~22% of image width
    target_width = int(base.width * 0.22)
    scale = target_width / wm.width
    wm_w = max(1, int(wm.width * scale))
    wm_h = max(1, int(wm.height * scale))
    wm_scaled = wm.resize((wm_w, wm_h), Image.LANCZOS)

    # Pick the best corner based on smoothness
    x, y, brightness = _pick_best_corner(base, wm_w, wm_h, margin)

    # Choose colors based on background brightness
    # brightness > 140 → dark watermark on light bg
    # brightness ≤ 140 → white watermark on dark bg
    if brightness > 140:
        fill_color = (20, 20, 20)      # near-black fill
        outline_color = (255, 255, 255)  # white outline
    else:
        fill_color = (255, 255, 255)   # white fill
        outline_color = (0, 0, 0)       # black outline

    # Build fill layer (colored version of the watermark shape)
    wm_fill = _tint(wm_scaled, fill_color)
    fill_alpha = wm_fill.getchannel("A").point(lambda p: int(p * opacity))
    wm_fill.putalpha(fill_alpha)

    # Build outline layer: dilate the alpha channel to create a ring
    alpha = wm_scaled.getchannel("A")
    # Grow the alpha mask to create outline
    dilated = alpha.filter(ImageFilter.MaxFilter(5))  # 2px dilation on each side
    # Blur slightly for smooth edges
    dilated = dilated.filter(ImageFilter.GaussianBlur(0.8))
    # Subtract the original alpha to get just the outline ring
    import numpy as np
    dilated_arr = np.asarray(dilated, dtype=np.int16)
    orig_arr = np.asarray(alpha, dtype=np.int16)
    ring_arr = np.clip(dilated_arr - orig_arr // 2, 0, 255).astype(np.uint8)
    ring_alpha = Image.fromarray(ring_arr, mode="L")
    ring_alpha = ring_alpha.point(lambda p: int(p * 0.85))  # outline opacity

    outline_rgb = Image.new("RGBA", wm_scaled.size, (*outline_color, 0))
    outline_rgb.putalpha(ring_alpha)

    # Composite: outline first (wider), then fill on top
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    overlay.paste(outline_rgb, (x, y), outline_rgb)
    overlay.paste(wm_fill, (x, y), wm_fill)

    result = Image.alpha_composite(base, overlay)

    buf = io.BytesIO()
    result.convert("RGB").save(buf, format="PNG", quality=95)
    return buf.getvalue()
