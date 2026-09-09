"""
scrape_games.py

Scrapes ALL of /past-games/ (every season the page lists, not just the
current one) since it's a single page fetch regardless of how much history
it contains. Stores each game's result in SQLite, then rebuilds a
"standings after each game date" table for EVERY (season, regular/playoffs)
combination found -- so historical seasons and playoffs are covered
automatically, no separate backfill step needed for this script.

Usage:
    python scripts/scrape_games.py                 # scrape all seasons found on the page
    python scripts/scrape_games.py --season-key 2026-2027   # restrict to just one season
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
    parse_iso_prefix,
    SCORE_RE,
    event_id_from_url,
    season_type_and_key,
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


def scrape_past_games(season_key_filter: str | None = None):
    """
    Returns a list of dicts, one per completed game. By default (no filter)
    returns EVERY season found on the page; pass season_key_filter (e.g.
    "2026-2027") to restrict to just one season's games (both regular season
    and its playoffs).
    """
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

        season_type, season_key = season_type_and_key(season_cell_text)
        if season_key_filter is not None and season_key != season_key_filter:
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
                "season_key": season_key,
                "season_type": season_type,
                "game_date": iso_date,
                "game_datetime": iso_datetime,
                "team1": team1,
                "team2": team2,
                "score1": int(score_match.group(1)),
                "score2": int(score_match.group(2)),
                "url": url,
            }
        )

    return games


def upsert_games(conn, games):
    added, updated = 0, 0
    now = datetime.now(timezone.utc).isoformat()
    for g in games:
        cur = conn.execute("SELECT 1 FROM games WHERE event_id = ?", (g["event_id"],))
        exists = cur.fetchone() is not None
        conn.execute(
            """
            INSERT INTO games (event_id, season_label, season_key, season_type, game_date, game_datetime,
                                team1, team2, score1, score2, url, scraped_at)
            VALUES (:event_id, :season_label, :season_key, :season_type, :game_date, :game_datetime,
                    :team1, :team2, :score1, :score2, :url, :now)
            ON CONFLICT(event_id) DO UPDATE SET
                season_label=excluded.season_label,
                season_key=excluded.season_key,
                season_type=excluded.season_type,
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


