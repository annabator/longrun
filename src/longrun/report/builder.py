"""Assemble the static HTML dashboard.

The output is a single self-contained file. Plotly's JavaScript is inlined
rather than pulled from a CDN, which keeps the dashboard working offline and,
more to the point, means opening a page full of personal health data makes no
outbound request to anyone.
"""

import datetime as dt
import html
import json
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.offline import get_plotlyjs

from longrun.analysis.cycle import Phase
from longrun.errors import ReportError
from longrun.models import Metric, is_running_sport
from longrun.report import figures, frames
from longrun.report.theme import DARK, FONT_STACK, LIGHT, Palette

ACTIVITY_TABLE_ROWS = 25
"""How many recent activities the summary table lists."""


def build_report(
    data: frames.ReportData,
    output_path: Path,
    *,
    title: str = "longrun",
    since: dt.date | None = None,
    whole_history: bool = False,
) -> Path:
    """Render the dashboard to a self-contained HTML file.

    Args:
        data: Loaded report frames.
        output_path: Where to write the HTML file; parent directories are
            created as needed.
        title: Page title.
        since: Earliest date to show. Every dated panel is pinned to the same
            range from here, so a series with a fortnight of readings and one
            with two years share a timeline instead of each auto-scaling to
            its own. Applied to the axis, not by dropping rows, so a personal
            best or a rolling baseline stays computed over the whole history.
            Defaults to the last :data:`~longrun.report.frames.
            DEFAULT_WINDOW_MONTHS` months.
        whole_history: Show every reading ever stored, however old.

    Returns:
        The path written.

    Raises:
        ReportError: If the file cannot be written.
    """
    window = frames.report_window(data, since, whole_history=whole_history)
    document = _render_document(
        title=title,
        summary=_summary_tiles(data),
        trend_div=_figure_div(figures.build_trend_figure(data, window), "trend"),
        wellness_div=_figure_div(figures.build_wellness_figure(data, window), "wellness"),
        cycle_section=_cycle_section(data),
        volume_div=_figure_div(figures.build_weekly_volume_figure(data, window), "volume"),
        load_context_section=_load_context_section(data, window),
        activity_table=_activity_table(data),
        generated_at=dt.datetime.now(tz=dt.UTC),
    )

    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(document, encoding="utf-8")
    except OSError as exc:
        msg = f"Could not write the report to {output_path}: {exc}"
        raise ReportError(msg) from exc
    return output_path


def _figure_div(figure: go.Figure, div_id: str) -> str:
    """Render a figure as an embeddable div, without a second copy of Plotly."""
    return str(
        figure.to_html(
            full_html=False,
            include_plotlyjs=False,
            div_id=div_id,
            config={"displayModeBar": False, "responsive": True},
        )
    )


def _summary_tiles(data: frames.ReportData) -> str:
    """Render the headline figures as stat tiles."""
    tiles: list[tuple[str, str, str]] = []

    if not data.activities.empty:
        # Running only: a ride's distance is real, but it is not mileage.
        runs = frames.running_activities(data.activities)
        others = frames.other_activities(data.activities)
        total_km = float(runs["distance_m"].sum()) / 1000.0 if not runs.empty else 0.0
        tiles.append(("Runs", f"{len(runs):,}", "logged"))
        tiles.append(("Distance", f"{total_km:,.0f}", "km running"))
        if not others.empty:
            tiles.append(("Cross-training", f"{len(others):,}", "other sessions"))

    parkrun = frames.metric_series(data.tests, Metric.PARKRUN_5K)
    if not parkrun.empty:
        best = float(parkrun["value"].min())
        tiles.append(("parkrun best", frames.format_duration(best), f"over {len(parkrun)} runs"))

    vo2max = frames.metric_series(data.tests, Metric.VO2MAX)
    if not vo2max.empty:
        latest = vo2max.iloc[-1]
        when = latest["date"].strftime("%b %Y")
        tiles.append(("Latest VO₂max", f"{float(latest['value']):.1f}", when))

    if not tiles:
        return ""
    cells = "\n".join(
        f"""      <div class="tile">
        <div class="tile-label">{html.escape(label)}</div>
        <div class="tile-value">{html.escape(value)}</div>
        <div class="tile-note">{html.escape(note)}</div>
      </div>"""
        for label, value, note in tiles
    )
    return f'    <section class="tiles">\n{cells}\n    </section>'


