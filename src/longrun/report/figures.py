"""Build the dashboard's Plotly figures.

Two rules shape everything here.

The trend view is small multiples on one shared timeline, never a single
panel with several y-axes. Weight, VO2max, a 5k time and a resting heart
rate have no common scale, and overlaying them on twinned axes lets the
author slide two lines into whatever relationship they want to imply. Stacked
panels sharing an x-axis let the eye line events up in time while each series
keeps an honest scale of its own.

Nothing here computes a statistic. Figures are drawn from series prepared in
:mod:`longrun.report.frames`.
"""

import datetime as dt
from collections.abc import Sequence
from typing import Any

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from longrun.analysis.cycle import Phase, PhaseSpan, typical_phase_spans
from longrun.models import Metric
from longrun.report import frames
from longrun.report.theme import (
    FONT_STACK,
    LIGHT,
    LINE_WIDTH,
    MARKER_SIZE,
    PB_MARKER_SIZE,
    RAW_LINE_WIDTH,
    ROLE_ACCENT,
    ROLE_RAW,
    ROLE_SERIES,
    SPARSE_LINE_DASH,
    Palette,
)

_PANEL_HEIGHT_PX = 190

EMPTY_PANEL_MESSAGE = "No data yet - run an ingest command first"
"""Shown in place of a panel with nothing to plot; the page script styles it."""


def build_trend_figure(
    data: frames.ReportData, window: tuple[dt.date, dt.date] | None = None
) -> go.Figure:
    """Build the shared-timeline trend view.

    Four stacked panels - weight, VO2max, parkrun 5k and resting heart rate -
    share one x-axis, so points can be read against each other in time
    without being forced onto a common scale. Personal bests are marked.

    Args:
        data: Loaded report frames.
        window: Date range every panel is pinned to, for alignment.

    Returns:
        The trend figure.
    """
    weight = _weight_series(data.wellness)
    vo2max = frames.metric_series(data.tests, Metric.VO2MAX)
    parkrun = frames.mark_personal_bests(frames.metric_series(data.tests, Metric.PARKRUN_5K))
    resting = frames.resting_hr_trend(data.wellness)

    titles = (
        "Weight (kg)",
        "VO<sub>2</sub>max (ml/kg/min)",
        "parkrun 5k (lower is faster)",
        "Resting heart rate (bpm)",
    )
    figure = make_subplots(
        rows=4,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.07,
        subplot_titles=titles,
    )

    _add_sparse_series(figure, weight, row=1, name="Weight", unit="kg")
    _add_sparse_series(figure, vo2max, row=2, name="VO2max", unit="ml/kg/min")
    _add_parkrun_series(figure, parkrun, row=3)
    _add_resting_hr_series(figure, resting, row=4)

    figure.update_layout(
        height=_PANEL_HEIGHT_PX * 4,
        hovermode="x unified",
        showlegend=True,
    )
    # Only the lower two panels hold more than one series. A single shared
    # legend would list their entries together above a panel they do not
    # describe, so each gets its own legend anchored to its own panel.
    _place_panel_legend(figure, legend="legend2", row=3)
    _place_panel_legend(figure, legend="legend3", row=4)
    shown = _within(parkrun, "date", window)
    if not shown.empty:
        ticks, labels = _duration_ticks(shown["value"])
        figure.update_yaxes(tickvals=ticks, ticktext=labels, row=3, col=1)

    _apply_window(figure, window)
    _apply_base_layout(figure)
    return figure


