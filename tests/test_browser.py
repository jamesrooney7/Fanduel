"""BrowserFetcher orchestration via a fake page — no Playwright, no network."""

import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from fanduel_scraper.browser import BrowserFetcher
from fanduel_scraper.config import Config
from fanduel_scraper.errors import GeoBlockedError

EVENT_ID = 33840322
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "dump"


def _fixture_text(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


def _tab_of(url: str):
    return parse_qs(urlparse(url).query).get("tab", [None])[0]


class FakePage:
    """Stands in for a Playwright page: .evaluate() returns canned tab bodies."""

    def __init__(self, failures: dict[str, int] | None = None):
        self.bodies = {
            "popular": _fixture_text("33840322_00_default.json"),
            "player-props": _fixture_text("33840322_01_player-props.json"),
            "game-props": _fixture_text("33840322_02_game-props.json"),
        }
        self.failures = failures or {}
        self.calls: list[str] = []

    def evaluate(self, _js, arg):
        tab = _tab_of(arg["url"])
        self.calls.append(tab)
        if tab in self.failures:
            return {"status": self.failures[tab], "body": ""}
        return {"status": 200, "body": self.bodies[tab]}


class NavigationOnlyPage(FakePage):
    """In-page fetch always fails (CORS-style HTTP 0); only navigation works."""

    def evaluate(self, _js, arg):
        self.calls.append(_tab_of(arg["url"]))
        return {"status": 0, "body": "fetch error: TypeError: Failed to fetch"}

    class _Resp:
        def __init__(self, status, body):
            self.status = status
            self._body = body

        def text(self):
            return self._body

    def goto(self, url, **_kwargs):
        tab = _tab_of(url)
        if tab in self.failures:
            return self._Resp(self.failures[tab], "")
        return self._Resp(200, self.bodies[tab])


def make_fetcher(**config_kwargs):
    config = Config(**config_kwargs)
    return BrowserFetcher(config, navigate_url="https://example.com", sleep=lambda s: None)


def test_collect_walks_all_tabs():
    fetcher = make_fetcher()
    page = FakePage()
    payloads = fetcher._collect(page, EVENT_ID)
    assert [p.tab for p in payloads] == ["popular", "player-props", "game-props"]
    assert page.calls == ["popular", "player-props", "game-props"]


def test_collect_skips_failing_tab():
    fetcher = make_fetcher()
    page = FakePage(failures={"player-props": 500})
    payloads = fetcher._collect(page, EVENT_ID)
    assert [p.tab for p in payloads] == ["popular", "game-props"]


def test_collect_aborts_on_geo_block():
    fetcher = make_fetcher()
    page = FakePage(failures={"player-props": 403})
    with pytest.raises(GeoBlockedError):
        fetcher._collect(page, EVENT_ID)


def test_falls_back_to_navigation_when_fetch_blocked():
    # Simulates the real-world CORS failure: in-page fetch() returns HTTP 0,
    # so the fetcher must fall back to navigating to the API URL.
    fetcher = make_fetcher()
    page = NavigationOnlyPage()
    payloads = fetcher._collect(page, EVENT_ID)
    assert [p.tab for p in payloads] == ["popular", "player-props", "game-props"]


def test_collect_produces_parseable_payloads():
    from fanduel_scraper.parser import parse_event

    fetcher = make_fetcher()
    payloads = fetcher._collect(FakePage(), EVENT_ID)
    result = parse_event(payloads, EVENT_ID, "2026-06-12T00:00:00Z")
    assert result.event.name == "Los Angeles Lakers @ Boston Celtics"
    assert result.stats.selections_total == 17


def test_dump_raw_written_in_browser_mode(tmp_path):
    fetcher = make_fetcher(dump_raw_dir=tmp_path)
    fetcher._collect(FakePage(), EVENT_ID)
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == [
        "33840322_00_popular.json",
        "33840322_01_player-props.json",
        "33840322_02_game-props.json",
    ]
    json.loads((tmp_path / "33840322_00_popular.json").read_text(encoding="utf-8"))
