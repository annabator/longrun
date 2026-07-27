"""Finding the fastest continuous 5k inside an activity.

The streams here are fabricated at known paces, so a failure points at the
scan rather than at ambiguity in the data.
"""

import pandas as pd
import pytest

from longrun.analysis.splits import best_split, is_pause_compressed


def _stream(segments: list[tuple[float, float]], hr: float = 150.0) -> pd.DataFrame:
    """Build a stream from `(metres, seconds_per_km)` segments.

    Real streams open at distance zero, which matters: an activity covering
    exactly 5000 m can only yield a 5000 m split if it starts from nothing.
    """
    time = distance = 0.0
    rows = [{"t_s": 0.0, "distance_m": 0.0, "hr": hr}]
    for metres, pace in segments:
        covered = 0.0
        while covered < metres - 1e-9:
            step = min(1000.0 / pace, metres - covered)
            time += step / (1000.0 / pace)
            distance += step
            covered += step
            rows.append({"t_s": time, "distance_m": distance, "hr": hr})
    return pd.DataFrame(rows)


def test_a_flat_five_k_is_its_own_split() -> None:
    split = best_split(_stream([(5000, 240.0)]))

    assert split is not None
    assert split.duration_s == pytest.approx(1200.0, abs=1.0)
    assert split.start_t_s == pytest.approx(0.0, abs=1.0)


def test_the_late_start_parkrun_excludes_the_jog_to_the_line() -> None:
    """The case this scan exists for.

    Half a kilometre of jogging then a 21:11 five: taking the whole activity
    would report about 24:11, three minutes slower than was run.
    """
    activity = _stream([(500, 360.0), (5000, 254.2)])

    split = best_split(activity)

    assert split is not None
    assert split.duration_s == pytest.approx(1271.0, abs=3.0)
    # It begins after the jog, not at the start of the recording.
    assert split.start_t_s == pytest.approx(180.0, abs=5.0)
    # And is markedly quicker than the whole-activity figure would suggest.
    assert split.duration_s < float(activity["t_s"].iloc[-1]) - 100


def test_a_fast_five_k_inside_a_long_run_is_found() -> None:
    split = best_split(_stream([(4000, 330.0), (5000, 250.0), (3000, 330.0)]))

    assert split is not None
    assert split.duration_s == pytest.approx(1250.0, abs=5.0)


def test_the_best_five_k_of_a_steady_ten_k() -> None:
    split = best_split(_stream([(10000, 300.0)]))

    assert split is not None
    assert split.duration_s == pytest.approx(1500.0, abs=5.0)


def test_a_run_shorter_than_the_distance_has_no_split() -> None:
    assert best_split(_stream([(4000, 240.0)])) is None


def test_the_split_carries_its_own_heart_rate() -> None:
    """Effort is judged over the split, not over the whole activity."""
    activity = pd.concat(
        [_stream([(2000, 360.0)], hr=130.0), _stream([(5000, 250.0)], hr=178.0)],
        ignore_index=True,
    )
    activity["t_s"] = range(len(activity))
    activity["distance_m"] = activity["distance_m"].cumsum() / 100

    split = best_split(activity)

    assert split is not None
    assert split.avg_hr is not None


def test_a_paused_recording_is_refused() -> None:
    """A window spanning a stop covers distance in time never spent running."""
    paused = pd.DataFrame(
        {
            "t_s": [0, 1, 2, 300, 301, 302, 303, 304, 305, 306, 307],
            "distance_m": [index * 600 for index in range(11)],
            "hr": [150.0] * 11,
        }
    )

    assert is_pause_compressed(paused) is True
    assert best_split(paused) is None


def test_a_continuous_recording_is_not_flagged_as_paused() -> None:
    assert is_pause_compressed(_stream([(5000, 240.0)])) is False


def test_distance_walking_backwards_is_refused() -> None:
    """A GPS glitch must not manufacture distance that was never covered."""
    glitched = pd.DataFrame(
        {
            "t_s": list(range(12)),
            "distance_m": [0, 600, 1200, 900, 2400, 3000, 3600, 4200, 4800, 5400, 6000, 6600],
            "hr": [150.0] * 12,
        }
    )

    assert best_split(glitched) is None


def test_a_stream_with_too_few_samples_is_refused() -> None:
    coarse = pd.DataFrame({"t_s": [0, 600, 1200], "distance_m": [0, 3000, 6000], "hr": [150.0] * 3})

    assert best_split(coarse) is None


def test_an_empty_stream_has_no_split() -> None:
    assert best_split(pd.DataFrame()) is None
    assert is_pause_compressed(pd.DataFrame()) is False


def test_uneven_sampling_is_handled_on_elapsed_time() -> None:
    """Smart recording varies the interval, so counting samples would lie."""
    rows = []
    time = distance = 0.0
    for index in range(400):
        # Alternating 1s and 5s gaps, at a constant 4:00/km.
        step_s = 1.0 if index % 2 else 5.0
        time += step_s
        distance += step_s * (1000.0 / 240.0)
        rows.append({"t_s": time, "distance_m": distance, "hr": 150.0})
    uneven = pd.DataFrame([{"t_s": 0.0, "distance_m": 0.0, "hr": 150.0}, *rows])

    split = best_split(uneven)

    assert split is not None
    assert split.duration_s == pytest.approx(1200.0, abs=10.0)