def rebuild_standings_snapshots_for(conn, season_key: str, season_type: str, game_rows: list):
    """
    Recomputes the full standings-by-date history for ONE (season_key,
    season_type) group from scratch, from the already-fetched game_rows for
    that group (which may span several raw season_label variants -- e.g.
    "Playoffs 2023", "Playoffs Round 2 2023", "A Finals 2023" all fold into
    one combined playoffs standings computation for season_key 2022-2023).
    Points: Win=2, Tie=1, Loss=0.

    Note: this same simple formula is applied to playoffs too, for lack of
    a documented alternative -- the site's own playoff standings table
    showed fractional point totals suggesting some kind of bonus-point
    system per round, which isn't reverse-engineered here. Treat playoff
    standings from this script as "game record over the playoffs," not
    necessarily identical to the league's own playoff standings table.
    """
    display_label = f"Regular Season {season_key}" if season_type == "regular" else f"Playoffs {season_key}"

    totals = defaultdict(lambda: {"gp": 0, "w": 0, "l": 0, "t": 0, "gf": 0, "ga": 0})
    snapshot_rows = []

    games_by_date = defaultdict(list)
    for r in game_rows:
        games_by_date[r["game_date"]].append(r)
    dates_in_order = sorted(games_by_date.keys())

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

        for team, stats in totals.items():
            pts = stats["w"] * 2 + stats["t"] * 1
            snapshot_rows.append(
                {
                    "season_label": display_label,
                    "season_key": season_key,
                    "season_type": season_type,
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
            (season_label, season_key, season_type, as_of_date, team, team_icon, team_color,
             gp, w, l, t, pts, gf, ga, diff)
        VALUES (:season_label, :season_key, :season_type, :as_of_date, :team, :team_icon, :team_color,
                :gp, :w, :l, :t, :pts, :gf, :ga, :diff)
        """,
        snapshot_rows,
    )
    return len(snapshot_rows)


def rebuild_all_standings_snapshots(conn):
    """
    Rebuilds standings snapshots for every distinct (season_key, season_type)
    combination present in `games` -- combining every raw season_label
    variant that maps to the same group (see season_type_and_key's
    docstring for why that matters: several playoff-round label spellings
    all fold into one combined playoffs group per season).

    standings_snapshots is fully derived from `games` and rebuilt from
    scratch every run, so it's safe to drop and recreate it outright here --
    that also sidesteps ever needing an ALTER-TABLE-style primary key
    migration for this specific table as the grouping key has evolved.
    """
    conn.execute("DROP TABLE IF EXISTS standings_snapshots")
    conn.execute(
        """
        CREATE TABLE standings_snapshots (
            season_label    TEXT NOT NULL,
            season_key      TEXT NOT NULL,
            season_type     TEXT NOT NULL,
            as_of_date      TEXT NOT NULL,
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
            PRIMARY KEY (season_key, season_type, as_of_date, team)
        )
        """
    )

    rows = conn.execute(
        "SELECT season_key, season_type, game_date, team1, team2, score1, score2 FROM games "
        "WHERE season_key IS NOT NULL ORDER BY season_key, season_type, game_date ASC, event_id ASC"
    ).fetchall()

    groups = defaultdict(list)
    for r in rows:
        groups[(r["season_key"], r["season_type"])].append(r)

    total = 0
    for (season_key, season_type), game_rows in groups.items():
        total += rebuild_standings_snapshots_for(conn, season_key, season_type, game_rows)
    conn.commit()
    return total, len(groups)


def export_csvs(conn):
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)

    games = conn.execute("SELECT * FROM games ORDER BY game_date, event_id").fetchall()
    with open(EXPORTS_DIR / "games.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(games[0].keys() if games else
                         ["event_id", "season_label", "season_key", "season_type", "game_date", "game_datetime",
                          "team1", "team2", "score1", "score2", "url", "scraped_at"])
        for g in games:
            writer.writerow([g[k] for k in g.keys()])

    snaps = conn.execute(
        "SELECT * FROM standings_snapshots ORDER BY season_key, season_type, as_of_date, team"
    ).fetchall()
    with open(EXPORTS_DIR / "standings_snapshots.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(snaps[0].keys() if snaps else
                         ["season_label", "season_key", "season_type", "as_of_date", "team", "team_icon",
                          "team_color", "gp", "w", "l", "t", "pts", "gf", "ga", "diff"])
        for s in snaps:
            writer.writerow([s[k] for k in s.keys()])
    return snaps


def push_standings_to_sheets(snaps):
    """
    Optional: if GCP_SA_KEY_JSON is set, overwrite a 'Standings History' tab
    in the shared Google Sheet with the current standings-by-date data
    (across all seasons/types -- the sheet isn't season-scoped).
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
        help="Restrict to one season, e.g. 2026-2027. Default: scrape every season found on the page.",
    )
    args = parser.parse_args()

    scope = f"season {args.season_key}" if args.season_key else "all seasons found on the page"
    print(f"Scraping past games ({scope}) ...")
    games = scrape_past_games(args.season_key)
    print(f"Found {len(games)} completed games on {PAST_GAMES_URL}")

    conn = get_db()
    added, updated = upsert_games(conn, games)
    print(f"Games table: +{added} new, {updated} updated")

    snap_count, n_seasons = rebuild_all_standings_snapshots(conn)
    print(f"Rebuilt {snap_count} standings-snapshot rows across {n_seasons} season/type groups")

    snaps = export_csvs(conn)
    print(f"Exported CSVs to {EXPORTS_DIR}")

    push_standings_to_sheets(snaps)

    conn.execute(
        "INSERT INTO scrape_log (script, run_at, rows_added, rows_updated, notes) VALUES (?, ?, ?, ?, ?)",
        ("scrape_games.py", datetime.now(timezone.utc).isoformat(), added, updated, args.season_key or "all"),
    )
    conn.commit()
    conn.close()


if __name__ == "__main__":
    main()
