"""Fetch event-page JSON by driving a real headless browser (Playwright).

FanDuel's markets endpoint is gated behind JavaScript-generated bot tokens, so
a plain HTTP client is rejected at the edge. Instead we open the game page in a
real Chromium — which runs the JS and earns the bot cookies — then fetch each
tab's JSON. Because the page (az.sportsbook.fanduel.com) and the API
(api.sportsbook.fanduel.com) are different origins, an in-page fetch can be
blocked by CORS, so we try several mechanisms per tab and keep the first that
returns valid JSON:

  1. in-page fetch() with credentials   (the app's own XHR style)
  2. a direct browser navigation to the API URL  (no CORS; carries cookies)
  3. in-page fetch() without credentials (works when the API uses ACAO: *)

The orchestration is unit-tested with a fake page; only fetch_event touches
Playwright.
"""

from __future__ import annotations

import json
import logging
import random
import time
from pathlib import Path
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

# Runs inside the page: fetch the API with the page's own identity/cookies.
_FETCH_JS = """
async ({url, credentials}) => {
  try {
    const resp = await fetch(url, {
      credentials: credentials,
      headers: { 'Accept': 'application/json' },
    });
    return { status: resp.status, body: await resp.text() };
  } catch (e) {
    return { status: 0, body: 'fetch error: ' + String(e) };
  }
}
"""

# Hide the most obvious automation tell before any page script runs.
_STEALTH_JS = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"


def _looks_like_json(body: str) -> bool:
    return bool(body) and body.lstrip()[:1] in ("{", "[")


def _snippet(body: str, limit: int = 160) -> str:
    text = (body or "").strip().replace("\n", " ")
    return text[:limit]