def build_wellness_figure(
    data: frames.ReportData, window: tuple[dt.date, dt.date] | None = None
) -> go.Figure:
    """Build the wellness view: HRV rMSSD above resting heart rate.

    Two panels rather than one, because milliseconds and beats per minute
    share no scale and a twinned axis would invite reading a relationship
    off the crossing points.

    Unlike the trend view, this figure starts where its own data starts. HRV
    and resting heart rate arrive together and only from the day the watch
    began syncing them, so holding them to the shared timeline would open the
    section with months of blank axis. The two panels still share a timeline
    with each other, which is the comparison that matters here.

    Args:
        data: Loaded report frames.
        window: Date range to respect, narrowed to where the readings begin.

    Returns:
        The wellness figure.
    """
    hrv = frames.rolling_baseline(
        frames.wellness_series(data.wellness, "hrv_rmssd"), frames.HRV_BASELINE_WINDOW
    )
    resting = frames.resting_hr_trend(data.wellness)
    window = _from_first_reading(window, hrv, resting.rename(columns={"resting_hr": "value"}))

    figure = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.12,
        subplot_titles=("HRV rMSSD (ms)", "Resting heart rate (bpm)"),
    )
    _add_daily_with_baseline(
        figure,
        hrv,
        row=1,
        value_column="value",
        unit="ms",
        baseline_label=frames.HRV_BASELINE_LABEL,
        legend="legend",
    )
    _add_daily_with_baseline(
        figure,
        resting.rename(columns={"resting_hr": "value"}),
        row=2,
        value_column="value",
        unit="bpm",
        baseline_label=frames.RESTING_HR_WINDOW_LABEL,
        legend="legend2",
    )

    figure.update_layout(height=_PANEL_HEIGHT_PX * 2 + 40, hovermode="x unified", showlegend=True)
    _place_panel_legend(figure, legend="legend", row=1)
    _place_panel_legend(figure, legend="legend2", row=2)
    _apply_window(figure, window)
    _apply_base_layout(figure)
    return figure


def build_cycle_figure(data: frames.ReportData) -> go.Figure:
    """Overlay wellness and effort metrics on the cycle day they fell on.

    Every cycle is drawn in the same recessive grey, with the most recent one
    picked out. Colouring each cycle separately would need a dozen hues nobody
    can tell apart, and would suggest that which cycle a point came from is
    the interesting thing; the spread at a given cycle day is.

    Markers rather than joined lines, because these readings are days apart
    and a line between them would draw values that were never measured.

    Each phase is shaded, in the order the phases occur. The boundaries are
    medians across the logged cycles rather than events, so the bands are
    drawn softly and no single cycle is obliged to match them.

    Every panel says how many cycles it was built from. A reading only lands
    here if a logged cycle covers the day it was taken, and the four measures
    start at different times, so one panel can rest on nine cycles while the
    one below it rests on four. Left unstated, a sparse panel looks like
    missing cycles rather than a metric the watch only began reporting later.

    Args:
        data: Loaded report frames, including derived cycles.

    Returns:
        The cycle figure.
    """
    panels = (
        ("HRV rMSSD (ms)", data.wellness, "date", "hrv_rmssd"),
        ("Resting heart rate (bpm)", data.wellness, "date", "resting_hr"),
        ("Weight (kg)", data.wellness, "date", "weight_kg"),
        (
            "VO<sub>2</sub>max (ml/kg/min)",
            frames.metric_series(data.tests, Metric.VO2MAX),
            "date",
            "value",
        ),
    )
    # Placed first, so each panel's title can carry its own cycle count.
    placements = [
        frames.by_cycle_day(frame, date_column, value_column, data.cycles)
        for _, frame, date_column, value_column in panels
    ]
    figure = make_subplots(
        rows=len(panels),
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.07,
        subplot_titles=tuple(
            _panel_title(title, placed)
            for (title, *_), placed in zip(panels, placements, strict=True)
        ),
    )

    spans = typical_phase_spans(data.cycles)
    latest_start = data.cycles[-1].start if data.cycles else None

    for row, placed in enumerate(placements, start=1):
        if placed.empty:
            _annotate_empty(figure, row=row)
            continue
        # Points first: Plotly drops a shape aimed at a subplot that holds no
        # traces yet, so shading before plotting silently draws nothing. The
        # bands still sit behind the points, which is a layer setting rather
        # than a question of insertion order.
        _add_cycle_scatter(figure, placed, row=row, latest_start=latest_start)
        _shade_cycle_phases(figure, row, spans)

    figure.update_layout(
        height=_PANEL_HEIGHT_PX * len(panels) + 60,
        showlegend=True,
        hovermode="closest",
    )
    figure.update_xaxes(title_text="Cycle day", row=len(panels), col=1)
    _place_panel_legend(figure, legend="legend", row=1)
    # No date window here: this figure's x-axis is cycle day, not the calendar.
    _apply_base_layout(figure)
    return figure


def _panel_title(title: str, placed: pd.DataFrame) -> str:
    """Append the number of cycles a panel actually rests on."""
    if placed.empty:
        return title
    cycles = int(placed["cycle_start"].nunique())
    return f"{title} &nbsp;·&nbsp; {cycles} cycle{'s' if cycles != 1 else ''}"


