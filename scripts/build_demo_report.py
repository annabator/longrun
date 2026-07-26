"""Build a demo dashboard from the synthetic sample dataset.

This is how the example in the README is produced, and a way to see what the
tool does without connecting a Strava account or holding any real data:

    uv run python scripts/build_demo_report.py

Output goes to `data/demo/`, which is gitignored along with the rest of
`data/`. The input is entirely fabricated - see
`scripts/generate_sample_data.py`.
"""

import json
from pathlib import Path

from longrun.ingest.manual import load_tests_csv, load_wellness_csv
from longrun.ingest.strava.mapper import activity_from_payload, stream_points_from_payload
from longrun.report import build_report, load_report_data
from longrun.store import Store

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_DIR = ROOT / "sample_data"
OUTPUT_DIR = ROOT / "data" / "demo"


def main() -> None:
    """Seed a throwaway store from the sample data and render the dashboard."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    database = OUTPUT_DIR / "demo.db"
    database.unlink(missing_ok=True)

    payload = json.loads((SAMPLE_DIR / "strava_activities.json").read_text(encoding="utf-8"))

    with Store.open(database) as store:
        store.upsert_wellness(load_wellness_csv(SAMPLE_DIR / "wellness_sample.csv"))
        store.upsert_tests(load_tests_csv(SAMPLE_DIR / "tests_sample.csv"))
        store.upsert_activities([activity_from_payload(item) for item in payload["activities"]])
        for strava_id, streams in payload["streams"].items():
            store.upsert_streams(int(strava_id), stream_points_from_payload(streams))

        report = build_report(
            load_report_data(store), OUTPUT_DIR / "report.html", title="longrun demo"
        )

    size_mb = report.stat().st_size / 1_000_000
    print(f"Wrote {report} ({size_mb:.1f} MB, self-contained)")  # noqa: T201 - developer script


if __name__ == "__main__":
    main()
