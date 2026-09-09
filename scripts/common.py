"""
Shared helpers for the ORMRHL scrapers.

Handles:
- polite HTTP fetching with retries
- SQLite connection + schema
- figuring out which season we're currently in
"""

import os
import re
import time
import sqlite3
from pathlib import Path
from datetime import date

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://ormrhl.com"

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "data" / "ormrhl.db"
EXPORTS_DIR = REPO_ROOT / "exports"

# Be a polite scraper: identify ourselves, don't hammer the server.
HEADERS = {
    "User-Agent": "ormrhl-personal-stats-bot/1.0 (+personal use, low frequency)"
}
REQUEST_DELAY_SECONDS = 1.0  # pause between requests to the same host


def get_soup(url: str, retries: int = 3, backoff: float = 2.0) -> BeautifulSoup:
    """Fetch a URL and return a parsed BeautifulSoup object, with retries."""
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            time.sleep(REQUEST_DELAY_SECONDS)
            return BeautifulSoup(resp.text, "lxml")
        except requests.RequestException as e:
            last_err = e
            if attempt < retries:
                time.sleep(backoff * attempt)
    raise RuntimeError(f"Failed to fetch {url} after {retries} attempts: {last_err}")


def get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS games (
            event_id        TEXT PRIMARY KEY,
            season_label    TEXT NOT NULL,   -- e.g. "Regular Season 2026-2027" or "Playoffs 2027"
            game_date       TEXT NOT NULL,   -- ISO date, YYYY-MM-DD
            game_datetime   TEXT,            -- ISO datetime if available
            team1           TEXT NOT NULL,
            team2           TEXT NOT NULL,
            score1          INTEGER,
            score2          INTEGER,
            url             TEXT NOT NULL,
            scraped_at      TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS standings_snapshots (
            season_label    TEXT NOT NULL,
            as_of_date      TEXT NOT NULL,   -- standings after all games up to and including this date
            team            TEXT NOT NULL,
            team_icon       TEXT,
            team_color      TEXT,
            gp              INTEGER NOT NULL,
            w               INTEGER NOT NULL,
            l               INTEGER NOT NULL,
            t               INTEGER NOT NULL,
            pts             INTEGER NOT NULL,
            gf              INTEGER NOT NULL,
            ga              INTEGER NOT NULL,
            diff            INTEGER NOT NULL,
            PRIMARY KEY (season_label, as_of_date, team)
        );

        CREATE TABLE IF NOT EXISTS player_game_stats (
            event_id        TEXT NOT NULL,
            season_label    TEXT NOT NULL,
            game_date       TEXT NOT NULL,
            team            TEXT NOT NULL,
            team_icon       TEXT,
            team_color      TEXT,
            player_name     TEXT NOT NULL,
            player_slug     TEXT NOT NULL,
            position        TEXT,
            goals           INTEGER NOT NULL DEFAULT 0,
            assists         INTEGER NOT NULL DEFAULT 0,
            points          INTEGER NOT NULL DEFAULT 0,
            minor_pen       INTEGER NOT NULL DEFAULT 0,
            major_pen       INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (event_id, player_slug)
        );

        CREATE TABLE IF NOT EXISTS scrape_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            script          TEXT NOT NULL,
            run_at          TEXT NOT NULL,
            rows_added      INTEGER NOT NULL,
            rows_updated    INTEGER NOT NULL,
            notes           TEXT
        );
        """
    )
    conn.commit()


def current_season_window(today: date | None = None) -> dict:
    """
    ORMRHL seasons run roughly Sept -> April, labeled like "2025-2026".
    If we're in Jul/Aug/Sep..Dec, the season is (this year -> next year).
    If we're in Jan..Jun, the season is (last year -> this year).

    Returns dict with:
      - regular_season_label: "Regular Season YYYY-YYYY"
      - playoffs_label_fragment: "Playoffs YYYY"  (playoffs are labeled by the END year)
      - start_year, end_year
    """
    today = today or date.today()
    if today.month >= 7:  # July onward -> new season is starting up
        start_year, end_year = today.year, today.year + 1
    else:
        start_year, end_year = today.year - 1, today.year

    return {
        "start_year": start_year,
        "end_year": end_year,
        "regular_season_label": f"Regular Season {start_year}-{end_year}",
        "playoffs_label_fragment": f"Playoffs {end_year}",
        "season_key": f"{start_year}-{end_year}",
    }


ISO_DATETIME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})")
SCORE_RE = re.compile(r"^\s*(\d+)\s*-\s*(\d+)")  # leading "N - N"; tolerate trailing text (e.g. period/live markers)

CONFIG_PATH = REPO_ROOT / "config" / "teams.json"
_team_config_cache = None


def _load_team_config() -> dict:
    """
    Loads config/teams.json — the user-editable file for team colors/logos.
    Cached per-process. Falls back to an empty config (neutral colors, no
    logos) if the file is missing, so a bad/missing config never breaks a
    scrape run.
    """
    global _team_config_cache
    if _team_config_cache is not None:
        return _team_config_cache
    import json

    if not CONFIG_PATH.exists():
        _team_config_cache = {}
        return _team_config_cache
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        data = {}
    data.pop("_readme", None)
    _team_config_cache = data
    return data


def _team_entry(team_name: str) -> dict:
    """Best-effort config lookup — exact match first, then substring either way."""
    if not team_name:
        return {}
    cfg = _load_team_config()
    if team_name in cfg:
        return cfg[team_name]
    for key, entry in cfg.items():
        if key.startswith("_"):
            continue
        if key.lower() in team_name.lower() or team_name.lower() in key.lower():
            return entry
    return {}


def team_icon(team_name: str) -> str:
    """Logo URL from config/teams.json, or '' if the team/logo isn't configured."""
    return _team_entry(team_name).get("logo", "")


