# LoseIt Local MCP: agent instructions

Shared rules (pull before work, tests and lint, secrets, commit attribution)
live in the global `~/.config/agents/AGENTS.md`. If that file is not loaded
(cloud or CI sessions), the short version is: `git pull --ff-only` first, run
the tests below before committing, never commit secrets, and end agent commits
with a `Co-Authored-By: <Tool> (<model>) <email>` trailer.

## Which coding agent

Use Claude Code with Sonnet for small, contained fixes driven by an alert email
(scrape timeouts, expired sessions, catch-up runs). Use Opus 5.5 for multi-file,
schema, or session-security changes. Grok Build is the second opinion.

## Repo specifics

- After a pull that touches `requirements.txt`:
  `source .venv/bin/activate && pip install -r requirements.txt`.
- Tests: `.venv/bin/python -m pytest` (CI runs the same suite).

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

## Layout

- `src/db.py` — SQLite schema + helpers (`daily_summary`, `food_log`,
  `weight_log`, `water_log`).
- `src/loseit_mcp.py` — MCP server (mcp v2) exposing 4 read-only tools:
  `get_daily_summary`, `get_food_log`, `get_weight_history`,
  `get_water_log`.
- `src/scraper.py` — Playwright scraper. `login` opens a headed browser and
  saves the session to `~/.loseit-data/state.json`; `run` fetches yesterday
  (or `--since YYYY-MM-DD`), `--headed` for a visible browser. `fetch_day`,
  `fetch_weight`, and `fetch_water` read the live site. Individual exercise
  entries are deliberately not scraped; only the day's exercise-calorie
  total is kept, so remaining calories match LoseIt's own figure.
- `scripts/install-launchd.sh` — writes launchd agents into
  `~/Library/LaunchAgents` with absolute paths derived at install time
  (labels: `com.loseit-mcp.scraper`, `com.loseit-mcp.scraper-health`).
- `requirements.txt` — playwright + mcp.

## Changing the scraper

When LoseIt's UI changes, re-record selectors with
`playwright codegen --load-storage=~/.loseit-data/state.json https://www.loseit.com`
and confirm URL patterns and date formats with Rob rather than assuming.
Test on a few recent days with `python src/scraper.py run --since <date> --headed`,
then check the stored rows:

```
sqlite3 ~/.loseit-data/loseit.db "SELECT * FROM daily_summary ORDER BY date DESC LIMIT 5;"
sqlite3 ~/.loseit-data/loseit.db "SELECT * FROM food_log ORDER BY date DESC LIMIT 10;"
```

A change is done when 2-3 real days match loseit.com by hand and a headless
`run` succeeds (that is how it runs daily). Logs: `~/.loseit-data/logs/`.

The MCP server is wired into Claude Desktop through
`~/Library/Application Support/Claude/claude_desktop_config.json`, which
also holds other `mcpServers` entries (`garmin`): merge, don't overwrite.

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