PHASE_ORDER: tuple[Phase, ...] = (
    Phase.MENSTRUAL,
    Phase.FOLLICULAR,
    Phase.OVULATION,
    Phase.LUTEAL,
)
"""The phases in the order they occur, which fixes their colours.

Colour follows the phase, never its position in whatever spans happened to be
derivable, so a cycle too short to have a follicular band does not repaint the
other three.
"""


def phase_colour(phase: Phase, palette: Palette = LIGHT) -> str:
    """Return the band colour for one phase.

    Args:
        phase: The phase to colour.
        palette: The palette to read from; defaults to the light one.

    Returns:
        A hex colour, falling back to the menstrual shade for an unknown phase.
    """
    index = PHASE_ORDER.index(phase) if phase in PHASE_ORDER else 0
    return palette.phases[index]


def _add_cycle_scatter(
    figure: go.Figure, placed: pd.DataFrame, *, row: int, latest_start: object
) -> None:
    """Draw every cycle's points, with the most recent one picked out."""
    earlier = placed.loc[placed["cycle_start"] != latest_start]
    latest = placed.loc[placed["cycle_start"] == latest_start]

    if not earlier.empty:
        figure.add_scatter(
            x=earlier["cycle_day"],
            y=earlier["value"],
            name="Earlier cycles",
            mode="markers",
            marker={"size": MARKER_SIZE - 1, "color": LIGHT.raw, "symbol": "circle"},
            opacity=0.55,
            meta={"role": ROLE_RAW},
            showlegend=row == 1,
            legend="legend",
            hovertemplate="Day %{x}: %{y:.1f}<extra></extra>",
            row=row,
            col=1,
        )
    if not latest.empty:
        figure.add_scatter(
            x=latest["cycle_day"],
            y=latest["value"],
            name="Most recent cycle",
            mode="markers",
            marker={"size": MARKER_SIZE, "color": LIGHT.series, "symbol": "circle"},
            meta={"role": ROLE_SERIES},
            showlegend=row == 1,
            legend="legend",
            hovertemplate="Day %{x}: %{y:.1f}<extra></extra>",
            row=row,
            col=1,
        )


def _shade_cycle_phases(figure: go.Figure, row: int, spans: Sequence[PhaseSpan]) -> None:
    """Shade each phase behind the points.

    The bands are unlabelled in the plot: in-plot labels collided with the
    subplot titles and with each other, and the key beside the figure has room
    to say what each phase is, which a four-character label does not.

    Each shape is named for its phase so the page's theme script can recolour
    it without depending on the order shapes happen to have been added in.
    """
    for span in spans:
        figure.add_vrect(
            # Half-day margins, so a band covers its whole day rather than
            # stopping at the point that sits on the boundary.
            x0=span.first_day - 0.5,
            x1=span.last_day + 0.5,
            name=f"phase:{PHASE_ORDER.index(span.phase)}",
            fillcolor=phase_colour(span.phase),
            opacity=0.55,
            line_width=0,
            layer="below",
            row=row,
            col=1,
        )


def build_load_context_figure(
    data: frames.ReportData, window: tuple[dt.date, dt.date] | None = None
) -> go.Figure:
    """Build the cross-training view: non-running sessions per week.

    Counted in sessions and in the load figure Intervals.icu computes, not in
    distance: distance means nothing across a swim, a gym session and an hour
    of windsurfing, and summing it with running mileage is what this panel
    exists to stop.

    Args:
        data: Loaded report frames.
        window: Date range every panel is pinned to, for alignment.

    Returns:
        The cross-training figure.
    """
    weekly = frames.load_context_by_week(data.activities)
    figure = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.12,
        subplot_titles=("Sessions per week", "Training load per week"),
    )

    if weekly.empty:
        _annotate_empty(figure, row=1)
        _annotate_empty(figure, row=2)
    else:
        figure.add_bar(
            x=weekly["week_start"],
            y=weekly["sessions"],
            name="Sessions",
            marker={"color": LIGHT.series, "line": {"width": 0}},
            meta={"role": ROLE_SERIES},
            hovertemplate="Week of %{x|%d %b %Y}<br>%{y} sessions<extra></extra>",
            row=1,
            col=1,
        )
        figure.add_bar(
            x=weekly["week_start"],
            y=weekly["load"],
            name="Load",
            marker={"color": LIGHT.series, "line": {"width": 0}},
            meta={"role": ROLE_SERIES},
            hovertemplate="Week of %{x|%d %b %Y}<br>load %{y:.0f}<extra></extra>",
            row=2,
            col=1,
        )

    figure.update_layout(
        height=_PANEL_HEIGHT_PX * 2,
        showlegend=False,
        bargap=0.25,
        hovermode="x unified",
    )
    _apply_window(figure, window)
    _apply_base_layout(figure)
    return figure