def _load_context_section(data: frames.ReportData, window: tuple[dt.date, dt.date] | None) -> str:
    """Render the cross-training panel, or nothing when there is none."""
    if frames.other_activities(data.activities).empty:
        return ""
    figure = _figure_div(figures.build_load_context_figure(data, window), "load-context")
    return f"""  <section>
    <h2>Cross-training</h2>
    <p class="note">
      Everything that is not running, counted in sessions and in the load
      Intervals.icu computes rather than in distance. A heavy week here
      explains a tired run that the running log on its own does not.
    </p>
    {figure}
  </section>"""


def _cycle_section(data: frames.ReportData) -> str:
    """Render the cycle panel, or nothing at all when it was not asked for.

    Returning an empty string when no cycles were supplied is what keeps this
    data out of a dashboard by default: a report built without asking for it
    contains no trace, not even an empty heading.
    """
    if not data.cycles:
        return ""
    figure = _figure_div(figures.build_cycle_figure(data), "cycle")
    logged = len(data.cycles)
    earliest = data.cycles[0].start
    return f"""  <section class="private">
    <h2>By cycle day <span class="badge">private</span></h2>
    <p class="note">
      Readings placed on the cycle day they fell on, every cycle overlaid.
      Cycle boundaries come from logged period days, and the shaded phases are
      medians across those cycles &mdash; where the boundaries usually fall,
      not where they fell in any one cycle. Ovulation in particular is
      inferred by counting back from the next period rather than observed.
      Points only, since readings days apart say nothing about the days
      between, and no averages or tests of difference are computed over what
      is a handful of cycles.
    </p>
    <p class="note">
      {logged} cycle{"s" if logged != 1 else ""} logged, the earliest starting
      {earliest:%-d %B %Y}. Each panel says how many of them it rests on, which
      is fewer where the measure started later. A reading from before
      {earliest:%-d %B %Y} has no cycle day to be placed on and is left out
      rather than guessed at &mdash; logging an earlier period brings it in.
    </p>
    {figure}
{_phase_key()}
    <p class="note">
      What follows each phase is textbook physiology, not a reading of the
      chart above. Whether any of it shows up in these points is exactly the
      question the panel is for, and the honest answer over a handful of
      cycles is usually that it is too early to say.
    </p>
    <p class="note">
      This section only appears because it was asked for with
      <code>--with-cycle</code>. The underlying data is stored separately from
      everything else and is never sent anywhere.
    </p>
  </section>"""


_PHASE_NOTES: tuple[tuple[Phase, str, str], ...] = (
    (
        Phase.MENSTRUAL,
        "Menstrual",
        "Both hormones at their lowest. <strong>Resting heart rate</strong> "
        "and core temperature are usually at their lowest for the cycle and "
        "<strong>HRV</strong> at its highest, so a readiness score can read "
        "green on a day that feels anything but. <strong>Weight</strong> "
        "drops back as the previous phase's fluid goes. Blood lost over a "
        "heavy period is the one thing here with a well-evidenced effect on "
        "<strong>aerobic capacity</strong>, and it works through iron stores "
        "over months rather than day to day.",
    ),
    (
        Phase.FOLLICULAR,
        "Follicular",
        "Oestrogen climbs towards its mid-cycle peak. Resting heart rate and "
        "temperature stay low and <strong>weight</strong> is at its most "
        "stable, which makes this the cleanest stretch for comparing one "
        "reading against another. Often described as the best phase for hard "
        "sessions and <strong>strength</strong> work; the trial evidence for "
        "that is thin and inconsistent, so treat it as a hypothesis to check "
        "against your own points rather than a rule.",
    ),
    (
        Phase.OVULATION,
        "Ovulation",
        "Oestrogen peaks and drops away, and the sustained temperature rise "
        "that follows is what actually marks the boundary. Some studies find "
        "a small peak in maximal <strong>strength</strong> around here; "
        "others find nothing. Nothing about it is visible in the metrics on "
        "this page, which is why the band is inferred rather than observed.",
    ),
    (
        Phase.LUTEAL,
        "Luteal",
        "Progesterone rises and the picture shifts. Core temperature runs a "
        "few tenths higher, <strong>resting heart rate</strong> a little "
        "higher and <strong>HRV</strong> a little lower &mdash; enough to "
        "move a readiness verdict with nothing wrong. <strong>Weight</strong> "
        "can rise a kilo or more on fluid, not tissue. Heat is harder to "
        "shed, so <strong>aerobic</strong> work at a fixed pace costs more "
        "in warm conditions. That matters for the "
        "<strong>VO<sub>2</sub>max</strong> panel above: a watch infers the "
        "number from heart rate at a given pace, so a luteal heart-rate lift "
        "can push the estimate down while aerobic capacity is unchanged.",
    ),
)
"""General physiology, in the order the phases occur.

Deliberately about the phases rather than about the athlete: this text is
fixed, and describing what the chart appears to show would be an
interpretation of a handful of cycles dressed up as a finding.

Each note is anchored to a measure the panel actually plots, so the key
explains what to look at rather than reading as a health leaflet. Where the
evidence is weak - most of the performance claims - it says so, because a
confident sentence here would be the most misleading thing on the page.
"""


