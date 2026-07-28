"""Colour and typography tokens for the dashboard.

Both modes are chosen rather than derived: the dark values are their own
steps against the dark surface, not an inversion of the light ones. Every
colour used to draw data was run through a colour-vision check against the
light and dark surfaces before being adopted, for the pairs that actually
share a panel:

* series against accent, which meet in the parkrun panel;
* series against raw, which meet wherever a daily reading sits behind its
  baseline.

The one pair that lands in the marginal band is series against raw under
tritanopia. It stays, because those two never rely on colour alone: the raw
series is a thin translucent line behind a solid one twice its weight, and
both are named in the panel's own legend.

Colour deliberately carries no identity in the trend view. Each panel holds a
single series and is named by its own title, so the panels share one hue and
the reader is never asked to match a colour to a legend.
"""

from dataclasses import dataclass

FONT_STACK = 'system-ui, -apple-system, "Segoe UI", sans-serif'

LINE_WIDTH = 1.5
"""Weight of a plotted line. Thin, so the shape reads before the ink does."""

RAW_LINE_WIDTH = 1
"""Weight of an unsmoothed daily series shown behind its baseline."""

MARKER_SIZE = 5
"""Small. These charts are about the trend; a point's exact value is a hover
away, and large markers turn a sparse series into a row of beads."""

PB_MARKER_SIZE = 9
"""Personal bests are the exception: they are meant to be found at a glance."""

SPARSE_LINE_DASH = "dot"
"""Sparse series are joined with a dotted line.

Weight and VO2max are measured days or weeks apart, so the line between two
readings is drawn, not observed. Dotting it says so while still carrying the
eye along the trend, which a bare scatter of points does not.
"""


@dataclass(frozen=True)
class Palette:
    """Colour tokens for one mode.

    Attributes:
        series: The single hue used for plotted data.
        accent: Reserved for personal bests, always paired with a distinct
            marker shape and a label so the colour never carries meaning alone.
        raw: Recessive ink for an unsmoothed series shown behind its average.
        phases: Cycle phase bands, in the order the phases occur.
    """

    surface: str
    page: str
    ink_primary: str
    ink_secondary: str
    ink_muted: str
    grid: str
    axis: str
    series: str
    accent: str
    raw: str
    phases: tuple[str, str, str, str]


LIGHT = Palette(
    surface="#fcfcfb",
    page="#f9f9f7",
    ink_primary="#0b0b0b",
    ink_secondary="#52514e",
    ink_muted="#898781",
    grid="#e1e0d9",
    axis="#c3c2b7",
    series="#7c6bd4",
    accent="#e0559b",
    raw="#a39086",
    phases=("#e492b8", "#fbe7f1", "#f0a9cb", "#f4c6da"),
)

DARK = Palette(
    surface="#1a1a19",
    page="#0d0d0d",
    ink_primary="#ffffff",
    ink_secondary="#c3c2b7",
    ink_muted="#898781",
    grid="#2c2c2a",
    axis="#383835",
    series="#8f7fe0",
    accent="#dd5590",
    raw="#a89078",
    phases=("#8c3a60", "#2e1e26", "#6f3450", "#4e2b39"),
)

ROLE_SERIES = "series"
ROLE_RAW = "raw"
ROLE_ACCENT = "accent"
