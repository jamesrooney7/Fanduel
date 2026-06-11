import pytest

from fanduel_scraper.errors import EventNotFoundError, SchemaDriftError
from fanduel_scraper.models import TabPayload
from fanduel_scraper.parser import (
    dig,
    discover_tabs,
    extract_american_odds,
    extract_decimal_odds,
    parse_event,
    parse_event_info,
)

EVENT_ID = 33840322


def default_payloads(load_fixture):
    return [TabPayload("default", load_fixture("33840322_00_default.json"))]


def test_dig():
    data = {"a": {"b": {"c": 1}}}
    assert dig(data, "a", "b", "c") == 1
    assert dig(data, "a", "x", "c") is None
    assert dig(data, "a", "b", "c", "d", default="fallback") == "fallback"
    assert dig(None, "a") is None


def test_parse_event_info(load_fixture):
    info = parse_event_info(load_fixture("33840322_00_default.json"), EVENT_ID)
    assert info.name == "Los Angeles Lakers @ Boston Celtics"
    assert info.open_date_utc == "2026-06-12T00:30:00.000Z"
    assert info.event_id == EVENT_ID


def test_event_missing_raises_event_not_found():
    payload = {"attachments": {"events": {}, "markets": {"1.1": {}}}}
    with pytest.raises(EventNotFoundError):
        parse_event_info(payload, EVENT_ID)


def test_default_tab_without_markets_is_schema_drift(load_fixture):
    payload = load_fixture("33840322_00_default.json")
    del payload["attachments"]["markets"]
    with pytest.raises(SchemaDriftError) as excinfo:
        parse_event([TabPayload("default", payload)], EVENT_ID, "2026-06-11T18:00:00Z")
    assert "--dump-raw" in excinfo.value.hint


def test_default_tab_row_and_market_counts(load_fixture):
    result = parse_event(default_payloads(load_fixture), EVENT_ID, "2026-06-11T18:00:00Z")
    stats = result.stats
    assert stats.tabs_fetched == 1
    assert stats.markets_total == 6
    assert stats.markets_skipped_no_runners == 1  # Series Correct Score has no runners
    assert stats.selections_total == len(result.rows) == 9
    assert stats.runners_with_unknown_odds == 1  # SGP-only runner without winRunnerOdds


def test_moneyline_row_fields(load_fixture):
    result = parse_event(default_payloads(load_fixture), EVENT_ID, "2026-06-11T18:00:00Z")
    row = next(r for r in result.rows if r.market_name == "Moneyline" and "Lakers" in r.runner_name)
    assert row.scrape_time_utc == "2026-06-11T18:00:00Z"
    assert row.event_id == EVENT_ID
    assert row.event_name == "Los Angeles Lakers @ Boston Celtics"
    assert row.event_start_utc == "2026-06-12T00:30:00.000Z"
    assert row.tab == "default"
    assert row.market_id == "1.100000001"
    assert row.market_type == "MONEY_LINE"
    assert row.market_status == "OPEN"
    assert row.selection_id == 510001
    assert row.american_odds == 240
    assert row.decimal_odds == 3.4
    assert row.runner_status == "ACTIVE"


def test_spread_row_handicaps(load_fixture):
    result = parse_event(default_payloads(load_fixture), EVENT_ID, "t")
    spread_rows = [r for r in result.rows if r.market_name == "Spread Betting"]
    assert sorted(r.handicap for r in spread_rows) == [-6.5, 6.5]
    assert all(r.american_odds == -110 for r in spread_rows)
    # trueOdds is preferred over the rounded display value
    assert all(r.decimal_odds == 1.9091 for r in spread_rows)


def test_total_rows(load_fixture):
    result = parse_event(default_payloads(load_fixture), EVENT_ID, "t")
    over = next(r for r in result.rows if r.runner_name == "Over 224.5")
    under = next(r for r in result.rows if r.runner_name == "Under 224.5")
    assert over.handicap == under.handicap == 224.5
    assert over.american_odds == -112
    assert under.american_odds == -108


def test_suspended_market_is_data_not_filtered(load_fixture):
    result = parse_event(default_payloads(load_fixture), EVENT_ID, "t")
    suspended = [r for r in result.rows if r.market_name == "Race To 20 Points"]
    assert len(suspended) == 2
    assert all(r.market_status == "SUSPENDED" for r in suspended)
    assert all(r.runner_status == "SUSPENDED" for r in suspended)
    # Odds present in the payload are still extracted for suspended runners.
    assert {r.american_odds for r in suspended} == {184, -240}


def test_sgp_only_runner_gets_blank_odds(load_fixture):
    result = parse_event(default_payloads(load_fixture), EVENT_ID, "t")
    row = next(r for r in result.rows if r.market_name == "Both Teams To Score 120+ Points")
    assert row.american_odds is None
    assert row.decimal_odds is None
    assert row.runner_name == "Yes"


