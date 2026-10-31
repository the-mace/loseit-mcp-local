"""
LoseIt scraper using Playwright.

Selectors below were captured by driving a real logged-in session with
Playwright and inspecting the rendered DOM (LoseIt's web app is a GWT
single-page app with no per-day URL -- /log/date/YYYY-MM-DD 404s. Day
navigation only works through the prev/next arrow buttons in the UI, which
is why _goto_date() clicks through them and confirms the on-page date
header rather than trusting a URL).

Most CSS classes on this page are GWT-obfuscated build hashes (e.g.
"GPOEIKGJPB") that will very likely change on LoseIt's next deploy. Where
possible the selectors below key off stable things instead: fixed built-in
GWT widget classes (gwt-Anchor, gwt-HTML), title/role attributes
(title="Previous", role="button"), and label-text lookups (find the cell
whose text is exactly "Budget", read its sibling). This is more robust to
a re-deploy than hardcoded hash classes, but is still DOM-shape-dependent
-- expect it to need touch-ups eventually.

Scope: food log, daily notes, body weight, and water intake. Individual
exercise entries are intentionally not scraped (out of scope per requirements)
even though the daily log page includes them -- the one exception is the day's
aggregate "Exercise" calories-burned total from the summary row, captured only
so calories_remaining_after_exercise can match LoseIt's own over/under-budget
figure, which nets out exercise credit.

Run modes:
  python scraper.py login       -> prompts for email/password, logs in via
                                    the app's token API (see auth.py), saves
                                    tokens and seeds ~/.loseit-data/state.json
  python scraper.py login --browser
                                -> old flow: headed browser, log in manually;
                                    no refresh token, so it lasts 14 days
  python scraper.py run         -> headless run: fetch + store data for
                                    yesterday (or --since YYYY-MM-DD)
  python scraper.py run --headed -> same as run, but visible browser
                                     (useful while you're still tuning selectors)
"""
import argparse
import getpass
import json
import re
import sys
import time
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
import auth
import db

STATE_PATH = Path.home() / ".loseit-data" / "state.json"
LOSEIT_BASE = "https://www.loseit.com"
# These carry a 14-day access token; browsing does not extend them, only
# auth.refresh_session does. Once they expire, www.loseit.com renders the
# public marketing page instead of the GWT day log.
_AUTH_COOKIE_NAMES = auth.AUTH_COOKIE_NAMES
_DATE_HEADER_RE_JS = r"^[A-Za-z]+\s+[A-Za-z]{3}\s+\d{1,2},\s+\d{4}$"


def _auth_session_expired(state_path: Path, now_ts: float | None = None) -> bool:
    """True when saved auth cookies are missing or past their expiry.

    Session cookies (expires -1/0/None) count as still valid: Playwright
    will send them on the next launch. Unknown cookie names are ignored so
    a LoseIt rename falls through to the live-page check instead.
    """
    now_ts = time.time() if now_ts is None else now_ts
    try:
        data = json.loads(state_path.read_text())
    except (OSError, json.JSONDecodeError):
        return True
    cookies = [c for c in data.get("cookies", []) if c.get("name") in _AUTH_COOKIE_NAMES]
    if not cookies:
        return True
    for c in cookies:
        exp = c.get("expires")
        if exp in (None, -1, 0):
            return False
        try:
            if float(exp) > now_ts:
                return False
        except (TypeError, ValueError):
            continue
    return True


def _page_state(page) -> str:
    """Classify the current page: dashboard, login, marketing, or unknown."""
    return page.evaluate(
        """(pattern) => {
            const url = location.href;
            if (/\\/login/i.test(url)) return 'login';
            const re = new RegExp(pattern);
            const els = document.querySelectorAll('div,span');
            for (const el of els) {
                if (el.children.length === 0) {
                    const t = el.textContent.replace(/\\u00a0/g, ' ').trim();
                    if (re.test(t)) return 'dashboard';
                }
            }
            if (document.querySelector('.prevArrowButton, .nextArrowButton')) {
                return 'dashboard';
            }
            const text = document.body ? document.body.innerText : '';
            if (/Sign Up for Free/i.test(text) && /Log In/i.test(text)) {
                return 'marketing';
            }
            if (document.querySelector('input[type=password]')) return 'login';
            return 'unknown';
        }""",
        _DATE_HEADER_RE_JS,
    )


