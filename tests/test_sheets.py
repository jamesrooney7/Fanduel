"""Pure sheets helpers — no gspread import, no network."""

from datetime import datetime
from pathlib import Path

import pytest

from fanduel_scraper.errors import SheetsAccessError, SheetsAuthError
from fanduel_scraper.models import HEADER, SelectionRow, row_to_cells
from fanduel_scraper.sheets import (
    _read_client_email,
    extract_spreadsheet_id,
    worksheet_title,
)

SHEET_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcdef"


class TestExtractSpreadsheetId:
    def test_full_url(self):
        url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid=0"
        assert extract_spreadsheet_id(url) == SHEET_ID

    def test_url_without_suffix(self):
        url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}"
        assert extract_spreadsheet_id(url) == SHEET_ID

    def test_bare_id(self):
        assert extract_spreadsheet_id(SHEET_ID) == SHEET_ID

    @pytest.mark.parametrize("bad", ["", "  ", "mysheet", "https://example.com/x"])
    def test_rejects_garbage(self, bad):
        with pytest.raises(SheetsAccessError):
            extract_spreadsheet_id(bad)


class TestWorksheetTitle:
    WHEN = datetime(2026, 6, 11, 18, 30)

    def test_basic(self):
        title = worksheet_title("Los Angeles Lakers @ Boston Celtics", self.WHEN)
        assert title == "Los Angeles Lakers @ Boston Celtics — 2026-06-11 18.30"

    def test_strips_forbidden_chars(self):
        title = worksheet_title("Weird [Name]: a/b\\c *? game", self.WHEN)
        assert not set("[]:*?/\\") & set(title)
        assert "Weird Name" in title

    def test_truncates_long_names(self):
        title = worksheet_title("X" * 200, self.WHEN)
        assert len(title) <= 90
        assert title.endswith("— 2026-06-11 18.30")

    def test_empty_name(self):
        assert worksheet_title("", self.WHEN) == "Event — 2026-06-11 18.30"


class TestReadClientEmail:
    def test_missing_file(self, tmp_path):
        with pytest.raises(SheetsAuthError) as excinfo:
            _read_client_email(tmp_path / "service_account.json")
        assert "README" in excinfo.value.hint

    def test_valid_key(self, tmp_path):
        key = tmp_path / "sa.json"
        key.write_text('{"client_email": "bot@project.iam.gserviceaccount.com"}')
        assert _read_client_email(key) == "bot@project.iam.gserviceaccount.com"

    def test_json_without_email(self, tmp_path):
        key = tmp_path / "sa.json"
        key.write_text("{}")
        with pytest.raises(SheetsAuthError):
            _read_client_email(key)

    def test_not_json(self, tmp_path):
        key = tmp_path / "sa.json"
        key.write_text("not json at all")
        with pytest.raises(SheetsAuthError):
            _read_client_email(key)


def make_row(**overrides):
    base = dict(
        scrape_time_utc="2026-06-11T18:00:00Z",
        event_id=33840322,
        event_name="Los Angeles Lakers @ Boston Celtics",
        event_start_utc="2026-06-12T00:30:00.000Z",
        tab="default",
        market_id="1.100000001",
        market_name="Moneyline",
        market_type="MONEY_LINE",
        market_status="OPEN",
        selection_id=510001,
        runner_name="Los Angeles Lakers",
        handicap=None,
        american_odds=240,
        decimal_odds=3.4,
        runner_status="ACTIVE",
    )
    base.update(overrides)
    return SelectionRow(**base)


def test_row_to_cells_blanks_and_numbers():
    cells = row_to_cells(make_row())
    assert len(cells) == len(HEADER)
    assert cells[HEADER.index("handicap")] == ""  # None -> blank cell
    assert cells[HEADER.index("american_odds")] == 240  # numbers stay numeric
    assert cells[HEADER.index("decimal_odds")] == 3.4
    assert cells[HEADER.index("event_id")] == 33840322
