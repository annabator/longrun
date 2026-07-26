# longrun

A training tracker for running that has no finish line. Most tools are built
around a goal race and answer "am I on track for October?" – longrun is built
for training you intend to keep doing for decades, and answers "what has
actually been happening to me?"

It pulls activities from Intervals.icu (pace, heart rate, per-activity
streams, plus how the session felt and what it cost) and stores them alongside
the measurements that never reach an activity file – resting heart rate, HRV,
sleep, estimated VO₂max, weight, parkrun times, blood results. Some of those
your watch records but never syncs; the rest come from a GP, a set of scales,
or a spreadsheet. Everything lands in one local SQLite file and comes back out
as trend visualisations on a shared timeline.

![The dashboard, built from the synthetic sample dataset](docs/example-dashboard.png)

*Generated from `sample_data/`, which is entirely fabricated. See
[Sample data](#sample-data).*

## The design principle

**All analysis is deterministic and computed in Python. Any future LLM layer
only ever narrates pre-computed outputs – it never invents numbers or training
plans.**

This constrains the architecture: every figure traces back to a value computed
by ordinary code from rows in the store, and the reporting layer makes no model
calls. The v1.2 narrator will receive finished numbers and phrase them, rather
than being handed the database and asked what it thinks.

For the same reason the dashboard computes **no correlation coefficients**, and
says so on the page. These metrics are monthly at best, and an r-value over a
handful of irregular points looks far more authoritative than it is.

## Architecture

```mermaid
flowchart LR
    subgraph Sources
        I[Intervals.icu API<br/>default]
        F1[FIT files<br/>data/raw · R-R]
        S[Strava API<br/>secondary]
        C[Manual CSVs<br/>weight · VO₂max · parkrun · bloods]
    end

    subgraph Ingestion["Ingestion – one Ingester interface"]
        A1[IntervalsIngester]
        A2[FitFolderIngester]
        A3[StravaIngester]
        L[CSV loaders]
    end

    subgraph Store["SQLite store"]
        T1[(activities)]
        T2[(activity_streams)]
        T3[(wellness_daily)]
        T4[(tests · long format)]
    end

    subgraph Reporting["Reporting – deterministic"]
        FR[pandas frames<br/>+ derived series]
        P[Plotly figures]
        H[Self-contained HTML]
    end

    I --> A1
    F1 --> A2
    S --> A3
    C --> L
    A1 & A2 & A3 --> T1 & T2
    A1 --> T3
    L --> T3 & T4
    T1 & T2 & T3 & T4 --> FR --> P --> H

    N["v1.2: LLM narrator<br/>(narrates only)"]:::future
    FR -.->|pre-computed numbers| N
    V["v1.1: VDOT plan engine"]:::future
    FR -.-> V

    classDef future stroke-dasharray: 4 4
```

Provider payloads never travel past a mapper: every adapter satisfies one
`Ingester` interface and translates its source into the same few Pydantic
models, so adding a source means adding an adapter rather than changing the
store or the reports.

## Data model

| Table | Holds |
|---|---|
| `activities` | One row per activity, keyed on `(source, external_id)`; includes `feel`, `rpe`, `icu_load`, `icu_intensity` |
| `activity_streams` | Per-sample series: HR, velocity, distance, altitude, cadence, `rr_ms` |
| `wellness_daily` | One row per date: resting HR, sleeping HR, HRV rMSSD, sleep, SpO₂, weight, CTL, ATL, form |
| `tests` | **Long format** – one row per `(date, metric, source)` |

`tests` being long rather than wide is the load-bearing decision. VO₂max, race
times, blood results, body composition and anything measured in future are all
rows, so tracking something new never needs a migration.

Activities are keyed on `(source, external_id)` rather than one provider's id,
so the same session seen by two sources stays two rows and neither can
overwrite the other.

Every write is an upsert on a natural key, so re-ingesting is safe.
`wellness_daily` merges with `COALESCE`, so a CSV carrying only weight cannot
erase a resting HR that came from a different export.

### What comes from where

| | Intervals.icu | FIT files | Manual CSV |
|---|---|---|---|
| Activities + streams | ✅ | ✅ | – |
| feel / RPE / load | ✅ | – | – |
| Daily wellness, CTL/ATL/form | ✅ | – | ✅ |
| Beat-to-beat R-R | – | ✅ | – |
| Weight, VO₂max, bloods, parkrun | – | – | ✅ |

VO₂max is source-dependent: some watches estimate it and pass it through,
others never produce one. longrun does not assume it exists – if your watch
supplies it, ingest it; if not, log it by CSV like any other measurement.

### Why parkrun rather than any 5k

`parkrun_5k` is kept as its own metric because it is a fixed, measured course
run under roughly comparable conditions, which makes the series comparable
across years. Folding every 5k into one line would mix a hard solo effort, a
treadmill run and a parkrun together and flatten the trend.

Nothing here is parkrun-only. The long format means `5k`, `10k` or anything
else is simply another metric name – add rows and it is tracked. Only the
dashboard's dedicated panel is parkrun-specific.

## Setup

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
git clone git@github.com:annabator/longrun.git
cd longrun
uv sync
uv run pre-commit install
```

### Connecting Intervals.icu

1. Open <https://intervals.icu/settings> and find your **Athlete ID** and
   **API Key**.
2. `cp .env.example .env`, then fill in `INTERVALS_ATHLETE_ID` and
   `INTERVALS_API_KEY`.

There is no OAuth dance: the key is used as an HTTP basic password with the
literal username `API_KEY`.

### Connecting Strava (optional)

Strava now requires a paid subscription for the endpoints longrun uses, which
is why Intervals.icu is the default. If you have one, register an application
at <https://www.strava.com/settings/api>, set the callback domain to
`localhost`, put the client id and secret in `.env`, then run
`uv run longrun auth strava` once and paste back the `code=...` value from the
redirect. Tokens are cached with owner-only permissions and refreshed silently
from then on.

## Usage

```bash
# The default: activities, streams and daily wellness. Incremental and safe
# to repeat.
uv run longrun ingest intervals

# A cautious first run
uv run longrun ingest intervals --limit 10 --no-streams

# Parse FIT exports from data/raw, the only source of beat-to-beat R-R
uv run longrun ingest fit

# Backfill history from spreadsheets
uv run longrun ingest manual --wellness my_wellness.csv
uv run longrun ingest manual --tests my_results.csv

# Build the dashboard
uv run longrun report --output report.html
```

Use `--db path/to.db` to work against a different store.

### CSV formats

`--wellness` – only `date` is required; blank cells mean "not recorded".
Weight usually lives here, since Intervals.icu only carries it if something
syncs it in:

```csv
date,resting_hr,hrv_rmssd,sleep_hours,sleep_score,weight_kg,source,notes
2026-01-06,48,61,7.5,84,61.2,watch,
2026-01-07,47,64,8.1,88,,watch,
```

`hrv` is still accepted as a synonym for `hrv_rmssd`, so spreadsheets written
against v1.0 keep loading unchanged.

`--tests` – long format; `date`, `metric` and `value` are required:

```csv
date,metric,value,unit,source,notes
2026-01-04,parkrun_5k,22:47,,parkrun,windy
2026-01-06,vo2max,52.1,ml/kg/min,watch,
2026-02-11,vitamin_d,62,nmol/L,gp,
```

Two conveniences, both deliberate:

- **Durations may be written as recorded.** `22:47` becomes 1367 seconds with
  the unit set to `s`, which is what lets personal bests compare correctly.
  `1:29:04` works too.
- **Dates must be ISO-8601** (`YYYY-MM-DD`). `03/04/2026` is rejected rather
  than guessed at, because a misread date silently bends a trend line.

Metric **names** are trimmed and lower-cased, so `VO2max` and `vo2max` stay one
series rather than becoming two. Values and units are stored exactly as given.

## Sample data

`sample_data/` holds a fabricated eighteen-month history: daily resting HR,
HRV, sleep and SpO₂, derived CTL/ATL/form, monthly weight and VO₂max,
fortnightly parkruns, occasional blood results, and 259 activities. A seeded
generator produces it, so its provenance is checkable rather than asserted:

```bash
uv run python scripts/generate_sample_data.py   # regenerate (byte-identical)
uv run python scripts/build_demo_report.py      # build the demo dashboard
```

This is what the test suite and the screenshot above run on. No real training
history is in this repository.

## Privacy

- `data/`, all databases, `.env` and token files are gitignored, and a
  pre-commit hook fails if one is ever staged.
- Only `.env.example`, holding placeholders, is committed.
- Credentials are held as `SecretStr`, so a stray log line cannot leak them.
- The dashboard inlines Plotly rather than loading it from a CDN, so opening a
  page of personal health data makes no outbound request. A test asserts this.

## Development

```bash
uv run ruff check .      # lint
uv run ruff format .     # format
uv run mypy              # strict type checking
uv run pytest            # tests
```

Type hints throughout, docstrings on public functions, `mypy --strict` clean.

## Roadmap

- **v1.0.** Ingestion, storage, trend visualisations.
- **v1.1 – in progress.** Intervals.icu as the default source, daily wellness
  and fitness/form panels (done); next, a VDOT anchor from published formulas,
  a readiness signal from ln(rMSSD), and an adaptive weekly session menu. All
  deterministic and computed in Python; the `analysis` package holds it.
- **v1.2 – LLM weekly narrative.** A short written summary of the week, phrasing
  figures already computed by v1.0 and v1.1. It does not see the database, and
  it does not produce numbers or plans.

## Licence

MIT – see [LICENSE](LICENSE).
