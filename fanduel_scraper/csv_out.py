"""CSV output — the default, zero-setup way to save a snapshot."""

from __future__ import annotations

import csv
import re
from datetime import datetime
from pathlib import Path

from .models import HEADER, SelectionRow, row_to_cells


def suggest_csv_filename(event_name: str, event_id: int, when_local: datetime) -> str:
    """A descriptive, filesystem-safe filename for a snapshot, e.g.
    'los-angeles-lakers-boston-celtics_33840322_2026-06-11_1830.csv'."""
    slug = re.sub(r"[^a-z0-9]+", "-", (event_name or "").lower()).strip("-")
    slug = slug[:60].strip("-") or "fanduel-event"
    stamp = when_local.strftime("%Y-%m-%d_%H%M")
    return f"{slug}_{event_id}_{stamp}.csv"


def write_csv(path: Path, rows: list[SelectionRow]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(HEADER)
        for row in rows:
            writer.writerow(row_to_cells(row))