def build_weekly_volume_figure(
    data: frames.ReportData, window: tuple[dt.date, dt.date] | None = None
) -> go.Figure:
    """Build the weekly running volume bar chart.

    Args:
        data: Loaded report frames.
        window: Date range every panel is pinned to, for alignment.

    Returns:
        The volume figure.
    """
    weekly = frames.weekly_volume(data.activities)
    figure = go.Figure()
    if weekly.empty:
        _annotate_empty(figure)
    else:
        figure.add_bar(
            x=weekly["week_start"],
            y=weekly["distance_km"],
            name="Weekly distance",
            marker={"color": LIGHT.series, "line": {"width": 0}},
            meta={"role": ROLE_SERIES},
            hovertemplate="Week of %{x|%d %b %Y}<br>%{y:.1f} km<extra></extra>",
        )
    figure.update_layout(
        height=280,
        showlegend=False,
        bargap=0.25,
        yaxis_title="km",
        hovermode="x unified",
    )
    _apply_window(figure, window)
    _apply_base_layout(figure)
    return figure


def _place_panel_legend(figure: go.Figure, *, legend: str, row: int) -> None:
    """Anchor a legend to the top-right of one panel in the trend figure.

    Args:
        figure: The subplot figure being built.
        legend: Layout legend name, such as `legend2`.
        row: The 1-based subplot row the legend describes.
    """
    axis = figure.layout[f"yaxis{row}" if row > 1 else "yaxis"]
    domain_top = float(axis.domain[1])
    figure.update_layout(
        {
            legend: {
                "orientation": "h",
                "xanchor": "right",
                "x": 1.0,
                "yanchor": "bottom",
                "y": domain_top + 0.008,
                "bgcolor": "rgba(0,0,0,0)",
                "borderwidth": 0,
                "font": {"size": 12},
            }
        }
    )


def _add_daily_with_baseline(
    figure: go.Figure,
    series: pd.DataFrame,
    *,
    row: int,
    value_column: str,
    unit: str,
    baseline_label: str,
    legend: str,
) -> None:
    """Add a noisy daily series behind its rolling baseline."""
    if series.empty:
        _annotate_empty(figure, row=row)
        return

    figure.add_scatter(
        x=series["date"],
        y=series[value_column],
        name="Daily reading",
        mode="lines",
        line={"color": LIGHT.raw, "width": RAW_LINE_WIDTH},
        opacity=0.45,
        meta={"role": ROLE_RAW},
        hovertemplate=f"%{{y:.0f}} {unit}<extra></extra>",
        legend=legend,
        row=row,
        col=1,
    )
    if "rolling" in series:
        figure.add_scatter(
            x=series["date"],
            y=series["rolling"],
            name=baseline_label,
            mode="lines",
            line={"color": LIGHT.series, "width": LINE_WIDTH},
            meta={"role": ROLE_SERIES},
            hovertemplate=f"%{{y:.1f}} {unit}<extra></extra>",
            legend=legend,
            row=row,
            col=1,
        )


def _weight_series(wellness: pd.DataFrame) -> pd.DataFrame:
    """Extract the recorded weights as a date/value frame."""
    if wellness.empty or "weight_kg" not in wellness:
        return pd.DataFrame(columns=["date", "value"])
    recorded = wellness.loc[wellness["weight_kg"].notna(), ["date", "weight_kg"]]
    return recorded.rename(columns={"weight_kg": "value"}).reset_index(drop=True)


def _add_sparse_series(
    figure: go.Figure, series: pd.DataFrame, *, row: int, name: str, unit: str
) -> None:
    """Add a sparse measurement series as connected markers."""
    if series.empty:
        _annotate_empty(figure, row=row)
        return
    figure.add_scatter(
        x=series["date"],
        y=series["value"],
        name=name,
        mode="lines+markers",
        line={"color": LIGHT.series, "width": LINE_WIDTH, "dash": SPARSE_LINE_DASH},
        marker={"size": MARKER_SIZE, "color": LIGHT.series},
        meta={"role": ROLE_SERIES},
        showlegend=False,
        hovertemplate=f"%{{y:.1f}} {unit}<extra></extra>",
        row=row,
        col=1,
    )