class BrowserFetcher:
    def __init__(
        self, config: Config, navigate_url: str, sleep: Callable[[float], None] = time.sleep
    ):
        self.config = config
        self.navigate_url = navigate_url
        self._sleep = sleep

    def _timeout_ms(self) -> int:
        return int(max(self.config.request_timeout, 30) * 1000)

    def fetch_event(self, event_id: int) -> list[TabPayload]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise ScraperError(
                "Playwright is not installed.",
                hint=(
                    "Install it once with:\n"
                    "  pip install -r requirements.txt\n"
                    "  python -m playwright install chromium"
                ),
            ) from exc

        log.info("Launching %s browser ...", "visible" if self.config.headed else "headless")
        with sync_playwright() as pw:
            browser = self._launch(pw)
            try:
                context = browser.new_context(
                    locale="en-US",
                    user_agent=api.BROWSER_HEADERS["User-Agent"],
                    viewport={"width": 1366, "height": 900},
                )
                context.add_init_script(_STEALTH_JS)
                page = context.new_page()
                page.set_default_timeout(self._timeout_ms())
                self._open(page)
                return self._collect(page, event_id)
            finally:
                browser.close()

    def _launch(self, pw):
        launch_args = ["--disable-blink-features=AutomationControlled"]
        headless = not self.config.headed
        errors = []
        # Prefer the user's real Google Chrome (least detectable); fall back to
        # Playwright's bundled Chromium.
        for channel in ("chrome", None):
            try:
                kwargs = {"headless": headless, "args": launch_args}
                if channel:
                    kwargs["channel"] = channel
                return pw.chromium.launch(**kwargs)
            except Exception as exc:  # noqa: BLE001 - report all attempts together
                errors.append(f"{channel or 'bundled chromium'}: {exc}")
        raise ScraperError(
            "Could not launch a browser.\n" + "\n".join(errors),
            hint=(
                "Install the bundled browser once with:\n"
                "  python -m playwright install chromium\n"
                "(or install Google Chrome on this machine)."
            ),
        )

    def _open(self, page) -> None:
        log.info("Opening %s ...", self.navigate_url)
        try:
            page.goto(self.navigate_url, wait_until="domcontentloaded", timeout=self._timeout_ms())
        except Exception as exc:  # noqa: BLE001
            log.warning("Page did not finish loading cleanly (%s); continuing.", exc)
        # Let the SPA settle and any deferred bot scripts set their cookies.
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:  # noqa: BLE001
            pass
        self._sleep(2.0)
        self._save_page_snapshot(page)
        log.info("Browser session ready.")

    def _save_page_snapshot(self, page) -> None:
        """When dumping, save what the browser actually sees — invaluable for
        diagnosing bot challenges, login walls, or state pickers."""
        dump_dir = self.config.dump_raw_dir
        if not dump_dir:
            return
        dump_dir.mkdir(parents=True, exist_ok=True)
        try:
            page.screenshot(path=str(dump_dir / "page.png"), full_page=True)
            log.info("Saved page screenshot to %s", dump_dir / "page.png")
        except Exception as exc:  # noqa: BLE001
            log.debug("Could not save screenshot: %s", exc)
        try:
            (dump_dir / "page.html").write_text(page.content(), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            log.debug("Could not save page HTML: %s", exc)

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

        attempts: list[tuple[str, int, str]] = []
        last_body = ""
        for desc, fetch in self._strategies(page, url):
            status, body = fetch()
            if body:
                last_body = body
            log.debug("tab %r via %s -> HTTP %s (%d bytes)", tab, desc, status, len(body or ""))
            if status in (403, 451):
                self._dump(event_id, index, tab, body)
                raise GeoBlockedError(
                    f"FanDuel rejected the browser request (HTTP {status}).",
                    hint=(
                        "Run this from a US residential connection in a state where "
                        "FanDuel operates, with any VPN turned off."
                    ),
                )
            if status == 404:
                self._dump(event_id, index, tab, body)
                raise EventNotFoundError(
                    f"Event {event_id} was not found (HTTP 404).",
                    hint="Open the game page in your browser and copy the URL exactly.",
                )
            if status == 200 and _looks_like_json(body):
                self._dump(event_id, index, tab, body)
                try:
                    return json.loads(body)
                except json.JSONDecodeError as exc:
                    raise SchemaDriftError(
                        f"FanDuel returned malformed JSON for tab {tab!r}.",
                        hint="Re-run with --dump-raw dumps/ and share the output files.",
                    ) from exc
            attempts.append((desc, status, _snippet(body)))

        self._dump(event_id, index, tab, last_body)
        detail = " | ".join(f"{d}: HTTP {s} {snip!r}" for d, s, snip in attempts)
        raise NetworkError(
            f"Could not fetch tab {tab!r} in the browser. Attempts: {detail}",
            hint=(
                "Re-run with --dump-raw dumps/ -v and try --headed; then share "
                "dumps/page.png so the page state can be inspected."
            ),
        )

    def _strategies(self, page, url: str):
        return [
            ("fetch(include)", lambda: self._evaluate_fetch(page, url, "include")),
            ("navigate", lambda: self._navigate_fetch(page, url)),
            ("fetch(omit)", lambda: self._evaluate_fetch(page, url, "omit")),
        ]

    def _evaluate_fetch(self, page, url: str, credentials: str) -> tuple[int, str]:
        try:
            result = page.evaluate(_FETCH_JS, {"url": url, "credentials": credentials})
        except Exception as exc:  # noqa: BLE001
            return 0, f"evaluate error: {exc}"
        if not isinstance(result, dict):
            return 0, ""
        return int(result.get("status") or 0), str(result.get("body") or "")

    def _navigate_fetch(self, page, url: str) -> tuple[int, str]:
        try:
            response = page.goto(url, wait_until="commit", timeout=self._timeout_ms())
            if response is None:
                return 0, ""
            return int(response.status), response.text()
        except Exception as exc:  # noqa: BLE001
            return 0, f"navigation error: {exc}"

    def _dump(self, event_id: int, index: int, tab: str, body: str) -> None:
        if self.config.dump_raw_dir and body:
            write_dump(self.config.dump_raw_dir, event_id, index, tab, body)
