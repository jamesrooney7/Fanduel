"""Command-line entrypoint: python -m fanduel_scraper <EVENT URL or ID>."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .config import Config, load_config
from .errors import ScraperError
from .event_ref import parse_event_ref
from .models import ScrapeResult


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m fanduel_scraper",
        description=(
            "Snapshot every bet FanDuel currently offers on one game. "
            "Writes a CSV by default; can write to Google Sheets instead."
        ),
        epilog=(
            "By default a CSV is saved in the current folder. To write to Google "
            "Sheets instead, set GOOGLE_SPREADSHEET (see the README)."
        ),
    )
    p.add_argument("event", metavar="EVENT", help="FanDuel game URL or numeric event id")
    p.add_argument(
        "--state",
        default=None,
        help="(deprecated, ignored) the API endpoint is no longer state-specific",
    )
    p.add_argument(
        "--default-tab",
        default=None,
        metavar="TAB",
        help=(
            "tab fetched first to discover the rest (default: popular). Set this if "
            "the run fails because the default tab is named something else for a sport."
        ),
    )
    p.add_argument(
        "--spreadsheet",
        default=None,
        metavar="ID_OR_URL",
        help=(
            "write to this Google spreadsheet instead of a CSV (or set "
            "GOOGLE_SPREADSHEET in .env). Requires the one-time setup in the README."
        ),
    )
    p.add_argument(
        "--service-account",
        default=None,
        metavar="PATH",
        help="path to the service-account key JSON (default: ./service_account.json)",
    )
    p.add_argument(
        "--timezone",
        default=None,
        help="IANA timezone for the API and worksheet titles (default: America/New_York)",
    )
    p.add_argument(
        "--dump-raw",
        default=None,
        metavar="DIR",
        help="save every raw FanDuel response (and a page screenshot) into DIR for debugging",
    )
    p.add_argument(
        "--headed",
        action="store_true",
        help="show the browser window while scraping (helps if a headless run gets blocked)",
    )
    p.add_argument(
        "--csv",
        default=None,
        metavar="PATH",
        help="write the CSV to PATH (default: an auto-named CSV in the current folder)",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    p.add_argument("--version", action="version", version=f"fanduel_scraper {__version__}")
    # Debugging escape hatches (hidden from --help):
    p.add_argument("--from-dump", default=None, metavar="DIR", help=argparse.SUPPRESS)
    p.add_argument("--http", action="store_true", help=argparse.SUPPRESS)  # legacy raw-HTTP path
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    config = load_config(args)
    logging.basicConfig(
        level=logging.DEBUG if config.verbose else logging.INFO,
        format="%(message)s",
        stream=sys.stderr,
    )
    try:
        return _run(args.event, config)
    except ScraperError as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        if exc.hint:
            print(exc.hint, file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


def _run(event_ref: str, config: Config) -> int:
    from . import api
    from .parser import parse_event

    event_id = parse_event_ref(event_ref)
    scrape_time = datetime.now(timezone.utc)

    if config.from_dump_dir:
        tab_payloads = api.load_dump(config.from_dump_dir, event_id)
    elif config.use_http:
        tab_payloads = api.FanDuelClient(config).fetch_event(event_id)
    else:
        from .browser import FALLBACK_URL, BrowserFetcher

        ref = event_ref.strip()
        navigate_url = ref if ref.lower().startswith("http") else FALLBACK_URL
        tab_payloads = BrowserFetcher(config, navigate_url).fetch_event(event_id)

    result = parse_event(
        tab_payloads, event_id, scrape_time.strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    summary = _summarize(result)
    when_local = _to_local(scrape_time, config.timezone)

    # CSV is the zero-setup default. Google Sheets is used only when a
    # spreadsheet is configured and the user didn't force CSV with --csv.
    if config.spreadsheet and config.csv_path is None:
        from .sheets import SheetsWriter

        writer = SheetsWriter(config.service_account_file, config.spreadsheet)
        url = writer.write_snapshot(result.event, result.rows, when_local)
        print(f"{summary}\nWrote worksheet: {url}")
        return 0

    from .csv_out import suggest_csv_filename, write_csv

    path = config.csv_path or Path(
        suggest_csv_filename(result.event.name, event_id, when_local)
    )
    write_csv(path, result.rows)
    print(f"{summary}\nWrote CSV: {path.resolve()}")
    return 0


def _to_local(when_utc: datetime, tz_name: str) -> datetime:
    try:
        from zoneinfo import ZoneInfo

        return when_utc.astimezone(ZoneInfo(tz_name))
    except Exception:
        return when_utc


def _summarize(result: ScrapeResult) -> str:
    stats = result.stats
    parts = [
        f"Scraped '{result.event.name or result.event.event_id}':",
        f"{stats.tabs_fetched} tab(s), {stats.markets_total} market(s), "
        f"{stats.selections_total} selection(s).",
    ]
    extras = []
    if stats.markets_skipped_no_runners:
        extras.append(f"{stats.markets_skipped_no_runners} market(s) had no selections")
    if stats.duplicate_markets_merged:
        extras.append(
            f"{stats.duplicate_markets_merged} duplicate market(s) merged across tabs"
        )
    if stats.runners_with_unknown_odds:
        extras.append(
            f"{stats.runners_with_unknown_odds} selection(s) had unrecognized odds"
        )
    if extras:
        parts.append("(" + "; ".join(extras) + ")")
    return " ".join(parts)
