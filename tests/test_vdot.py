"""VDOT and pace derivation, checked against published reference values.

The point of these tests is that the formulas reproduce the well-known
figures without any table being copied. The 5k anchors are the ones that
matter most, since a parkrun is what this project actually anchors on.
"""

import pytest

from longrun.analysis.vdot import (
    RaceResult,
    Zone,
    equivalent_results,
    oxygen_cost,
    pace_zones,
    riegel_predict,
    sustainable_fraction,
    vdot_from_result,
    velocity_for_vdot,
)


@pytest.mark.parametrize(
    ("distance_m", "duration_s", "expected_vdot"),
    [
        (5000, 20 * 60, 49.8),
        (5000, 25 * 60, 38.3),
        (5000, 30 * 60, 31.0),
        (42195, 3 * 3600, 53.5),
        (1500, 5 * 60, 54.5),
    ],
)
def test_vdot_matches_published_values(
    distance_m: float, duration_s: float, expected_vdot: float
) -> None:
    """Computed from the formulas, these land on the familiar numbers."""
    vdot = vdot_from_result(RaceResult(distance_m, duration_s))

    assert vdot == pytest.approx(expected_vdot, abs=0.5)


def test_a_faster_result_gives_a_higher_vdot() -> None:
    quick = vdot_from_result(RaceResult(5000, 19 * 60))
    slow = vdot_from_result(RaceResult(5000, 23 * 60))

    assert quick > slow


def test_a_result_needs_a_positive_distance_and_time() -> None:
    with pytest.raises(ValueError, match="positive"):
        vdot_from_result(RaceResult(0, 1200))
    with pytest.raises(ValueError, match="positive"):
        vdot_from_result(RaceResult(5000, 0))


def test_oxygen_cost_rises_with_velocity() -> None:
    assert oxygen_cost(200) < oxygen_cost(300) < oxygen_cost(400)


def test_sustainable_fraction_falls_with_duration() -> None:
    assert sustainable_fraction(6) > sustainable_fraction(30) > sustainable_fraction(60)


def test_sustainable_fraction_crosses_one_around_eleven_minutes() -> None:
    """VDOT equals VO2max at roughly an eleven-minute effort.

    Shorter than that the curve exceeds 1.0, which is not a bug: a short
    hard effort draws on anaerobic contribution and runs above VO2max.
    """
    assert sustainable_fraction(11) == pytest.approx(1.0, abs=0.01)
    assert sustainable_fraction(6) > 1.0
    assert sustainable_fraction(60) < 1.0


def test_velocity_inverts_the_oxygen_cost_curve() -> None:
    velocity = velocity_for_vdot(50.0, 1.0)

    assert oxygen_cost(velocity) == pytest.approx(50.0, abs=0.05)


def test_pace_zones_are_ordered_easiest_to_hardest() -> None:
    """Easy is the slowest pace and repetition the quickest."""
    zones = {zone.zone: zone.seconds_per_km for zone in pace_zones(50.0)}

    assert (
        zones[Zone.EASY]
        > zones[Zone.MARATHON]
        > zones[Zone.THRESHOLD]
        > zones[Zone.INTERVAL]
        > zones[Zone.REPETITION]
    )


def test_pace_zones_are_plausible_for_a_known_vdot() -> None:
    """VDOT 50 should put threshold near 4:15/km and easy near 5:10/km."""
    zones = {zone.zone: zone.seconds_per_km for zone in pace_zones(50.0)}

    assert zones[Zone.THRESHOLD] == pytest.approx(4 * 60 + 15, abs=15)
    assert zones[Zone.EASY] == pytest.approx(5 * 60 + 10, abs=25)


def test_pace_label_reads_as_minutes_per_kilometre() -> None:
    zone = next(zone for zone in pace_zones(50.0) if zone.zone is Zone.THRESHOLD)

    assert zone.pace_label.endswith("/km")
    assert ":" in zone.pace_label


def test_riegel_predicts_a_longer_distance_as_slower_per_kilometre() -> None:
    """Doubling the distance costs more than doubling the time."""
    five_k = RaceResult(5000, 20 * 60)

    ten_k = riegel_predict(five_k, 10000)

    assert ten_k > 2 * five_k.duration_s
    assert ten_k == pytest.approx(41.6 * 60, abs=60)


def test_riegel_is_symmetric_back_to_the_original_distance() -> None:
    five_k = RaceResult(5000, 20 * 60)

    ten_k = RaceResult(10000, riegel_predict(five_k, 10000))

    assert riegel_predict(ten_k, 5000) == pytest.approx(five_k.duration_s, abs=1.0)


def test_riegel_rejects_a_zero_distance() -> None:
    with pytest.raises(ValueError, match="positive"):
        riegel_predict(RaceResult(5000, 1200), 0)


def test_equivalent_results_predicts_each_distance_asked_for() -> None:
    predicted = equivalent_results(RaceResult(5000, 20 * 60), [10000, 21097])

    assert [round(result.distance_m) for result in predicted] == [10000, 21097]
    assert predicted[0].duration_s < predicted[1].duration_s
