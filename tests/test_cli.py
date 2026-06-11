"""End-to-end CLI behavior over the offline --from-dump path (no network)."""

import csv
from pathlib import Path

import pytest

from fanduel_scraper.cli import main

FIXTURE_DIR = (Path(__file__).parent / "fixtures" / "dump").resolve()
EVENT = "33840322"


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    # Ignore any real .env / env vars so tests don't accidentally route to Sheets.
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("GOOGLE_SPREADSHEET", raising=False)
    monkeypatch.delenv("FANDUEL_STATE", raising=False)


def test_default_output_is_csv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = main([EVENT, "--from-dump", str(FIXTURE_DIR)])
    assert rc == 0
    csvs = list(tmp_path.glob("*.csv"))
    assert len(csvs) == 1
    assert "33840322" in csvs[0].name
    with csvs[0].open(newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    assert rows[0][0] == "scrape_time_utc"
    assert len(rows) == 1 + 17  # header + every selection across all dumped tabs


def test_csv_flag_writes_to_given_path(tmp_path):
    out = tmp_path / "mybets.csv"
    rc = main([EVENT, "--from-dump", str(FIXTURE_DIR), "--csv", str(out)])
    assert rc == 0
    assert out.is_file()


def test_bad_event_ref_returns_exit_code_2():
    assert main(["not-an-id", "--from-dump", str(FIXTURE_DIR)]) == 2
