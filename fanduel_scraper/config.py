"""Configuration with precedence: CLI flags > process env > .env file > defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_APP_KEY = "FhMFpcPWXMeyZxOx"  # public key embedded in FanDuel's web client


@dataclass(frozen=True)
class Config:
    state: str = "nj"
    app_key: str = DEFAULT_APP_KEY
    timezone: str = "America/New_York"
    service_account_file: Path = Path("service_account.json")
    spreadsheet: str | None = None
    request_timeout: float = 20.0
    min_delay: float = 0.5
    max_delay: float = 1.5
    dump_raw_dir: Path | None = None
    from_dump_dir: Path | None = None
    csv_path: Path | None = None
    verbose: bool = False


def load_config(args) -> Config:
    from dotenv import load_dotenv

    load_dotenv()  # process env vars win over .env (override=False is the default)

    def pick(cli_value, env_name: str, default):
        if cli_value is not None:
            return cli_value
        env_value = os.environ.get(env_name)
        if env_value:
            return env_value
        return default

    return Config(
        state=str(pick(args.state, "FANDUEL_STATE", "nj")).strip().lower(),
        app_key=str(pick(None, "FANDUEL_APP_KEY", DEFAULT_APP_KEY)),
        timezone=str(pick(args.timezone, "FANDUEL_TIMEZONE", "America/New_York")),
        service_account_file=Path(
            pick(args.service_account, "GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
        ),
        spreadsheet=pick(args.spreadsheet, "GOOGLE_SPREADSHEET", None),
        dump_raw_dir=Path(args.dump_raw) if args.dump_raw else None,
        from_dump_dir=Path(args.from_dump) if args.from_dump else None,
        csv_path=Path(args.csv) if args.csv else None,
        verbose=bool(args.verbose),
    )
