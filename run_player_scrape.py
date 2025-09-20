import os
import json
import time
import re
from datetime import datetime
from collections import defaultdict

import requests
from bs4 import BeautifulSoup

import gspread
from google.oauth2.service_account import Credentials

# -----------------------------
# Config via environment vars
# -----------------------------
BASE_URL = "https://ormrhl.com/event/"

DOC_NAME = os.getenv("GOOGLE_SHEETS_DOC_NAME", "ORMRHL Player Stats 2026")
WORKSHEET_NAME = os.getenv("WORKSHEET_NAME", "Player Stats")

# Event range (end exclusive)
EVENT_START = int(os.getenv("EVENT_START", "5069"))
EVENT_END   = int(os.getenv("EVENT_END", "5189"))
EVENT_NUMBERS = list(range(EVENT_START, EVENT_END))

# polite pacing between requests
REQUEST_SLEEP = float(os.getenv("REQUEST_SLEEP", "0.4"))

# -----------------------------
# Team logos provided
# -----------------------------
TEAM_LOGOS = {
    "Grizzlies": "https://ormrhl.com/wp-content/uploads/2022/09/grizzlies-128x128.png",
    "Eagles": "https://ormrhl.com/wp-content/uploads/2021/11/Eagles_logo-128x128.png",
    "Komodo": "https://ormrhl.com/wp-content/uploads/2022/09/komodo-128x128.png",
    "Cobras": "https://ormrhl.com/wp-content/uploads/2021/11/Cobras_Logo-128x128.png",
    "Wildcats": "https://ormrhl.com/wp-content/uploads/2021/11/wildcats_logo-128x128.png",
    "Rhinos": "https://ormrhl.com/wp-content/uploads/2021/11/Rhinos_logo-128x128.png",
    "Leafs": "https://ormrhl.com/wp-content/uploads/2025/09/leafsfin-128x128.png",
    "Kings": "https://ormrhl.com/wp-content/uploads/2025/09/kingsfin-128x128.png",
}

def norm_team_name(name: str) -> str:
    """Normalize scraped team labels to match keys in TEAM_LOGOS when possible."""
    if not name:
        return ""
    n = name.strip()
    # Some pages might use uppercase or trailing spaces; keep simple normalization
    return n

def team_icon_for(name: str) -> str:
    n = norm_team_name(name)
    return TEAM_LOGOS.get(n, "")

# -----------------------------
# Google auth: read JSON from env secret
# -----------------------------
def get_gspread_client_from_env():
    """
    Reads the entire service-account JSON from env var GCP_SA_KEY_JSON,
    writes it to a temp file on the runner, and authorizes gspread.
    """
    svc_json = os.getenv("GCP_SA_KEY_JSON")
    if not svc_json:
        raise RuntimeError("Missing GCP_SA_KEY_JSON secret. Add it in GitHub → Settings → Secrets → Actions.")

    key_data = json.loads(svc_json)
    temp_path = "service_account_temp.json"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(key_data, f)

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = Credentials.from_service_account_file(temp_path, scopes=scopes)
    return gspread.authorize(creds)

# -----------------------------
# Sheets helpers
# -----------------------------
NEW_HEADERS = [
    'Date', 'Player', 'Team', 'Team Icon', 'Goals', 'Assists', 'Total Points', 'Cumulative Points'
]

def get_or_create_worksheet(gc, doc_name, worksheet_name, headers):
    try:
        sh = gc.open(doc_name)
    except gspread.SpreadsheetNotFound:
        raise RuntimeError(
            f"Spreadsheet '{doc_name}' not found or not shared with the Service Account."
        )

    try:
        ws = sh.worksheet(worksheet_name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=worksheet_name, rows=1000, cols=12)
        ws.append_row(headers)
        return ws

    # Ensure headers are present (and in correct order)
    existing = ws.row_values(1)
    if [h.strip() for h in existing] != headers:
        if not existing:
            ws.append_row(headers)
        else:
            ws.delete_rows(1)
            ws.insert_row(headers, 1)
    return ws

def header_index_map(ws):
    """Build a dict of column name -> index from the current header row."""
    hdr = ws.row_values(1)
    return {name.strip(): idx for idx, name in enumerate(hdr)}

def build_existing_cumulative(ws):
    """
    Scan existing worksheet rows to seed 'cumulative points' so runs can be resumed.
    Uses header names to find the right columns.
    """
    idx = header_index_map(ws)
    player_col = idx.get('Player', 1)
    cumulative_col = idx.get('Cumulative Points', 7)

    cumu = defaultdict(int)
    try:
        data = ws.get_all_values()
    except Exception as e:
        print(f"Warning reading sheet: {e}")
        return cumu

    if not data or len(data) <= 1:
        return cumu

    for row in data[1:]:
        if len(row) <= max(player_col, cumulative_col):
            continue
        player = row[player_col].strip()
        try:
            cp = int(row[cumulative_col])
        except ValueError:
            cp = 0
        if cp > cumu[player]:
            cumu[player] = cp
    return cumu

