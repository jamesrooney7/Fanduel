"""HTTP client for FanDuel's internal event-page API.

FanDuel rejects clients whose TLS fingerprint doesn't look like a real
browser (plain `requests` gets 403 no matter the headers), so the default
transport is curl_cffi impersonating Chrome. The transport is injectable
for tests, and `load_dump()` replays a --dump-raw directory with no
network at all.

Endpoint and params mirror exactly what sportsbook.fanduel.com's own web
client sends:
    https://api.sportsbook.fanduel.com/sbapi/event-page
        ?_ak=...&eventId=...&tab=<title-slug>
        &useCombinedTouchdownsVirtualMarket=true&useQuickBets=true
Each tab is a separate request; the tab value is the lower-cased tab title
(e.g. "Goals" -> "goals"), discovered from the layout of the first response.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from pathlib import Path
from typing import Callable

from . import parser
from .config import Config
from .errors import (
    EventNotFoundError,
    GeoBlockedError,
    NetworkError,
    SchemaDriftError,
    ScraperError,
)
from .models import TabPayload

log = logging.getLogger(__name__)

BASE_URL = "https://api.sportsbook.fanduel.com/sbapi/event-page"
API_HOST = "api.sportsbook.fanduel.com"

BROWSER_HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://sportsbook.fanduel.com/",
    "Origin": "https://sportsbook.fanduel.com",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
}

RETRY_BACKOFF = (1.0, 2.0, 4.0)  # waits between the 4 total attempts
RATE_LIMIT_BACKOFF = 10.0


def build_params(app_key: str, event_id: int, tab: str | None = None) -> dict[str, str]:
    params = {
        "_ak": app_key,
        "eventId": str(event_id),
        "useCombinedTouchdownsVirtualMarket": "true",
        "useQuickBets": "true",
    }
    if tab:
        params["tab"] = tab
    return params


def classify_http_error(status: int, body_snippet: str = "") -> ScraperError:
    if status in (403, 451):
        return GeoBlockedError(
            f"FanDuel rejected the request (HTTP {status}).",
            hint=(
                "This almost always means the request did not come from a US "
                "residential IP in a state where FanDuel operates.\n"
                "- Run this tool from your home network in a state where FanDuel is legal.\n"
                "- VPNs and cloud/datacenter IPs are blocked.\n"
                "- If you are already on a US residential connection, FanDuel may have "
                "tightened its defenses — re-run with --dump-raw dumps/ and share the output."
            ),
        )
    if status == 404:
        return EventNotFoundError(
            "Event was not found (HTTP 404).",
            hint=(
                "The game may have finished, been removed, or the ID/URL is wrong.\n"
                "Tip: open the game page in your browser and copy the URL exactly; "
                "the event id is the number at the end."
            ),
        )
    return NetworkError(f"Unexpected HTTP {status} from FanDuel: {body_snippet!r}")


def _build_curl_cffi_transport(config: Config) -> Callable:
    try:
        from curl_cffi import requests as curl_requests
    except ImportError as exc:
        raise ScraperError(
            "curl_cffi is not installed.",
            hint="Run: pip install -r requirements.txt",
        ) from exc

    session = curl_requests.Session(impersonate="chrome", headers=BROWSER_HEADERS)

    def transport(url: str, params: dict, timeout: float):
        return session.get(url, params=params, timeout=timeout)

    return transport


class FanDuelClient:
    def __init__(
        self,
        config: Config,
        sleep: Callable[[float], None] = time.sleep,
        transport: Callable | None = None,
    ):
        self.config = config
        self._sleep = sleep
        self._transport = transport or _build_curl_cffi_transport(config)

    def fetch_event(self, event_id: int) -> list[TabPayload]:
        """Fetch the layout, discover every tab, then fetch each tab's markets.

        The first request (no tab) returns the page layout used to discover the
        tab list. Every discovered tab is then fetched by its title slug and
        merged; markets are de-duplicated by id downstream, so any overlap
        between tabs is harmless. A failing secondary tab is logged and skipped
        (partial data beats none), EXCEPT a geo-block, which aborts immediately.
        """
        log.info("Fetching event %s (layout) from %s ...", event_id, BASE_URL)
        first = self.fetch_tab(event_id, tab=None, dump_index=0, dump_tab="default")
        payloads = [TabPayload("default", first)]

        _default_tab, tabs = parser.discover_tabs(first)
        if tabs:
            log.info("Found %d tab(s): %s", len(tabs), ", ".join(tabs))
        else:
            log.info("No tabs discovered; using the default response only.")

        for index, tab in enumerate(tabs, start=1):
            self._sleep(random.uniform(self.config.min_delay, self.config.max_delay))
            log.info("[%d/%d] fetching tab '%s' ...", index, len(tabs), tab)
            try:
                payload = self.fetch_tab(event_id, tab=tab, dump_index=index, dump_tab=tab)
            except GeoBlockedError:
                raise
            except ScraperError as exc:
                log.warning("Tab '%s' failed (%s); continuing without it.", tab, exc)
                continue
            payloads.append(TabPayload(tab, payload))
        return payloads

    def fetch_tab(
        self, event_id: int, tab: str | None, dump_index: int = 0, dump_tab: str = "default"
    ) -> dict:
        params = build_params(self.config.app_key, event_id, tab)
        body = self._request_with_retries(params)
        self._maybe_dump(event_id, dump_index, dump_tab, body)
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise SchemaDriftError(
                f"FanDuel responded with something that is not JSON (tab={dump_tab!r}). "
                "Their API may have changed, or a block page was returned.",
                hint="Re-run with --dump-raw dumps/ and share the output files.",
            ) from exc

    def _request_with_retries(self, params: dict) -> str:
        last_error: Exception | None = None
        for attempt in range(len(RETRY_BACKOFF) + 1):
            if attempt:
                wait = RETRY_BACKOFF[attempt - 1] * random.uniform(0.8, 1.2)
                log.debug("Retrying in %.1fs (attempt %d) ...", wait, attempt + 1)
                self._sleep(wait)
            try:
                response = self._transport(BASE_URL, params, self.config.request_timeout)
            except ScraperError:
                raise
            except Exception as exc:  # connect errors, timeouts, TLS failures
                last_error = exc
                log.debug("Request failed: %s", exc)
                continue

            status = getattr(response, "status_code", None)
            text = getattr(response, "text", "") or ""
            if status == 200:
                return text
            if status in (403, 404, 451):
                # Deterministic rejections — retrying cannot help.
                raise classify_http_error(status, text[:200])
            if status == 429:
                log.warning(
                    "Rate limited (HTTP 429); waiting %.0fs before retrying.",
                    RATE_LIMIT_BACKOFF,
                )
                self._sleep(RATE_LIMIT_BACKOFF)
                last_error = NetworkError("HTTP 429 from FanDuel")
                continue
            last_error = NetworkError(f"HTTP {status} from FanDuel")
            log.debug("HTTP %s; will retry.", status)

        raise NetworkError(
            f"Could not get a good response from {API_HOST} after "
            f"{len(RETRY_BACKOFF) + 1} attempts ({last_error}).",
            hint=(
                "Check your internet connection and any firewall/VPN. Note: this "
                "tool cannot work from cloud servers or most corporate networks — "
                "FanDuel only answers US residential connections."
            ),
        )

    def _maybe_dump(self, event_id: int, index: int, tab: str, body_text: str) -> None:
        dump_dir = self.config.dump_raw_dir
        if not dump_dir:
            return
        dump_dir.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", tab) or "default"
        path = dump_dir / f"{event_id}_{index:02d}_{slug}.json"
        path.write_text(body_text, encoding="utf-8")
        log.debug("Dumped raw response to %s", path)


DUMP_FILE_RE = re.compile(r"^(?P<event>\d+)_(?P<index>\d+)_(?P<tab>.+)\.json$")


def load_dump(dump_dir: Path, event_id: int | None = None) -> list[TabPayload]:
    """Replay a --dump-raw directory instead of hitting the network."""
    if not dump_dir.is_dir():
        raise ScraperError(f"Dump directory not found: {dump_dir}")
    entries: list[tuple[int, str, Path]] = []
    for path in sorted(dump_dir.iterdir()):
        match = DUMP_FILE_RE.match(path.name)
        if not match:
            continue
        if event_id is not None and int(match.group("event")) != event_id:
            continue
        entries.append((int(match.group("index")), match.group("tab"), path))
    if not entries:
        raise ScraperError(
            f"No dump files for event {event_id} found in {dump_dir}.",
            hint=(
                "Expected files named like 33840322_00_default.json "
                "(as written by --dump-raw)."
            ),
        )
    entries.sort(key=lambda entry: entry[0])
    payloads = []
    for _index, tab, path in entries:
        try:
            payloads.append(TabPayload(tab, json.loads(path.read_text(encoding="utf-8"))))
        except json.JSONDecodeError as exc:
            raise SchemaDriftError(f"{path.name} is not valid JSON: {exc}") from exc
    return payloads
