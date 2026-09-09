"""
scrape_games.py

Scrapes /past-games/ for the current ORMRHL season, stores each game's
result in SQLite, then rebuilds a "standings after each game date" table
so you can chart how the standings moved over the course of the season
(great for a Flourish bar-chart-race).

Usage:
    python scripts/scrape_games.py                 # auto-detect current season
    python scripts/scrape_games.py --season-key 2026-2027
"""

import os
import argparse
import csv
from collections import defaultdict
from datetime import datetime, timezone

from common import (
    BASE_URL,
    get_soup,
    get_db,
    current_season_window,
    parse_iso_prefix,
    SCORE_RE,
    event_id_from_url,
    team_icon,
    team_color,
    get_sheets_client,
    overwrite_worksheet,
    sheets_doc_name,
    EXPORTS_DIR,
)

PAST_GAMES_URL = f"{BASE_URL}/past-games/"


def find_past_games_table(soup):
    """The past-games page has one big table with headers Date/Event/Time-Results/Season/Venue."""
    for table in soup.find_all("table"):
        header_cells = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        if not header_cells:
            first_row = table.find("tr")
            if first_row:
                header_cells = [c.get_text(strip=True).lower() for c in first_row.find_all(["th", "td"])]
        if any("date" in h for h in header_cells) and any("event" in h for h in header_cells):
            return table
    raise RuntimeError("Could not find the past-games table on the page — site layout may have changed.")


def split_matchup(event_text: str):
    """
    'Leafs vs Cobras' -> ('Leafs', 'Cobras')
    'D Finals Rhinos vs Leafs' -> ('Rhinos', 'Leafs')  (best-effort; playoff-final
    slugged pages sometimes prefix a round label before the real team names)
    """
    if " vs " not in event_text:
        return None, None
    left, right = event_text.split(" vs ", 1)
    team2 = right.strip()
    # Strip a leading round/division label like "A Finals ", "D Division ", "C - "
    left = left.strip()
    for pattern in (" Finals ", " Division ", " - "):
        if pattern in left:
            left = left.split(pattern)[-1].strip()
    team1 = left
    return team1, team2


def scrape_past_games(season_key: str):
    """Returns a list of dicts, one per game, for the given season (regular season + playoffs)."""
    window = current_season_window()
    if season_key != window["season_key"]:
        start_year, end_year = (int(x) for x in season_key.split("-"))
        window = {
            "start_year": start_year,
            "end_year": end_year,
            "regular_season_label": f"Regular Season {start_year}-{end_year}",
            "playoffs_label_fragment": f"Playoffs {end_year}",
            "season_key": season_key,
        }

    soup = get_soup(PAST_GAMES_URL)
    table = find_past_games_table(soup)

    games = []
    rows = table.find_all("tr")
    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 4:
            continue  # header row or malformed row

        date_cell_text = cells[0].get_text(strip=True)
        event_cell = cells[1]
        result_cell_text = cells[2].get_text(strip=True)
        season_cell_text = cells[3].get_text(strip=True)

        # Only keep games belonging to the target season (regular season or its playoffs)
        is_regular = season_cell_text == window["regular_season_label"]
        is_playoffs = window["playoffs_label_fragment"] in season_cell_text
        if not (is_regular or is_playoffs):
            continue

        score_match = SCORE_RE.match(result_cell_text)
        if not score_match:
            continue  # game hasn't been played / no score posted yet

        iso_date, iso_datetime = parse_iso_prefix(date_cell_text)
        if not iso_date:
            continue

        link = event_cell.find("a")
        if not link or not link.get("href"):
            continue
        url = link["href"]
        event_id = event_id_from_url(url)

        team1, team2 = split_matchup(link.get_text(strip=True))
        if not team1 or not team2:
            continue

        games.append(
            {
                "event_id": event_id,
                "season_label": season_cell_text,
                "game_date": iso_date,
                "game_datetime": iso_datetime,
                "team1": team1,
                "team2": team2,
                "score1": int(score_match.group(1)),
                "score2": int(score_match.group(2)),
                "url": url,
            }
        )

    return games, window


def upsert_games(conn, games):
    added, updated = 0, 0
    now = datetime.now(timezone.utc).isoformat()
    for g in games:
        cur = conn.execute("SELECT 1 FROM games WHERE event_id = ?", (g["event_id"],))
        exists = cur.fetchone() is not None
        conn.execute(
            """
            INSERT INTO games (event_id, season_label, game_date, game_datetime,
                                team1, team2, score1, score2, url, scraped_at)
            VALUES (:event_id, :season_label, :game_date, :game_datetime,
                    :team1, :team2, :score1, :score2, :url, :now)
            ON CONFLICT(event_id) DO UPDATE SET
                season_label=excluded.season_label,
                game_date=excluded.game_date,
                game_datetime=excluded.game_datetime,
                team1=excluded.team1,
                team2=excluded.team2,
                score1=excluded.score1,
                score2=excluded.score2,
                url=excluded.url,
                scraped_at=excluded.scraped_at
            """,
            {**g, "now": now},
        )
        if exists:
            updated += 1
        else:
            added += 1
    conn.commit()
    return added, updated


