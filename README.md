# longrun

A holistic, race-less endurance-training tracker. Most running tools are built
around a goal race and answer "am I on track for October?" — longrun is built
for training that has no finish line, and answers "what has actually been
happening to me?" It ingests activities from Strava (pace, heart rate, streams),
stores them alongside the things a watch never sees — weight, VO₂max, resting
heart rate, parkrun times, blood tests such as ferritin — and turns the whole
lot into trend visualisations on one shared timeline. Everything lives in a
local SQLite file that never leaves the machine.

![The dashboard, built from the synthetic sample dataset](docs/example-dashboard.png)

*Generated from `sample_data/`, which is entirely fabricated. See
[Sample data](#sample-data).*

## The design principle

**All analysis is deterministic and computed in Python. Any future LLM layer
only ever narrates pre-computed outputs — it never invents numbers or training
plans.**

This is a constraint on the architecture, not a note about good intentions.
Every figure on the dashboard traces back to a value computed by ordinary code
from rows in the store, and the reporting layer contains no model calls at all.
When the v1.2 weekly narrative arrives, it will receive a finished set of
numbers and be asked to phrase them — it will not be handed the database and
asked what it thinks. A tool that quietly hallucinates a VO₂max trend or a
training week is worse than no tool.

The same instinct shows up in a smaller decision: the dashboard computes **no
correlation coefficients**, and says so on the page. These metrics are sampled
monthly at best, and an r-value over a handful of irregular points reads as
authoritative while being nothing of the sort. The panels show the series and
let the reader look.

## Architecture

```mermaid
flowchart LR
    subgraph Sources
        S[Strava API]
        C[Manual CSVs<br/>weight · VO₂max · parkrun · bloods]
    end

    subgraph Ingestion
        A[OAuth2 + token refresh]
        R[Rate-limit-aware client]
        M[Mappers]
        L[CSV loaders]
    end

    subgraph Store["SQLite store"]
        T1[(activities)]
        T2[(activity_streams)]
        T3[(wellness_daily)]
        T4[(tests · long format)]
    end

    subgraph Reporting["Reporting — deterministic"]
        F[pandas frames<br/>+ derived series]
        P[Plotly figures]
        H[Self-contained HTML]
    end

    S --> A --> R --> M --> T1
    M --> T2
    C --> L --> T3
    L --> T4
    T1 & T2 & T3 & T4 --> F --> P --> H

    N["v1.2: LLM narrator<br/>(narrates only)"]:::future
    F -.->|pre-computed numbers| N
    V["v1.1: VDOT plan engine"]:::future
    F -.-> V

    classDef future stroke-dasharray: 4 4
```

Provider payloads never travel past a mapper: ingestion adapters translate
Strava JSON and CSV rows into the same handful of Pydantic models, so adding a
second provider means adding a mapper rather than touching the store or the
reports.

## Data model

| Table | Holds |
|---|---|
| `activities` | One row per activity, keyed on `strava_id` |
| `activity_streams` | Per-sample time series: HR, velocity, distance, altitude, cadence |
| `wellness_daily` | One row per date: resting HR, HRV, sleep, weight |
| `tests` | **Long format** — one row per `(date, metric, source)` |

`tests` being long rather than wide is the load-bearing decision. VO₂max,
parkrun times, ferritin, body composition and anything measured in future are
all rows, so tracking a new metric never needs a migration or a schema change.

Every write is an upsert on a natural key, so re-ingesting is safe.
`wellness_daily` merges with `COALESCE`, meaning a CSV carrying only weight
cannot erase a resting HR that came from a different export.

## Setup

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
git clone git@github.com:annabator/longrun.git
cd longrun
uv sync
uv run pre-commit install
```

### Registering a Strava API application

1. Go to <https://www.strava.com/settings/api> and create an application.
2. Set **Authorization Callback Domain** to `localhost`.
3. Copy the **Client ID** and **Client Secret**.
4. Create your `.env`:

   ```bash
   cp .env.example .env
   ```

   then fill in `STRAVA_CLIENT_ID` and `STRAVA_CLIENT_SECRET`.

5. Authorise once:

   ```bash
   uv run longrun auth strava
   ```

   This prints a URL to approve. Afterwards you are redirected to an address
   containing `code=...`; the page itself may fail to load, which is expected —
   only the address bar matters. Paste the code value back into the prompt.
   Tokens are cached in `data/strava_tokens.json` with owner-only permissions
   and refreshed silently from then on.

## Usage

```bash
# Pull activities and their streams. Resumes from the latest stored activity.
uv run longrun ingest strava

# A cautious first run
uv run longrun ingest strava --limit 10 --no-streams

# Backfill history from spreadsheets
uv run longrun ingest manual --wellness my_wellness.csv
uv run longrun ingest manual --tests my_parkruns.csv

# Build the dashboard
uv run longrun report --output report.html
```

Use `--db path/to.db` to work against a different store.

### CSV formats

`--wellness` — only `date` is required; blank cells mean "not recorded":

```csv
date,resting_hr,hrv,sleep_hours,sleep_score,weight_kg,source,notes
2026-01-06,48,61,7.5,84,61.2,withings,
2026-01-07,47,64,8.1,88,,withings,
```

`--tests` — long format; `date`, `metric` and `value` are required:

```csv
date,metric,value,unit,source,notes
2026-01-04,parkrun_5k,22:47,,parkrun,windy
2026-01-06,vo2max,52.1,ml/kg/min,garmin,
2026-02-11,ferritin,38,ug/L,gp,fasted
```

Two conveniences, both deliberate:

- **Durations may be written as recorded.** `22:47` becomes 1367 seconds with
  the unit normalised to `s`, which is what lets personal bests compare
  correctly. `1:29:04` works too.
- **Dates must be ISO-8601** (`YYYY-MM-DD`). `03/04/2026` is rejected rather
  than guessed at — a misread date silently bends a trend line, which is worse
  than an error naming the file and row.

Metric names are lower-cased and trimmed, so `VO2max` and `vo2max` stay one
series.

## Sample data

`sample_data/` holds a fabricated eighteen-month history — daily resting HR and
sleep, monthly weight and VO₂max, fortnightly parkruns, three ferritin
readings, and 253 activities. It is produced by a seeded generator, so the
provenance is checkable rather than asserted:

```bash
uv run python scripts/generate_sample_data.py   # regenerate (byte-identical)
uv run python scripts/build_demo_report.py      # build the demo dashboard
```

This is what the test suite and the screenshot above run on. No real training
history is in this repository.

## Privacy

This repository is intended to become public, so the boundary is enforced
rather than remembered:

- `data/`, all databases, `.env` and token files are gitignored, and a
  pre-commit hook hard-fails if one is ever staged.
- Only `.env.example`, holding placeholders, is committed.
- Credentials are held as `SecretStr`, so a stray log line cannot leak them.
- The generated dashboard inlines Plotly rather than loading it from a CDN:
  opening a page of personal health data makes no outbound request to anyone.
  There is a test asserting this.

## Development

```bash
uv run ruff check .      # lint
uv run ruff format .     # format
uv run mypy              # strict type checking
uv run pytest            # tests
```

Type hints throughout, docstrings on public functions, `mypy --strict` clean.

## Roadmap

- **v1.0 — this release.** Ingestion, storage, trend visualisations.
- **v1.1 — VDOT plan engine.** Derive VDOT from parkrun and race efforts, and
  generate training paces and week structures. Deterministic, computed in
  Python; the `analysis` package is reserved for it.
- **v1.2 — LLM weekly narrative.** A short written summary of the week. It
  receives figures already computed by v1.0 and v1.1 and phrases them. It does
  not see the database, and it does not produce numbers or plans.

## Licence

MIT — see [LICENSE](LICENSE).
