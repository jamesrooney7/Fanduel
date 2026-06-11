# FanDuel Game Scraper → Google Sheets

Point it at one FanDuel game and it grabs **every bet currently offered** on that
game — moneyline, spreads, totals, every player prop, game prop, and alternate
line, across every tab of the event page — and writes them as one tidy snapshot
into a tab of your Google Sheet.

```
$ python -m fanduel_scraper "https://sportsbook.fanduel.com/basketball/nba/lakers-@-celtics-33840322"
Fetching event 33840322 (default tab) ...
Found 6 additional tab(s): player-props, game-props, ...
Scraped 'Los Angeles Lakers @ Boston Celtics': 7 tab(s), 412 market(s), 1238 selection(s).
Wrote worksheet: https://docs.google.com/spreadsheets/d/.../edit#gid=183920114
```

Each run adds a **new worksheet** named for the game and the time you ran it, so
you can snapshot the same game repeatedly and compare.

---

## How it works (and the one big catch)

FanDuel's website is powered by an internal JSON API. This tool calls that same
API directly — no fragile HTML scraping, no headless browser — and gets back
fully structured odds. To get past FanDuel's bot defenses it uses
[`curl_cffi`](https://github.com/lexiforest/curl_cffi) to imitate a real
Chrome browser's network fingerprint, which is what makes the request look
legitimate.

**The catch: you must run this from a US residential internet connection in a
state where FanDuel operates.** FanDuel blocks VPNs, cloud servers, and
datacenter IPs. It will not work from a corporate VPN, an AWS/GCP box, or most
office networks. Run it from home.

> This is reverse-engineered from FanDuel's public web client, so the API shape
> can change without notice. If a run comes back looking wrong, see
> [Troubleshooting](#troubleshooting) — there's a `--dump-raw` mode built
> specifically to make fixes quick.

---

## Install

Requires **Python 3.10+**.

```bash
pip install -r requirements.txt
```

---

## Google Sheets setup (one time, ~5 minutes)

The tool writes to your spreadsheet through a Google **service account** — a
robot Google account with its own credentials. You create it once.

1. Go to the [Google Cloud Console](https://console.cloud.google.com/) and
   create a project (or pick an existing one).
2. Enable two APIs for that project (search each by name in the console, click
   **Enable**):
   - **Google Sheets API**
   - **Google Drive API**
3. Go to **IAM & Admin → Service Accounts → Create service account**. Give it
   any name (e.g. `fanduel-scraper`) and click **Done** — you don't need to
   grant it any project roles.
4. Open the new service account → **Keys** tab → **Add key → Create new key →
   JSON**. A `.json` file downloads.
5. Save that file as **`service_account.json`** in this project's folder.
   (It's already in `.gitignore`, so it won't be committed.)
6. Create a Google Sheet to receive the data (sign in as your normal account,
   e.g. `jamesrooney7@gmail.com`). A blank spreadsheet is fine.
7. **Share the spreadsheet with the service account.** Open
   `service_account.json`, copy the `client_email` value (it looks like
   `fanduel-scraper@your-project.iam.gserviceaccount.com`), click **Share** in
   the spreadsheet, paste that address, and give it **Editor** access.
   *This step is what trips people up — if the tool says it can't open the
   sheet, it's almost always because this share is missing. The error message
   prints the exact address to share with.*
8. Copy the spreadsheet's URL from your browser.

---

## Configure

```bash
cp .env.example .env
```

Edit `.env`:

| Key | What it is |
|---|---|
| `FANDUEL_STATE` | The state subdomain to query — use the state you're physically in (`nj`, `pa`, `mi`, `il`, `co`, `az`, `ny`, …). |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | Path to your key file (default `service_account.json`). |
| `GOOGLE_SPREADSHEET` | The spreadsheet URL (or just its ID) from step 8. |
| `FANDUEL_APP_KEY` | Leave commented out unless FanDuel rotates its public key (rare). |

Any of these can be overridden per run with a command-line flag.

---

## Usage

```bash
# From a full game URL (copy it straight from your browser):
python -m fanduel_scraper "https://sportsbook.fanduel.com/basketball/nba/lakers-@-celtics-33840322"

# From just the numeric event id (the number at the end of the URL):
python -m fanduel_scraper 33840322

# Override the state for this run:
python -m fanduel_scraper 33840322 --state pa

# Write to a specific spreadsheet instead of the one in .env:
python -m fanduel_scraper 33840322 --spreadsheet "https://docs.google.com/spreadsheets/d/.../edit"

# Save the raw API responses while scraping (for debugging — see below):
python -m fanduel_scraper 33840322 --dump-raw dumps/

# See all options:
python -m fanduel_scraper --help
```

On success you'll get a summary line and a link to the new worksheet. The
columns are:

`scrape_time_utc, event_id, event_name, event_start_utc, tab, market_id,
market_name, market_type, market_status, selection_id, runner_name, handicap,
american_odds, decimal_odds, runner_status`

Odds are written as numbers (e.g. `-110`, `3.4`) so you can sort and filter
them in Sheets. Suspended markets and selections are included with their status
in the `market_status` / `runner_status` columns — nothing is filtered out.

---

## Troubleshooting

| Symptom | What it means / what to do |
|---|---|
| **`error: FanDuel rejected the request (HTTP 403)`** | You're not on an accepted connection. Run from a **US residential** network in a **legal state**, turn off any **VPN**, and make sure `--state` matches where you are. Cloud/datacenter IPs are always blocked. |
| **`error: Could not open the spreadsheet`** | The sheet isn't shared with the service account (or the ID/URL is wrong). The message prints the `client_email` — share the sheet with that address as **Editor**. |
| **`error: Event ... was not found`** | The game may have ended or been removed, or the URL/ID is wrong. Open the game in your browser and copy the URL again. |
| **`error: ... did not match the expected shape` (schema drift)** | FanDuel changed their internal API. Re-run with `--dump-raw dumps/`, then send the JSON files from `dumps/` so the parser can be updated. As a stopgap you can still capture data locally with the hidden `--csv out.csv` flag. |
| **`error: Google Sheets API quota hit`** | You ran it many times in a minute. Wait ~60s and retry (one run uses only 2–3 write calls). |
| **Odds columns are blank for some bets** | Some selections (e.g. same-game-parlay-only markets) genuinely have no standalone price; those are included with blank odds. If *everything* is blank, it's likely schema drift — use `--dump-raw`. |
| **FanDuel changed its public key** | Uncomment `FANDUEL_APP_KEY` in `.env` and set the new value (find it in the network requests on sportsbook.fanduel.com). |

### Exit codes

`0` success · `2` bad event URL/ID · `3` geo-blocked (403) · `4` event not found ·
`5` schema drift · `6` Google Sheets problem · `7` network/connection problem.

---

## Notes & disclaimer

- For **personal use**. Automated scraping may conflict with FanDuel's Terms of
  Service — you are responsible for how you use this.
- It's polite by default: one snapshot per run, with short randomized delays
  between tab requests.
- Odds data is informational and can change second to second; a snapshot is a
  point in time, not a live feed.

---

## Development

```bash
python -m pytest          # full test suite (no network needed)
```

Tests run entirely against bundled fixtures in `tests/fixtures/dump/`, which are
saved in the exact format `--dump-raw` produces. That means the debugging loop
is:

1. User hits schema drift → re-runs with `--dump-raw dumps/`.
2. Sends the `dumps/` files.
3. We replay them offline with the hidden flag
   `python -m fanduel_scraper <id> --from-dump dumps/ --csv /tmp/out.csv`,
   fix `fanduel_scraper/parser.py`, and drop the dump in as a new test fixture.

All knowledge of FanDuel's JSON shape lives in `fanduel_scraper/parser.py`, so
that's the only file schema changes should ever touch.