def _phase_key() -> str:
    """Render the phase colour key and what each phase is."""
    items = "\n".join(
        f"""      <div class="phase">
        <span class="swatch" style="background: var(--phase-{index})"></span>
        <div><strong>{html.escape(name)}</strong> {note}</div>
      </div>"""
        for index, (_, name, note) in enumerate(_PHASE_NOTES)
    )
    return f'    <div class="phase-key">\n{items}\n    </div>'


def _activity_table(data: frames.ReportData) -> str:
    """Render the most recent activities as an accessible HTML table."""
    if data.activities.empty:
        return '    <p class="empty">No activities stored yet.</p>'

    recent = data.activities.sort_values("start_time", ascending=False).head(ACTIVITY_TABLE_ROWS)
    dates = recent["start_time"].dt.strftime("%Y-%m-%d").tolist()
    names = recent["name"].astype(str).tolist()
    sports = recent["sport_type"].astype(str).tolist()
    distances = recent["distance_m"].astype(float).tolist()
    moving_times = recent["moving_time_s"].astype(int).tolist()
    heart_rates = recent["avg_hr"].tolist()

    rows: list[str] = []
    for date, name, sport, distance_m, moving_s, avg_hr in zip(
        dates, names, sports, distances, moving_times, heart_rates, strict=True
    ):
        distance_km = float(distance_m) / 1000.0
        # Pace is a running idea. Printing one for a ride reads as an absurd
        # running pace rather than as the cycling speed it actually is.
        pace = (
            frames.format_duration(moving_s / distance_km)
            if distance_km > 0 and is_running_sport(sport)
            else "-"
        )
        heart_rate = "-" if pd.isna(avg_hr) else f"{float(avg_hr):.0f}"
        rows.append(
            "        <tr>"
            f"<td>{html.escape(str(date))}</td>"
            f"<td>{html.escape(name)}</td>"
            f"<td>{html.escape(sport)}</td>"
            f'<td class="num">{distance_km:.2f}</td>'
            f'<td class="num">{frames.format_duration(moving_s)}</td>'
            f'<td class="num">{pace}</td>'
            f'<td class="num">{heart_rate}</td>'
            "</tr>"
        )
    body = "\n".join(rows)
    return f"""    <table>
      <caption>The {len(recent)} most recent activities</caption>
      <thead>
        <tr>
          <th scope="col">Date</th>
          <th scope="col">Name</th>
          <th scope="col">Type</th>
          <th scope="col" class="num">Distance (km)</th>
          <th scope="col" class="num">Moving time</th>
          <th scope="col" class="num">Pace (/km)</th>
          <th scope="col" class="num">Avg HR</th>
        </tr>
      </thead>
      <tbody>
{body}
      </tbody>
    </table>"""


def _render_document(
    *,
    title: str,
    summary: str,
    trend_div: str,
    wellness_div: str,
    cycle_section: str,
    volume_div: str,
    load_context_section: str,
    activity_table: str,
    generated_at: dt.datetime,
) -> str:
    """Compose the full HTML document."""
    palettes = json.dumps(
        {
            "light": _palette_tokens(LIGHT),
            "dark": _palette_tokens(DARK),
        }
    )
    stamp = generated_at.strftime("%d %B %Y, %H:%M UTC")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
{_STYLES}
</style>
<!-- Plotly is inlined here, ahead of the figures: each figure div carries its
     own newPlot call, which runs as the parser reaches it. -->
