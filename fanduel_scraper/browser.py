"""Fetch event-page JSON by driving a real browser (Playwright).

FanDuel's markets API rejects any request that doesn't carry the headers the
web app attaches (a bare request returns HTTP 400), and because the page
(az.sportsbook.fanduel.com) and the API (api.sportsbook.fanduel.com) are
different origins, a 400 reads as a CORS "Failed to fetch" from inside the page.

So instead of constructing the request ourselves, we let the app make it and
harvest the result:

  1. Open the game page in a real browser; it runs FanDuel's JS, earns the bot
     cookies, and fetches the default tab itself.
  2. Capture that response body directly (real data, no CORS) and the exact
     request headers the app used.
  3. Replay those headers via in-page fetch() to pull every other tab.

The replay/merge orchestration is unit-tested with a fake page; only
fetch_event touches Playwright.
"""

from __future__ import annotations

import json
import logging
import random
import time
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlencode, urlparse

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
EVENT_PAGE_PATH = "/sbapi/event-page"

# Runs inside the page: replay the app's request from the page's own context.
_FETCH_JS = """
async ({url, headers}) => {
  try {
    const resp = await fetch(url, { credentials: 'include', headers: headers });
    return { status: resp.status, body: await resp.text() };
  } catch (e) {
    return { status: 0, body: 'fetch error: ' + String(e) };
  }
}
"""

_STEALTH_JS = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"

# Headers a browser manages itself; fetch() silently ignores attempts to set
# them, so there's no point replaying them.
_FORBIDDEN_HEADERS = {
    "host", "connection", "content-length", "origin", "referer", "user-agent",
    "cookie", "accept-encoding", "accept-charset", "te", "trailer",
    "transfer-encoding", "upgrade", "via", "date", "dnt", "keep-alive", "expect",
    "content-type",
}
_FORBIDDEN_PREFIXES = ("sec-", "proxy-", ":")


def _looks_like_json(body: str) -> bool:
    return bool(body) and body.lstrip()[:1] in ("{", "[")


def _snippet(body: str, limit: int = 160) -> str:
    return (body or "").strip().replace("\n", " ")[:limit]


def _tab_param(url: str) -> str | None:
    return parse_qs(urlparse(url).query).get("tab", [None])[0]


def _event_id_param(url: str) -> int | None:
    raw = parse_qs(urlparse(url).query).get("eventId", [None])[0]
    try:
        return int(raw) if raw is not None else None
    except ValueError:
        return None


def replayable_headers(headers: dict | None) -> dict:
    """Keep the headers the app added that fetch() will actually let us set
    (notably authorization / x-* tokens); drop browser-managed ones."""
    out = {}
    for key, value in (headers or {}).items():
        low = key.lower()
        if low in _FORBIDDEN_HEADERS or low.startswith(_FORBIDDEN_PREFIXES):
            continue
        out[key] = value
    out.setdefault("Accept", "application/json")
    return out