def team_color(team_name: str) -> str:
    """Bar/brand color from config/teams.json, or a neutral fallback for unknown teams."""
    entry = _team_entry(team_name)
    if "color" in entry:
        return entry["color"]
    return _load_team_config().get("_fallback_color", "#6B7684")


def get_sheets_client():
    """
    Returns an authorized gspread client, or None if Sheets sync isn't
    configured (GCP_SA_KEY_JSON not set) — callers should treat Sheets
    sync as optional and skip it gracefully rather than failing the run.
    """
    svc_json = os.environ.get("GCP_SA_KEY_JSON")
    if not svc_json:
        return None
    import gspread
    from google.oauth2.service_account import Credentials

    key_data = __import__("json").loads(svc_json)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(key_data, scopes=scopes)
    return gspread.authorize(creds)


def sheets_doc_name() -> str:
    """
    GOOGLE_SHEETS_DOC_NAME as a GitHub Actions repo Variable comes through
    as an empty string (not unset) when it doesn't exist, which would
    silently defeat a plain os.environ.get(key, default) — so treat "" the
    same as unset.
    """
    return os.environ.get("GOOGLE_SHEETS_DOC_NAME") or "ORMRHL Stats"


def overwrite_worksheet(gc, doc_name: str, worksheet_name: str, header: list, rows: list):
    """
    Fully replaces the contents of one worksheet tab with `header` + `rows`.
    SQLite is the source of truth, so a clean overwrite each run is simpler
    and more robust than incrementally appending/de-duping in Sheets.
    """
    import gspread

    try:
        sh = gc.open(doc_name)
    except gspread.SpreadsheetNotFound:
        raise RuntimeError(
            f"Spreadsheet '{doc_name}' not found or not shared with the service account."
        )

    n_rows = max(len(rows) + 10, 100)
    n_cols = max(len(header) + 2, 10)
    try:
        ws = sh.worksheet(worksheet_name)
        ws.resize(rows=n_rows, cols=n_cols)
        ws.clear()
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=worksheet_name, rows=n_rows, cols=n_cols)

    if rows:
        ws.update([header] + rows, value_input_option="RAW")
    else:
        ws.update([header], value_input_option="RAW")


def parse_iso_prefix(cell_text: str):
    """
    The 'Date' column on /past-games/ concatenates a hidden ISO sort-key with
    the human-readable date, e.g. '2026-04-18 15:30:59April 18, 2026'.
    Pull out the ISO date/time prefix, which is the reliable part.
    """
    m = ISO_DATETIME_RE.match(cell_text.strip())
    if not m:
        return None, None
    return m.group(1), f"{m.group(1)} {m.group(2)}"


def event_id_from_url(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]
