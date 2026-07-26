"""Generate the synthetic sample dataset committed under `sample_data/`.

Every value here is fabricated by a seeded pseudo-random generator. No real
athlete data is involved, which is what makes it safe to commit to a
repository intended to become public, and what lets the test suite and the
README example run without anyone's history.

The seed is fixed, so regenerating produces byte-identical files and a diff
only appears when this script changes:

    uv run python scripts/generate_sample_data.py
"""

import datetime as dt
import json
import random
from pathlib import Path
from typing import Any

SEED = 20260101
START = dt.date(2025, 1, 1)
END = dt.date(2026, 6, 30)

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "sample_data"

# A fictional athlete: gently improving fitness across eighteen months, with
# enough noise that no trend is a clean line.
WEIGHT_START_KG, WEIGHT_END_KG = 63.4, 60.6
RESTING_HR_START, RESTING_HR_END = 54.0, 47.0
HRV_START, HRV_END = 45.0, 62.0
VO2MAX_START, VO2MAX_END = 46.0, 53.5
PARKRUN_START_S, PARKRUN_END_S = 24 * 60 + 40, 21 * 60 + 15
BODY_FAT_START_PCT, BODY_FAT_END_PCT = 26.5, 22.4

STREAM_SAMPLE_INTERVAL_S = 5
ACTIVITIES_WITH_STREAMS = 3

# Time constants for the fitness and fatigue series, matching the convention
# training trackers use.
CTL_TIME_CONSTANT_DAYS = 42.0
ATL_TIME_CONSTANT_DAYS = 7.0
THRESHOLD_HR = 168.0


def main() -> None:
    """Write the three sample files, overwriting any previous version."""
    rng = random.Random(SEED)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    wellness = _build_wellness(rng)
    tests = _build_tests(rng)
    activities = _build_activities(rng)
    streams = _build_streams(rng, activities[-ACTIVITIES_WITH_STREAMS:])
    _add_fitness_series(wellness, activities)

    _write_csv(
        OUTPUT_DIR / "wellness_sample.csv",
        (
            "date",
            "resting_hr",
            "avg_sleeping_hr",
            "hrv_rmssd",
            "sleep_score",
            "sleep_hours",
            "spo2",
            "weight_kg",
            "ctl",
            "atl",
            "form",
            "source",
            "notes",
        ),
        wellness,
    )
    _write_csv(
        OUTPUT_DIR / "tests_sample.csv",
        ("date", "metric", "value", "unit", "source", "notes"),
        tests,
    )
    (OUTPUT_DIR / "strava_activities.json").write_text(
        json.dumps({"activities": activities, "streams": streams}, indent=2) + "\n",
        encoding="utf-8",
    )

    print(  # noqa: T201 - this is a developer script, not application output
        f"Wrote {len(wellness)} wellness rows, {len(tests)} test rows, "
        f"{len(activities)} activities and streams for {len(streams)} of them "
        f"to {OUTPUT_DIR}"
    )


def _progress(day: dt.date) -> float:
    """Return how far `day` sits through the sample period, from 0.0 to 1.0."""
    return (day - START).days / (END - START).days


def _drift(day: dt.date, start: float, end: float) -> float:
    """Interpolate between a starting and ending value across the period."""
    return start + (end - start) * _progress(day)


def _build_wellness(rng: random.Random) -> list[dict[str, Any]]:
    """Daily resting HR, HRV and sleep, with weight recorded roughly monthly."""
    rows: list[dict[str, Any]] = []
    day = START
    while day <= END:
        # Weight gets recorded on the first Monday of each month only, which
        # is what makes the trend view sparse.
        weight = ""
        if day.weekday() == 0 and day.day <= 7:
            weight = f"{_drift(day, WEIGHT_START_KG, WEIGHT_END_KG) + rng.gauss(0, 0.35):.1f}"
        resting_hr = _drift(day, RESTING_HR_START, RESTING_HR_END) + rng.gauss(0, 2.0)
        rows.append(
            {
                "date": day.isoformat(),
                "resting_hr": f"{resting_hr:.0f}",
                # Sleeping heart rate sits a few beats above the morning
                # resting reading, as it does on a real watch.
                "avg_sleeping_hr": f"{resting_hr + rng.uniform(2.0, 6.0):.0f}",
                "hrv_rmssd": f"{_drift(day, HRV_START, HRV_END) + rng.gauss(0, 5.0):.0f}",
                "sleep_score": f"{rng.uniform(58, 96):.0f}",
                "sleep_hours": f"{rng.uniform(6.1, 8.6):.1f}",
                "spo2": f"{rng.uniform(94.0, 99.0):.0f}",
                "weight_kg": weight,
                "source": "synthetic",
                "notes": "",
            }
        )
        day += dt.timedelta(days=1)
    return rows


