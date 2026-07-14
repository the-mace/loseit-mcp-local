# LoseIt Local MCP — Build Brief

## Context

I use LoseIt (loseit.com) for diet/calorie tracking. There's no official
API and no working data export on the web app. This project scrapes my
own account via a headless browser, stores it locally, and exposes it to
Claude Desktop via MCP — mirroring an existing Garmin MCP setup I already
have running (same architecture: local Playwright/Python scraper → SQLite
→ stdio MCP server → `claude_desktop_config.json`).

**Known constraint:** this scrapes a personal account via the web UI and
will be fragile against LoseIt UI changes. Flag clearly if something during
the build makes unattended daily runs non-viable (e.g. LoseIt requires
solving a CAPTCHA on every login).

## What's already built

- `src/db.py` — SQLite schema + helpers (daily_summary, food_log,
  weight_log, exercise_log tables). Complete, should not need changes.
- `src/loseit_mcp.py` — MCP server (FastMCP) exposing 4 read-only tools
  over that SQLite data. Complete, should not need changes unless the
  scraped schema needs to change.
- `src/scraper.py` — Playwright scraper. **Login/session persistence is
  implemented and should work as-is.** The actual data-extraction
  functions (`fetch_day`, `fetch_weight`, `fetch_exercise`) are stubbed
  with `NotImplementedError` and docstring TODOs — this is the main thing
  to build.
- `scripts/install-launchd.sh` — writes launchd agents into
  `~/Library/LaunchAgents` with absolute paths derived at install time
  (labels: `com.loseit-mcp.scraper`, `com.loseit-mcp.scraper-health`).
- `requirements.txt` — playwright + mcp.

## Your task, in order

1. **Set up the environment.**
   ```
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   playwright install chromium
   ```

2. **Get me logged in.**
   Run `python src/scraper.py login`. This opens a real (headed) browser
   window on my Mac. Tell me to log in manually, wait for me to confirm,
   then it saves session state to `~/.loseit-data/state.json`.

3. **Record the real selectors together with me.**
   Run `playwright codegen --load-storage=~/.loseit-data/state.json
   https://www.loseit.com` and have me navigate to: a specific day's log
   page, the weight log, and the exercise log. Watch the generated code
   and the Inspector panel to capture the real selectors/URL patterns for:
   - daily calorie budget / eaten / remaining
   - daily macro totals (protein/carbs/fat) if shown
   - the repeating food-log row structure (name, brand, quantity, units,
     calories, macros per item)
   - weight log entries per date
   - exercise log entries per date (name, duration, calories burned)

   Ask me to confirm URL patterns and date formats rather than assuming.

4. **Fill in `fetch_day`, `fetch_weight`, `fetch_exercise` in
   `src/scraper.py`** using the real selectors from step 3. Keep the
   existing function signatures and the `db.upsert_daily_summary` /
   `db.insert_food_log_entry` / `db.upsert_weight` /
   `db.insert_exercise_entry` calls — just replace the stub bodies with
   real `page.goto()` / `page.locator()` logic.

5. **Test on a small range with a visible browser first:**
   ```
   python src/scraper.py run --since <5 days ago> --headed
   ```
   Then verify against the actual SQLite data:
   ```
   sqlite3 ~/.loseit-data/loseit.db "SELECT * FROM daily_summary ORDER BY date DESC LIMIT 5;"
   sqlite3 ~/.loseit-data/loseit.db "SELECT * FROM food_log ORDER BY date DESC LIMIT 10;"
   ```
   Iterate with me until the numbers match what I see on loseit.com for
   those same days. Don't consider this done until we've cross-checked
   at least 2-3 real days by hand.

6. **Run it headless once cleanly** (`python src/scraper.py run`, no
   `--headed`) to confirm it works without a visible browser — this is
   how it'll run daily.

7. **Schedule it.** Install launchd agents (paths filled from this checkout):
   ```
   bash scripts/install-launchd.sh --email <your-alert-email>
   launchctl start com.loseit-mcp.scraper   # trigger once immediately
   tail -f ~/.loseit-data/logs/scraper.log
   ```

8. **Wire the MCP server into Claude Desktop.** Config file lives at
   `~/Library/Application Support/Claude/claude_desktop_config.json`. If
   it already has other `mcpServers` entries (I have a `garmin` one),
   merge — don't overwrite the file. Add:
   ```json
   "loseit": {
     "command": "<absolute path>/.venv/bin/python",
     "args": ["<absolute path>/src/loseit_mcp.py"]
   }
   ```
   Tell me to fully quit (Cmd+Q) and reopen Claude Desktop, then confirm
   I see a `loseit` connector with 4 tools.

## Guardrails

- Don't guess at LoseIt's DOM structure — always drive it from what we
  actually observe together via codegen or by inspecting real page output.
- Don't weaken session security (e.g. don't print or log my LoseIt
  password; `.env`/`state.json` should stay out of any git commit —
  confirm `.gitignore` covers them if I decide to put this in git).
- If LoseIt's login flow requires solving anything that can't run
  unattended (recurring CAPTCHA, email verification each time), stop and
  tell me — that changes whether the daily scheduled run is viable at all,
  and I'd rather know than have it silently fail every morning.
- Cross-check scraped numbers against the real LoseIt UI before calling
  any step done — don't assume selectors worked just because the script
  ran without throwing.
