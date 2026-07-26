"""Colour and typography tokens for the dashboard.

Both modes are chosen rather than derived: the dark values are their own
steps against the dark surface, not an inversion of the light ones. Every
colour used to draw data was checked against the light and dark surfaces for
contrast and colour-vision separation before being adopted.

Colour deliberately carries no identity in the trend view. Each panel holds a
single series and is named by its own title, so the panels share one hue and
the reader is never asked to match a colour to a legend.
"""

from dataclasses import dataclass

FONT_STACK = 'system-ui, -apple-system, "Segoe UI", sans-serif'

LINE_WIDTH = 2
MARKER_SIZE = 8
PB_MARKER_SIZE = 11


@dataclass(frozen=True)
class Palette:
    """Colour tokens for one mode.

    Attributes:
        series: The single hue used for plotted data.
        accent: Reserved for personal bests, always paired with a distinct
            marker shape and a label so the colour never carries meaning alone.
        raw: Recessive ink for an unsmoothed series shown behind its average.
    """

    surface: str
    page: str
    ink_primary: str
    ink_secondary: str
    ink_muted: str
    grid: str
    axis: str
    series: str
    series_alt: str
    accent: str
    raw: str


LIGHT = Palette(
    surface="#fcfcfb",
    page="#f9f9f7",
    ink_primary="#0b0b0b",
    ink_secondary="#52514e",
    ink_muted="#898781",
    grid="#e1e0d9",
    axis="#c3c2b7",
    series="#2a78d6",
    series_alt="#eb6834",
    accent="#0ca30c",
    raw="#898781",
)

DARK = Palette(
    surface="#1a1a19",
    page="#0d0d0d",
    ink_primary="#ffffff",
    ink_secondary="#c3c2b7",
    ink_muted="#898781",
    grid="#2c2c2a",
    axis="#383835",
    series="#3987e5",
    series_alt="#d95926",
    accent="#0ca30c",
    raw="#898781",
)

ROLE_SERIES = "series"
ROLE_SERIES_ALT = "series_alt"
ROLE_RAW = "raw"
ROLE_ACCENT = "accent"