def _await_dashboard(page, timeout_ms: int = 30000):
    """Wait until the GWT day log is visible, or fail if we landed logged out."""
    deadline = time.time() + timeout_ms / 1000
    logged_out_hits = 0
    last_state = "unknown"
    while time.time() < deadline:
        last_state = _page_state(page)
        if last_state == "dashboard":
            return
        if last_state in ("login", "marketing"):
            logged_out_hits += 1
            # A couple of samples so a slow redirect isn't a false logout.
            if logged_out_hits >= 4:
                raise RuntimeError(
                    "LoseIt session expired or is not logged in. "
                    "Run `.venv/bin/python src/scraper.py login`, then `.venv/bin/python src/scraper.py run` to catch up."
                )
        page.wait_for_timeout(500)
    raise RuntimeError(
        f"Couldn't find the displayed date header on the page (last state: {last_state})."
    )


def do_api_login():
    """Prompt for LoseIt credentials and log in through the app's token API."""
    default = (auth.load_tokens() or {}).get("username", "")
    prompt = f"LoseIt email [{default}]: " if default else "LoseIt email: "
    username = input(prompt).strip() or default
    if not username:
        raise SystemExit("An email address is required.")
    password = getpass.getpass("LoseIt password (not shown): ")
    try:
        auth.password_login(username, password, STATE_PATH)
    except auth.AuthError as exc:
        raise SystemExit(str(exc))
    print(f"Logged in. Tokens saved to {auth.TOKENS_PATH}; session seeded in {STATE_PATH}.")


def do_login():
    """Opens a headed browser so you can log in manually (handles any
    CAPTCHA/verification LoseIt throws up), then saves the session."""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(f"{LOSEIT_BASE}/login")
        print("Log in manually in the opened browser window (complete any CAPTCHA).")
        print("If there is a 'remember me' / stay signed in option, turn it on.")
        print("This will save the session once your daily log is visible — no need to press Enter.")
        deadline = time.time() + 15 * 60
        while time.time() < deadline:
            if _page_state(page) == "dashboard":
                break
            page.wait_for_timeout(1000)
        else:
            raise RuntimeError(
                "Timed out waiting for a logged-in LoseIt daily log. "
                "Leave the log page visible and re-run `.venv/bin/python src/scraper.py login`."
            )
        context.storage_state(path=str(STATE_PATH))
        STATE_PATH.chmod(0o600)
        browser.close()
    print(f"Session saved to {STATE_PATH}")


def _read_displayed_date(page) -> date:
    """Read the 'Monday Jul 13, 2026' style header. Used to confirm date
    navigation actually landed where we think it did, instead of trusting
    a fixed sleep after clicking the arrow buttons."""
    text = page.evaluate(
        """(pattern) => {
            const re = new RegExp(pattern);
            const els = document.querySelectorAll('div,span');
            for (const el of els) {
                if (el.children.length === 0) {
                    const t = el.textContent.replace(/\\u00a0/g, ' ').trim();
                    if (re.test(t)) return t;
                }
            }
            return null;
        }""",
        _DATE_HEADER_RE_JS,
    )
    if not text:
        raise RuntimeError("Couldn't find the displayed date header on the page.")
    return datetime.strptime(text, "%A %b %d, %Y").date()


def _day_fingerprint(page) -> str:
    """Cheap snapshot of the bound day log: summary Food value plus food names.

    The date header updates before GWT rebinds Budget/Food and the meal
    list. Waiting for this to change after a click avoids reading the
    previous day's totals (Food showing '-' / 0 while the header already
    says the new date).
    """
    return page.evaluate(
        """() => {
            const norm = s => (s || '').replace(/[\\s\\u00a0]+/g, ' ').trim();
            let food = '';
            const el = Array.from(document.querySelectorAll('div.gwt-HTML'))
                .find(e => e.textContent.trim() === 'Food');
            if (el && el.nextElementSibling) food = el.nextElementSibling.textContent.trim();
            const names = Array.from(document.querySelectorAll('a.gwt-Anchor'))
                .map(a => norm(a.textContent)).filter(Boolean).slice(0, 12);
            return food + '|' + names.join(',');
        }"""
    )