def append_rows(ws, rows):
    if not rows:
        return
    ws.append_rows(rows, value_input_option="RAW")

# -----------------------------
# Scraper helpers
# -----------------------------
def is_valid_player_name(name):
    return bool(name and not name.isdigit() and re.search(r"[a-zA-Z]", name))

def safe_int(text):
    text = (text or "").strip()
    return int(text) if text.isdigit() else 0

def extract_event_date(soup: BeautifulSoup):
    # Looks for <th>Date</th> then its next <td>, common on ORMRHL pages
    th_date = soup.find('th', string=lambda x: x and x.strip().lower() == "date")
    if not th_date:
        return None
    td = th_date.find_next('td')
    if not td:
        return None
    raw = td.text.strip()
    # Try to normalize to YYYY-MM-DD
    for fmt in ("%B %d, %Y", "%Y-%m-%d", "%d-%m-%Y", "%m/%d/%Y"):
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw

def extract_teams_from_header(soup: BeautifulSoup):
    """
    Try to read the two team names shown near the top of the event.
    Returns a list like ['Team A', 'Team B'] or [].
    """
    teams = []

    # Common pattern seen previously: <td class="data-name">Team</td>
    for td in soup.find_all('td', {'class': 'data-name'}):
        t = td.get_text(strip=True)
        if t:
            teams.append(t)

    # Deduplicate while preserving order
    cleaned = []
    for t in teams:
        if t not in cleaned:
            cleaned.append(t)

    # Only keep first 2
    return cleaned[:2]

def parse_event(event_number):
    url = f"{BASE_URL}{event_number}/"
    try:
        resp = requests.get(url, timeout=20)
    except requests.RequestException as e:
        print(f"[{event_number}] Request error: {e}")
        return None, []

    if resp.status_code != 200:
        print(f"[{event_number}] HTTP {resp.status_code}")
        return None, []

    soup = BeautifulSoup(resp.content, "html.parser")

    date_str = extract_event_date(soup)
    teams = extract_teams_from_header(soup)  # e.g., ['Grizzlies', 'Eagles']

    # Gather all player rows in the page order
    player_rows = soup.find_all('tr', {'class': ['lineup', 'odd', 'even']})
    if not player_rows:
        return date_str, []

    # Strategy:
    # - Assign Team #1 to rows until we hit a "Total" row, then switch to Team #2.
    # - This matches common lineup tables where each team block ends with a "Total" row.
    team_idx = 0  # 0 = first team, 1 = second team
    player_stats = []

    for row in player_rows:
        tds = row.find_all('td')
        if len(tds) < 2:
            continue

        name_cell = tds[1].get_text(strip=True)
        if name_cell == "Total":
            # Next rows belong to the other team block
            team_idx = min(team_idx + 1, 1)
            continue

        if len(tds) < 7:
            continue  # not a valid player stat row

        name = name_cell
        if not is_valid_player_name(name):
            continue

        goals   = safe_int(tds[3].get_text())
        assists = safe_int(tds[4].get_text())
        total_points = goals + assists

        # Determine team name
        team_name = teams[team_idx] if teams and team_idx < len(teams) else (teams[0] if teams else "")
        team_name = norm_team_name(team_name)
        icon_url = team_icon_for(team_name)

        player_stats.append({
            "name": name,
            "team": team_name,
            "team_icon": icon_url,
            "goals": goals,
            "assists": assists,
            "total_points": total_points
        })

    return date_str, player_stats

# -----------------------------
# Main
# -----------------------------
def main():
    gc = get_gspread_client_from_env()
    ws = get_or_create_worksheet(gc, DOC_NAME, WORKSHEET_NAME, NEW_HEADERS)
    cumulative_points = build_existing_cumulative(ws)

    pending = []
    processed_events = 0

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

                pending.append([
                    date_str,
                    name,
                    p['team'],
                    p['team_icon'],
                    goals,
                    assists,
                    total_points,
                    cumulative_points[name],
                ])

        processed_events += 1
        if len(pending) >= 50 or (processed_events % 10 == 0):
            append_rows(ws, pending)
            pending.clear()

        if REQUEST_SLEEP > 0:
            time.sleep(REQUEST_SLEEP)

    append_rows(ws, pending)
    print("Done: Player stats (with Team & Icon) have been written to Google Sheets.")

if __name__ == "__main__":
    main()
