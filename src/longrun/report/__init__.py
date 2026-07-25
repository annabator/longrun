"""Reporting: deterministic trend visualisations over the stored records.

Every number rendered here is computed in Python from the store. Nothing in
this package estimates, predicts or narrates - see the design principle in
README.md.
"""

from longrun.report.builder import build_report
from longrun.report.frames import ReportData, load_report_data

__all__ = ["ReportData", "build_report", "load_report_data"]