def _goto_date(page, target: date):
    """Click the prev/next day arrow enough times to reach `target`.

    LoseIt has no per-day URL (confirmed: /log/date/YYYY-MM-DD 404s), so
    navigation only happens through these in-page buttons. We verify by
    re-reading the date header after each click rather than assuming N
    clicks worked — important on catch-up runs that may be tens of days.
    After the header changes we also wait for the day's food/summary
    data to rebind; the header otherwise wins the race.
    """
    current = _read_displayed_date(page)
    if current == target:
        return
    selector = ".nextArrowButton" if target > current else ".prevArrowButton"
    steps = abs((target - current).days)
    for _ in range(steps):
        before = current
        before_fp = _day_fingerprint(page)
        page.click(selector)
        for _ in range(20):
            current = _read_displayed_date(page)
            if current != before:
                break
            page.wait_for_timeout(150)
        else:
            raise RuntimeError(
                f"Date header did not change after clicking toward {target.isoformat()}; "
                f"still {current.isoformat()}"
            )
        for _ in range(25):
            if _day_fingerprint(page) != before_fp:
                break
            page.wait_for_timeout(100)
    if current != target:
        raise RuntimeError(
            f"Could not navigate to {target.isoformat()}; "
            f"page still shows {current.isoformat()}"
        )


def _parse_amount(token: str):
    """'14' / '1.5' / '7/8' / '½' / '1½' -> float, else None."""
    if token and unicodedata.category(token[-1]) == "No":
        whole = token[:-1]
        if whole and not whole.isdigit():
            return None
        return (int(whole) if whole else 0) + unicodedata.numeric(token[-1])
    if "/" in token:
        num, _, den = token.partition("/")
        if not (num.isdigit() and den.isdigit()) or int(den) == 0:
            return None
        return int(num) / int(den)
    try:
        return float(token)
    except ValueError:
        return None


def _is_fraction(token: str) -> bool:
    if len(token) == 1:
        return unicodedata.category(token) == "No" and 0 < unicodedata.numeric(token) < 1
    num, sep, den = token.partition("/")
    return bool(sep) and num.isdigit() and den.isdigit() and int(den) != 0


def _parse_quantity(text: str):
    """'2  Servings' -> (2.0, 'Servings'); '1/2 Each' -> (0.5, 'Each');
    '14 7/8 Fluid ounces' -> (14.875, 'Fluid ounces'); '1 ½ Cups' -> (1.5, 'Cups')."""
    tokens = text.split()
    qty = _parse_amount(tokens[0]) if tokens else None
    if qty is None or len(tokens) < 2:
        return None, text.strip()
    rest = tokens[1:]
    # LoseIt renders mixed amounts as two tokens: whole number, then fraction.
    if tokens[0].isdigit() and _is_fraction(rest[0]):
        qty += _parse_amount(rest[0])
        rest = rest[1:]
    return qty, " ".join(rest)


def _parse_number(text: str) -> float:
    text = text.strip().replace(",", "")
    if not text or text == "-":
        return 0.0
    return float(text)


def _summary_value(page, label: str):
    """Read a value from the Budget/Food/Exercise/Net/Over-Under row: each
    cell is a label div immediately followed by a sibling value div."""
    return page.evaluate(
        """(label) => {
            const el = Array.from(document.querySelectorAll('div.gwt-HTML'))
                .find(e => e.textContent.trim() === label);
            return el && el.nextElementSibling ? el.nextElementSibling.textContent.trim() : null;
        }""",
        label,
    )


