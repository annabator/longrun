# CLAUDE.md

Guidance for working in this repository.

## Standing instructions

1. **Never add `Co-Authored-By` or "Generated with" lines to commits.** No AI
   attribution in commit messages, trailers, PR titles or PR bodies. Commits are
   authored solely by the repository owner, using the existing git identity.
2. **Do not chain multiple bash commands in one call.** Run them separately, one
   per call, so that a failure is attributable to the command that caused it.

## What this project is

A holistic, race-less running and fitness tracker. Most training tools are built
around a goal race; this one is built for training that has no finish line, and
answers "what has actually been happening to me?"

It ingests activities from Strava (pace, heart rate, per-activity streams),
stores them alongside manually logged health metrics — weight, VO₂max, resting
heart rate, parkrun 5k times, blood tests such as ferritin — and produces trend
visualisations. Everything lives in a local SQLite file.

## The design principle

**All analysis is deterministic and computed in Python. Any future LLM layer
only ever narrates pre-computed outputs — it never invents numbers or training
plans.**

Treat this as a constraint on the architecture rather than a preference. Every
figure the dashboard renders must trace back to a value computed by ordinary
code from rows in the store. The reporting layer contains no model calls. When
the v1.2 narrator lands, it receives a finished set of numbers and phrases them;
it is not handed the database and asked what it thinks.

A related rule, already implemented: **do not compute or display correlation
coefficients, regressions or other inferential statistics** across the health
metrics. They are sampled monthly at best, and a coefficient over a handful of
irregular points reads as authoritative while being nothing of the sort. Show
the series descriptively and let the reader look.

## Scope

- **v1.0 (current):** ingestion, storage, trend visualisations. Nothing else.
- **v1.1:** VDOT plan engine — training paces and week structures derived from
  parkrun and race efforts. Deterministic. The `longrun.analysis` package is
  reserved for it and is currently empty by design.
- **v1.2:** LLM weekly narrative, narrating v1.0/v1.1 outputs only.

Do not build v1.1 or v1.2 features into v1.0. Leaving clean room for them is the
goal; filling it early is not.

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
  store/            SQLite schema + upsert repository
  ingest/
    strava/         OAuth2, rate-limit-aware client, mappers
    manual.py       CSV loaders
  report/           frames -> figures -> self-contained HTML
  analysis/         reserved for v1.1
```

Provider payloads must not travel past a mapper. Ingestion adapters translate
external representations into `longrun.models`; the store and reporting layers
never see a Strava field name.

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

Before committing:

```bash
uv run ruff check .
uv run ruff format .
uv run mypy
uv run pytest
```