def rebuild_standings_snapshots(conn, season_label_regular: str):
    """
    Recomputes the full standings-by-date history for the regular season
    from scratch (cheap enough given league size — a few hundred games/season).
    Points: Win=2, Tie=1, Loss=0 (matches ORMRHL's published regular-season standings).
    """
    rows = conn.execute(
        """
        SELECT game_date, team1, team2, score1, score2
        FROM games
        WHERE season_label = ?
        ORDER BY game_date ASC, event_id ASC
        """,
        (season_label_regular,),
    ).fetchall()

    conn.execute("DELETE FROM standings_snapshots WHERE season_label = ?", (season_label_regular,))

    totals = defaultdict(lambda: {"gp": 0, "w": 0, "l": 0, "t": 0, "gf": 0, "ga": 0})
    snapshot_rows = []

    dates_in_order = sorted(set(r["game_date"] for r in rows))
    games_by_date = defaultdict(list)
    for r in rows:
        games_by_date[r["game_date"]].append(r)

    for d in dates_in_order:
        for r in games_by_date[d]:
            t1, t2, s1, s2 = r["team1"], r["team2"], r["score1"], r["score2"]
            totals[t1]["gp"] += 1
            totals[t2]["gp"] += 1
            totals[t1]["gf"] += s1
            totals[t1]["ga"] += s2
            totals[t2]["gf"] += s2
            totals[t2]["ga"] += s1
            if s1 > s2:
                totals[t1]["w"] += 1
                totals[t2]["l"] += 1
            elif s2 > s1:
                totals[t2]["w"] += 1
                totals[t1]["l"] += 1
            else:
                totals[t1]["t"] += 1
                totals[t2]["t"] += 1

        # snapshot standings as of this date for every team seen so far
        for team, stats in totals.items():
            pts = stats["w"] * 2 + stats["t"] * 1
            snapshot_rows.append(
                {
                    "season_label": season_label_regular,
                    "as_of_date": d,
                    "team": team,
                    "team_icon": team_icon(team),
                    "team_color": team_color(team),
                    "gp": stats["gp"],
                    "w": stats["w"],
                    "l": stats["l"],
                    "t": stats["t"],
                    "pts": pts,
                    "gf": stats["gf"],
                    "ga": stats["ga"],
                    "diff": stats["gf"] - stats["ga"],
                }
            )

    conn.executemany(
        """
        INSERT INTO standings_snapshots
            (season_label, as_of_date, team, team_icon, team_color, gp, w, l, t, pts, gf, ga, diff)
        VALUES (:season_label, :as_of_date, :team, :team_icon, :team_color, :gp, :w, :l, :t, :pts, :gf, :ga, :diff)
        """,
        snapshot_rows,
    )
    conn.commit()
    return len(snapshot_rows)


def export_csvs(conn):
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)

    games = conn.execute("SELECT * FROM games ORDER BY game_date, event_id").fetchall()
    with open(EXPORTS_DIR / "games.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(games[0].keys() if games else
                         ["event_id", "season_label", "game_date", "game_datetime",
                          "team1", "team2", "score1", "score2", "url", "scraped_at"])
        for g in games:
            writer.writerow([g[k] for k in g.keys()])

    snaps = conn.execute(
        "SELECT * FROM standings_snapshots ORDER BY season_label, as_of_date, team"
    ).fetchall()
    with open(EXPORTS_DIR / "standings_snapshots.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(snaps[0].keys() if snaps else
                         ["season_label", "as_of_date", "team", "team_icon", "team_color", "gp", "w", "l", "t",
                          "pts", "gf", "ga", "diff"])
        for s in snaps:
            writer.writerow([s[k] for k in s.keys()])
    return snaps


def push_standings_to_sheets(snaps):
    """
    Optional: if GCP_SA_KEY_JSON is set, overwrite a 'Standings History' tab
    in the shared Google Sheet with the current standings-by-date data.
    Silently skipped if Sheets sync isn't configured.
    """
    gc = get_sheets_client()
    if gc is None:
        print("Sheets sync not configured (no GCP_SA_KEY_JSON) — skipping Sheets push.")
        return
    if not snaps:
        print("No standings rows to push to Sheets.")
        return
    header = list(snaps[0].keys())
    rows = [[s[k] for k in header] for s in snaps]
    try:
        overwrite_worksheet(gc, sheets_doc_name(), "Standings History", header, rows)
        print(f"Pushed {len(rows)} rows to Google Sheet '{sheets_doc_name()}' / tab 'Standings History'")
    except Exception as e:
        # Sheets sync is a bonus feature, not the source of truth (SQLite is) --
        # a failure here (e.g. hitting Google's per-workbook cell limit) should
        # never take down the actual scrape/commit pipeline.
        print(f"WARNING: Sheets push failed, continuing anyway: {e}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--season-key",
        default=None,
        help="Override season, e.g. 2026-2027. Defaults to auto-detected current season.",
    )
    args = parser.parse_args()

    window = current_season_window()
    season_key = args.season_key or window["season_key"]

    print(f"Scraping past games for season {season_key} ...")
    games, window = scrape_past_games(season_key)
    print(f"Found {len(games)} completed games for this season on {PAST_GAMES_URL}")

    conn = get_db()
    added, updated = upsert_games(conn, games)
    print(f"Games table: +{added} new, {updated} updated")

    snap_count = rebuild_standings_snapshots(conn, window["regular_season_label"])
    print(f"Rebuilt {snap_count} standings-snapshot rows for '{window['regular_season_label']}'")

    snaps = export_csvs(conn)
    print(f"Exported CSVs to {EXPORTS_DIR}")

    push_standings_to_sheets(snaps)

    conn.execute(
        "INSERT INTO scrape_log (script, run_at, rows_added, rows_updated, notes) VALUES (?, ?, ?, ?, ?)",
        ("scrape_games.py", datetime.now(timezone.utc).isoformat(), added, updated, season_key),
    )
    conn.commit()
    conn.close()


if __name__ == "__main__":
    main()
