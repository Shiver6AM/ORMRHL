"""
scrape_player_stats.py

By default, re-scrapes box scores for EVERY game in the season (not just
new ones) each run, since scorekeepers can correct stats after the fact —
this is a small rec league (a couple hundred games/season), so a full daily
refresh is cheap and keeps corrections flowing through automatically. Pass
--incremental for a faster run that only fetches games not seen before
(useful for quick manual checks, not recommended for the scheduled job).

Also builds a cumulative "scoring race over time" export for charting.

Usage:
    python scripts/scrape_player_stats.py                  # full refresh (default)
    python scripts/scrape_player_stats.py --incremental     # only new games
    python scripts/scrape_player_stats.py --season-key 2026-2027
"""

import argparse
import csv
from datetime import datetime, timezone

from common import (
    BASE_URL, get_soup, get_db, current_season_window, team_icon, team_color,
    get_sheets_client, overwrite_worksheet, sheets_doc_name, EXPORTS_DIR,
)

EVENT_URL_TMPL = f"{BASE_URL}/event/{{event_id}}/"

ROSTER_HEADER_HINTS = {"player", "position", "g", "a"}


def is_valid_player_name(name: str) -> bool:
    """Filters out stray numeric-only cells and the 'Total' summary row."""
    import re
    return bool(name and name.lower() != "total" and not name.isdigit() and re.search(r"[a-zA-Z]", name))


def find_roster_tables_sportspress(soup):
    """
    Primary strategy: target SportsPress plugin markup directly, the same
    approach the previous scraper attempt used successfully against the live
    site (.sp-section-content-performance / .sp-table-caption / tr.lineup).
    Returns a list of (team_name, table_element) — the table here is the
    <table class="sp-event-performance">, handled by a dedicated row parser
    below since its row structure doesn't match the generic header-matching path.
    """
    rosters = []
    blocks = soup.select(".sp-section-content-performance .sp-template-event-performance")
    if not blocks:
        blocks = soup.select(".sp-event-performance-tables .sp-template-event-performance")
    for block in blocks:
        caption = block.find("h4", class_="sp-table-caption")
        team_name = caption.get_text(strip=True) if caption else None
        table = block.find("table", class_="sp-event-performance")
        if table is not None:
            rosters.append((team_name, table))
    return rosters


def extract_team_icons_from_page(soup):
    mapping = {}
    for span in soup.select(".sp-section-content-logos .sp-team-logo"):
        img = span.find("img")
        src = (img.get("src", "").strip() if img else "")
        for strong in span.select(".sp-team-name"):
            name = strong.get_text(strip=True)
            if name and src:
                mapping[name] = src
    return mapping


def parse_sportspress_table(table, team_name, icon_url, event_id, season_label, game_date):
    rows_out = []
    tbody = table.find("tbody")
    rows = tbody.find_all("tr", class_="lineup") if tbody else []
    for row in rows:
        tds = row.find_all("td")
        if len(tds) < 5:
            continue
        name = tds[1].get_text(strip=True)
        if not is_valid_player_name(name):
            continue
        link = tds[1].find("a")
        slug = link["href"].rstrip("/").rsplit("/", 1)[-1] if (link and link.get("href")) else name.lower().replace(" ", "-")
        position = tds[2].get_text(strip=True) if len(tds) > 2 else None

        def safe_int(cell):
            txt = cell.get_text(strip=True)
            return int(txt) if txt.lstrip("-").isdigit() else 0

        goals = safe_int(tds[3]) if len(tds) > 3 else 0
        assists = safe_int(tds[4]) if len(tds) > 4 else 0
        minor = safe_int(tds[5]) if len(tds) > 5 else 0
        major = safe_int(tds[6]) if len(tds) > 6 else 0

        rows_out.append({
            "event_id": event_id,
            "season_label": season_label,
            "game_date": game_date,
            "team": team_name or "Unknown",
            "team_icon": icon_url or team_icon(team_name or ""),
            "team_color": team_color(team_name or ""),
            "player_name": name,
            "player_slug": slug,
            "position": position,
            "goals": goals,
            "assists": assists,
            "points": goals + assists,
            "minor_pen": minor,
            "major_pen": major,
        })
    return rows_out


def find_roster_tables_generic(soup):
    """
    Fallback strategy (in case a page's markup doesn't match SportsPress'
    templates — e.g. some of the odd slugged playoff-final pages): match
    tables generically by header text instead of a specific CSS structure.
    """
    rosters = []
    for table in soup.find_all("table"):
        header_row = table.find("tr")
        if not header_row:
            continue
        headers = [c.get_text(strip=True).lower() for c in header_row.find_all(["th", "td"])]
        if not ROSTER_HEADER_HINTS.issubset(set(headers)):
            continue
        team_name = None
        for sib in table.find_all_previous(["h1", "h2", "h3", "h4", "h5"]):
            text = sib.get_text(strip=True)
            if text:
                team_name = text
                break
        rosters.append((team_name, headers, table))
    return rosters


def col_index(headers, *candidates):
    for cand in candidates:
        if cand in headers:
            return headers.index(cand)
    return None