def _nutrient_value(page, label: str):
    """Read a value from the 'My Nutrients' tab table: a label <td>
    followed by a sibling value <td> like '140g'."""
    return page.evaluate(
        """(label) => {
            const el = Array.from(document.querySelectorAll('td'))
                .find(e => e.textContent.trim() === label);
            const sib = el ? el.nextElementSibling : null;
            return sib ? sib.textContent.trim() : null;
        }""",
        label,
    )


def _click_tab(page, label: str):
    """Click the visible tab/label matching exact text `label`.

    Several of these labels (e.g. "Weight") also exist as a hidden
    aria-hidden accessibility duplicate elsewhere in the DOM, which
    Playwright's `text=` locator can match instead of the real, visible
    tab -- causing a click timeout. This filters to elements actually
    laid out on the page (offsetParent !== null) before clicking.
    """
    ok = page.evaluate(
        """(label) => {
            // Tab labels have empty <i>/<b> decorator children, so this
            // can't require a true leaf node -- just require the element's
            // own text (not a bigger ancestor's) match exactly. GWT also
            // renders these with &nbsp; between words instead of a plain
            // space, so normalize both sides before comparing.
            //
            // Some of these labels also have an aria-hidden accessibility
            // duplicate elsewhere in the DOM (display:none), wrapped in a
            // <td> that ISN'T itself hidden -- only its child div is. Two
            // checks handle this: excluding any candidate whose own child
            // already matches (catches the wrapping <td>), and checking
            // getComputedStyle for display:none directly on the candidate
            // itself (catches the hidden div). innerText looked like the
            // obvious tool for this but is unreliable here -- it doesn't
            // reflect display:none correctly for this page in headless
            // Chromium, so this uses textContent + explicit style checks
            // instead.
            const norm = s => s.replace(/[\\s\\u00a0]+/g, ' ').trim();
            const target = norm(label);
            const isHidden = e => {
                // getComputedStyle only reflects an element's OWN display
                // value, not an ancestor's display:none (that's not an
                // inherited property) -- so this has to walk up.
                let node = e;
                while (node && node.nodeType === 1) {
                    const cs = getComputedStyle(node);
                    if (cs.display === 'none' || cs.visibility === 'hidden') return true;
                    node = node.parentElement;
                }
                return false;
            };
            const els = Array.from(document.querySelectorAll('div,span,td'));
            const el = els.find(e => norm(e.textContent) === target && !isHidden(e)
                && !Array.from(e.children).some(c => norm(c.textContent) === target));
            if (el) { el.click(); return true; }
            return false;
        }""",
        label,
    )
    if not ok:
        raise RuntimeError(f"Couldn't find a visible tab labeled {label!r}")


def _meal_food_items(page):
    """Return [{meal, name, quantity_text, calories_text}, ...] for the
    Breakfast/Lunch/Dinner/Snacks sections of the day log, or None when the
    meal log isn't on the page. The Exercise section further down the same
    log is deliberately not read here -- fitness data is out of scope for
    this scraper."""
    return page.evaluate(
        """() => {
            function findMealContainer() {
                const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                let n;
                while (n = walker.nextNode()) {
                    if (n.textContent.includes('Breakfast:')) return n.parentElement;
                }
                return null;
            }
            let el = findMealContainer();
            let found = false;
            for (let depth = 0; el && depth < 15; depth++) {
                const t = el.textContent;
                if (t.includes('Breakfast') && t.includes('Lunch') && t.includes('Dinner') && t.includes('Snacks') && t.includes('Exercise')) { found = true; break; }
                el = el.parentElement;
            }
            if (!found) return null;
            const mealNames = ['Breakfast', 'Lunch', 'Dinner', 'Snacks'];
            const results = [];
            for (let i = 0; i < mealNames.length; i++) {
                const row = el.children[i];
                if (!row) continue;
                row.querySelectorAll('a.gwt-Anchor').forEach(a => {
                    const tr = a.closest('tr');
                    const tds = Array.from(tr.children).map(td => td.textContent.trim());
                    results.push({
                        meal: mealNames[i],
                        name: a.textContent.trim(),
                        quantity_text: tds[2] || '',
                        calories_text: tds[3] || '',
                    });
                });
            }
            return results;
        }"""
    )


