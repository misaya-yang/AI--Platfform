"""Layout dispatch table for PptxRenderer.

Each layout is a free function taking
``(prs, slide, ir, s, ctx, index, prims)``.  Adding a new layout is a
matter of dropping it into one of the modules and wiring it into
:data:`LAYOUT_DISPATCH` below.
"""

from __future__ import annotations

from .data import draw_chart, draw_comparison_table
from .divider import draw_blank, draw_dark_card, draw_section_divider
from .emphasis import draw_big_stat, draw_pull_quote_card, draw_quote, draw_stat_callout
from .structural import (
    draw_grid_2x2,
    draw_halfbleed_image,
    draw_icon_row,
    draw_numbered_flow,
    draw_two_col,
)
from .title import draw_title, draw_title_content


LAYOUT_DISPATCH = {
    "title": draw_title,
    "title_content": draw_title_content,
    "two_col": draw_two_col,
    "quote": draw_quote,
    "stat_callout": draw_stat_callout,
    "icon_row": draw_icon_row,
    "grid_2x2": draw_grid_2x2,
    "halfbleed_image": draw_halfbleed_image,
    "chart": draw_chart,
    "blank": draw_blank,
    "section_divider": draw_section_divider,
    "big_stat": draw_big_stat,
    "comparison_table": draw_comparison_table,
    "pull_quote_card": draw_pull_quote_card,
    "dark_card": draw_dark_card,
    "numbered_flow": draw_numbered_flow,
}


__all__ = ["LAYOUT_DISPATCH"]
