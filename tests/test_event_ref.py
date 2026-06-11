import pytest

from fanduel_scraper.errors import InvalidEventRefError
from fanduel_scraper.event_ref import parse_event_ref


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("33840322", 33840322),
        ("  33840322  ", 33840322),
        (
            "https://sportsbook.fanduel.com/basketball/nba/lakers-@-celtics-33840322",
            33840322,
        ),
        (
            "https://sportsbook.fanduel.com/basketball/nba/lakers-@-celtics-33840322?tab=player-props",
            33840322,
        ),
        (
            "https://sportsbook.fanduel.com/basketball/nba/lakers-@-celtics-33840322#totals",
            33840322,
        ),
        (
            "https://sportsbook.fanduel.com/basketball/nba/lakers-@-celtics-33840322/",
            33840322,
        ),
        (
            "https://sportsbook.fanduel.com/basketball/nba/lakers-%40-celtics-33840322",
            33840322,
        ),
        (
            "https://pa.sportsbook.fanduel.com/football/nfl/eagles-@-cowboys-987654",
            987654,
        ),
        ("https://sportsbook.fanduel.com/baseball/mlb/yankees-@-red-sox-12345?foo=1&bar=2", 12345),
    ],
)
def test_valid_refs(ref, expected):
    assert parse_event_ref(ref) == expected


@pytest.mark.parametrize(
    "ref",
    [
        "",
        "   ",
        "https://sportsbook.fanduel.com/basketball/nba",
        "not-a-url-and-not-an-id",
        "https://sportsbook.fanduel.com/basketball/nba/lakers-@-celtics-",
    ],
)
def test_invalid_refs(ref):
    with pytest.raises(InvalidEventRefError) as excinfo:
        parse_event_ref(ref)
    # The error must teach the user what valid input looks like.
    assert "33840322" in excinfo.value.hint


def test_invalid_ref_exit_code():
    with pytest.raises(InvalidEventRefError) as excinfo:
        parse_event_ref("garbage")
    assert excinfo.value.exit_code == 2