class BrowserFetcher:
    def __init__(
        self, config: Config, navigate_url: str, sleep: Callable[[float], None] = time.sleep
    ):
        self.config = config
        self.navigate_url = navigate_url
        self._sleep = sleep

    def _timeout_ms(self) -> int:
        return int(max(self.config.request_timeout, 30) * 1000)

    # -- Playwright entry point (not unit-tested; keep it thin) --------------

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

        # Headless browsers are more likely to be fingerprinted and served a
        # stripped page that never makes the market request, so if a headless
        # run captures nothing, fall back to a visible window automatically.
        modes = [True] if self.config.headed else [False, True]
        with sync_playwright() as pw:
            for attempt, headed in enumerate(modes):
                is_last = attempt == len(modes) - 1
                result = self._attempt(pw, event_id, headed, is_last)
                if result is not None:
                    return result
                if not is_last:
                    log.warning(
                        "Headless run captured no data; retrying with a visible "
                        "browser window ..."
                    )
        raise NetworkError(
            f"The page loaded but no market data for event {event_id} was captured.",
            hint=(
                "Most likely the game has finished or hasn't opened for betting yet "
                "(a finished game's URL redirects away). Double-check the game is "
                "live/upcoming on FanDuel.\n"
                "If it is, re-run with --dump-raw dumps/ and look at dumps/page.png "
                "to see what the browser showed."
            ),
        )

    def _attempt(self, pw, event_id: int, headed: bool, is_last: bool):
        """One launch+open+harvest; returns payloads, or None if nothing captured."""
        log.info("Launching %s browser ...", "visible" if headed else "headless")
        context = self._launch_context(pw, headed)
        try:
            context.add_init_script(_STEALTH_JS)
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(self._timeout_ms())

            responses: list = []
            req_headers: dict = {}

            def on_response(resp):
                try:
                    if EVENT_PAGE_PATH in resp.url:
                        responses.append(resp)
                except Exception:  # noqa: BLE001
                    pass

            def on_request(req):
                try:
                    if EVENT_PAGE_PATH in req.url and not req_headers:
                        req_headers.update(req.all_headers())
                except Exception:  # noqa: BLE001
                    pass

            page.on("response", on_response)
            page.on("request", on_request)

            self._open(page)
            harvested = self._wait_for_data(responses, event_id, headed)

            if self.config.dump_raw_dir:
                self._save_page_snapshot(page)
                if req_headers:
                    self._dump_headers(req_headers)

            if not harvested:
                return None

            replay = replayable_headers(req_headers)
            log.info(
                "Captured the app's own market request; replaying %d header(s) "
                "for the remaining tabs.",
                len(replay),
            )
            return self._collect(page, event_id, harvested, replay)
        finally:
            context.close()

    def _launch_context(self, pw, headed: bool):
        # A persistent profile means a solved "press & hold" challenge stays
        # solved across runs (the bot cookie is reused), so you rarely see it
        # more than once.
        profile = self._profile_dir()
        profile.mkdir(parents=True, exist_ok=True)
        common = {
            "user_data_dir": str(profile),
            "headless": not headed,
            "args": ["--disable-blink-features=AutomationControlled"],
            "locale": "en-US",
            "user_agent": api.BROWSER_HEADERS["User-Agent"],
            "viewport": {"width": 1366, "height": 900},
        }
        errors = []
        for channel in ("chrome", None):  # real Chrome first (least detectable)
            try:
                kwargs = dict(common)
                if channel:
                    kwargs["channel"] = channel
                return pw.chromium.launch_persistent_context(**kwargs)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{channel or 'bundled chromium'}: {exc}")
        raise ScraperError(
            "Could not launch a browser.\n" + "\n".join(errors),
            hint=(
                "Install the bundled browser once with:\n"
                "  python -m playwright install chromium\n"
                "(or install Google Chrome on this machine)."
            ),
        )

    def _profile_dir(self) -> Path:
        return self.config.profile_dir or (Path.home() / ".fanduel_scraper" / "chrome-profile")

    def _open(self, page) -> None:
        log.info("Opening %s ...", self.navigate_url)
        try:
            page.goto(self.navigate_url, wait_until="domcontentloaded", timeout=self._timeout_ms())
        except Exception as exc:  # noqa: BLE001
            log.warning("Page did not finish loading cleanly (%s); continuing.", exc)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:  # noqa: BLE001
            pass
        self._sleep(2.0)

    def _wait_for_data(self, responses, event_id: int, headed: bool) -> dict[str, str]:
        """Poll until the app fetches this event's markets. In a visible window
        this gives you time to solve a 'press & hold' / 'verify you're human'
        challenge by hand — once solved, the page loads and we capture it."""
        harvested = self._read_responses(responses, event_id)
        if harvested or self.config.solve_timeout <= 0:
            return harvested
        if headed:
            log.warning(
                "No market data yet. If the browser window shows a 'press & hold' or "
                "'verify you are human' challenge, solve it now — scraping continues "
                "automatically once the page loads (waiting up to %d seconds).",
                int(self.config.solve_timeout),
            )
        deadline = time.monotonic() + (self.config.solve_timeout if headed else 12.0)
        while time.monotonic() < deadline:
            self._sleep(1.5)
            harvested = self._read_responses(responses, event_id)
            if harvested:
                log.info("Market data captured.")
                return harvested
        return {}

    def _read_responses(self, responses, event_id: int) -> dict[str, str]:
        """Pull JSON bodies out of the event-page responses the app made, keeping
        only those for the event we asked for (a redirected page can fetch other
        events' data)."""
        harvested: dict[str, str] = {}
        for resp in responses:
            try:
                if getattr(resp, "status", 0) != 200:
                    continue
                if _event_id_param(resp.url) != event_id:
                    continue
                body = resp.text()
            except Exception:  # noqa: BLE001
                continue
            if not _looks_like_json(body):
                continue
            tab = _tab_param(resp.url) or self.config.default_tab
            harvested.setdefault(tab, body)
        return harvested

    # -- Orchestration (unit-tested via a fake page) ------------------------

    def _collect(
        self, page, event_id: int, harvested: dict[str, str], replay_headers: dict
    ) -> list[TabPayload]:
        payloads: list[TabPayload] = []
        index = 0
        for tab, body in harvested.items():
            payloads.append(TabPayload(tab, self._loads(body, tab)))
            self._dump(event_id, index, tab, body)
            index += 1

        first = payloads[0].payload
        _default_slug, tabs = parser.discover_tabs(first)
        have = {p.tab for p in payloads}
        remaining = [tab for tab in tabs if tab not in have]
        if remaining:
            log.info("Found %d more tab(s): %s", len(remaining), ", ".join(remaining))
        else:
            log.info("No additional tabs to fetch.")

        for tab in remaining:
            self._sleep(random.uniform(self.config.min_delay, self.config.max_delay))
            log.info("fetching tab '%s' ...", tab)
            try:
                payload = self._replay_fetch(page, event_id, tab, index, replay_headers)
            except GeoBlockedError:
                raise
            except ScraperError as exc:
                log.warning("Tab '%s' failed (%s); continuing without it.", tab, exc)
                index += 1
                continue
            payloads.append(TabPayload(tab, payload))
            index += 1
        return payloads

    def _replay_fetch(self, page, event_id: int, tab: str, index: int, headers: dict) -> dict:
        params = build_params(self.config.app_key, event_id, tab)
        url = f"{BASE_URL}?{urlencode(params)}"
        status, body = self._evaluate_fetch(page, url, headers)
        if body:
            self._dump(event_id, index, tab, body)
        if status in (403, 451):
            raise GeoBlockedError(
                f"FanDuel rejected the request (HTTP {status}).",
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
        if status == 200 and _looks_like_json(body):
            return self._loads(body, tab)
        raise NetworkError(f"tab {tab!r}: HTTP {status} {_snippet(body)!r}")

    def _evaluate_fetch(self, page, url: str, headers: dict) -> tuple[int, str]:
        try:
            result = page.evaluate(_FETCH_JS, {"url": url, "headers": headers})
        except Exception as exc:  # noqa: BLE001
            return 0, f"evaluate error: {exc}"
        if not isinstance(result, dict):
            return 0, ""
        return int(result.get("status") or 0), str(result.get("body") or "")

    def _loads(self, body: str, tab: str) -> dict:
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise SchemaDriftError(
                f"FanDuel returned malformed JSON for tab {tab!r}.",
                hint="Re-run with --dump-raw dumps/ and share the output files.",
            ) from exc

    # -- Dump helpers --------------------------------------------------------

    def _dump(self, event_id: int, index: int, tab: str, body: str) -> None:
        if self.config.dump_raw_dir and body:
            write_dump(self.config.dump_raw_dir, event_id, index, tab, body)

    def _dump_headers(self, headers: dict) -> None:
        try:
            self.config.dump_raw_dir.mkdir(parents=True, exist_ok=True)
            path = self.config.dump_raw_dir / "app_request_headers.json"
            path.write_text(json.dumps(headers, indent=2, sort_keys=True), encoding="utf-8")
            log.info("Saved the app's request headers to %s", path)
        except Exception as exc:  # noqa: BLE001
            log.debug("Could not save request headers: %s", exc)

    def _save_page_snapshot(self, page) -> None:
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