def _add_parkrun_series(figure: go.Figure, parkrun: pd.DataFrame, *, row: int) -> None:
    """Add parkrun results, with personal bests marked distinctly."""
    if parkrun.empty:
        _annotate_empty(figure, row=row)
        return

    labels = [frames.format_duration(value) for value in parkrun["value"]]
    figure.add_scatter(
        x=parkrun["date"],
        y=parkrun["value"],
        name="parkrun result",
        mode="lines+markers",
        line={"color": LIGHT.series, "width": LINE_WIDTH, "dash": SPARSE_LINE_DASH},
        marker={"size": MARKER_SIZE, "symbol": "circle", "color": LIGHT.series},
        meta={"role": ROLE_SERIES},
        customdata=labels,
        hovertemplate="%{customdata}<extra></extra>",
        legend="legend2",
        row=row,
        col=1,
    )

    bests = parkrun.loc[parkrun["is_pb"]]
    if bests.empty:
        return
    # A distinct symbol, not just a distinct colour, so the marking survives
    # colour-vision differences and greyscale printing.
    figure.add_scatter(
        x=bests["date"],
        y=bests["value"],
        name="Personal best",
        mode="markers",
        marker={
            "size": PB_MARKER_SIZE,
            "symbol": "diamond",
            "color": LIGHT.accent,
            "line": {"width": 1.5, "color": LIGHT.surface},
        },
        meta={"role": ROLE_ACCENT},
        customdata=[frames.format_duration(value) for value in bests["value"]],
        hovertemplate="Personal best: %{customdata}<extra></extra>",
        legend="legend2",
        row=row,
        col=1,
    )


def _add_resting_hr_series(figure: go.Figure, resting: pd.DataFrame, *, row: int) -> None:
    """Add daily resting HR behind its rolling average."""
    if resting.empty:
        _annotate_empty(figure, row=row)
        return

    figure.add_scatter(
        x=resting["date"],
        y=resting["resting_hr"],
        name="Daily reading",
        mode="lines",
        line={"color": LIGHT.raw, "width": RAW_LINE_WIDTH},
        opacity=0.45,
        meta={"role": ROLE_RAW},
        hovertemplate="%{y:.0f} bpm<extra></extra>",
        legend="legend3",
        row=row,
        col=1,
    )
    figure.add_scatter(
        x=resting["date"],
        y=resting["rolling"],
        name=frames.RESTING_HR_WINDOW_LABEL,
        mode="lines",
        line={"color": LIGHT.series, "width": LINE_WIDTH},
        meta={"role": ROLE_SERIES},
        hovertemplate="%{y:.1f} bpm average<extra></extra>",
        legend="legend3",
        row=row,
        col=1,
    )


