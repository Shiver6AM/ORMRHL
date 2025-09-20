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

    # Parse to validate, then write to a temp file
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
        ws = sh.add_worksheet(title=worksheet_name, rows=1000, cols=10)
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

def build_existing_cumulative(ws):
    cumu = defaultdict(int)
    try:
        data = ws.get_all_values()
    except Exception as e:
        print(f"Warning reading sheet: {e}")
        return cumu
    if not data or len(data) <= 1:
        return cumu
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

def append_rows(ws, rows):
    if not rows:
        return
    # batch append for efficiency
    ws.append_rows(rows, value_input_option="RAW")

# -----------------------------
# Scraper helpers
# -----------------------------
def is_valid_player_name(name):
    return bool(name and not name.isdigit() and re.search(r"[a-zA-Z]", name))

def safe_int(text):
    text = (text or "").strip()
    return int(text) if text.isdigit() else 0

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

    # Date
    date_str = None
    th_date = soup.find('th', string=lambda x: x and x.strip().lower() == "date")
    if th_date:
        td = th_date.find_next('td')
        if td:
            raw = td.text.strip()
            # try common formats → normalize to YYYY-MM-DD where possible
            for fmt in ("%B %d, %Y", "%Y-%m-%d", "%d-%m-%Y", "%m/%d/%Y"):
                try:
                    dt = datetime.strptime(raw, fmt)
                    date_str = dt.strftime("%Y-%m-%d")
                    break
                except ValueError:
                    continue
            if not date_str:
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
        if name == "Total" or not is_valid_player_name(name):
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

# -----------------------------
# Main
# -----------------------------
def main():
    headers = ['Date', 'Player', 'Goals', 'Assists', 'Total Points', 'Cumulative Points']
    gc = get_gspread_client_from_env()
    ws = get_or_create_worksheet(gc, DOC_NAME, WORKSHEET_NAME, headers)
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
                cumulative_points[name] += total_points
                pending.append([date_str, name, goals, assists, total_points, cumulative_points[name]])

        processed_events += 1
        if len(pending) >= 50 or (processed_events % 10 == 0):
            append_rows(ws, pending)
            pending.clear()

        if REQUEST_SLEEP > 0:
            time.sleep(REQUEST_SLEEP)

    append_rows(ws, pending)
    print("Done: Player stats have been written to Google Sheets.")

if __name__ == "__main__":
    main()