def _day_notes(page):
    """Return the day's journal note text from the Notes section of the log,
    or None if empty / not present.

    Observed DOM (GWT day log): a section header "Notes", then one or more
    rows with an `a.gwt-Anchor` whose text is exactly "Note" and a sibling
    div holding the body (e.g. "Mom visiting before cruise"). Multiple note
    bodies are joined with newlines. Keys off the stable "Note" anchor text
    rather than obfuscated GWT class hashes.
    """
    return page.evaluate(
        """() => {
            const norm = s => (s || '').replace(/[\\s\\u00a0]+/g, ' ').trim();
            const isHidden = e => {
                let node = e;
                while (node && node.nodeType === 1) {
                    const cs = getComputedStyle(node);
                    if (cs.display === 'none' || cs.visibility === 'hidden') return true;
                    node = node.parentElement;
                }
                return false;
            };

            // Prefer a Notes section container so a food literally named
            // "Note" elsewhere on the page can't be mis-read as a journal entry.
            let header = null;
            for (const el of document.querySelectorAll('div,td,span')) {
                if (norm(el.textContent) !== 'Notes') continue;
                if (Array.from(el.children).some(c => norm(c.textContent) === 'Notes')) continue;
                if (isHidden(el)) continue;
                header = el;
                break;
            }
            if (!header) return null;

            // The "Notes" header sits several levels above the Note row
            // (header table -> outer tbody that also holds the Note anchor).
            // Keep climbing until we find Note anchors, or until we've
            // spilled into the full meal log without any -- empty Notes.
            let container = header;
            for (let depth = 0; depth < 16 && container; depth++) {
                const noteAnchors = Array.from(container.querySelectorAll('a.gwt-Anchor'))
                    .filter(a => norm(a.textContent) === 'Note' && !isHidden(a));
                if (noteAnchors.length) {
                    const bodies = noteAnchors.map(a => {
                        if (a.nextElementSibling) return norm(a.nextElementSibling.textContent);
                        // Fallback: parent text minus the "Note" label.
                        const parent = a.parentElement;
                        if (!parent) return '';
                        return norm(parent.textContent).replace(/^Note\\s*/, '');
                    }).filter(Boolean);
                    return bodies.length ? bodies.join('\\n') : null;
                }
                const t = norm(container.textContent);
                if (t.includes('Breakfast') && t.includes('Lunch') && t.includes('Dinner')) {
                    return null;
                }
                container = container.parentElement;
            }
            return null;
        }"""
    )


# Row calories summed to the Food total exactly on every day checked
# (Jul-Oct 2026); this only absorbs float noise.
_FOOD_TOTAL_TOLERANCE = 0.5


def _food_confirmed(items, food_total: float) -> bool:
    """True when the scraped meal list is safe to replace the stored day
    with: the list was found and its calories add up to the summary's Food
    total. A stale list (the header rebinds before the meal rows) or a
    half-rendered page fails the sum."""
    if items is None:
        return False
    total = sum(_parse_number(i["calories_text"]) for i in items)
    return abs(total - food_total) <= _FOOD_TOTAL_TOLERANCE


