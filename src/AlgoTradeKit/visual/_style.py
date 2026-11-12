"""
algotradekit.visual._style
~~~~~~~~~~~~~~~~~~~~~~~~~~
A chart's colours (v1.3.0): what ``Chart(style=…)`` and
``TraderConfig(chart_style=…)`` take.

    Chart(style={"background": "#101820", "up_color": "#26a69a", "grid": False})

The style belongs to the theme the chart starts in (``theme=``); the other theme
keeps its own colours.  The viewer's Settings panel can change any of them in the
browser, and that choice sits on top of the program's until «Reset colours».

Pure: nothing beyond the standard library, so ``trader`` can check a style without
loading the chart.
"""
from __future__ import annotations

import re

#: The keys a style takes, in the order the messages name them.
STYLE_KEYS = ("background", "up_color", "down_color", "grid")

_COLOUR = re.compile(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})")


def check_style(style, *, where: str = "style") -> dict:
    """The checked copy of *style*, or ``ValueError`` naming the key at fault.

    ``background``, ``up_color`` and ``down_color`` are colours — ``"#rgb"``,
    ``"#rrggbb"`` or ``"#rrggbbaa"``, kept as written — and ``grid`` is
    ``True`` or ``False``.  A key whose value is ``None`` is left out: the
    theme's own value shows.  ``None`` for the whole style is ``{}``.

    *where* names the style in the message: ``"TraderConfig.chart_style"``
    gives «TraderConfig.chart_style['background']: 'black' is not a colour —
    use '#rrggbb'».
    """
    if style is None:
        return {}
    if not isinstance(style, dict):
        raise ValueError(
            f"{where}: {style!r} is not a dict of background, up_color, down_color and grid"
        )
    checked = {}
    for key, value in style.items():
        if key not in STYLE_KEYS:
            raise ValueError(f"{where}: unknown key {key!r} — known: {', '.join(STYLE_KEYS)}")
        if value is None:
            continue
        if key == "grid":
            if not isinstance(value, bool):
                raise ValueError(f"{where}[{key!r}]: {value!r} is not True or False")
        elif not isinstance(value, str) or not _COLOUR.fullmatch(value):
            raise ValueError(f"{where}[{key!r}]: {value!r} is not a colour — use '#rrggbb'")
        checked[key] = value
    return checked