def parse_generic_table(team_name, headers, table, event_id, season_label, game_date):
    idx_player = col_index(headers, "player")
    idx_pos = col_index(headers, "position")
    idx_g = col_index(headers, "g")
    idx_a = col_index(headers, "a")
    idx_mi = col_index(headers, "mi")
    idx_ma = col_index(headers, "ma")

    rows_out = []
    for row in table.find_all("tr")[1:]:
        cells = row.find_all("td")
        if not cells or idx_player is None or idx_player >= len(cells):
            continue
        player_cell = cells[idx_player]
        player_link = player_cell.find("a")
        player_name = player_cell.get_text(strip=True)
        if not is_valid_player_name(player_name):
            continue
        player_slug = (
            player_link["href"].rstrip("/").rsplit("/", 1)[-1]
            if (player_link and player_link.get("href"))
            else player_name.lower().replace(" ", "-")
        )

        def safe_int(i):
            if i is None or i >= len(cells):
                return 0
            txt = cells[i].get_text(strip=True)
            return int(txt) if txt.lstrip("-").isdigit() else 0

        goals = safe_int(idx_g)
        assists = safe_int(idx_a)
        rows_out.append({
            "event_id": event_id,
            "season_label": season_label,
            "game_date": game_date,
            "team": team_name or "Unknown",
            "team_icon": team_icon(team_name or ""),
            "team_color": team_color(team_name or ""),
            "player_name": player_name,
            "player_slug": player_slug,
            "position": cells[idx_pos].get_text(strip=True) if idx_pos is not None else None,
            "goals": goals,
            "assists": assists,
            "points": goals + assists,
            "minor_pen": safe_int(idx_mi),
            "major_pen": safe_int(idx_ma),
        })
    return rows_out


def scrape_event_player_stats(event_id: str, season_label: str, game_date: str):
    url = EVENT_URL_TMPL.format(event_id=event_id)
    soup = get_soup(url)

    sportspress_rosters = find_roster_tables_sportspress(soup)
    if sportspress_rosters:
        icon_map = extract_team_icons_from_page(soup)
        stats_rows = []
        for team_name, table in sportspress_rosters:
            icon_url = icon_map.get(team_name) or team_icon(team_name or "")
            stats_rows.extend(
                parse_sportspress_table(table, team_name, icon_url, event_id, season_label, game_date)
            )
        return stats_rows

    # Fallback path — SportsPress markup not found on this page.
    generic_rosters = find_roster_tables_generic(soup)
    stats_rows = []
    for team_name, headers, table in generic_rosters:
        stats_rows.extend(parse_generic_table(team_name, headers, table, event_id, season_label, game_date))
    return stats_rows


def games_needing_stats(conn, season_key: str, incremental: bool):
    """
    By default (incremental=False) returns every game for the season, so a
    full re-scrape happens daily and picks up any post-game stat corrections.
    Pass incremental=True to only return games with no stats recorded yet
    (faster, but will miss corrections to already-scraped games).
    """
    window = current_season_window()
    if season_key != window["season_key"]:
        start_year, end_year = (int(x) for x in season_key.split("-"))
        regular_label = f"Regular Season {start_year}-{end_year}"
        playoffs_fragment = f"Playoffs {end_year}"
    else:
        regular_label = window["regular_season_label"]
        playoffs_fragment = window["playoffs_label_fragment"]

    games = conn.execute(
        """
        SELECT event_id, season_label, game_date FROM games
        WHERE season_label = ? OR season_label LIKE ?
        ORDER BY game_date ASC, event_id ASC
        """,
        (regular_label, f"%{playoffs_fragment}%"),
    ).fetchall()

    if not incremental:
        return games

    already_done = {
        r["event_id"]
        for r in conn.execute("SELECT DISTINCT event_id FROM player_game_stats").fetchall()
    }
    return [g for g in games if g["event_id"] not in already_done]


def upsert_stats(conn, rows):
    added = 0
    for r in rows:
        conn.execute(
            """
            INSERT INTO player_game_stats
                (event_id, season_label, game_date, team, team_icon, team_color, player_name, player_slug,
                 position, goals, assists, points, minor_pen, major_pen)
            VALUES (:event_id, :season_label, :game_date, :team, :team_icon, :team_color, :player_name, :player_slug,
                    :position, :goals, :assists, :points, :minor_pen, :major_pen)
            ON CONFLICT(event_id, player_slug) DO UPDATE SET
                team=excluded.team,
                team_icon=excluded.team_icon,
                team_color=excluded.team_color,
                position=excluded.position,
                goals=excluded.goals,
                assists=excluded.assists,
                points=excluded.points,
                minor_pen=excluded.minor_pen,
                major_pen=excluded.major_pen
            """,
            r,
        )
        added += 1
    conn.commit()
    return added