def test_string_odds_coercion(load_fixture):
    payloads = [TabPayload("player-props", load_fixture("33840322_01_player-props.json"))]
    result = parse_event(payloads, EVENT_ID, "t")
    over = next(r for r in result.rows if r.runner_name == "Over 27.5")
    assert over.american_odds == -110  # from string "-110"
    assert over.decimal_odds == 1.9091  # from string "1.9091"
    plus = next(r for r in result.rows if r.runner_name == "LeBron James 30+ Points")
    assert plus.american_odds == 105  # from string "+105"


def test_american_odds_int_fallback(load_fixture):
    payloads = [TabPayload("player-props", load_fixture("33840322_01_player-props.json"))]
    result = parse_event(payloads, EVENT_ID, "t")
    row = next(r for r in result.rows if r.runner_name == "Anthony Davis 30+ Points")
    assert row.american_odds == 150  # only americanOddsInt present


def test_decimal_display_fallback(load_fixture):
    payloads = [TabPayload("player-props", load_fixture("33840322_01_player-props.json"))]
    result = parse_event(payloads, EVENT_ID, "t")
    row = next(
        r for r in result.rows if r.market_name == "LeBron James To Record A Triple-Double"
    )
    assert row.american_odds is None
    assert row.decimal_odds == 5.5
    # decimal present -> not counted as unknown odds
    assert result.stats.runners_with_unknown_odds == 0


def test_unrecognized_odds_shape(load_fixture):
    payloads = [TabPayload("game-props", load_fixture("33840322_02_game-props.json"))]
    result = parse_event(payloads, EVENT_ID, "t")
    row = next(r for r in result.rows if r.market_name == "Winning Margin")
    assert row.american_odds is None
    assert row.decimal_odds is None
    assert result.stats.runners_with_unknown_odds == 1


def test_extract_odds_on_empty_runner():
    assert extract_american_odds({}) is None
    assert extract_decimal_odds({}) is None


class TestDiscoverTabs:
    def test_real_shape_uses_title_slugs_in_display_order(self, load_fixture):
        default_tab, tabs = discover_tabs(load_fixture("33840322_00_default.json"))
        # Slugs come from each tab's TITLE (not its numeric id), ordered by
        # tabsDisplayOrder, with defaultTab (id 2 -> "Popular") identified.
        assert tabs == ["popular", "player-props", "game-props"]
        assert default_tab == "popular"

    def test_real_world_soccer_layout(self):
        # Trimmed from a real FanDuel World Cup event-page response.
        payload = {
            "layout": {
                "defaultTab": 2,
                "tabsDisplayOrder": [2, 242, 183, 43, 387, 118, 72, 321, 119, 211, 120, 160, 282, 184],
                "tabs": {
                    "2": {"id": 2, "title": "Popular"},
                    "43": {"id": 43, "title": "Goals"},
                    "72": {"id": 72, "title": "Half"},
                    "118": {"id": 118, "title": "Team Props"},
                    "119": {"id": 119, "title": "Shots"},
                    "120": {"id": 120, "title": "Corners"},
                    "160": {"id": 160, "title": "Cards + Fouls"},
                    "183": {"id": 183, "title": "Goal Scorer"},
                    "184": {"id": 184, "title": "Penalties"},
                    "211": {"id": 211, "title": "Assists"},
                    "242": {"id": 242, "title": "Same Game Parlay™"},
                    "282": {"id": 282, "title": "Saves"},
                    "321": {"id": 321, "title": "Shots on Target"},
                    "387": {"id": 387, "title": "Quick Bets"},
                },
            }
        }
        default_tab, tabs = discover_tabs(payload)
        assert default_tab == "popular"
        assert tabs == [
            "popular", "same-game-parlay", "goal-scorer", "goals", "quick-bets",
            "team-props", "half", "shots-on-target", "shots", "assists",
            "corners", "cards-fouls", "saves", "penalties",
        ]

    def test_tabs_missing_from_display_order_are_appended(self):
        payload = {
            "layout": {
                "defaultTab": 2,
                "tabsDisplayOrder": [2],
                "tabs": {
                    "2": {"title": "Popular"},
                    "43": {"title": "Goals"},
                },
            }
        }
        assert discover_tabs(payload) == ("popular", ["popular", "goals"])

    def test_list_shape_fallback(self):
        payload = {"layout": {"tabs": ["Popular", "Player Props", "Popular"]}}
        assert discover_tabs(payload) == (None, ["popular", "player-props"])

    def test_unknown_shape_falls_back_to_single_tab(self):
        assert discover_tabs({"layout": {"tabs": 42}}) == (None, [])
        assert discover_tabs({}) == (None, [])
        assert discover_tabs({"layout": {}}) == (None, [])