def _duration_ticks(values: "pd.Series[float]") -> tuple[list[float], list[str]]:
    """Return tick positions and `mm:ss` labels spanning a duration series."""
    low = float(values.min())
    high = float(values.max())
    span = max(high - low, 1.0)
    # Aim for four or five ticks on a round number of seconds.
    for step in (15, 30, 60, 120, 300, 600):
        if span / step <= 5:
            break
    start = int(low // step) * step
    ticks: list[float] = []
    position = float(start)
    while position <= high + step:
        ticks.append(position)
        position += step
    return ticks, [frames.format_duration(tick) for tick in ticks]


def _annotate_empty(figure: go.Figure, row: int | None = None) -> None:
    """State plainly that a panel has no data, rather than drawing nothing."""
    kwargs: dict[str, Any] = {
        "text": EMPTY_PANEL_MESSAGE,
        "showarrow": False,
        "font": {"color": LIGHT.ink_muted, "size": 13},
        "xref": "x domain",
        "yref": "y domain",
        "x": 0.5,
        "y": 0.5,
    }
    if row is not None:
        figure.add_annotation(row=row, col=1, **kwargs)
    else:
        figure.add_annotation(**kwargs)


def _apply_window(figure: go.Figure, window: tuple[dt.date, dt.date] | None) -> None:
    """Pin every date axis in a figure to the same range.

    Set on the axis rather than by dropping rows, so a personal best or a
    rolling baseline computed over the whole history stays correct while the
    view narrows. A series with a week of readings and one with two years
    then sit on the same timeline, which is the point of the shared view.

    Each y-axis is then fitted to what the window actually shows. Left to
    itself, Plotly scales an axis to every point in the trace, including the
    ones now off the left-hand edge - so a personal best set two years ago
    squashes this year's results into half a panel to leave room for a marker
    nobody can see.

    Args:
        figure: The figure to constrain.
        window: `(start, end)` dates, or None to leave auto-ranging alone.
    """
    if window is None:
        return
    start, end = window
    figure.update_xaxes(range=[start.isoformat(), end.isoformat()])
    _fit_y_to_window(figure, window)


_Y_PADDING = 0.10
"""Share of the visible spread left as breathing room above and below."""


def _fit_y_to_window(figure: go.Figure, window: tuple[dt.date, dt.date]) -> None:
    """Scale each y-axis to the points inside the window."""
    first, last = pd.Timestamp(window[0]), pd.Timestamp(window[1])

    visible: dict[str, list[float]] = {}
    for trace in figure.data:
        xs, ys = getattr(trace, "x", None), getattr(trace, "y", None)
        if xs is None or ys is None:
            continue
        values = visible.setdefault(str(trace.yaxis or "y"), [])
        # A bar is read from the baseline, so its axis has to include zero
        # whatever the bars themselves reach.
        if trace.type == "bar":
            values.append(0.0)
        values.extend(
            float(y)
            for x, y in zip(xs, ys, strict=True)
            if y is not None and not pd.isna(y) and first <= pd.Timestamp(x) <= last
        )

    for axis, values in visible.items():
        if not values:
            # Nothing visible on this panel; auto-ranging says more than an
            # invented range would.
            continue
        low, high = min(values), max(values)
        padding = (high - low) * _Y_PADDING or max(abs(high) * 0.02, 0.5)
        key = f"yaxis{axis[1:]}" if len(axis) > 1 else "yaxis"
        figure.update_layout({key: {"range": [low - padding, high + padding]}})


def _from_first_reading(
    window: tuple[dt.date, dt.date] | None, *frames_: pd.DataFrame
) -> tuple[dt.date, dt.date] | None:
    """Move a window's start forward to the first reading in these frames.

    Only ever narrows. A window asked for explicitly still holds, and a
    figure whose series begins later than that simply does not open with
    empty axis.

    Args:
        window: The window to narrow, or None to leave auto-ranging alone.
        *frames_: Frames carrying a `date` column.

    Returns:
        The narrowed window, or None when there was none to narrow.
    """
    if window is None:
        return None
    firsts = [
        pd.Timestamp(frame["date"].min()).date()
        for frame in frames_
        if not frame.empty and "date" in frame
    ]
    if not firsts:
        return window
    start, end = window
    return max(start, min(firsts)), end


def _within(
    frame: pd.DataFrame, date_column: str, window: tuple[dt.date, dt.date] | None
) -> pd.DataFrame:
    """Return the rows falling inside a window, or all of them without one."""
    if window is None or frame.empty:
        return frame
    dates = pd.to_datetime(frame[date_column])
    first, last = pd.Timestamp(window[0]), pd.Timestamp(window[1])
    return frame.loc[(dates >= first) & (dates <= last)]


def _apply_base_layout(figure: go.Figure) -> None:
    """Apply recessive chrome shared by every figure."""
    figure.update_layout(
        template="none",
        font={"family": FONT_STACK, "size": 13, "color": LIGHT.ink_secondary},
        paper_bgcolor=LIGHT.surface,
        plot_bgcolor=LIGHT.surface,
        margin={"l": 64, "r": 24, "t": 48, "b": 40},
        hoverlabel={"font": {"family": FONT_STACK, "size": 12}},
    )
    figure.update_xaxes(
        showgrid=False,
        showline=True,
        linecolor=LIGHT.axis,
        ticks="outside",
        tickcolor=LIGHT.axis,
        tickfont={"color": LIGHT.ink_muted},
    )
    figure.update_yaxes(
        gridcolor=LIGHT.grid,
        zeroline=False,
        showline=False,
        tickfont={"color": LIGHT.ink_muted},
        title_font={"color": LIGHT.ink_muted, "size": 12},
    )
    for annotation in figure.layout.annotations:
        if annotation.text != EMPTY_PANEL_MESSAGE:
            annotation.font.size = 13
            annotation.font.color = LIGHT.ink_primary
            annotation.xanchor = "left"
            annotation.x = 0