def export_player_game_stats_csv(conn):
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    rows = conn.execute(
        "SELECT * FROM player_game_stats ORDER BY game_date, event_id, team, player_name"
    ).fetchall()
    with open(EXPORTS_DIR / "player_game_stats.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(rows[0].keys() if rows else
                         ["event_id", "season_label", "game_date", "team", "team_icon", "team_color", "player_name",
                          "player_slug", "position", "goals", "assists", "points",
                          "minor_pen", "major_pen"])
        for r in rows:
            writer.writerow([r[k] for k in r.keys()])


def export_scoring_race_csv(conn, season_key: str):
    """
    Long-format cumulative points per player per game-date, for EVERY player
    who has recorded a stat this season (no top-N cap here — the chart UI
    does its own configurable top-N + pagination client-side, so it needs
    the full pool to page through).

    Each row's `team`/`team_icon`/`team_color` reflect that player's most
    recent team as of that date — so a mid-season trade shows the old team
    on rows before the trade and the new team from the trade date onward,
    rather than being frozen at whichever team they started the season on.
    """
    window = current_season_window()
    if season_key != window["season_key"]:
        start_year, end_year = season_key.split("-")
        regular_label = f"Regular Season {start_year}-{end_year}"
    else:
        regular_label = window["regular_season_label"]

    rows = conn.execute(
        """
        SELECT game_date, player_name, player_slug, team, team_icon, team_color, goals, assists, points
        FROM player_game_stats
        WHERE season_label = ?
        ORDER BY game_date ASC, event_id ASC
        """,
        (regular_label,),
    ).fetchall()

    running = {}
    out_rows = []
    for r in rows:
        key = r["player_slug"]
        if key not in running:
            running[key] = {"goals": 0, "assists": 0, "points": 0, "player_name": r["player_name"]}
        running[key]["goals"] += r["goals"]
        running[key]["assists"] += r["assists"]
        running[key]["points"] += r["points"]
        # Always take THIS row's team/icon/color -- i.e. the player's most
        # recent team as of this game -- rather than whatever it was set to
        # on their first appearance. This is what makes a mid-season trade
        # show up correctly from the trade date forward.
        out_rows.append(
            {
                "game_date": r["game_date"],
                "player_name": running[key]["player_name"],
                "player_slug": key,
                "team": r["team"],
                "team_icon": r["team_icon"],
                "team_color": r["team_color"],
                "cumulative_goals": running[key]["goals"],
                "cumulative_assists": running[key]["assists"],
                "cumulative_points": running[key]["points"],
            }
        )

    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    fieldnames = ["game_date", "player_name", "player_slug", "team", "team_icon", "team_color",
                  "cumulative_goals", "cumulative_assists", "cumulative_points"]
    with open(EXPORTS_DIR / "scoring_race.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(out_rows)
    return out_rows


def push_scoring_race_to_sheets(out_rows):
    gc = get_sheets_client()
    if gc is None:
        print("Sheets sync not configured (no GCP_SA_KEY_JSON) — skipping Sheets push.")
        return
    if not out_rows:
        print("No scoring-race rows to push to Sheets.")
        return
    header = list(out_rows[0].keys())
    rows = [[r[k] for k in header] for r in out_rows]
    try:
        overwrite_worksheet(gc, sheets_doc_name(), "Scoring Race", header, rows)
        print(f"Pushed {len(rows)} rows to Google Sheet '{sheets_doc_name()}' / tab 'Scoring Race'")
    except Exception as e:
        print(f"WARNING: Sheets push failed, continuing anyway: {e}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--season-key", default=None, help="e.g. 2026-2027")
    parser.add_argument("--incremental", action="store_true",
                         help="Only scrape games with no stats yet (faster, but misses "
                              "corrections to already-scraped games). Default is a full "
                              "re-scrape of every game, since stats can be corrected after "
                              "the fact.")
    args = parser.parse_args()

    window = current_season_window()
    season_key = args.season_key or window["season_key"]

    conn = get_db()
    todo = games_needing_stats(conn, season_key, incremental=args.incremental)
    mode = "incremental (new games only)" if args.incremental else "full refresh (all games)"
    print(f"{len(todo)} game(s) to scrape for season {season_key} [{mode}]")

    total_rows = 0
    for i, g in enumerate(todo, start=1):
        print(f"  [{i}/{len(todo)}] event {g['event_id']} ({g['game_date']}) ...")
        try:
            rows = scrape_event_player_stats(g["event_id"], g["season_label"], g["game_date"])
        except Exception as e:
            print(f"    WARNING: failed to scrape event {g['event_id']}: {e}")
            continue
        upsert_stats(conn, rows)
        total_rows += len(rows)

    print(f"Upserted {total_rows} player-game stat lines")

    export_player_game_stats_csv(conn)
    race_rows = export_scoring_race_csv(conn, season_key)
    print(f"Exported scoring_race.csv ({len(race_rows)} rows across all players)")

    push_scoring_race_to_sheets(race_rows)

    conn.execute(
        "INSERT INTO scrape_log (script, run_at, rows_added, rows_updated, notes) VALUES (?, ?, ?, ?, ?)",
        ("scrape_player_stats.py", datetime.now(timezone.utc).isoformat(), total_rows, 0, season_key),
    )
    conn.commit()
    conn.close()


if __name__ == "__main__":
    main()