def _add_fitness_series(wellness: list[dict[str, Any]], activities: list[dict[str, Any]]) -> None:
    """Fill CTL, ATL and form on the wellness rows, in place.

    These are derived from the synthetic activities the same way a training
    tracker derives them: an exponentially weighted mean of daily load over 42
    days for fitness and 7 days for fatigue, with form the difference. Deriving
    rather than inventing them keeps the sample internally consistent, so a
    hard training week shows up as fatigue rising above fitness.
    """
    load_by_date: dict[str, float] = {}
    for activity in activities:
        day = str(activity["start_date"])[:10]
        load_by_date[day] = load_by_date.get(day, 0.0) + _session_load(activity)

    ctl = 0.0
    atl = 0.0
    for row in wellness:
        load = load_by_date.get(str(row["date"]), 0.0)
        ctl += (load - ctl) / CTL_TIME_CONSTANT_DAYS
        atl += (load - atl) / ATL_TIME_CONSTANT_DAYS
        row["ctl"] = f"{ctl:.1f}"
        row["atl"] = f"{atl:.1f}"
        row["form"] = f"{ctl - atl:.1f}"


def _session_load(activity: dict[str, Any]) -> float:
    """Return a training load for one synthetic session.

    Duration scaled by the square of intensity, which is the usual shape:
    an hour hard costs far more than an hour easy.
    """
    hours = float(activity["moving_time"]) / 3600.0
    avg_hr = float(activity.get("average_heartrate") or THRESHOLD_HR * 0.85)
    intensity = avg_hr / THRESHOLD_HR
    return hours * intensity**2 * 100.0


def _build_tests(rng: random.Random) -> list[dict[str, Any]]:
    """Monthly VO2max and body composition, fortnightly parkruns, rare bloods."""
    rows: list[dict[str, Any]] = []

    day = START
    while day <= END:
        if day.day == 1:
            rows.append(
                _test_row(
                    day,
                    "vo2max",
                    f"{_drift(day, VO2MAX_START, VO2MAX_END) + rng.gauss(0, 0.5):.1f}",
                    "ml/kg/min",
                )
            )
            rows.append(
                _test_row(
                    day,
                    "body_fat_pct",
                    f"{_drift(day, BODY_FAT_START_PCT, BODY_FAT_END_PCT) + rng.gauss(0, 0.4):.1f}",
                    "%",
                )
            )
        day += dt.timedelta(days=1)

    # parkrun is Saturday morning; this athlete makes roughly every other one.
    day = _next_weekday(START, weekday=5)
    while day <= END:
        if rng.random() < 0.55:
            seconds = _drift(day, PARKRUN_START_S, PARKRUN_END_S) + rng.gauss(0, 22)
            rows.append(
                _test_row(
                    day,
                    "parkrun_5k",
                    _format_duration(seconds),
                    "",  # written as mm:ss, so the loader normalises the unit
                    source="parkrun",
                )
            )
        day += dt.timedelta(days=7)

    # Vitamin D swings with the seasons in the UK: a post-winter trough, a
    # post-summer peak, then a better spring once supplementing. Three readings
    # in eighteen months is what a real blood panel history looks like.
    for month, value, note in ((3, 38, "post-winter"), (10, 76, ""), (16, 62, "supplementing")):
        blood_day = _add_months(START, month)
        rows.append(
            _test_row(blood_day, "vitamin_d", str(value), "nmol/L", source="gp", notes=note)
        )

    rows.sort(key=lambda row: (row["date"], row["metric"]))
    return rows


def _build_activities(rng: random.Random) -> list[dict[str, Any]]:
    """Strava-shaped activity summaries, three to four runs a week."""
    activities: list[dict[str, Any]] = []
    activity_id = 9_000_000_000
    day = START
    while day <= END:
        # Tuesday, Thursday, Saturday and a longer Sunday most weeks.
        if day.weekday() in (1, 3, 5, 6) and rng.random() < 0.82:
            activity_id += 1
            activities.append(_activity(rng, day, activity_id))
        day += dt.timedelta(days=1)
    return activities


