"""Training goals: what the athlete is currently trying to do.

Kept in a small JSON file rather than the database, because it is
configuration a person edits, not a record of something that happened. It
lives under `data/`, so it is gitignored along with everything else there.

The plan engine reads this to bias what it suggests. Nothing here is
prescriptive on its own.
"""

import json
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from longrun.errors import ConfigError


class Emphasis(StrEnum):
    """What the current block of training is mostly for.

    Attributes:
        BASE: Aerobic volume, mostly easy running.
        THRESHOLD: Sustained work around the lactate threshold.
        VO2MAX: Short, hard intervals near maximal aerobic power.
        CONSOLIDATION: A lighter block to absorb the previous ones.
    """

    BASE = "base"
    THRESHOLD = "threshold"
    VO2MAX = "vo2max"
    CONSOLIDATION = "consolidation"


class Goals(BaseModel):
    """The athlete's current intent.

    Attributes:
        emphasis: What this block is for.
        runs_per_week: How many runs a week are wanted.
        quality_sessions: How many of those should be hard.
        long_run: Whether a long easy run is wanted each week.
        constraints: Free text the engine does not parse, kept so that a
            plan can be read alongside the reasons it might not fit.
    """

    model_config = ConfigDict(frozen=True)

    emphasis: Emphasis = Emphasis.BASE
    runs_per_week: int = Field(default=3, ge=1, le=14)
    quality_sessions: int = Field(default=1, ge=0, le=7)
    long_run: bool = True
    constraints: str = ""

    def describe(self) -> str:
        """Return a one-line summary for the CLI."""
        parts = [
            f"emphasis {self.emphasis}",
            f"{self.runs_per_week} runs/week",
            f"{self.quality_sessions} quality",
        ]
        if self.long_run:
            parts.append("long run")
        return ", ".join(parts)


def load_goals(path: Path) -> Goals:
    """Read goals from `path`, falling back to the defaults.

    A missing file is not an error: it means nothing has been set yet, and
    the defaults describe a reasonable three-run week.

    Args:
        path: Location of the goals file.

    Returns:
        The stored goals, or the defaults.

    Raises:
        ConfigError: If the file exists but cannot be read.
    """
    if not path.exists():
        return Goals()
    try:
        return Goals.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        msg = f"Could not read goals from {path}: {exc}"
        raise ConfigError(msg) from exc


def save_goals(goals: Goals, path: Path) -> None:
    """Write goals to `path`.

    Args:
        goals: The goals to store.
        path: Location of the goals file; parents are created.

    Raises:
        ConfigError: If the file cannot be written.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(goals.model_dump(mode="json"), indent=2), encoding="utf-8")
    except OSError as exc:
        msg = f"Could not write goals to {path}: {exc}"
        raise ConfigError(msg) from exc
