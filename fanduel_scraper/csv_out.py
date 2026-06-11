"""Debug escape hatch: write the snapshot as a CSV instead of Google Sheets."""

from __future__ import annotations

import csv
from pathlib import Path

from .models import HEADER, SelectionRow, row_to_cells


def write_csv(path: Path, rows: list[SelectionRow]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(HEADER)
        for row in rows:
            writer.writerow(row_to_cells(row))