<script>{get_plotlyjs()}</script>
</head>
<body>
<main>
  <header>
    <h1>{html.escape(title)}</h1>
    <p class="subtitle">Training and health trends &middot; generated {html.escape(stamp)}</p>
  </header>
{summary}
  <section>
    <h2>Trends on a shared timeline</h2>
    <p class="note">
      Each measure keeps its own scale in its own panel, sharing one time axis.
      These readings are sparse and irregular, so the panels are shown for
      description only: no correlation, regression or other statistic is
      computed between them, because a coefficient over a handful of monthly
      points would look far more authoritative than it deserves.
    </p>
    {trend_div}
  </section>
  <section>
    <h2>Wellness</h2>
    <p class="note">
      Daily readings in grey behind their trailing baseline. The baseline is
      what a reading is worth judging against: a single low morning means
      little, a run of them below baseline is the signal.
    </p>
    {wellness_div}
  </section>
{cycle_section}
  <section>
    <h2>Weekly running volume</h2>
    <p class="note">
      Running only. Cycling and other sports record a distance too, but it is
      not mileage and is not counted here.
    </p>
    {volume_div}
  </section>
{load_context_section}
  <section>
    <h2>Recent activities</h2>
{activity_table}
  </section>
  <footer>
    <p>Generated by longrun. Every figure is computed from the local store.</p>
  </footer>