def fetch_day(page, day: date) -> bool:
    """Scrape the daily calorie budget/eaten totals, macro totals, daily notes,
    and the individual food log entries for `day` off the LoseIt dashboard
    (which doubles as the per-day log view once navigated to that date).

    The stored food rows for `day` are replaced with exactly what LoseIt
    shows, so edits and deletions carry over. Returns False, leaving the
    stored rows alone, when the meal list can't be confirmed for `day`."""
    _goto_date(page, day)

    calorie_budget = _parse_number(_summary_value(page, "Budget") or "0")
    calories_eaten = _parse_number(_summary_value(page, "Food") or "0")
    # Just the daily aggregate burned-calories total from the summary row
    # (not the individual exercise entries) -- needed so
    # calories_remaining_after_exercise matches the "over/under budget"
    # figure LoseIt itself shows, which nets out exercise credit. LoseIt
    # always displays this cell as a negative ("-693", a subtraction from
    # the running total); we store the positive burned magnitude instead
    # so the column name matches its sign.
    exercise_calories_burned = abs(_parse_number(_summary_value(page, "Exercise") or "0"))

    notes = _day_notes(page)

    _click_tab(page, "My Nutrients")
    page.wait_for_timeout(500)
    protein_g = _parse_number((_nutrient_value(page, "Protein") or "0g").rstrip("g"))
    carbs_g = _parse_number((_nutrient_value(page, "Carbohydrates") or "0g").rstrip("g"))
    fat_g = _parse_number((_nutrient_value(page, "Fat") or "0g").rstrip("g"))

    db.upsert_daily_summary({
        "date": day.isoformat(),
        "calorie_budget": calorie_budget,
        "calories_eaten": calories_eaten,
        "exercise_calories_burned": exercise_calories_burned,
        "calories_remaining_before_exercise": calorie_budget - calories_eaten,
        "calories_remaining_after_exercise": calorie_budget - calories_eaten + exercise_calories_burned,
        "protein_g": protein_g,
        "carbs_g": carbs_g,
        "fat_g": fat_g,
        "notes": notes,
    })

    items = _meal_food_items(page)
    if _read_displayed_date(page) != day or not _food_confirmed(items, calories_eaten):
        return False
    rows = []
    for item in items:
        quantity, units = _parse_quantity(item["quantity_text"])
        rows.append({
            "meal": item["meal"],
            "name": item["name"],
            "brand": None,  # not exposed as a separate field in the row DOM
            "quantity": quantity,
            "units": units,
            "calories": _parse_number(item["calories_text"]),
            "protein_g": None,  # per-item macros aren't shown without opening each entry
            "carbs_g": None,
            "fat_g": None,
        })
    db.replace_food_log(day.isoformat(), rows)
    return True


# The label ("Oct 30 weight", or "today's weight") is stable text; the
# input's GWT hash class is not. Past days render the input disabled.
_WEIGHT_INPUT_JS = r"""() => {
    const norm = s => (s || '').replace(/[\s\u00a0]+/g, ' ').trim();
    const isHidden = e => {
        for (let n = e; n && n.nodeType === 1; n = n.parentElement) {
            const cs = getComputedStyle(n);
            if (cs.display === 'none' || cs.visibility === 'hidden') return true;
        }
        return false;
    };
    const label = Array.from(document.querySelectorAll('div.gwt-HTML'))
        .find(e => /^(today's|[A-Za-z]{3,9} \d{1,2}(, \d{4})?) weight$/i.test(norm(e.textContent)) && !isHidden(e));
    let box = label;
    for (let i = 0; box && i < 12; i++, box = box.parentElement) {
        const el = box.querySelector('input.gwt-TextBox');
        if (el) return {label: norm(label.textContent), value: el.value.trim()};
    }
    return null;
}"""


_MONTHS = ("january", "february", "march", "april", "may", "june", "july",
           "august", "september", "october", "november", "december")
_WEIGHT_LABEL_RE = re.compile(r"^([a-z]{3,9}) (\d{1,2})(?:, (\d{4}))? weight$")


def _weight_label_matches(label: str, day: date, today: date) -> bool:
    """The widget can stay bound to another day after tab switches; its
    label names the day it is actually showing. LoseIt abbreviates months
    to three letters except "Sept"."""
    label = label.lower()
    if day == today:
        return label == "today's weight"
    m = _WEIGHT_LABEL_RE.match(label)
    if not m:
        return False
    month, dom, year = m.groups()
    return (
        len(month) >= 3
        and _MONTHS[day.month - 1].startswith(month)
        and int(dom) == day.day
        and (year is None or int(year) == day.year)
    )


