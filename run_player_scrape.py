import requests
import re
from bs4 import BeautifulSoup
import gspread
from google.oauth2.service_account import Credentials
from collections import defaultdict
from datetime import datetime
import time

# =======================
# CONFIG — EDIT THESE
# =======================
BASE_URL = "https://ormrhl.com/event/"
# Example: playoffs sample range; change as needed
EVENT_NUMBERS = list(range(5069, 5189))   # End is exclusive; adjust for your season

GOOGLE_SHEETS_DOC_NAME = "ORMRHL 2026 Regular Season"   # Your Google Sheet name
WORKSHEET_NAME = "Player Stats"                       # Tab name to write player rows
SERVICE_ACCOUNT_JSON = r"C:\Users\jonat\Downloads\ORMRHL Stats Visualizer\2026\ormrhl_key.json"  # Path to your JSON key

# Optional: request pacing to be polite to the site (in seconds)
REQUEST_SLEEP = 0.4

# =======================
# GOOGLE SHEETS AUTH
# =======================
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]
creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_JSON, scopes=SCOPES)
gc = gspread.authorize(creds)

def get_or_create_worksheet(doc_name: str, worksheet_name: str, headers: list[str]):
    """
    Open (or create) the Google Sheet and worksheet.
    Ensures header row exists (exact order).
    """
    try:
        sh = gc.open(doc_name)
    except gspread.SpreadsheetNotFound:
        # If you want to auto-create the spreadsheet, you can uncomment below:
        # sh = gc.create(doc_name)
        # NOTE: If created programmatically, also share it with your Google user, or open from Drive and move it.
        raise RuntimeError(f"Spreadsheet '{doc_name}' not found. Create it and share with the Service Account.")

    try:
        ws = sh.worksheet(worksheet_name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=worksheet_name, rows=1000, cols=10)
        ws.append_row(headers)
        return ws

    # Ensure headers are present (and in correct order)
    existing = ws.row_values(1)
    if [h.strip() for h in existing] != headers:
        if not existing:
            ws.append_row(headers)
        else:
            # Replace header row to keep consistent schema
            ws.delete_rows(1)
            ws.insert_row(headers, 1)

    return ws

# =======================
# HELPERS / PARSERS
# =======================
def is_valid_player_name(name: str) -> bool:
    """True if name is non-empty, not purely numeric, and has at least one alpha."""
    return bool(name and not name.isdigit() and re.search(r'[a-zA-Z]', name))

def safe_int(text: str) -> int:
    text = (text or "").strip()
    return int(text) if text.isdigit() else 0

def parse_event(event_number: int):
    """
    Scrape one event page and return (date_str, player_stats) where:
      - date_str: 'YYYY-MM-DD' if found (or None)
      - player_stats: list of dicts with keys: name, goals, assists, total_points
    """
    url = f"{BASE_URL}{event_number}/"
    try:
        resp = requests.get(url, timeout=20)
    except requests.RequestException as e:
        print(f"[{event_number}] Request error: {e}")
        return None, []

    if resp.status_code != 200:
        print(f"[{event_number}] HTTP {resp.status_code}")
        return None, []

    soup = BeautifulSoup(resp.content, 'html.parser')

    # Date (looks up <th>Date</th> then next <td>)
    date_str = None
    th_date = soup.find('th', string=lambda x: x and x.strip().lower() == "date")
    if th_date:
        td = th_date.find_next('td')
        if td:
            raw = td.text.strip()
            # Try to normalize to YYYY-MM-DD
            # If it already looks like YYYY-MM-DD, keep it; else try parsing common formats
            try:
                # attempt flexible parse
                dt = datetime.strptime(raw, "%B %d, %Y")  # e.g., "March 5, 2026"
                date_str = dt.strftime("%Y-%m-%d")
            except ValueError:
                try:
                    dt = datetime.strptime(raw, "%Y-%m-%d")
                    date_str = dt.strftime("%Y-%m-%d")
                except ValueError:
                    # Fallback: keep raw
                    date_str = raw
    else:
        print(f"[{event_number}] Warning: Date not found")

    # Player rows
    player_stats = []
    player_rows = soup.find_all('tr', {'class': ['lineup', 'odd', 'even']})
    for row in player_rows:
        tds = row.find_all('td')
        if len(tds) < 7:
            continue
        name = tds[1].get_text(strip=True)
        if name == "Total":
            continue
        if not is_valid_player_name(name):
            continue

        goals = safe_int(tds[3].get_text())
        assists = safe_int(tds[4].get_text())
        total_points = goals + assists

        player_stats.append({
            "name": name,
            "goals": goals,
            "assists": assists,
            "total_points": total_points
        })

    return date_str, player_stats

def build_existing_cumulative(ws) -> dict:
    """
    Scan existing worksheet rows to seed 'cumulative points' so runs can be resumed.
    Returns dict: { player_name: max_cumulative_points_seen }
    Assumes header row is present and columns are:
      ['Date', 'Player', 'Goals', 'Assists', 'Total Points', 'Cumulative Points']
    """
    cumu = defaultdict(int)
    try:
        data = ws.get_all_values()
    except Exception as e:
        print(f"Warning reading sheet: {e}")
        return cumu

    if not data or len(data) <= 1:
        return cumu

    # Columns: 0=Date, 1=Player, 2=Goals, 3=Assists, 4=Total Points, 5=Cumulative Points
    for row in data[1:]:
        if len(row) < 6:
            continue
        player = row[1].strip()
        try:
            cp = int(row[5])
        except ValueError:
            cp = 0
        if cp > cumu[player]:
            cumu[player] = cp
    return cumu

def append_player_rows(ws, rows: list[list]):
    """
    Efficiently append in batches to reduce API calls.
    """
    if not rows:
        return
    # gspread best practice: append_rows if available; fall back to batch_update
    try:
        ws.append_rows(rows, value_input_option="RAW")
    except AttributeError:
        # Older gspread versions
        for r in rows:
            ws.append_row(r, value_input_option="RAW")

# =======================
# MAIN
# =======================
def main():
    headers = ['Date', 'Player', 'Goals', 'Assists', 'Total Points', 'Cumulative Points']
    ws = get_or_create_worksheet(GOOGLE_SHEETS_DOC_NAME, WORKSHEET_NAME, headers)
    cumulative_points = build_existing_cumulative(ws)

    pending_rows = []
    processed = 0

    for ev in EVENT_NUMBERS:
        date_str, players = parse_event(ev)
        if date_str and players:
            for p in players:
                name = p['name']
                goals = p['goals']
                assists = p['assists']
                total_points = p['total_points']

                # Persisted cumulative across runs
                cumulative_points[name] += total_points

                pending_rows.append([
                    date_str, name, goals, assists, total_points, cumulative_points[name]
                ])
        else:
            # You might still want to record "no data" events; skipping here
            pass

        processed += 1
        # Flush every ~50 rows or every few events to avoid large batches
        if len(pending_rows) >= 50 or (processed % 10 == 0):
            append_player_rows(ws, pending_rows)
            pending_rows.clear()

        if REQUEST_SLEEP:
            time.sleep(REQUEST_SLEEP)

    # Final flush
    append_player_rows(ws, pending_rows)

    print("Done! Player stats have been written to Google Sheets.")

if __name__ == "__main__":
    main()
