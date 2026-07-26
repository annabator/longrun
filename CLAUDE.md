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

Beat-to-beat R-R intervals are not in the Intervals.icu streams API, so
ingestion downloads each activity's file and reads them out of it: the
original upload first, the Intervals-generated FIT as a fallback. `ingest fit`
still handles files from anywhere else. R-R is currently averaged to one value
per second to fit `activity_streams`; that is fine for heart-rate recovery
between reps, but per-beat analysis (artifact or ectopic detection) would need
its own storage rather than that table.

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
- **v1.1 (current):** Intervals.icu as the default source, daily wellness and
  fitness/form panels - done. Still to come: a VDOT anchor from published
  formulas (Daniels O2-cost and Riegel, never copyrighted tables), a readiness
  signal from a rolling ln(rMSSD) baseline, per-run fitness analysis, block
  rotation, and a non-strict weekly session menu with rollover. All
  deterministic, all in `longrun.analysis`.
- **v1.2:** LLM weekly narrative, narrating v1.0/v1.1 outputs only.

Do not build v1.2 features early. The plan engine must stay rule-based: a
session menu or a readiness verdict is produced by thresholds that can be
tested against fixed inputs, never by a model.

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

## Layout

```
src/longrun/
  cli.py            typer command tree; commands stay thin
  config.py         pydantic-settings; credentials are SecretStr
  models.py         the only shapes crossing module boundaries
  errors.py         LongrunError hierarchy; the CLI prints these plainly
  store/
    schema.py       DDL at the current SCHEMA_VERSION
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