def fetch_weight(page, day: date, today: date | None = None) -> bool:
    """Scrape the body weight logged for `day`, if any, from the Weight
    widget on the dashboard.

    Returns False when the widget can't be found or shows a different day,
    so the caller can fail the run loudly.

    Switching onto the Weight sub-tab resets its internal date-binding to
    "today", independent of whatever the main date header already shows
    -- and since _goto_date() only looks at that header, if it's already
    showing `day` (e.g. because fetch_day() navigated there first) it
    sees nothing to correct and clicks nothing. What actually resyncs the
    widget is a genuine arrow-click event, so this always forces a
    harmless round trip (prev then next) after switching tabs, regardless
    of whether the header already looked right.
    """
    _click_tab(page, "Weight")
    page.wait_for_timeout(300)
    page.click(".prevArrowButton")
    page.wait_for_timeout(400)
    page.click(".nextArrowButton")
    page.wait_for_timeout(400)
    _goto_date(page, day)
    found = page.evaluate(_WEIGHT_INPUT_JS)
    if found is None or not _weight_label_matches(found["label"], day, today or date.today()):
        return False
    if found["value"]:
        db.upsert_weight(day.isoformat(), float(found["value"]), "lb")
    return True


# Keyed off the widget's visible label: the input's GWT hash class changed
# on a LoseIt redeploy (GPOEIKGBUC -> GPOEIKGPTC, Oct 2026) and the old
# exact-class match then found nothing, silently.
_WATER_INPUT_JS = r"""() => {
    const norm = s => (s || '').replace(/[\s\u00a0]+/g, ' ').trim().toLowerCase();
    const isHidden = e => {
        for (let n = e; n && n.nodeType === 1; n = n.parentElement) {
            const cs = getComputedStyle(n);
            if (cs.display === 'none' || cs.visibility === 'hidden') return true;
        }
        return false;
    };
    const label = Array.from(document.querySelectorAll('div.gwt-HTML'))
        .find(e => norm(e.textContent) === "today's water intake" && !isHidden(e));
    let box = label;
    for (let i = 0; box && i < 12; i++, box = box.parentElement) {
        const el = box.querySelector('input.gwt-TextBox');
        if (el) return el.value.trim();
    }
    return null;
}"""


def fetch_water(page, day: date) -> bool:
    """Scrape fluid ounces logged for `day` from the Water Intake widget.

    Returns False when the widget itself can't be found (as opposed to a
    day with nothing logged), so the caller can fail the run loudly.

    That widget only refreshes to match the currently-navigated date if
    you're on a *different* sub-tab (e.g. Weight) at the moment the date
    changes -- switching to Water Intake while it's already the active
    tab does not refresh it. So: switch off it, navigate, then switch to
    it, every time, regardless of what tab was active before.
    """
    _click_tab(page, "Weight")
    page.wait_for_timeout(300)
    _goto_date(page, day)
    _click_tab(page, "Water Intake")
    page.wait_for_timeout(500)
    value = page.evaluate(_WATER_INPUT_JS)
    if value is None:
        return False
    if value:
        db.upsert_water(day.isoformat(), _parse_number(value))
    return True


# Retry a hung document load (cold network, or a request that never
# finishes). A logged-out page raises RuntimeError and is not retried.
_HOME_ATTEMPTS = 3
_HOME_GOTO_TIMEOUT_MS = 20000
_HOME_RETRY_PAUSE_MS = 2000


def _transient_navigation_error(exc: BaseException) -> bool:
    if isinstance(exc, PlaywrightTimeoutError):
        return True
    return isinstance(exc, PlaywrightError) and "net::" in str(exc)


def _open_logged_in_home(page) -> None:
    """Open LoseIt and wait until the day log is on screen.

    ``networkidle`` is the wrong signal. Tag-manager and CDN requests
    stay in flight after the GWT log has rendered, so a 30s networkidle
    wait fails the run before ``_await_dashboard`` ever runs. Readiness
    is the dashboard itself.
    """
    for attempt in range(1, _HOME_ATTEMPTS + 1):
        try:
            page.goto(
                LOSEIT_BASE,
                wait_until="domcontentloaded",
                timeout=_HOME_GOTO_TIMEOUT_MS,
            )
            _await_dashboard(page)
            return
        except Exception as exc:
            if attempt == _HOME_ATTEMPTS or not _transient_navigation_error(exc):
                raise
            print(
                f"LoseIt home timed out (attempt {attempt}/{_HOME_ATTEMPTS}); retrying."
            )
            page.wait_for_timeout(_HOME_RETRY_PAUSE_MS)


