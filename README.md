# LoseIt local MCP

A local pipeline: Playwright scrapes your LoseIt data daily → stores it in
SQLite → an MCP server reads it into Claude Desktop. There is no public
LoseIt API, so this reads your own account through the web UI and keeps
the data on your machine. The scraper may need updates if LoseIt changes
their site.

**License:** MIT (see [LICENSE](LICENSE)).

## Privacy note

Session cookies, SQLite food/weight history, and logs live under
`~/.loseit-data/` (outside this repo). Do not commit that directory, any
`state.json`, or `*.db` files. The included `.gitignore` is set up to keep
them out.

## 1. Install dependencies

```bash
cd /path/to/loseit-mcp-local
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

## 2. Log in once (saves your session)

```bash
python src/scraper.py login
```

A real browser window opens. Log into LoseIt manually (this sidesteps any
CAPTCHA/bot-check LoseIt might show, since it's genuinely you clicking).
Once you're on your dashboard, go back to the terminal and press Enter.
Your session is saved to `~/.loseit-data/state.json`.

## 3. Run the scraper

Selectors in `src/scraper.py` were captured against LoseIt's GWT web UI and
**will break** when that DOM changes. If a run fails after a LoseIt deploy,
re-record with:

```bash
playwright codegen --load-storage=~/.loseit-data/state.json https://www.loseit.com
```

…and update the locators in `src/scraper.py`.

Test on a small range first, with a visible browser:

```bash
python src/scraper.py run --since 2026-07-10 --headed
```

Check the data landed correctly:

```bash
sqlite3 ~/.loseit-data/loseit.db "SELECT * FROM daily_summary ORDER BY date DESC LIMIT 5;"
```

Headless (how the scheduled job runs):

```bash
python src/scraper.py run
```

## 4. Schedule the daily run (+ optional email alerts)

Two launchd jobs (macOS):

| Job | When | What |
|-----|------|------|
| `com.loseit-mcp.scraper` | 06:00 | Scrape via `scripts/run-scraper.sh`. On failure, emails you if configured. On success, writes `~/.loseit-data/last_success`. |
| `com.loseit-mcp.scraper-health` | 12:00 | If `last_success` is older than 36h (or missing and DB is stale), emails a STALE alert (at most once per day) if configured. |

Alerts use the local `mail` command and only send when
`LOSEIT_ALERT_EMAIL` is set. Install the agents (paths are filled in from
this checkout — nothing machine-specific is stored in the repo):

```bash
# recommended: pass your alert address at install time
bash scripts/install-launchd.sh --email you@example.com

# or without email (failures only show up in local logs)
bash scripts/install-launchd.sh
```

`install-launchd.sh` copies the shell wrappers to `~/.loseit-data/bin/` and
points launchd at those copies. macOS TCC blocks launchd from *executing*
scripts under `~/Documents` (Python in the project tree is fine). Re-run
the installer after you edit the scripts so the copies stay in sync.

Test immediately:

```bash
launchctl start com.loseit-mcp.scraper
tail -f ~/.loseit-data/logs/scraper.log
```

Test the health check:

```bash
launchctl start com.loseit-mcp.scraper-health
# or: bash scripts/health-check.sh
```

Uninstall:

```bash
bash scripts/uninstall-launchd.sh
```

## 5. Wire the MCP server into Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`
(merge into `mcpServers` if the file already has other entries). Use the
**absolute** path to this checkout:

```json
{
  "mcpServers": {
    "loseit": {
      "command": "/ABSOLUTE/PATH/TO/loseit-mcp-local/.venv/bin/python",
      "args": ["/ABSOLUTE/PATH/TO/loseit-mcp-local/src/loseit_mcp.py"]
    }
  }
}
```

Fully quit and reopen Claude Desktop. You should see 4 tools under a
`loseit` connector: `get_daily_summary`, `get_food_log`,
`get_weight_history`, `get_water_log`.

## Notes

- The MCP server only ever reads local SQLite — it never talks to LoseIt
  itself. All the network activity is in `scraper.py`, run on its own
  schedule.
- Scope: daily summary (including aggregate exercise calories burned),
  food log, weight, and water. Individual exercise entries are not scraped.
- Persistent logs live under `~/.loseit-data/logs/`. Success stamp:
  `~/.loseit-data/last_success`.
- Session cookies can expire or get invalidated; if `run` starts failing
  across the board, redo step 2 (`python src/scraper.py login`).
