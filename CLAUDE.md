# CLAUDE.md

Guidance for working in this repository.

## Standing instructions

1. **Never add `Co-Authored-By` or "Generated with" lines to commits.** No AI
   attribution in commit messages, trailers, PR titles or PR bodies. Commits are
   authored solely by the repository owner, using the existing git identity.
2. **Do not chain multiple bash commands in one call.** Run them separately, one
   per call, so that a failure is attributable to the command that caused it.

## What this project is

A training tracker for running that has no finish line. Most tools are built
around a goal race; this one is built for training the owner intends to keep
doing for decades, and answers "what has actually been happening to me?"

It ingests activities from Intervals.icu (pace, heart rate, per-activity
streams, plus feel, RPE and the load figures Intervals computes) and stores
them alongside measurements that never reach an activity file: resting heart
rate, HRV, sleep, estimated VO₂max, weight, parkrun 5k times, blood results.
Some are recorded by a watch but never synced; the rest come from a GP, a set
of scales or a spreadsheet. Everything lives in a local SQLite file.

Intervals.icu is the default source. Strava is kept as a secondary adapter but
now needs a paid subscription for the endpoints involved.

Beat-to-beat R-R intervals are not in the Intervals.icu streams API, so `--rr`
downloads each activity's file and reads them out of it: the original upload
first, the Intervals-generated FIT as a fallback. It is off by default because
nothing consumes the data yet; do not switch the default on until something
does. `ingest fit` still handles files from anywhere else. R-R is currently
averaged to one value per second to fit `activity_streams`; that is fine for
heart-rate recovery between reps, but per-beat analysis (artifact or ectopic
detection) would need its own storage rather than that table.

Weight is never read from Intervals.icu. It repeats the sign-up figure on
every wellness record, so it is a constant wearing a measurement's clothes;
ingesting it would flatten the weight trend and overwrite manual entries,
since wellness merges field by field. Weight arrives by CSV only.

The rolling CTL, ATL and form figures were removed in schema v3. They are
averages over a modelled load, and this project prefers the measured series
they derive from. Per-activity `icu_load` stays on `activities`. Do not
reintroduce the rolling versions: Intervals.icu already shows them.

VO₂max is source-dependent: some watches estimate it, others never do. Never
assume it is present - if it is missing, it is logged manually like any other
measurement.

`parkrun_5k` is kept as its own metric rather than a generic 5k because it is a
fixed, measured course under comparable conditions, which is what makes the
series comparable across years. The long format means other distances are just
more metric names; nothing about the tool is parkrun-only.

## The design principle

**All analysis is deterministic and computed in Python. Any future LLM layer
only ever narrates pre-computed outputs – it never invents numbers or training
plans.**

Treat this as a constraint on the architecture rather than a preference. Every
figure the dashboard renders must trace back to a value computed by ordinary
code from rows in the store, or to a deterministic figure ingested from
upstream (Intervals.icu computes CTL, ATL and per-activity load; those are
stored as given rather than recomputed, and never estimated). The reporting
layer contains no model calls. When the v1.2 narrator lands, it receives a
finished set of numbers and phrases them; it is not handed the database and
asked what it thinks.

A related rule, already implemented: **do not compute or display correlation
coefficients, regressions or other inferential statistics** across the health
metrics. They are sampled monthly at best, and a coefficient over a handful of
irregular points reads as authoritative while being nothing of the sort. Show
the series descriptively and let the reader look.

## Scope

- **v1.0:** ingestion, storage, trend visualisations.
- **v1.1 (current):** Intervals.icu as the default source, daily wellness
  panels, optional cycle tracking, and the plan engine - VDOT anchor,
  readiness, per-run fitness measures, block rotation, weekly menu,
  adaptation. All deterministic, all in `longrun.analysis`.
- **v1.2:** LLM weekly narrative, narrating v1.0/v1.1 outputs only.

### Parked: real API calls for the training plan

**Not started, and not to be started without an explicit decision.** The owner
wants the plan itself generated through proper Claude API calls at some point,
rather than only narrated. Recorded here so a later session finds it.

Read it as a decision that is owed, not a task that is queued, because it
runs straight into the design principle below: today nothing about a session
menu comes from a model. Before any of it is built, settle where the line
sits. Some options, roughly in order of how much they give away:

1. **Narration only** (the current v1.2 plan). The menu is computed; the model
   phrases it. Nothing it says can change a number.
2. **Model proposes, engine validates.** The model may suggest a session, and
   deterministic code checks it against the block, the backlog cap, the
   readiness verdict and the sizing rules, rejecting anything that fails.
   Every figure printed still comes from the engine.
3. **Model plans directly.** Abandons the principle. Would need the README
   rewritten rather than quietly contradicted.

Whatever is chosen, these hold: no training paces, distances or readiness
verdicts invented by a model; every number on screen traceable to code; and
the whole feature working offline, since a plan that needs a network call and
an API key is worse than the one that exists now. Option 2 looks like the one
worth costing first.

Rules the plan engine must keep:

- **Never reproduce a copyrighted table.** VDOT comes from the Daniels and
  Gilbert equations and Riegel's formula, solved numerically. If a value is
  wanted that the formulas do not give, derive it - do not paste a table.
- **Suggest, never silently adjust.** Adaptation proposes a re-test or an
  easier week; it does not move the anchor on its own. Paces that drift
  upward unattended end up unreachable with no explanation.
- **The menu is a priority list, not a timetable.** Nothing is assigned to a
  weekday, nothing is overdue, and readiness attaches notes rather than
  removing sessions.
