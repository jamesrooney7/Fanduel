import json
from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "dump"

FIXTURE_FILES = [
    "33840322_00_default.json",
    "33840322_01_player-props.json",
    "33840322_02_game-props.json",
]


@pytest.fixture
def fixture_dir() -> Path:
    return FIXTURE_DIR


@pytest.fixture
def load_fixture():
    def _load(name: str) -> dict:
        return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))

    return _load


@pytest.fixture
def all_tab_payloads(load_fixture):
    """The three fixtures as TabPayloads, in dump order (default first)."""
    from fanduel_scraper.models import TabPayload

    return [
        TabPayload("default", load_fixture(FIXTURE_FILES[0])),
        TabPayload("player-props", load_fixture(FIXTURE_FILES[1])),
        TabPayload("game-props", load_fixture(FIXTURE_FILES[2])),
    ]
