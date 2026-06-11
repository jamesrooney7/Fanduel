"""Write a snapshot into a Google Spreadsheet via a service account (gspread).

The whole snapshot goes up in ONE values update (plus the worksheet creation),
so a run uses 2-3 write calls — far under Google's ~60 writes/min quota.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path

from .errors import SheetsAccessError, SheetsAuthError, SheetsError, SheetsQuotaError
from .models import HEADER, EventInfo, SelectionRow, row_to_cells

log = logging.getLogger(__name__)

_SPREADSHEET_URL_RE = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]+)")
_BARE_ID_RE = re.compile(r"[A-Za-z0-9_-]{20,}")
_FORBIDDEN_TITLE_CHARS = re.compile(r"[\[\]:*?/\\]")
_MAX_TITLE_LEN = 90


def extract_spreadsheet_id(ref: str) -> str:
    """Accept a full docs.google.com URL or a bare spreadsheet ID."""
    text = (ref or "").strip()
    if not text:
        raise SheetsAccessError(
            "No spreadsheet configured.",
            hint=(
                "Set GOOGLE_SPREADSHEET in .env (full URL or just the ID) "
                "or pass --spreadsheet."
            ),
        )
    match = _SPREADSHEET_URL_RE.search(text)
    if match:
        return match.group(1)
    if _BARE_ID_RE.fullmatch(text):
        return text
    raise SheetsAccessError(
        f"Could not understand spreadsheet reference {ref!r}.",
        hint=(
            "Pass either the full https://docs.google.com/spreadsheets/d/... URL "
            "or the bare spreadsheet ID."
        ),
    )


def worksheet_title(event_name: str, when_local: datetime) -> str:
    """'Lakers @ Celtics — 2026-06-11 18.30', sanitized for Sheets tab rules."""
    name = _FORBIDDEN_TITLE_CHARS.sub(" ", event_name or "Event")
    name = re.sub(r"\s+", " ", name).strip() or "Event"
    stamp = when_local.strftime("%Y-%m-%d %H.%M")
    title = f"{name} — {stamp}"
    if len(title) > _MAX_TITLE_LEN:
        keep = _MAX_TITLE_LEN - len(stamp) - 4  # "… — " around the stamp
        title = f"{name[:keep].rstrip()}… — {stamp}"
    return title


def _read_client_email(service_account_file: Path) -> str:
    if not service_account_file.is_file():
        raise SheetsAuthError(
            f"Google service account file not found at '{service_account_file}'.",
            hint=(
                'Follow the "Google Sheets setup (one time, ~5 min)" section of the '
                "README, or pass --service-account /path/to/service_account.json."
            ),
        )
    try:
        data = json.loads(service_account_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SheetsAuthError(
            f"Could not read service account file '{service_account_file}': {exc}",
            hint="It should be the unmodified JSON key file downloaded from Google Cloud.",
        ) from exc
    email = data.get("client_email", "")
    if not email:
        raise SheetsAuthError(
            f"'{service_account_file}' has no client_email — is it really a "
            "service-account key?",
            hint=(
                "Re-download the JSON key from Google Cloud "
                "(IAM & Admin → Service Accounts → Keys)."
            ),
        )
    return email


class SheetsWriter:
    def __init__(self, service_account_file: Path, spreadsheet_ref: str):
        self.spreadsheet_id = extract_spreadsheet_id(spreadsheet_ref)
        self.client_email = _read_client_email(service_account_file)
        try:
            import gspread
        except ImportError as exc:
            raise SheetsAuthError(
                "gspread is not installed.",
                hint="Run: pip install -r requirements.txt",
            ) from exc
        self._gspread = gspread
        try:
            self._client = gspread.service_account(filename=str(service_account_file))
        except Exception as exc:
            raise SheetsAuthError(
                f"Could not authenticate with the Google service account ({exc}).",
                hint=(
                    f"Check that {service_account_file} is the JSON key downloaded "
                    "from Google Cloud (see README)."
                ),
            ) from exc

    def write_snapshot(
        self, event: EventInfo, rows: list[SelectionRow], when_local: datetime
    ) -> str:
        """Create a new pre-sized worksheet and write header + all rows in one call.

        Returns the worksheet URL.
        """
        spreadsheet = self._open_spreadsheet()
        title = worksheet_title(event.name, when_local)
        values = [HEADER] + [row_to_cells(row) for row in rows]
        worksheet = self._add_worksheet(
            spreadsheet, title, rows=len(values) + 1, cols=len(HEADER)
        )
        try:
            worksheet.update(values=values, range_name="A1", value_input_option="RAW")
        except self._gspread.exceptions.APIError as exc:
            raise self._classify_api_error(exc) from exc
        try:
            worksheet.freeze(rows=1)
        except Exception:
            log.debug("Could not freeze the header row (cosmetic only).")
        return (
            f"https://docs.google.com/spreadsheets/d/{self.spreadsheet_id}"
            f"/edit#gid={worksheet.id}"
        )

    def _open_spreadsheet(self):
        try:
            return self._client.open_by_key(self.spreadsheet_id)
        except self._gspread.exceptions.SpreadsheetNotFound as exc:
            raise SheetsAccessError(
                "Could not open the spreadsheet (not found).",
                hint=(
                    "Either the ID/URL is wrong, or the sheet is not shared with the "
                    "service account.\nOpen the spreadsheet and share it (Editor) "
                    f"with: {self.client_email}"
                ),
            ) from exc
        except self._gspread.exceptions.APIError as exc:
            raise self._classify_api_error(exc) from exc

    def _add_worksheet(self, spreadsheet, title: str, rows: int, cols: int):
        for suffix in [""] + [f" ({n})" for n in range(2, 10)]:
            try:
                return spreadsheet.add_worksheet(title=title + suffix, rows=rows, cols=cols)
            except self._gspread.exceptions.APIError as exc:
                if "already exists" in str(exc).lower():
                    continue  # two snapshots in the same minute — try ' (2)', ' (3)', ...
                raise self._classify_api_error(exc) from exc
        raise SheetsError(
            f"Could not create a worksheet named like {title!r} (too many name collisions)."
        )

    def _classify_api_error(self, exc) -> SheetsError:
        status = None
        try:
            status = exc.response.status_code
        except Exception:
            pass
        if status == 429:
            return SheetsQuotaError(
                "Google Sheets API quota hit (HTTP 429).",
                hint=(
                    "Wait ~60 seconds and re-run. (The limit is ~60 write calls/min; "
                    "this tool uses 2-3 per run.)"
                ),
            )
        if status in (403, 404):
            return SheetsAccessError(
                f"Could not access the spreadsheet (HTTP {status}).",
                hint=(
                    "Either the ID/URL is wrong, or the sheet is not shared with the "
                    "service account.\nOpen the spreadsheet and share it (Editor) "
                    f"with: {self.client_email}"
                ),
            )
        return SheetsError(f"Google Sheets API error: {exc}")
