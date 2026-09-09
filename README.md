# ORMRHL Stats Scraper

Scrapes https://ormrhl.com for game-by-game results and individual player
box scores, stores everything in SQLite, exports CSVs, and publishes two
free, self-updating "race chart" pages via GitHub Pages — a standings race
and a scoring race — styled with real team colors and logos, with no need
for a paid Flourish plan.

## Why not Flourish for the automatic updates?

Flourish's Live CSV / Google Sheets linking (what makes a chart re-pull
data on its own) is only available on their paid **Publisher** and
**Enterprise** plans. On the free plan there's no way to point a chart at a
URL and have it refresh itself. Since the free plan isn't changing, this
project instead publishes two lightweight D3-based race-chart pages
straight from GitHub Pages, reading the CSVs this scraper produces —
genuinely automatic, free, and styled to match Flourish's look (team
logos + brand colors on every bar). You can still manually import any of
the `exports/*.csv` files into Flourish for a one-off if you ever want it.

## What it does

- **`scripts/scrape_games.py`** — scrapes `/past-games/` for the current
  season (auto-detected from today's date, or override with
  `--season-key`), upserts each completed game, and rebuilds a full
  **standings-by-date** history. Since the site publishes the full season
  schedule up front and posts scores as early as mid-game, this always
  re-parses the *entire* past-games listing and upserts every row — so a
  score that was live/partial when first scraped gets corrected
  automatically on the next run. Exports `exports/games.csv` and
  `exports/standings_snapshots.csv`.

- **`scripts/scrape_player_stats.py`** — by default, re-scrapes box scores
  for **every** game in the season (not just new ones), since scorekeepers
  can correct stats after a game. This is a small rec league, so a full
  daily refresh is cheap (a couple hundred small page fetches). Pass
  `--incremental` for a faster run that only fetches games with no stats
  yet — fine for a quick manual check, not recommended for the scheduled
  job, since it will miss corrections. Exports
  `exports/player_game_stats.csv` and a cumulative **scoring race** file,
  `exports/scoring_race.csv`, covering every player who's recorded a stat
  (no top-N cap at the data layer — the chart itself handles that, so it
  has a full pool to page through).

  **Mid-season trades:** each player's `team`/`team_icon`/`team_color` in
  the scoring-race export reflect whatever team they played for *as of
  that game* — so if a player is traded, games before the trade still show
  their old team and everything from the trade date onward shows the new
  one, without needing to touch historical rows.

Both scripts also optionally push the same data to Google Sheets if you
set a `GCP_SA_KEY_JSON` secret (see "Optional: Google Sheets" below).
Entirely inert if that secret isn't set.

Run `scrape_games.py` before `scrape_player_stats.py` — the second script
reads its list of games from the database the first one populates.

## Team colors and logos (config/teams.json)

Since the league site doesn't publish official team colors, `config/teams.json`
is a plain, human-editable file mapping each team to a `color` (hex) and a
fallback `logo` URL:

```json
"Leafs": { "color": "#1B4D8E", "logo": "https://ormrhl.com/wp-content/uploads/2025/09/leafsfin-128x128.png" }
```

The colors shipped here are placeholder guesses — edit this file any time
to swap in real brand colors, and add new rows as new teams join the
league. Matching is exact-name-first, then substring, so `"Komodo"` in the
config will also match `"Komodo Dragons"` scraped from the site. Team
logos scraped live off each game page (when present) take priority over
this file's `logo` fallback; the `color` here is always what's used for
bar colors, since the site has no equivalent "official color" to scrape.

## The self-updating charts (docs/)

`docs/` is a small static site meant to be served by **GitHub Pages**:

- `docs/index.html` — landing page linking to both charts
- `docs/standings.html` — animated bar-chart-race of team points over the season
- `docs/scoring.html` — animated bar-chart-race of the top scorers' running point totals
- `docs/race-chart.js` — the shared, reusable D3 rendering logic
- `docs/style.css` — styling

Both charts show each bar in its team's color with the team logo overlaid
at the end of the bar, matching Flourish's look. Each chart also has:

