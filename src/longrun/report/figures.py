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

from collections.abc import Sequence
from statistics import median
from typing import Any

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from longrun.analysis.cycle import Cycle, median_cycle_length, ovulation_estimate
from longrun.models import Metric
from longrun.report import frames
from longrun.report.theme import (
    FONT_STACK,
    LIGHT,
    LINE_WIDTH,
    MARKER_SIZE,
    PB_MARKER_SIZE,
    ROLE_ACCENT,
    ROLE_RAW,
    ROLE_SERIES,
)

_PANEL_HEIGHT_PX = 190

EMPTY_PANEL_MESSAGE = "No data yet - run an ingest command first"
"""Shown in place of a panel with nothing to plot; the page script styles it."""


def build_trend_figure(data: frames.ReportData) -> go.Figure:
    """Build the shared-timeline trend view.

    Four stacked panels - weight, VO2max, parkrun 5k and resting heart rate -
    share one x-axis, so points can be read against each other in time
    without being forced onto a common scale. Personal bests are marked.

    Args:
        data: Loaded report frames.

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
    if not parkrun.empty:
        ticks, labels = _duration_ticks(parkrun["value"])
        figure.update_yaxes(tickvals=ticks, ticktext=labels, row=3, col=1)

    _apply_base_layout(figure)
    return figure


def build_wellness_figure(data: frames.ReportData) -> go.Figure:
    """Build the wellness view: HRV rMSSD above resting heart rate.

    Two panels rather than one, because milliseconds and beats per minute
    share no scale and a twinned axis would invite reading a relationship
    off the crossing points.

    Args:
        data: Loaded report frames.

    Returns:
        The wellness figure.
    """
    hrv = frames.rolling_baseline(
        frames.wellness_series(data.wellness, "hrv_rmssd"), frames.HRV_BASELINE_WINDOW
    )
    resting = frames.resting_hr_trend(data.wellness)

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
    _apply_base_layout(figure)
    return figure


def build_cycle_figure(data: frames.ReportData) -> go.Figure:
    """Overlay wellness and effort metrics on the cycle day they fell on.

    Every cycle is drawn in the same recessive grey, with the most recent one
    picked out. Colouring each cycle separately would need a dozen hues nobody
    can tell apart, and would suggest that which cycle a point came from is
    the interesting thing; the spread at a given cycle day is.

    Markers rather than joined lines, because these readings are days apart
    and a line between them would draw values that were never measured. The
    menstrual span is shaded and the median ovulation estimate marked, both
    softly: those boundaries are estimates, not events.

    Args:
        data: Loaded report frames, including derived cycles.

    Returns:
        The cycle figure.
    """
    panels = (
        ("HRV rMSSD (ms)", data.wellness, "date", "hrv_rmssd"),
        ("Resting heart rate (bpm)", data.wellness, "date", "resting_hr"),
        ("Session RPE", data.activities, "start_time", "rpe"),
    )
    figure = make_subplots(
        rows=len(panels),
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.08,
        subplot_titles=tuple(title for title, *_ in panels),
    )

    median_period = _median_period_days(data.cycles)
    median_ovulation = _median_ovulation_day(data.cycles)
    latest_start = data.cycles[-1].start if data.cycles else None

    for row, (_, frame, date_column, value_column) in enumerate(panels, start=1):
        placed = frames.by_cycle_day(frame, date_column, value_column, data.cycles)
        if placed.empty:
            _annotate_empty(figure, row=row)
            continue
        # Points first: Plotly drops a shape aimed at a subplot that holds no
        # traces yet, so shading before plotting silently draws nothing. The
        # bands still sit behind the points, which is a layer setting rather
        # than a question of insertion order.
        _add_cycle_scatter(figure, placed, row=row, latest_start=latest_start)
        _shade_cycle_phases(figure, row, median_period, median_ovulation)

    figure.update_layout(
        height=_PANEL_HEIGHT_PX * len(panels) + 60,
        showlegend=True,
        hovermode="closest",
    )
    figure.update_xaxes(title_text="Cycle day", row=len(panels), col=1)
    _place_panel_legend(figure, legend="legend", row=1)
    _apply_base_layout(figure)
    return figure


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


def _shade_cycle_phases(
    figure: go.Figure, row: int, median_period: int | None, median_ovulation: int | None
) -> None:
    """Mark the median menstrual span and ovulation estimate.

    Visible enough to read but deliberately recessive, and labelled only on
    the first panel: these are medians across cycles, so they describe where
    the boundaries usually fall rather than where they fell in any one cycle.
    """
    # Left unlabelled on purpose. In-plot labels collided with the subplot
    # titles and with each other, and the caption above the figure already
    # says what the band and the line are - which is the right place for a
    # caveat this long anyway.
    if median_period:
        figure.add_vrect(
            x0=0.5,
            x1=median_period + 0.5,
            fillcolor=LIGHT.axis,
            opacity=0.28,
            line_width=0,
            layer="below",
            row=row,
            col=1,
        )
    if median_ovulation:
        figure.add_vline(
            x=median_ovulation,
            line_width=1.5,
            line_dash="dash",
            line_color=LIGHT.ink_muted,
            row=row,
            col=1,
        )


def _median_period_days(cycles: Sequence[Cycle]) -> int | None:
    """Return the median span of the logged periods, in days."""
    if not cycles:
        return None
    return int(median(cycle.period_days for cycle in cycles))


def _median_ovulation_day(cycles: Sequence[Cycle]) -> int | None:
    """Return the median estimated ovulation day across completed cycles."""
    median_length = median_cycle_length(cycles)
    days: list[int] = []
    for cycle in cycles:
        if not cycle.is_complete:
            continue
        estimate = ovulation_estimate(cycle, median_length)
        if estimate is not None:
            days.append((estimate - cycle.start).days + 1)
    return int(median(days)) if days else None


def build_load_context_figure(data: frames.ReportData) -> go.Figure:
    """Build the cross-training view: non-running sessions per week.

    Counted in sessions and in the load figure Intervals.icu computes, not in
    distance: distance means nothing across a swim, a gym session and an hour
    of windsurfing, and summing it with running mileage is what this panel
    exists to stop.

    Args:
        data: Loaded report frames.

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
    _apply_base_layout(figure)
    return figure


def build_weekly_volume_figure(data: frames.ReportData) -> go.Figure:
    """Build the weekly running volume bar chart.

    Args:
        data: Loaded report frames.

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
        line={"color": LIGHT.raw, "width": 1},
        opacity=0.55,
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
        line={"color": LIGHT.series, "width": LINE_WIDTH},
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
        line={"color": LIGHT.series, "width": LINE_WIDTH},
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
            "line": {"width": 2, "color": LIGHT.surface},
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
        line={"color": LIGHT.raw, "width": 1},
        opacity=0.55,
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
