"""Image watermark overlay service.

Applies the project watermark (src/assets/watermark.png) to generated images.
Uses white-tinted watermark with subtle drop shadow for a professional
embedded look — no background box, blends naturally with the image.
"""

import io
import logging
from pathlib import Path

from PIL import Image, ImageChops, ImageFilter

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


def _tint_white(img: Image.Image) -> Image.Image:
    """Convert watermark to pure white, keeping alpha channel."""
    r, g, b, a = img.split()
    white = Image.new("L", img.size, 255)
    return Image.merge("RGBA", (white, white, white, a))


def _make_shadow(alpha: Image.Image, radius: int = 3, offset: tuple = (2, 2)) -> Image.Image:
    """Create a soft dark drop shadow from an alpha mask."""
    shadow_alpha = alpha.copy()
    # Expand slightly and blur for soft shadow
    shadow_alpha = shadow_alpha.filter(ImageFilter.GaussianBlur(radius))
    # Dark shadow color
    shadow = Image.new("RGBA", alpha.size, (0, 0, 0, 0))
    shadow_color = Image.new("RGBA", alpha.size, (0, 0, 0, 180))
    shadow.paste(shadow_color, mask=shadow_alpha)
    # Offset
    offset_shadow = Image.new("RGBA", alpha.size, (0, 0, 0, 0))
    offset_shadow.paste(shadow, offset)
    return offset_shadow


def apply_watermark(image_bytes: bytes, opacity: float = 0.55, margin: int = 20) -> bytes:
    """Apply watermark to image bytes with professional embedded look.

    - White-tinted watermark (no colored text)
    - Subtle drop shadow for readability on any background
    - No background box — blends directly into image

    Args:
        image_bytes: Raw image file bytes.
        opacity: Watermark opacity 0.0-1.0.
        margin: Pixel margin from bottom-right corner.

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
    wm_scaled = wm.resize((int(wm.width * scale), int(wm.height * scale)), Image.LANCZOS)

    # Tint to white
    wm_white = _tint_white(wm_scaled)

    # Apply opacity
    alpha = wm_white.getchannel("A")
    alpha = alpha.point(lambda p: int(p * opacity))
    wm_white.putalpha(alpha)

    # Position: bottom-right
    x = base.width - wm_white.width - margin
    y = base.height - wm_white.height - margin

    # Build shadow layer
    shadow = _make_shadow(wm_scaled.getchannel("A"), radius=4, offset=(2, 2))
    shadow_alpha = shadow.getchannel("A").point(lambda p: int(p * 0.35))
    shadow.putalpha(shadow_alpha)

    # Composite: shadow first, then white watermark on top
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    overlay.paste(shadow, (x, y), shadow)
    overlay.paste(wm_white, (x, y), wm_white)

    result = Image.alpha_composite(base, overlay)

    buf = io.BytesIO()
    result.convert("RGB").save(buf, format="PNG", quality=95)
    return buf.getvalue()
