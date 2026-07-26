"""The guarantees that make cycle tracking safe to keep in this repository.

These are the tests worth having. Phase arithmetic being wrong would be
annoying; any of these failing would mean the most sensitive data in the
project had become committable, readable by another account, or reachable by
something that talks to a network.
"""

import datetime as dt
import subprocess
from pathlib import Path

from longrun.store.private import CycleStore

REPO_ROOT = Path(__file__).resolve().parent.parent

# Paths that must never be committable, whatever else changes in .gitignore.
FORBIDDEN_PATHS = (
    "data/private/cycle.db",
    "data/private/anything.db",
    "data/longrun.db",
    "cycle.db",
    "some/nested/private/cycle.db",
)


def _is_ignored(path: str) -> bool:
    """Return whether git would refuse to track `path`."""
    result = subprocess.run(
        ["git", "check-ignore", "-q", "--no-index", path],
        cwd=REPO_ROOT,
        check=False,
    )
    return result.returncode == 0


def test_cycle_paths_are_gitignored() -> None:
    """A loosened ignore rule must fail here rather than in a public repo."""
    for path in FORBIDDEN_PATHS:
        assert _is_ignored(path), f"{path} is not gitignored"


def test_the_cycle_database_is_owner_only(tmp_path: Path) -> None:
    """Another account on the same machine must not be able to read it."""
    path = tmp_path / "private" / "cycle.db"

    with CycleStore.open(path) as store:
        store.log_days([dt.date(2026, 1, 5)])

    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_the_cycle_store_reaches_no_network() -> None:
    """Manual entry only: nothing here may import an HTTP client.

    Checked structurally rather than promised in a docstring, so that adding
    a sync to this module would break the build.
    """
    source = (REPO_ROOT / "src" / "longrun" / "store" / "private.py").read_text(encoding="utf-8")

    for forbidden in ("httpx", "requests", "urllib", "socket", "longrun.ingest"):
        assert forbidden not in source, f"{forbidden} must not be reachable from the cycle store"


def test_cycle_analysis_reaches_no_network() -> None:
    """The same holds for the module that interprets the logged days."""
    source = (REPO_ROOT / "src" / "longrun" / "analysis" / "cycle.py").read_text(encoding="utf-8")

    for forbidden in ("httpx", "requests", "urllib", "socket", "longrun.ingest"):
        assert forbidden not in source


def test_the_reporting_layer_never_opens_the_cycle_store() -> None:
    """A report can only contain this data because a caller went and got it.

    The reporting package takes cycles as an argument. If it could open the
    private store itself, `--with-cycle` would be a suggestion rather than a
    gate.
    """
    report_dir = REPO_ROOT / "src" / "longrun" / "report"

    for module in report_dir.glob("*.py"):
        source = module.read_text(encoding="utf-8")
        assert "CycleStore" not in source, f"{module.name} must not open the cycle store"
        assert "store.private" not in source, f"{module.name} must not import the private store"


def test_the_main_store_holds_no_cycle_data() -> None:
    """Cycle data lives in its own file, which is the whole point of it."""
    schema = (REPO_ROOT / "src" / "longrun" / "store" / "schema.py").read_text(encoding="utf-8")

    assert "period" not in schema.lower()
    assert "cycle" not in schema.lower()
