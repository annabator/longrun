"""Find the fastest continuous distance inside an activity.

This is what lets a parkrun be found without knowing it was one. A watch
names everything "Morning Run", and the athlete may start it half a
kilometre before the line and keep going afterwards, so neither the name nor
the activity's total distance and time describe the race. Scanning the
distance stream for the quickest continuous 5000 m does: the jog to the start
simply falls outside the winning window.

It also removes a whole class of error. Matching on total distance would mean
normalising, say, 5150 m down to 5000 m, and there is no way to tell a course
genuinely 150 m long from a watch over-reading a measured one. A split is
exactly 5000 m by construction, so nothing needs correcting and no
measurement artifact can flatter the result.

Two properties of real streams are handled explicitly, because getting either
wrong produces a confident wrong answer:

* **Samples are not evenly spaced.** Suunto's smart recording varies the
  interval, so the scan works on elapsed seconds rather than sample counts.
* **Paused activities compress time.** If a watch stops recording at a red
  light, consecutive samples can span minutes of standing still, and a window
  across them is not a continuous effort. Such streams are rejected.
"""

from dataclasses import dataclass

import pandas as pd

FIVE_K_M = 5000.0

MIN_SAMPLES = 10
"""Below this a stream is too coarse to locate a split within."""

PAUSE_GAP_S = 30.0
"""A gap this long between samples means the recording was paused.

Longer than any smart-recording interval, short enough to catch a stop at a
junction. A stream containing one is not a continuous effort.
"""


@dataclass(frozen=True)
class Split:
    """The fastest continuous stretch of a given distance within an activity.

    Attributes:
        distance_m: The distance covered, exactly the target by construction.
        duration_s: How long it took.
        start_t_s: Elapsed seconds into the activity where it began.
        avg_hr: Mean heart rate across the window, if the stream carried one.
    """

    distance_m: float
    duration_s: float
    start_t_s: float
    avg_hr: float | None

    @property
    def end_t_s(self) -> float:
        """Elapsed seconds into the activity where the split ended."""
        return self.start_t_s + self.duration_s


def best_split(streams: pd.DataFrame, distance_m: float = FIVE_K_M) -> Split | None:
    """Return the quickest continuous `distance_m` in a stream.

    A two-pointer scan over cumulative distance: for each end sample, the
    start pointer advances while the window still covers the distance, and
    the shortest qualifying window wins.

    Args:
        streams: Per-sample rows with `t_s` and cumulative `distance_m`, and
            optionally `hr`.
        distance_m: The distance to look for.

    Returns:
        The fastest split, or None if the stream cannot support one.
    """
    samples = _usable_samples(streams)
    if samples is None:
        return None

    times = samples["t_s"].to_numpy(dtype=float)
    distances = samples["distance_m"].to_numpy(dtype=float)
    if distances[-1] - distances[0] < distance_m:
        return None

    best: Split | None = None
    start = 0
    for end in range(1, len(times)):
        # Advance the start while the window still covers the distance, so
        # that what remains is the tightest window ending at this sample.
        while distances[end] - distances[start + 1] >= distance_m:
            start += 1
        if distances[end] - distances[start] < distance_m:
            continue
        duration = times[end] - times[start]
        if best is None or duration < best.duration_s:
            best = Split(
                distance_m=distance_m,
                duration_s=float(duration),
                start_t_s=float(times[start]),
                avg_hr=_mean_hr(samples, start, end),
            )
    return best


def is_pause_compressed(streams: pd.DataFrame) -> bool:
    """Return whether a stream skips time, making a split meaningless.

    A window spanning a paused recording covers real distance in apparent
    time the athlete did not actually spend running.

    Args:
        streams: Per-sample rows with a `t_s` column.

    Returns:
        True if any gap between consecutive samples is suspiciously long.
    """
    if streams.empty or "t_s" not in streams:
        return False
    times = streams["t_s"].sort_values().to_numpy(dtype=float)
    samples_needed_for_a_gap = 2
    if len(times) < samples_needed_for_a_gap:
        return False
    return bool((times[1:] - times[:-1]).max() > PAUSE_GAP_S)


def _usable_samples(streams: pd.DataFrame) -> pd.DataFrame | None:
    """Return the samples a scan can run over, or None if it cannot."""
    if streams.empty or "t_s" not in streams or "distance_m" not in streams:
        return None

    samples = streams.loc[streams["distance_m"].notna() & streams["t_s"].notna()]
    samples = samples.sort_values("t_s")
    if len(samples) < MIN_SAMPLES or is_pause_compressed(samples):
        return None

    # Cumulative distance must only ever increase. A GPS glitch that walks it
    # backwards would otherwise produce a window covering distance that was
    # never run.
    distances = samples["distance_m"].to_numpy(dtype=float)
    if bool((distances[1:] < distances[:-1]).any()):
        return None
    return samples


def _mean_hr(samples: pd.DataFrame, start: int, end: int) -> float | None:
    """Return mean heart rate across a window, or None if it carries none."""
    if "hr" not in samples:
        return None
    window = samples["hr"].iloc[start : end + 1].dropna()
    if window.empty:
        return None
    return float(window.mean())
