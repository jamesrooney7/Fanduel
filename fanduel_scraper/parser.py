"""All knowledge about the shape of FanDuel's event-page JSON lives here.

The schema is reverse-engineered from FanDuel's web client and may drift.
Extraction is deliberately defensive: per-record problems degrade to blank
fields and counted stats; only a completely unusable default tab raises
SchemaDriftError. Nothing in here filters data — suspended markets and
runners are passed through as data.
"""

from __future__ import annotations

import logging
import re

from .errors import EventNotFoundError, SchemaDriftError
from .models import EventInfo, ParseStats, ScrapeResult, SelectionRow, TabPayload

log = logging.getLogger(__name__)


def dig(obj, *keys, default=None):
    """Nested dict lookup that returns `default` on any missing/non-dict step."""
    current = obj
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
    return default if current is None else current


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def discover_tabs(payload: dict) -> tuple[str | None, list[str]]:
    """Best-effort extraction of the event page's tab list.

    Known/plausible shapes:
      layout.tabs = {"popular": {"title": ..., "position": 1}, ...}
      layout.tabs = [{"slug"|"name"|"title": ..., "default"|"selected": bool}, ...]
      layout.tabs = ["popular", "player-props", ...]

    Returns (default_tab_or_None, ordered unique tab slugs). Unknown shapes
    return (None, []) so the caller falls back to single-tab mode — the run
    still produces output.
    """
    tabs = dig(payload, "layout", "tabs")
    labeled: list[tuple[str, dict]] = []

    if isinstance(tabs, dict):
        entries = [
            (str(slug), meta if isinstance(meta, dict) else {})
            for slug, meta in tabs.items()
        ]

        def position(entry: tuple[str, dict]):
            pos = entry[1].get("position")
            return pos if isinstance(pos, (int, float)) else float("inf")

        labeled = sorted(entries, key=position)
    elif isinstance(tabs, list):
        for item in tabs:
            if isinstance(item, str):
                labeled.append((item, {}))
            elif isinstance(item, dict):
                label = item.get("slug") or item.get("name") or item.get("title")
                if label:
                    labeled.append((str(label), item))
    else:
        if tabs is not None:
            log.warning(
                "Unrecognized layout.tabs shape (%s); scraping the default tab only.",
                type(tabs).__name__,
            )
        return (None, [])

    default_tab: str | None = None
    slugs: list[str] = []
    for label, meta in labeled:
        slug = _slugify(label)
        if not slug or slug in slugs:
            continue
        slugs.append(slug)
        if meta.get("default") is True or meta.get("selected") is True:
            default_tab = slug
    if not slugs:
        log.warning(
            "No tabs discovered in the response layout; scraping the default tab only."
        )
    return (default_tab, slugs)


def parse_event_info(payload: dict, event_id: int) -> EventInfo:
    events = dig(payload, "attachments", "events", default={})
    event = None
    if isinstance(events, dict):
        event = events.get(str(event_id)) or events.get(event_id)
    if not isinstance(event, dict):
        raise EventNotFoundError(
            f"Event {event_id} was not found in FanDuel's response.",
            hint=(
                "The game may have finished, been removed, or the ID/URL is wrong.\n"
                "Tip: open the game page in your browser and copy the URL exactly; "
                "the event id is the number at the end."
            ),
        )
    return EventInfo(
        event_id=event_id,
        name=str(event.get("name") or ""),
        open_date_utc=str(event.get("openDate") or ""),
    )


def _coerce_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _coerce_float(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def extract_american_odds(runner: dict) -> int | None:
    odds = _coerce_int(dig(runner, "winRunnerOdds", "americanDisplayOdds", "americanOdds"))
    if odds is None:
        odds = _coerce_int(
            dig(runner, "winRunnerOdds", "americanDisplayOdds", "americanOddsInt")
        )
    return odds


def extract_decimal_odds(runner: dict) -> float | None:
    odds = _coerce_float(dig(runner, "winRunnerOdds", "trueOdds", "decimalOdds", "decimalOdds"))
    if odds is None:
        odds = _coerce_float(dig(runner, "winRunnerOdds", "decimalDisplayOdds", "decimalOdds"))
    return odds


def parse_markets(payload: dict, tab: str) -> dict[str, dict]:
    """attachments.markets → {market_id: market_dict}; missing/odd shapes → {}."""
    markets = dig(payload, "attachments", "markets", default={})
    if not isinstance(markets, dict):
        log.warning("attachments.markets on tab %r is not an object; ignoring it.", tab)
        return {}
    return {
        str(market_id): market
        for market_id, market in markets.items()
        if isinstance(market, dict)
    }


def parse_event(
    tab_payloads: list[TabPayload], event_id: int, scrape_time_utc: str
) -> ScrapeResult:
    """Merge every tab's markets (dedup by market id, first tab wins) and
    flatten every runner into a SelectionRow."""
    if not tab_payloads:
        raise SchemaDriftError("No responses to parse.")

    event = parse_event_info(tab_payloads[0].payload, event_id)
    stats = ParseStats(tabs_fetched=len(tab_payloads))

    merged: dict[str, tuple[str, dict]] = {}  # market_id -> (tab first seen on, market)
    for tab_payload in tab_payloads:
        markets = parse_markets(tab_payload.payload, tab_payload.tab)
        if not markets and tab_payload is tab_payloads[0]:
            raise SchemaDriftError(
                "FanDuel responded, but no markets were found in the expected place "
                "(attachments.markets). Either no bets are currently offered for this "
                "event, or FanDuel's internal API has changed shape.",
                hint=(
                    "Re-run with --dump-raw dumps/ and share the resulting JSON files "
                    "so the parser can be updated. Nothing was written to Google Sheets."
                ),
            )
        for market_id, market in markets.items():
            if market_id in merged:
                stats.duplicate_markets_merged += 1
                continue
            merged[market_id] = (tab_payload.tab, market)

    stats.markets_total = len(merged)

    rows: list[SelectionRow] = []
    for market_id, (tab, market) in merged.items():
        runners = market.get("runners")
        if not isinstance(runners, list) or not runners:
            stats.markets_skipped_no_runners += 1
            continue
        market_name = str(market.get("marketName") or "")
        market_type = str(market.get("marketType") or "")
        market_status = str(market.get("marketStatus") or "")
        for runner in runners:
            if not isinstance(runner, dict):
                continue
            american = extract_american_odds(runner)
            decimal = extract_decimal_odds(runner)
            if american is None and decimal is None:
                stats.runners_with_unknown_odds += 1
            rows.append(
                SelectionRow(
                    scrape_time_utc=scrape_time_utc,
                    event_id=event_id,
                    event_name=event.name,
                    event_start_utc=event.open_date_utc,
                    tab=tab,
                    market_id=market_id,
                    market_name=market_name,
                    market_type=market_type,
                    market_status=market_status,
                    selection_id=runner.get("selectionId", ""),
                    runner_name=str(runner.get("runnerName") or ""),
                    handicap=_coerce_float(runner.get("handicap")),
                    american_odds=american,
                    decimal_odds=decimal,
                    runner_status=str(runner.get("runnerStatus") or ""),
                )
            )

    stats.selections_total = len(rows)
    if stats.runners_with_unknown_odds:
        log.warning(
            "%d selection(s) had no recognizable odds (left blank). If this looks "
            "wrong, re-run with --dump-raw dumps/ so the parser can be updated.",
            stats.runners_with_unknown_odds,
        )
    return ScrapeResult(event=event, rows=rows, stats=stats)