- A **"Show" count** input (defaults: 8 for standings, 10 for scoring) —
  change it any time to display more or fewer bars.
- **Prev / Next pagination** — once more entities exist than the current
  "Show" count (mainly relevant for the scoring race, once more than ~10
  players have points), page through the rest ranked by their final total.
- Play/pause and a scrubber to move through the season date by date.

The GitHub Actions workflow copies the freshly-scraped CSVs into
`docs/data/` and commits them every run, so once Pages is enabled these
pages update themselves with zero manual steps.

**One-time setup to enable this:**
1. Push this repo to GitHub.
2. Repo Settings → Pages → **Deploy from a branch** → branch `main`,
   folder `/docs` → Save.
3. Embed `https://yourname.github.io/ORMRHL/standings.html` (and
   `/scoring.html`) on your own site via `<iframe>`.

## Running locally

```bash
pip install -r requirements.txt
python scripts/scrape_games.py
python scripts/scrape_player_stats.py
```

Add `--season-key 2026-2027` to either script to override auto-detection.
Add `--incremental` to `scrape_player_stats.py` to skip already-scraped
games for a faster (but corrections-blind) run.

## Running on a schedule via GitHub Actions

`.github/workflows/scrape.yml` runs automatically every day at 9am UTC,
and can be triggered manually from the **Actions** tab → "Scrape ORMRHL
data" → **Run workflow**, with optional overrides for season and for a
faster incremental-only player-stats run. It commits the updated
`data/ormrhl.db`, `exports/*.csv`, and `docs/data/*.csv` straight back to
the repo. No secrets are required for this core path, since it only reads
a public site.

**On the full schedule being published up front:** since the whole
season's games (including ones not yet played) may appear on the site
before they happen, `scrape_games.py` only keeps rows with an actual score
in them — future/unplayed games are naturally skipped without any special
handling. A game whose score is captured mid-play will look "final" to the
scraper until the next run re-fetches and corrects it; running the
scheduled job more than once a day (or right after game nights) will
tighten that window if it matters to you.

## Optional: Google Sheets

If you ever upgrade to Flourish's Publisher plan (or just want a live
Sheet for yourself), set a `GCP_SA_KEY_JSON` repository secret containing
your Google service-account JSON key, shared with that service account's
email as an editor on a Sheet named `ORMRHL Stats` (or set
`GOOGLE_SHEETS_DOC_NAME` to match). Each run then overwrites two tabs —
"Standings History" and "Scoring Race" — with the current data. In that
Sheet: File → Share → Publish to web → CSV → check "automatically
republish when changes are made", then paste that URL into Flourish's
Data tab → Import from URL.

## Data model (SQLite)

- `games` — one row per game (event_id, teams, score, date, season)
- `standings_snapshots` — one row per (team, date), with color/logo
- `player_game_stats` — one row per (player, game), with that game's
  team/color/logo (so historical rows stay accurate through a trade)
- `scrape_log` — simple audit trail of each run

## Known assumptions / things to double check once real data flows through

1. **Player box scores** are parsed two ways: first by targeting the
   site's SportsPress plugin markup (`.sp-table-caption`,
   `.sp-event-performance`, `tr.lineup`) — the same selectors the original
   `run_player_scrape.py` used successfully — falling back to generic
   HTML-table header matching if that markup isn't found (useful for the
   oddly-slugged playoff-final pages).
2. **`MI` / `MA` columns** are stored as `minor_pen` / `major_pen` on the
   assumption they're penalty minutes — unconfirmed.
3. **Playoff "Finals" games** sometimes use slugged URLs with a round
   label in the title; the scraper strips known prefixes to recover team
   names — double-check once playoffs start.
4. **Standings points system** is hardcoded as Win=2, Tie=1, Loss=0.
5. **Skater points leaderboard**: no skater G/A/PTS leaderboard was found
   in `/player-stats-YYYY-YYYY/`'s static HTML, so the scoring race is
   built entirely from aggregated game box scores.
6. **Team colors** in `config/teams.json` are placeholder guesses — the
   site doesn't publish official ones. Update the file whenever you have
   real colors.
