"""Fetch event-page JSON by driving a real headless browser (Playwright).

FanDuel's markets endpoint is gated behind JavaScript-generated bot tokens, so
a plain HTTP client is rejected at the edge. Instead we open the game page in a
real headless Chromium — which runs the JS and earns the bot cookies — and then
fetch each tab's JSON *from inside the page context*, so every request carries
the genuine browser identity. The parsing/merging downstream is unchanged.

The orchestration (`_collect`) takes a `page`-like object with `.evaluate()` and
is unit-tested with a fake page; only `fetch_event` touches Playwright.
"""

from __future__ import annotations

import json
import logging
import random
import time
from typing import Callable
from urllib.parse import urlencode

from . import api, parser
from .api import BASE_URL, build_params, write_dump
from .config import Config
from .errors import (
    EventNotFoundError,
    GeoBlockedError,
    NetworkError,
    ScraperError,
    SchemaDriftError,
)
from .models import TabPayload

log = logging.getLogger(__name__)

FALLBACK_URL = "https://sportsbook.fanduel.com/"

# Runs inside the page: fetch the API with the page's own cookies/identity.
_FETCH_JS = """
async (url) => {
  try {
    const resp = await fetch(url, {
      credentials: 'include',
      headers: { 'Accept': 'application/json' },
    });
    return { status: resp.status, body: await resp.text() };
  } catch (e) {
    return { status: 0, body: String(e) };
  }
}
"""


class BrowserFetcher:
    def __init__(self, config: Config, navigate_url: str, sleep: Callable[[float], None] = time.sleep):
        self.config = config
        self.navigate_url = navigate_url
        self._sleep = sleep

    def fetch_event(self, event_id: int) -> list[TabPayload]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise ScraperError(
                "Playwright is not installed.",
                hint=(
                    "Install it once with:\n"
                    "  pip install -r requirements.txt\n"
                    "  playwright install chromium"
                ),
            ) from exc

        log.info("Launching headless browser ...")
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled"],
                )
            except Exception as exc:
                raise ScraperError(
                    f"Could not launch headless Chromium ({exc}).",
                    hint="Install the browser once with:  playwright install chromium",
                ) from exc
            try:
                context = browser.new_context(
                    locale="en-US",
                    user_agent=api.BROWSER_HEADERS["User-Agent"],
                    viewport={"width": 1366, "height": 900},
                )
                page = context.new_page()
                page.set_default_timeout(max(self.config.request_timeout, 30) * 1000)
                self._open(page)
                return self._collect(page, event_id)
            finally:
                browser.close()

    def _open(self, page) -> None:
        log.info("Opening %s ...", self.navigate_url)
        try:
            # Set up the response listener BEFORE navigating so we don't miss the
            # page's own data request, which proves the bot cookies are in place.
            with page.expect_response(
                lambda r: "/sbapi/event-page" in r.url, timeout=45000
            ):
                page.goto(self.navigate_url, wait_until="domcontentloaded", timeout=45000)
            log.info("Browser session ready.")
        except Exception as exc:
            log.warning(
                "Proceeding without confirming the page's own data request (%s).", exc
            )

    def _collect(self, page, event_id: int) -> list[TabPayload]:
        default_tab = self.config.default_tab
        first = self._fetch_tab(page, event_id, default_tab, index=0)
        payloads = [TabPayload(default_tab, first)]

        _default_slug, tabs = parser.discover_tabs(first)
        remaining = [tab for tab in tabs if tab != default_tab]
        if remaining:
            log.info("Found %d more tab(s): %s", len(remaining), ", ".join(remaining))
        else:
            log.info("No additional tabs discovered.")

        for index, tab in enumerate(remaining, start=1):
            self._sleep(random.uniform(self.config.min_delay, self.config.max_delay))
            log.info("[%d/%d] fetching tab '%s' ...", index, len(remaining), tab)
            try:
                payloads.append(TabPayload(tab, self._fetch_tab(page, event_id, tab, index)))
            except GeoBlockedError:
                raise
            except ScraperError as exc:
                log.warning("Tab '%s' failed (%s); continuing without it.", tab, exc)
                continue
        return payloads

    def _fetch_tab(self, page, event_id: int, tab: str, index: int) -> dict:
        params = build_params(self.config.app_key, event_id, tab)
        url = f"{BASE_URL}?{urlencode(params)}"
        status, body = self._evaluate_fetch(page, url)
        if self.config.dump_raw_dir and body:
            write_dump(self.config.dump_raw_dir, event_id, index, tab, body)

        if status == 200:
            try:
                return json.loads(body)
            except json.JSONDecodeError as exc:
                raise SchemaDriftError(
                    f"FanDuel returned non-JSON for tab {tab!r}.",
                    hint="Re-run with --dump-raw dumps/ and share the output files.",
                ) from exc
        if status in (403, 451):
            raise GeoBlockedError(
                f"FanDuel rejected the browser request (HTTP {status}).",
                hint=(
                    "Run this from a US residential connection in a state where "
                    "FanDuel operates, with any VPN turned off."
                ),
            )
        if status == 404:
            raise EventNotFoundError(
                f"Event {event_id} was not found (HTTP 404).",
                hint="Open the game page in your browser and copy the URL exactly.",
            )
        raise NetworkError(f"Tab {tab!r} returned HTTP {status} via the browser.")

    def _evaluate_fetch(self, page, url: str) -> tuple[int, str]:
        result = page.evaluate(_FETCH_JS, url)
        if not isinstance(result, dict):
            raise NetworkError("Unexpected result from the browser fetch.")
        return int(result.get("status") or 0), str(result.get("body") or "")
