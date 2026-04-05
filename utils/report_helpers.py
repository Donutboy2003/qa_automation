# utils/report_helpers.py
# Utility for writing report files to a consistent reports/ directory.

from __future__ import annotations

import os

REPORTS_DIR = "reports"


def report_path(filename: str) -> str:
    """
    Return the full path for a report file inside the reports/ directory.
    Creates the directory if it doesn't exist.

    Usage:
        with open(report_path("alt_suggestions.json"), "w") as f:
            json.dump(data, f)
    """
    os.makedirs(REPORTS_DIR, exist_ok=True)
    return os.path.join(REPORTS_DIR, filename)
