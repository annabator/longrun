"""Ingest a folder of FIT files.

The folder lives under `data/`, which is gitignored, so raw exports never
reach the repository.
"""

from pathlib import Path

from longrun.errors import IngestError
from longrun.ingest.base import IngestSummary, Progress
from longrun.ingest.fit.reader import iter_fit_files, read_fit_file
from longrun.models import Source
from longrun.store import Store

DEFAULT_RAW_DIR = Path("data/raw")
"""Where FIT exports are expected; gitignored along with the rest of `data/`."""


class FitFolderIngester:
    """Parses every FIT file in a folder into the store.

    Secondary to :class:`~longrun.ingest.intervals.IntervalsIngester`, and the
    only source of beat-to-beat R-R intervals.
    """

    source = Source.FIT

    def __init__(self, folder: Path = DEFAULT_RAW_DIR, *, skip_known: bool = True) -> None:
        """Configure a run.

        Args:
            folder: Directory of `.fit` files, searched recursively.
            skip_known: Skip files whose activity already has streams stored,
                since parsing is the expensive part here.
        """
        self._folder = folder
        self._skip_known = skip_known

    def ingest(self, store: Store, *, progress: Progress | None = None) -> IngestSummary:
        """Parse and store every FIT file in the folder.

        A file that cannot be parsed is reported and skipped rather than
        ending the run: one corrupt export should not block a backfill.

        Args:
            store: Destination store, already initialised.
            progress: Optional callback for progress lines.

        Returns:
            Counts of what was written.

        Raises:
            IngestError: If the folder does not exist.
        """
        report = progress or (lambda _: None)
        if not self._folder.is_dir():
            msg = (
                f"{self._folder} does not exist. Put your .fit exports there, "
                f"or pass --folder with another location."
            )
            raise IngestError(msg)

        already_have_streams = store.stored_stream_activity_ids(self.source)
        summary = IngestSummary()
        failures = 0

        for path in iter_fit_files(self._folder):
            if self._skip_known and path.stem in already_have_streams:
                summary = summary.merge(IngestSummary(streams_skipped=1))
                continue
            try:
                activity, points = read_fit_file(path)
            except IngestError as exc:
                failures += 1
                report(f"  skipped {path.name}: {exc}")
                continue

            store.upsert_activities([activity])
            written = (
                store.upsert_streams(self.source, activity.external_id, points) if points else 0
            )
            report(f"  {activity.start_time:%Y-%m-%d}  {path.name}  ({written} samples)")
            summary = summary.merge(
                IngestSummary(
                    activities=1,
                    activities_with_streams=1 if written else 0,
                    stream_points=written,
                )
            )

        if failures:
            report(f"{failures} file(s) could not be parsed and were skipped")
        return summary