- **Cap the backlog.** Rollover past the cap drops the oldest sessions.
- **Round a suggested distance to the half-kilometre.** A tenth of a
  kilometre is a precision none of these numbers has.
- **Size sessions from the run distribution, never as a share of weekly
  volume.** A percentage rule collapses at low mileage - 30% of a 10 km week
  is a 3 km "long run". Below the minimum run count, suggest nothing rather
  than inventing a distance.
- **Never require a subjective rating.** Readiness rests on HRV, resting heart
  rate and sleep. RPE is not used at all, and feel is read only where a watch
  supplied one. Do not add a factor that needs the athlete to type something.
- **A logged parkrun always outranks a detected effort**, however fast the
  detected one is. A discrepancy belongs in an adaptation suggestion, never in
  a silent change of anchor.
- **Effort gates must be able to return nothing.** A percentile always admits
  some fraction of runs, so it cannot say "nothing here was hard" - which is
  exactly what a block of easy running needs it to say. Calibrate against the
  athlete's own peak instead, and fail closed when there is too little data.

Running metrics filter on `is_running_sport()`. Cycling reports a distance
too, and summing it into weekly mileage is a bug that has already happened
once. Cross-training is counted in sessions and load, never in distance.

Do not build v1.2 features early. The plan engine must stay rule-based: a
session menu or a readiness verdict is produced by thresholds that can be
tested against fixed inputs, never by a model.

## Cycle tracking

The most sensitive data in the project, and the rules around it are not
negotiable:

- It lives in its own database (`data/private/cycle.db`, `0700`/`0600`), never
  in `longrun.db`. Do not add a cycle table to the main schema.
- Manual entry only. Never add an adapter, an API call, an export command, or
  any import of an HTTP client to `store/private.py` or `analysis/cycle.py`.
  Tests assert this structurally and will fail if it changes.
- Only logged period days are stored. Phase and cycle day are derived at read
  time and must never be written to the database.
- The reporting layer must not open the cycle store. It receives cycles as an
  argument, so `report --with-cycle` is a real gate rather than a convention.
- Phase is always presented as an estimate with a reason. Ovulation is
  inferred by counting back from the next period, so the cycle in progress is
  a projection and must be labelled as one.
- Any fixture for this feature must be obviously fabricated, and no generated
  report may ever be committed.

## Privacy

This repository is private today and intended to become public later, so real
data must never be committed under any circumstance.

- `data/`, all databases, `.env`, token files and exports are gitignored, and a
  local pre-commit hook hard-fails if one is staged.
- Only `.env.example`, holding placeholders, is committed.
- Tests and documentation run on `sample_data/`, which is entirely synthetic and
  produced by `scripts/generate_sample_data.py` from a fixed seed. If a test or
  an example needs data, extend the generator rather than reaching for anything
  real.
- The generated dashboard must stay self-contained: no CDN, no outbound request
  from a page of personal health metrics. There is a test asserting this.
- **Never reject a reading automatically.** A bad one is excluded by hand with
  a stated reason, recorded in `wellness_exclusions` and suppressed on read
  rather than deleted - the next sync would fetch a deleted row back. An
  outlier rule would eventually discard the genuinely unusual morning, which
  is the reading worth keeping.
- **Colours are validated, not chosen by eye.** Any change to the data hues in
  `report/theme.py` must be re-checked for colour-vision separation against
  both surfaces, for the pairs that actually share a panel. The module
  docstring records which pairs those are and where the current set sits.

## Layout

```
src/longrun/
  cli.py            typer command tree; commands stay thin
  config.py         pydantic-settings; credentials are SecretStr
  models.py         the only shapes crossing module boundaries
  errors.py         LongrunError hierarchy; the CLI prints these plainly
  store/
    schema.py       DDL at the current SCHEMA_VERSION
    exclusions.py   readings suppressed by hand, with the reason
    migrations.py   versioned, idempotent, row-preserving
    repository.py   upserts on natural keys
  ingest/
    base.py         the Ingester interface every adapter satisfies
    intervals/      default source: client, mappers, service
    fit/            FIT folder adapter; the only source of R-R
    strava/         secondary; OAuth2 client and mappers
    manual.py       CSV loaders
  report/           frames -> figures -> self-contained HTML
  analysis/         reserved for the v1.1 plan engine
```

Provider payloads must not travel past a mapper. Ingestion adapters translate
external representations into `longrun.models`; the store and reporting layers
never see a provider's field name.

Intervals.icu is inconsistent about naming - activity fields are snake_case,
wellness fields are camelCase, and the docs lag the deployed API. The mapper
therefore reads every field through a list of candidate keys rather than one
spelling. When adding a field, add its plausible aliases too: the failure mode
of guessing wrong is a column that silently fills with nulls.

## Conventions

- Python 3.12, `uv` for environment and dependencies.
- Type hints everywhere; `mypy --strict` must pass over `src`, `tests` and
  `scripts`.
- Docstrings on public functions, Google style, enforced by ruff's pydocstyle.
- `ruff check` and `ruff format` must pass.
- Conventional Commits, atomic and logically scoped. Prefer several small
  commits over one large one.
- Every store write is an upsert on a natural key, so re-ingesting is safe.
  Preserve that property when adding tables.
- Schema changes go through `store/migrations.py` with `SCHEMA_VERSION` bumped.
  A migration must preserve existing rows, be safe to re-run, and be tested
  against a database built from the *previous* version's DDL rather than from
  the current schema.

Before committing:

```bash
uv run ruff check .
uv run ruff format .
uv run mypy
uv run pytest
```
