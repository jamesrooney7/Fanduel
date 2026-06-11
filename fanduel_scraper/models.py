"""Data shapes shared across the scraper.

HEADER is the single source of truth for column order in Google Sheets and
CSV output; its entries match SelectionRow field names exactly.
"""

from __future__ import annotations

from dataclasses import dataclass, field

HEADER = [
    "scrape_time_utc",
    "event_id",
    "event_name",
    "event_start_utc",
    "tab",
    "market_id",
    "market_name",
    "market_type",
    "market_status",
    "selection_id",
    "runner_name",
    "handicap",
    "american_odds",
    "decimal_odds",
    "runner_status",
]


@dataclass(frozen=True)
class EventInfo:
    event_id: int
    name: str = ""
    open_date_utc: str = ""


@dataclass(frozen=True)
class SelectionRow:
    scrape_time_utc: str
    event_id: int
    event_name: str
    event_start_utc: str
    tab: str
    market_id: str
    market_name: str
    market_type: str
    market_status: str
    selection_id: object  # int when FanDuel sends an int, str otherwise
    runner_name: str
    handicap: float | None
    american_odds: int | None
    decimal_odds: float | None
    runner_status: str


@dataclass
class ParseStats:
    tabs_fetched: int = 0
    markets_total: int = 0
    markets_skipped_no_runners: int = 0
    selections_total: int = 0
    duplicate_markets_merged: int = 0
    runners_with_unknown_odds: int = 0


@dataclass(frozen=True)
class TabPayload:
    tab: str
    payload: dict


@dataclass(frozen=True)
class ScrapeResult:
    event: EventInfo
    rows: list[SelectionRow]
    stats: ParseStats


def row_to_cells(row: SelectionRow) -> list:
    """HEADER-ordered cell values; None becomes '' so spreadsheet cells stay blank.

    Numbers are passed through as numbers so Sheets can sort and filter them.
    """
    values = [getattr(row, column) for column in HEADER]
    return ["" if value is None else value for value in values]
