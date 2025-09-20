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

DOC_NAME = os.getenv("GOOGLE_SHEETS_DOC_NAME", "ORMRHL 2026 Regular Season Player Stats")
WORKSHEET_NAME = os.getenv("WORKSHEET_NAME", "Player Stats")

# Event range (end exclusive)
EVENT_START = int(os.getenv("EVENT_START", "5069"))
EVENT_END   = int(os.getenv("EVENT_END", "5189"))
EVENT_NUMBERS = list(range(EVENT_START, EVENT_END))

# polite pacing between requests
REQUEST_SLEEP = float(os.getenv("REQUEST_SLEEP", "0.4"))

# -----------------------------
# Team logos (fallback mapping)
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
    return (name or "").strip()

def team_icon_fallback(name: str) -> str:
    return TEAM_LOGOS.get(norm_team_name(name), "")

# -----------------------------
# Google auth: read JSON from env secret
# -----------------------------
def get_gspread_client_from_env():
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
    'Player', 'Date', 'Cumulative Points', 'Cumulative Goals', 'Cumulative Assists',
    'Team', 'Team Icon', 'Goals', 'Assists', 'Total Points'
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
        ws = sh.add_worksheet(title=worksheet_name, rows=1000, cols=16)
        ws.append_row(headers)
        return ws

    existing = ws.row_values(1)
    if [h.strip() for h in existing] != headers:
        if not existing:
            ws.append_row(headers)
        else:
            ws.delete_rows(1)
            ws.insert_row(headers, 1)
    return ws

def header_index_map(ws):
    hdr = ws.row_values(1)
    return {name.strip(): idx for idx, name in enumerate(hdr)}

def safe_int(text):
    text = (text or "").strip()
    return int(text) if text.isdigit() else 0

def build_existing_cumulative(ws):
    """
    Reconstruct cumulative totals from existing rows while de-duping by (Player, Date, Team).
    This makes reruns idempotent if previous rows are duplicated.
    """
    idx = header_index_map(ws)
    col_player   = idx.get('Player', 0)
    col_date     = idx.get('Date', 1)
    col_team     = idx.get('Team', 5)  # default arbitrary if missing
    col_goals    = idx.get('Goals', 7)
    col_assists  = idx.get('Assists', 8)
    col_points   = idx.get('Total Points', 9)

    totals = defaultdict(lambda: {"points": 0, "goals": 0, "assists": 0})
    seen = set()

    try:
        data = ws.get_all_values()
    except Exception as e:
        print(f"Warning reading sheet: {e}")
        return totals

    if not data or len(data) <= 1:
        return totals

    for row in data[1:]:
        # Guard on indexes present in the row
        max_needed = max(col_player, col_date, col_team, col_goals, col_assists, col_points)
        if len(row) <= max_needed:
            continue

        player = row[col_player].strip()
        date   = row[col_date].strip() if row[col_date] else ""
        team   = row[col_team].strip() if row[col_team] else ""

        # De-dupe key
        key = (player, date, team)
        if key in seen:
            continue
        seen.add(key)

        g = safe_int(row[col_goals]) if len(row) > col_goals else 0
        a = safe_int(row[col_assists]) if len(row) > col_assists else 0
        tp = safe_int(row[col_points]) if len(row) > col_points else (g + a)

        totals[player]["goals"]   += g
        totals[player]["assists"] += a
        totals[player]["points"]  += tp

    return totals

def append_rows(ws, rows):
    if not rows:
        return
    ws.append_rows(rows, value_input_option="RAW")

# -----------------------------
# Scraper helpers
# -----------------------------
def is_valid_player_name(name):
    return bool(name and not name.isdigit() and re.search(r"[a-zA-Z]", name))

def extract_event_date(soup: BeautifulSoup):
    th_date = soup.find('th', string=lambda x: x and x.strip().lower() == "date")
    if not th_date:
        return None
    td = th_date.find_next('td')
    if not td:
        return None
    raw = td.text.strip()
    for fmt in ("%B %d, %Y", "%Y-%m-%d", "%d-%m-%Y", "%m/%d/%Y"):
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw

def extract_team_icons_from_page(soup: BeautifulSoup):
    mapping = {}
    for span in soup.select(".sp-section-content-logos .sp-team-logo"):
        img = span.find("img")
        src = (img.get("src", "").strip() if img else "")
        for strong in span.select(".sp-team-name"):
            name = norm_team_name(strong.get_text(strip=True))
            if name and src:
                mapping[name] = src
    return mapping

def parse_event(event_number):
    """
    Parse:
      - date from Details table
      - each team block in Performance section:
          team name from h4.sp-table-caption
          icon from logos section (fallback to TEAM_LOGOS)
          player rows from tbody tr.lineup
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

    soup = BeautifulSoup(resp.content, "html.parser")

    date_str = extract_event_date(soup)
    page_icon_map = extract_team_icons_from_page(soup)

    player_stats = []

    performance_blocks = soup.select(".sp-section-content-performance .sp-template.sp-template-event-performance")
    if not performance_blocks:
        performance_blocks = soup.select(".sp-event-performance-tables .sp-template-event-performance")

    for block in performance_blocks:
        caption = block.find("h4", class_="sp-table-caption")
        if not caption:
            continue
        team_name = norm_team_name(caption.get_text(strip=True))
        icon_url = page_icon_map.get(team_name, team_icon_fallback(team_name))

        table = block.find("table", class_="sp-event-performance")
        if not table:
            continue
        tbody = table.find("tbody")
        rows = tbody.find_all("tr", class_="lineup") if tbody else []

        for row in rows:
            tds = row.find_all("td")
            if len(tds) < 5:
                continue
            name = tds[1].get_text(strip=True)
            if not is_valid_player_name(name) or name == "Total":
                continue
            goals = int(tds[3].get_text(strip=True) or 0) if tds[3].get_text(strip=True).isdigit() else 0
            assists = int(tds[4].get_text(strip=True) or 0) if tds[4].get_text(strip=True).isdigit() else 0
            total_points = goals + assists

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

    # Build cumulative totals from existing sheet (de-duped)
    cumulative = build_existing_cumulative(ws)  # {player: {"points": x, "goals": y, "assists": z}}

    pending = []
    processed_events = 0

    for ev in EVENT_NUMBERS:
        date_str, players = parse_event(ev)
        if date_str and players:
            for p in players:
                name    = p['name']
                goals   = p['goals']
                assists = p['assists']
                points  = p['total_points']

                # increment cumulatives
                cumulative[name]["goals"]   += goals
                cumulative[name]["assists"] += assists
                cumulative[name]["points"]  += points

                # ORDER:
                # Player, Date, Cumulative Points, Cumulative Goals, Cumulative Assists,
                # Team, Team Icon, Goals, Assists, Total Points
                pending.append([
                    name,
                    date_str,
                    cumulative[name]["points"],
                    cumulative[name]["goals"],
                    cumulative[name]["assists"],
                    p['team'],
                    p['team_icon'],
                    goals,
                    assists,
                    points,
                ])

        processed_events += 1
        if len(pending) >= 50 or (processed_events % 10 == 0):
            append_rows(ws, pending)
            pending.clear()

        if REQUEST_SLEEP > 0:
            time.sleep(REQUEST_SLEEP)

    append_rows(ws, pending)
    print("Done: Player stats with cumulative goals/assists have been written to Google Sheets.")

if __name__ == "__main__":
    main()
