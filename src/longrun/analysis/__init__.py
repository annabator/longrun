"""Deterministic training analysis.

Anything that turns stored records into an interpretation belongs here,
computed in Python and testable against fixed inputs - not in the reporting
layer, which only draws, and not in a model call. When the v1.2 narrator
arrives it will consume what this package returns rather than reasoning about
the data itself.

See README.md > The design principle.
"""

from longrun.analysis.cycle import (
    Confidence,
    Cycle,
    Phase,
    PhaseEstimate,
    PhaseSpan,
    cycle_day_for,
    cycles_from_days,
    median_cycle_length,
    ovulation_estimate,
    phase_for,
    typical_phase_spans,
)

__all__ = [
    "Confidence",
    "Cycle",
    "Phase",
    "PhaseEstimate",
    "PhaseSpan",
    "cycle_day_for",
    "cycles_from_days",
    "median_cycle_length",
    "ovulation_estimate",
    "phase_for",
    "typical_phase_spans",
]
