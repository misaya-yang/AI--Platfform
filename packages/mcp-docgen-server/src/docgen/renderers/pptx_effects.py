"""OOXML effect injection — shadows, gradients, transparency.

python-pptx's high-level API is limited. These helpers drop into the
underlying lxml tree to reach OOXML features the library can't express.

All functions operate on ``python-pptx`` Shape objects (``shape._element``
resolves to the CT_Shape XML element). Safe to call on rectangles,
rounded rectangles, ovals — not on text boxes (text boxes use the
``sp`` namespace but lack ``spPr`` in the form we want; wrap the text
on top of a backing shape instead).
"""

from __future__ import annotations

from lxml import etree

_A = "http://schemas.openxmlformats.org/drawingml/2006/main"


def _qn(tag: str) -> str:
    return f"{{{_A}}}{tag}"


def _find_or_create_spPr(shape):
    sp = shape._element
    sppr = sp.find(f".//{_qn('spPr')}")
    if sppr is None:
        sppr = etree.SubElement(sp, _qn("spPr"))
    return sppr


def _clear_and_create(parent, tag: str):
    """Remove any existing child with ``tag`` and create a fresh one."""
    existing = parent.find(_qn(tag))
    if existing is not None:
        parent.remove(existing)
    return etree.SubElement(parent, _qn(tag))


def add_outer_shadow(
    shape,
    *,
    blur_emu: int = 40_000,
    dist_emu: int = 23_000,
    dir_deg: int = 90,
    alpha_per_mille: int = 18_000,
    color_hex: str = "000000",
) -> None:
    """Inject ``a:effectLst/a:outerShdw``.

    * ``blur_emu`` — Gaussian-like spread (bigger = softer).
    * ``dist_emu`` — offset of shadow from shape.
    * ``dir_deg`` — 0 = right, 90 = down.
    * ``alpha_per_mille`` — 0..100000; 18000 ≈ 18% opacity.
    """
    sppr = _find_or_create_spPr(shape)
    effect = _clear_and_create(sppr, "effectLst")
    outer = etree.SubElement(effect, _qn("outerShdw"))
    outer.set("blurRad", str(blur_emu))
    outer.set("dist", str(dist_emu))
    outer.set("dir", str(dir_deg * 60_000))
    outer.set("rotWithShape", "0")
    srgb = etree.SubElement(outer, _qn("srgbClr"))
    srgb.set("val", color_hex)
    alpha = etree.SubElement(srgb, _qn("alpha"))
    alpha.set("val", str(alpha_per_mille))


def set_linear_gradient_fill(
    shape,
    *,
    stops: list[tuple[float, str]],
    angle_deg: float = 90.0,
) -> None:
    """Replace shape's fill with a linear gradient.

    ``stops`` is a list of ``(position_0_1, hex_rrggbb)`` tuples.
    ``angle_deg`` 0 = left→right, 90 = top→bottom.
    """
    sppr = _find_or_create_spPr(shape)
    # drop any existing fill
    for tag in ("solidFill", "gradFill", "blipFill", "noFill", "pattFill"):
        el = sppr.find(_qn(tag))
        if el is not None:
            sppr.remove(el)
    grad = etree.SubElement(sppr, _qn("gradFill"))
    grad.set("flip", "none")
    grad.set("rotWithShape", "1")
    lst = etree.SubElement(grad, _qn("gsLst"))
    for pos, hex_rrggbb in stops:
        gs = etree.SubElement(lst, _qn("gs"))
        gs.set("pos", str(int(pos * 100_000)))
        srgb = etree.SubElement(gs, _qn("srgbClr"))
        srgb.set("val", hex_rrggbb)
    lin = etree.SubElement(grad, _qn("lin"))
    lin.set("ang", str(int(angle_deg * 60_000)))
    lin.set("scaled", "1")


def set_transparency(shape, *, alpha_per_mille: int) -> None:
    """Apply transparency to an existing solid fill.

    If no solid fill exists, this is a no-op.
    ``alpha_per_mille`` 0 = fully opaque? No — 0 = transparent, 100000 = opaque.
    """
    sppr = _find_or_create_spPr(shape)
    solid = sppr.find(_qn("solidFill"))
    if solid is None:
        return
    for srgb in solid.findall(_qn("srgbClr")):
        existing = srgb.find(_qn("alpha"))
        if existing is not None:
            srgb.remove(existing)
        a = etree.SubElement(srgb, _qn("alpha"))
        a.set("val", str(alpha_per_mille))


def set_no_stroke(shape) -> None:
    """Remove any stroke from the shape."""
    sppr = _find_or_create_spPr(shape)
    ln = sppr.find(_qn("ln"))
    if ln is not None:
        sppr.remove(ln)
    ln = etree.SubElement(sppr, _qn("ln"))
    etree.SubElement(ln, _qn("noFill"))


def set_stroke(shape, *, color_hex: str, width_emu: int = 6350) -> None:
    """Set a 1px ≈ stroke on a shape (6350 EMU ≈ 0.5 pt, 12700 EMU = 1 pt)."""
    sppr = _find_or_create_spPr(shape)
    old = sppr.find(_qn("ln"))
    if old is not None:
        sppr.remove(old)
    ln = etree.SubElement(sppr, _qn("ln"))
    ln.set("w", str(width_emu))
    solid = etree.SubElement(ln, _qn("solidFill"))
    srgb = etree.SubElement(solid, _qn("srgbClr"))
    srgb.set("val", color_hex)


def set_rotation(shape, *, degrees: float) -> None:
    """Rotate the shape (in degrees, clockwise)."""
    shape.rotation = degrees