def resolve_since(since: str | None, today: date | None = None) -> date:
    """Start date for a run: explicit --since, else last stored daily_summary
    date (re-fetched in case it was edited), else 7 days before today.

    A default run therefore catch-up-fills any gap from the last valid
    fetch through yesterday, including after a stretch of failed jobs.
    """
    today = today or date.today()
    if since:
        return date.fromisoformat(since)
    last = db.latest_date("daily_summary")
    return date.fromisoformat(last) if last else (today - timedelta(days=7))


def run(since: date, headed: bool):
    try:
        auth.refresh_session(STATE_PATH)
    except auth.AuthError as exc:
        print(f"LoseIt rejected the saved refresh token: {exc} {auth.LOGIN_HINT}")
        sys.exit(1)
    except httpx.HTTPError as exc:
        # The saved cookies may still have days left; let the expiry check decide.
        print(f"Couldn't reach LoseIt to refresh the session ({exc}); trying the saved one.")
    if not STATE_PATH.exists():
        print("No saved session found. Run `.venv/bin/python src/scraper.py login`, then `.venv/bin/python src/scraper.py run` to catch up.")
        sys.exit(1)
    if _auth_session_expired(STATE_PATH):
        print(
            "Saved LoseIt auth cookies have expired. "
            "Run `.venv/bin/python src/scraper.py login`, then `.venv/bin/python src/scraper.py run` to catch up."
        )
        sys.exit(1)

    db.init_db()
    today = date.today()
    yesterday = today - timedelta(days=1)
    if since > yesterday:
        print(f"Nothing to fetch: since {since.isoformat()} is not before today.")
        return
    n_days = (yesterday - since).days + 1
    print(
        f"Catching up {n_days} day(s) from {since.isoformat()} through {yesterday.isoformat()}."
    )
    d = since
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        context = browser.new_context(storage_state=str(STATE_PATH))
        page = context.new_page()
        _open_logged_in_home(page)

        missing = []
        while d < today:
            print(f"Fetching {d.isoformat()}...")
            if not fetch_day(page, d):
                missing.append(f"food {d.isoformat()}")
            if not fetch_weight(page, d):
                missing.append(f"weight {d.isoformat()}")
            if not fetch_water(page, d):
                missing.append(f"water {d.isoformat()}")
            d += timedelta(days=1)

        # refresh session in case cookies rotated
        context.storage_state(path=str(STATE_PATH))
        STATE_PATH.chmod(0o600)
        browser.close()
    if missing:
        # Exit non-zero only after the other data is stored, so the
        # launchd wrapper sends its failure alert.
        print(
            f"Couldn't read the widget for {', '.join(missing)}; "
            "LoseIt's page markup may have changed. Everything else was saved."
        )
        sys.exit(2)
    print("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    login_p = sub.add_parser("login")
    login_p.add_argument("--browser", action="store_true", help="log in by hand in a headed browser (no refresh token; lasts 14 days)")
    run_p = sub.add_parser("run")
    run_p.add_argument("--since", type=str, default=None, help="YYYY-MM-DD, defaults to the last recorded date (re-fetched in case it was edited) through yesterday")
    run_p.add_argument("--headed", action="store_true")
    args = parser.parse_args()

    if args.cmd == "login":
        do_login() if args.browser else do_api_login()
    elif args.cmd == "run":
        db.init_db()
        # Re-fetch from the last recorded date itself (not +1): this
        # both re-confirms that day in case it was edited after the
        # fact, and naturally backfills any gap (e.g. laptop closed
        # for a few days, or a stretch of failed runs) up through
        # yesterday in the same pass.
        since = resolve_since(args.since)
        run(since, args.headed)
