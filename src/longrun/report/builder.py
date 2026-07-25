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

from longrun.errors import ReportError
from longrun.models import Metric
from longrun.report import figures, frames
from longrun.report.theme import DARK, FONT_STACK, LIGHT, Palette

ACTIVITY_TABLE_ROWS = 25
"""How many recent activities the summary table lists."""


def build_report(data: frames.ReportData, output_path: Path, *, title: str = "longrun") -> Path:
    """Render the dashboard to a self-contained HTML file.

    Args:
        data: Loaded report frames.
        output_path: Where to write the HTML file; parent directories are
            created as needed.
        title: Page title.

    Returns:
        The path written.

    Raises:
        ReportError: If the file cannot be written.
    """
    trend = figures.build_trend_figure(data)
    volume = figures.build_weekly_volume_figure(data)

    document = _render_document(
        title=title,
        summary=_summary_tiles(data),
        trend_div=_figure_div(trend, "trend"),
        volume_div=_figure_div(volume, "volume"),
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
        total_km = float(data.activities["distance_m"].sum()) / 1000.0
        tiles.append(("Activities", f"{len(data.activities):,}", "ingested"))
        tiles.append(("Distance", f"{total_km:,.0f}", "km total"))

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
        pace = frames.format_duration(moving_s / distance_km) if distance_km > 0 else "-"
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
    volume_div: str,
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
    <h2>Weekly running volume</h2>
    {volume_div}
  </section>
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


def _palette_tokens(palette: Palette) -> dict[str, str]:
    """Expose a palette to the page script as a plain mapping."""
    return {
        "surface": palette.surface,
        "series": palette.series,
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
.empty {{ color: var(--muted); }}
footer {{ color: var(--muted); font-size: 0.8rem; }}
@media (max-width: 640px) {{
  section {{ padding: 1rem 0.75rem; overflow-x: auto; }}
}}
"""


_THEME_SCRIPT = """
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
