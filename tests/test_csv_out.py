import csv

from fanduel_scraper.csv_out import write_csv
from fanduel_scraper.models import HEADER, TabPayload
from fanduel_scraper.parser import parse_event

EVENT_ID = 33840322


def test_csv_roundtrip(tmp_path, load_fixture):
    result = parse_event(
        [TabPayload("default", load_fixture("33840322_00_default.json"))],
        EVENT_ID,
        "2026-06-11T18:00:00Z",
    )
    out = tmp_path / "snapshot.csv"
    write_csv(out, result.rows)

    with out.open(newline="", encoding="utf-8") as fh:
        records = list(csv.reader(fh))
    assert records[0] == HEADER
    assert len(records) == 1 + 9  # header + every selection on the default tab

    def find(market_name, runner_name):
        return next(
            row
            for row in records[1:]
            if row[HEADER.index("market_name")] == market_name
            and row[HEADER.index("runner_name")] == runner_name
        )

    lakers_ml = find("Moneyline", "Los Angeles Lakers")
    assert lakers_ml[HEADER.index("american_odds")] == "240"
    # The SGP-only runner has blank odds cells, not "None".
    sgp_yes = find("Both Teams To Score 120+ Points", "Yes")
    assert sgp_yes[HEADER.index("american_odds")] == ""
    assert sgp_yes[HEADER.index("decimal_odds")] == ""