def _activity(rng: random.Random, day: dt.date, activity_id: int) -> dict[str, Any]:
    """Build one synthetic Strava activity summary."""
    is_long_run = day.weekday() == 6
    is_session = day.weekday() == 1

    if is_long_run:
        distance_km, name = rng.uniform(14.0, 21.5), "Sunday long run"
    elif is_session:
        distance_km, name = rng.uniform(7.0, 10.5), "Intervals"
    else:
        distance_km, name = rng.uniform(5.0, 9.0), "Easy miles"

    # Pace improves across the period; sessions are quicker, long runs slower.
    base_pace_s_per_km = _drift(day, 320.0, 285.0) + rng.gauss(0, 8)
    pace = base_pace_s_per_km + (25 if is_long_run else -20 if is_session else 0)
    moving_time = int(distance_km * pace)
    distance_m = round(distance_km * 1000, 1)
    avg_hr = round(_drift(day, 156.0, 148.0) + (6 if is_session else 0) + rng.gauss(0, 4), 1)

    start = dt.datetime.combine(day, dt.time(7, 15), tzinfo=dt.UTC) + dt.timedelta(
        minutes=rng.randint(-45, 90)
    )
    return {
        "id": activity_id,
        "name": name,
        "sport_type": "Run",
        "start_date": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "distance": distance_m,
        "moving_time": moving_time,
        "elapsed_time": moving_time + rng.randint(10, 240),
        "average_heartrate": avg_hr,
        "max_heartrate": round(avg_hr + rng.uniform(12, 26), 1),
        "average_speed": round(distance_m / moving_time, 3),
        "total_elevation_gain": round(distance_km * rng.uniform(3.0, 9.0), 1),
        "average_cadence": round(rng.uniform(82.0, 89.5), 1),
    }


def _build_streams(
    rng: random.Random, activities: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Build per-second-ish streams for a handful of activities."""
    streams: dict[str, dict[str, Any]] = {}
    for activity in activities:
        moving_time = int(activity["moving_time"])
        speed = float(activity["average_speed"])
        avg_hr = float(activity["average_heartrate"])

        times: list[int] = []
        heartrate: list[int] = []
        velocity: list[float] = []
        distance: list[float] = []
        altitude: list[float] = []
        cadence: list[int] = []

        travelled = 0.0
        elevation = rng.uniform(8.0, 40.0)
        for offset in range(0, moving_time + 1, STREAM_SAMPLE_INTERVAL_S):
            instant_speed = max(1.5, speed + rng.gauss(0, 0.25))
            travelled += instant_speed * STREAM_SAMPLE_INTERVAL_S
            elevation += rng.gauss(0, 0.4)
            # Heart rate drifts up through the run, as it does in practice.
            drift = 8.0 * (offset / max(moving_time, 1))
            times.append(offset)
            heartrate.append(round(avg_hr + drift + rng.gauss(0, 3)))
            velocity.append(round(instant_speed, 2))
            distance.append(round(travelled, 1))
            altitude.append(round(elevation, 1))
            cadence.append(round(rng.gauss(86, 2)))

        streams[str(activity["id"])] = {
            "time": {"data": times},
            "heartrate": {"data": heartrate},
            "velocity_smooth": {"data": velocity},
            "distance": {"data": distance},
            "altitude": {"data": altitude},
            "cadence": {"data": cadence},
        }
    return streams


def _test_row(
    day: dt.date,
    metric: str,
    value: str,
    unit: str,
    source: str = "synthetic",
    notes: str = "",
) -> dict[str, Any]:
    """Build one row for the long-format tests CSV."""
    return {
        "date": day.isoformat(),
        "metric": metric,
        "value": value,
        "unit": unit,
        "source": source,
        "notes": notes,
    }


def _format_duration(seconds: float) -> str:
    """Render seconds as `mm:ss`, the way a parkrun result is written."""
    total = round(seconds)
    return f"{total // 60}:{total % 60:02d}"


def _next_weekday(day: dt.date, weekday: int) -> dt.date:
    """Return the first date on or after `day` falling on `weekday`."""
    return day + dt.timedelta(days=(weekday - day.weekday()) % 7)


def _add_months(day: dt.date, months: int) -> dt.date:
    """Return `day` shifted by whole months, clamping the day of month."""
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    return dt.date(year, month, min(day.day, 28))


def _write_csv(path: Path, columns: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    """Write rows as CSV with a fixed column order and Unix line endings."""
    lines = [",".join(columns)]
    lines.extend(",".join(str(row.get(column, "")) for column in columns) for row in rows)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
