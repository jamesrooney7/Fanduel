"""BrowserFetcher replay/merge orchestration via a fake page — no Playwright."""

import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from fanduel_scraper.browser import BrowserFetcher, replayable_headers
from fanduel_scraper.config import Config
from fanduel_scraper.errors import GeoBlockedError

EVENT_ID = 33840322
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "dump"


def _fixture_text(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


BODIES = {
    "popular": _fixture_text("33840322_00_default.json"),
    "player-props": _fixture_text("33840322_01_player-props.json"),
    "game-props": _fixture_text("33840322_02_game-props.json"),
}


def _tab_of(url: str):
    return parse_qs(urlparse(url).query).get("tab", [None])[0]


class FakePage:
    """Stands in for a Playwright page: .evaluate() replays a tab fetch."""

    def __init__(self, failures: dict[str, int] | None = None):
        self.failures = failures or {}
        self.calls: list[str] = []

    def evaluate(self, _js, arg):
        tab = _tab_of(arg["url"])
        self.calls.append(tab)
        if tab in self.failures:
            return {"status": self.failures[tab], "body": ""}
        return {"status": 200, "body": BODIES[tab]}


def make_fetcher(**config_kwargs):
    config = Config(**config_kwargs)
    return BrowserFetcher(config, navigate_url="https://example.com", sleep=lambda s: None)


def test_replayable_headers_filters_and_keeps():
    raw = {
        "authorization": "Bearer xyz",
        "x-px-token": "tok",
        "accept": "application/json",
        "cookie": "secret",
        "user-agent": "Chrome",
        "sec-fetch-mode": "cors",
        "origin": "https://az.sportsbook.fanduel.com",
    }
    out = replayable_headers(raw)
    assert out["authorization"] == "Bearer xyz"
    assert out["x-px-token"] == "tok"
    assert "cookie" not in out
    assert "user-agent" not in out
    assert "sec-fetch-mode" not in out
    assert "origin" not in out


class FakeResponse:
    def __init__(self, url, status, body):
        self.url = url
        self.status = status
        self._body = body

    def text(self):
        return self._body


def test_read_responses_keeps_only_target_event():
    api_base = "https://api.sportsbook.fanduel.com/sbapi/event-page"
    responses = [
        FakeResponse(f"{api_base}?eventId=999&tab=popular", 200, '{"other": true}'),
        FakeResponse(f"{api_base}?eventId={EVENT_ID}&tab=popular", 200, BODIES["popular"]),
        FakeResponse(f"{api_base}?eventId={EVENT_ID}&tab=goals", 500, ""),  # non-200 ignored
    ]
    harvested = make_fetcher()._read_responses(responses, EVENT_ID)
    assert set(harvested) == {"popular"}
    assert harvested["popular"] == BODIES["popular"]


def test_collect_replays_remaining_tabs():
    # Only the default tab was harvested from the app; the rest are replayed.
    fetcher = make_fetcher()
    page = FakePage()
    payloads = fetcher._collect(page, EVENT_ID, {"popular": BODIES["popular"]}, {"Accept": "x"})
    assert [p.tab for p in payloads] == ["popular", "player-props", "game-props"]
    assert page.calls == ["player-props", "game-props"]  # popular not re-fetched


def test_collect_uses_harvested_without_refetch():
    fetcher = make_fetcher()
    page = FakePage()
    harvested = {k: BODIES[k] for k in ("popular", "player-props", "game-props")}
    payloads = fetcher._collect(page, EVENT_ID, harvested, {})
    assert [p.tab for p in payloads] == ["popular", "player-props", "game-props"]
    assert page.calls == []  # everything already harvested; no replay needed


def test_collect_skips_failing_replay_tab():
    fetcher = make_fetcher()
    page = FakePage(failures={"player-props": 500})
    payloads = fetcher._collect(page, EVENT_ID, {"popular": BODIES["popular"]}, {})
    assert [p.tab for p in payloads] == ["popular", "game-props"]


def test_collect_aborts_on_geo_block():
    fetcher = make_fetcher()
    page = FakePage(failures={"player-props": 403})
    with pytest.raises(GeoBlockedError):
        fetcher._collect(page, EVENT_ID, {"popular": BODIES["popular"]}, {})


def test_collect_produces_parseable_payloads():
    from fanduel_scraper.parser import parse_event

    fetcher = make_fetcher()
    payloads = fetcher._collect(FakePage(), EVENT_ID, {"popular": BODIES["popular"]}, {})
    result = parse_event(payloads, EVENT_ID, "2026-06-12T00:00:00Z")
    assert result.event.name == "Los Angeles Lakers @ Boston Celtics"
    assert result.stats.selections_total == 17


def test_dump_raw_written_in_browser_mode(tmp_path):
    fetcher = make_fetcher(dump_raw_dir=tmp_path)
    fetcher._collect(FakePage(), EVENT_ID, {"popular": BODIES["popular"]}, {})
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == [
        "33840322_00_popular.json",
        "33840322_01_player-props.json",
        "33840322_02_game-props.json",
    ]
    json.loads((tmp_path / "33840322_00_popular.json").read_text(encoding="utf-8"))