</main>
<script>
const LONGRUN_PALETTES = {palettes};
const EMPTY_MESSAGE = {json.dumps(figures.EMPTY_PANEL_MESSAGE)};
{_THEME_SCRIPT}
</script>
</body>
</html>
"""


def _palette_tokens(palette: Palette) -> dict[str, str | list[str]]:
    """Expose a palette to the page script as a plain mapping."""
    return {
        "surface": palette.surface,
        "series": palette.series,
        "phases": list(palette.phases),
        "accent": palette.accent,
        "raw": palette.raw,
        "grid": palette.grid,
        "axis": palette.axis,
        "muted": palette.ink_muted,
        "primary": palette.ink_primary,
        "secondary": palette.ink_secondary,
    }


_STYLES = f""":root {{
  color-scheme: light;
  --surface: {LIGHT.surface};
  --page: {LIGHT.page};
  --primary: {LIGHT.ink_primary};
  --secondary: {LIGHT.ink_secondary};
  --muted: {LIGHT.ink_muted};
  --grid: {LIGHT.grid};
  --border: rgba(11, 11, 11, 0.10);
  --phase-0: {LIGHT.phases[0]};
  --phase-1: {LIGHT.phases[1]};
  --phase-2: {LIGHT.phases[2]};
  --phase-3: {LIGHT.phases[3]};
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    color-scheme: dark;
    --surface: {DARK.surface};
    --page: {DARK.page};
    --primary: {DARK.ink_primary};
    --secondary: {DARK.ink_secondary};
    --muted: {DARK.ink_muted};
    --grid: {DARK.grid};
    --border: rgba(255, 255, 255, 0.10);
    --phase-0: {DARK.phases[0]};
    --phase-1: {DARK.phases[1]};
    --phase-2: {DARK.phases[2]};
    --phase-3: {DARK.phases[3]};
  }}
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  padding: 0 1.5rem 4rem;
  background: var(--page);
  color: var(--primary);
  font-family: {FONT_STACK};
  line-height: 1.5;
}}
main {{ max-width: 1100px; margin: 0 auto; }}
header {{ padding: 2.5rem 0 1.5rem; }}
h1 {{ margin: 0; font-size: 1.75rem; letter-spacing: -0.01em; }}
h2 {{ margin: 0 0 0.5rem; font-size: 1.1rem; font-weight: 600; }}
.subtitle {{ margin: 0.35rem 0 0; color: var(--secondary); font-size: 0.9rem; }}
section {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 1.25rem;
  margin-bottom: 1.25rem;
}}
.note {{
  margin: 0 0 0.75rem;
  color: var(--secondary);
  font-size: 0.85rem;
  max-width: 68ch;
}}
.tiles {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 1.25rem;
}}
.tile-label {{ color: var(--secondary); font-size: 0.8rem; }}
.tile-value {{ font-size: 1.9rem; font-weight: 600; letter-spacing: -0.02em; }}
.tile-note {{ color: var(--muted); font-size: 0.78rem; }}
table {{ width: 100%; border-collapse: collapse; font-size: 0.87rem; }}
caption {{ text-align: left; color: var(--secondary); font-size: 0.85rem; padding-bottom: 0.6rem; }}
th, td {{ text-align: left; padding: 0.45rem 0.6rem; border-bottom: 1px solid var(--grid); }}
th {{ color: var(--secondary); font-weight: 600; }}
.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
.private {{ border-color: var(--muted); }}
.badge {{
  display: inline-block;
  margin-left: 0.5rem;
  padding: 0.1rem 0.45rem;
  border: 1px solid var(--muted);
  border-radius: 999px;
  color: var(--secondary);
  font-size: 0.7rem;
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  vertical-align: middle;
}}
.phase-key {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 0.75rem 1.25rem;
  margin: 0.75rem 0 1rem;
  color: var(--secondary);
  font-size: 0.82rem;
}}
.phase {{ display: flex; gap: 0.55rem; align-items: baseline; }}
.phase strong {{ color: var(--primary); font-weight: 600; }}
.swatch {{
  flex: none;
  width: 0.85rem;
  height: 0.85rem;
  border-radius: 3px;
  border: 1px solid var(--border);
}}
code {{ font-size: 0.85em; }}
.empty {{ color: var(--muted); }}
footer {{ color: var(--muted); font-size: 0.8rem; }}
@media (max-width: 640px) {{
  section {{ padding: 1rem 0.75rem; overflow-x: auto; }}
}}
"""


_THEME_SCRIPT = r"""
function longrunApplyTheme(mode) {
  const tokens = LONGRUN_PALETTES[mode];
  document.querySelectorAll('.js-plotly-plot').forEach(function (gd) {
    if (!gd.data) { return; }
    gd.data.forEach(function (trace, index) {
      const role = (trace.meta && trace.meta.role) || 'series';
      const colour = tokens[role] || tokens.series;
      const update = {};
      if (trace.type === 'bar') {
        update['marker.color'] = colour;
      } else {
        update['line.color'] = colour;
        if (trace.marker) {
          update['marker.color'] = colour;
          if (trace.marker.line) { update['marker.line.color'] = tokens.surface; }
        }
      }
      Plotly.restyle(gd, update, [index]);
    });

    const layoutUpdate = {
      'paper_bgcolor': tokens.surface,
      'plot_bgcolor': tokens.surface,
      'font.color': tokens.secondary
    };
    Object.keys(gd.layout).forEach(function (key) {
      if (/^xaxis/.test(key)) {
        layoutUpdate[key + '.linecolor'] = tokens.axis;
        layoutUpdate[key + '.tickcolor'] = tokens.axis;
        layoutUpdate[key + '.tickfont.color'] = tokens.muted;
      } else if (/^yaxis/.test(key)) {
        layoutUpdate[key + '.gridcolor'] = tokens.grid;
        layoutUpdate[key + '.tickfont.color'] = tokens.muted;
      }
    });
    (gd.layout.shapes || []).forEach(function (shape, index) {
      const match = /^phase:(\d+)$/.exec(shape.name || '');
      if (match) {
        layoutUpdate['shapes[' + index + '].fillcolor'] = tokens.phases[Number(match[1])];
      }
    });
    (gd.layout.annotations || []).forEach(function (annotation, index) {
      const isEmptyNote = annotation.text === EMPTY_MESSAGE;
      layoutUpdate['annotations[' + index + '].font.color'] =
        isEmptyNote ? tokens.muted : tokens.primary;
    });
    Plotly.relayout(gd, layoutUpdate);
  });
}

(function () {
  const query = window.matchMedia('(prefers-color-scheme: dark)');
  const sync = function () { longrunApplyTheme(query.matches ? 'dark' : 'light'); };
  if (query.matches) { sync(); }
  query.addEventListener('change', sync);
})();
"""
