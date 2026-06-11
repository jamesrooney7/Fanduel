"""FanDuelClient behavior with an injected fake transport — no network, no curl_cffi."""

import json
from pathlib import Path

import pytest

from fanduel_scraper.api import (
    RETRY_BACKOFF,
    FanDuelClient,
    build_params,
    classify_http_error,
    load_dump,
)
from fanduel_scraper.config import Config
from fanduel_scraper.errors import (
    EventNotFoundError,
    GeoBlockedError,
    NetworkError,
    ScraperError,
)

EVENT_ID = 33840322


class FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


def make_client(transport):
    sleeps = []
    client = FanDuelClient(Config(), sleep=sleeps.append, transport=transport)
    return client, sleeps


def test_build_params_default_tab():
    params = build_params("KEY", EVENT_ID, "America/New_York", tab=None)
    assert params == {
        "_ak": "KEY",
        "eventId": "33840322",
        "betexRegion": "GBR",
        "capiJurisdiction": "intl",
        "currencyCode": "USD",
        "exchangeLocale": "en_US",
        "includePrices": "true",
        "includeRawMarkets": "false",
        "includeSuspended": "true",
        "language": "en",
        "regionCode": "NAMERICA",
        "timezone": "America/New_York",
    }


def test_build_params_with_tab():
    params = build_params("KEY", EVENT_ID, "America/New_York", tab="player-props")
    assert params["tab"] == "player-props"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (403, GeoBlockedError),
        (451, GeoBlockedError),
        (404, EventNotFoundError),
        (500, NetworkError),
    ],
)
def test_classify_http_error(status, expected):
    assert isinstance(classify_http_error(status, "", "nj"), expected)


def test_retries_then_success():
    calls = []

    def transport(url, params, timeout):
        calls.append(params)
        if len(calls) < 3:
            raise TimeoutError("boom")
        return FakeResponse(200, '{"ok": true}')

    client, sleeps = make_client(transport)
    body = client._request_with_retries({})
    assert json.loads(body) == {"ok": True}
    assert len(calls) == 3
    # Two backoff sleeps with +/-20% jitter around 1s and 2s.
    assert len(sleeps) == 2
    assert 0.8 <= sleeps[0] <= 1.2
    assert 1.6 <= sleeps[1] <= 2.4


def test_no_retry_on_geo_block():
    calls = []

    def transport(url, params, timeout):
        calls.append(1)
        return FakeResponse(403, "Forbidden")

    client, sleeps = make_client(transport)
    with pytest.raises(GeoBlockedError):
        client._request_with_retries({})
    assert len(calls) == 1
    assert sleeps == []


def test_all_attempts_fail():
    calls = []

    def transport(url, params, timeout):
        calls.append(1)
        raise ConnectionError("no route")

    client, sleeps = make_client(transport)
    with pytest.raises(NetworkError):
        client._request_with_retries({})
    assert len(calls) == len(RETRY_BACKOFF) + 1
    assert len(sleeps) == len(RETRY_BACKOFF)


def test_429_waits_longer():
    def transport(url, params, timeout):
        return FakeResponse(429, "slow down")

    client, sleeps = make_client(transport)
    with pytest.raises(NetworkError):
        client._request_with_retries({})
    assert 10.0 in sleeps


def _fixture_text(name: str) -> str:
    path = Path(__file__).parent / "fixtures" / "dump" / name
    return path.read_text(encoding="utf-8")


def make_tab_transport(failures: dict[str, FakeResponse] | None = None):
    """Transport that serves fixture bodies keyed by the requested tab."""
    bodies = {
        None: _fixture_text("33840322_00_default.json"),
        "popular": _fixture_text("33840322_00_default.json"),
        "player-props": _fixture_text("33840322_01_player-props.json"),
        "game-props": _fixture_text("33840322_02_game-props.json"),
    }
    failures = failures or {}
    calls = []

    def transport(url, params, timeout):
        tab = params.get("tab")
        calls.append(tab)
        if tab in failures:
            return failures[tab]
        return FakeResponse(200, bodies[tab])

    transport.calls = calls
    return transport


def test_fetch_event_walks_all_tabs():
    transport = make_tab_transport()
    client, sleeps = make_client(transport)
    payloads = client.fetch_event(EVENT_ID)
    # default fetch + 3 discovered tabs (no default marker -> all fetched)
    assert [p.tab for p in payloads] == ["default", "popular", "player-props", "game-props"]
    assert transport.calls == [None, "popular", "player-props", "game-props"]
    # One polite jittered delay before each tab request.
    assert len(sleeps) == 3
    assert all(0.5 <= s <= 1.5 for s in sleeps)


def test_fetch_event_skips_failing_secondary_tab():
    transport = make_tab_transport(failures={"player-props": FakeResponse(500, "oops")})
    client, sleeps = make_client(transport)
    payloads = client.fetch_event(EVENT_ID)
    assert [p.tab for p in payloads] == ["default", "popular", "game-props"]


def test_fetch_event_aborts_on_geo_block_mid_walk():
    transport = make_tab_transport(failures={"player-props": FakeResponse(403, "blocked")})
    client, _ = make_client(transport)
    with pytest.raises(GeoBlockedError):
        client.fetch_event(EVENT_ID)


def test_dump_raw_writes_files(tmp_path):
    transport = make_tab_transport()
    client = FanDuelClient(
        Config(dump_raw_dir=tmp_path), sleep=lambda s: None, transport=transport
    )
    client.fetch_event(EVENT_ID)
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == [
        "33840322_00_default.json",
        "33840322_01_popular.json",
        "33840322_02_player-props.json",
        "33840322_03_game-props.json",
    ]
    # Dumps are the exact bytes FanDuel sent, so they re-parse as JSON.
    json.loads((tmp_path / "33840322_00_default.json").read_text(encoding="utf-8"))


def test_load_dump_roundtrip(tmp_path):
    fixture_dir = Path(__file__).parent / "fixtures" / "dump"
    payloads = load_dump(fixture_dir, EVENT_ID)
    assert [p.tab for p in payloads] == ["default", "player-props", "game-props"]
    assert payloads[0].payload["attachments"]["events"]["33840322"]["eventId"] == EVENT_ID


def test_load_dump_wrong_event(tmp_path):
    fixture_dir = Path(__file__).parent / "fixtures" / "dump"
    with pytest.raises(ScraperError):
        load_dump(fixture_dir, 99999999)


def test_load_dump_missing_dir(tmp_path):
    with pytest.raises(ScraperError):
        load_dump(tmp_path / "nope", EVENT_ID)
