"""Multi-tab merge/dedup over all three fixtures together."""

from fanduel_scraper.parser import parse_event

EVENT_ID = 33840322


def test_merge_counts(all_tab_payloads):
    result = parse_event(all_tab_payloads, EVENT_ID, "2026-06-11T18:00:00Z")
    stats = result.stats
    assert stats.tabs_fetched == 3
    assert stats.markets_total == 12
    assert stats.duplicate_markets_merged == 1  # Moneyline repeats on player-props
    assert stats.markets_skipped_no_runners == 1
    assert stats.selections_total == len(result.rows) == 17
    assert stats.runners_with_unknown_odds == 2


def test_first_tab_wins_attribution(all_tab_payloads):
    result = parse_event(all_tab_payloads, EVENT_ID, "t")
    moneyline_rows = [r for r in result.rows if r.market_id == "1.100000001"]
    assert len(moneyline_rows) == 2
    assert all(r.tab == "default" for r in moneyline_rows)


def test_same_name_different_id_markets_both_kept(all_tab_payloads):
    result = parse_event(all_tab_payloads, EVENT_ID, "t")
    milestone_ids = {
        r.market_id for r in result.rows if r.market_name == "Player Points Milestones"
    }
    assert milestone_ids == {"1.100000008", "1.100000009"}


def test_rows_grouped_in_tab_order(all_tab_payloads):
    result = parse_event(all_tab_payloads, EVENT_ID, "t")
    tab_sequence = [r.tab for r in result.rows]
    # Once a later tab starts, earlier tabs never reappear (stable grouping).
    seen = []
    for tab in tab_sequence:
        if tab not in seen:
            seen.append(tab)
    assert seen == ["default", "player-props", "game-props"]
    assert tab_sequence == sorted(tab_sequence, key=seen.index)
