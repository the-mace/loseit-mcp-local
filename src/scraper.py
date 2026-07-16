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
  python scraper.py login       -> opens a headed browser, log in manually,
                                    saves session to ~/.loseit-data/state.json
  python scraper.py run         -> headless run: fetch + store data for
                                    yesterday (or --since YYYY-MM-DD)
  python scraper.py run --headed -> same as run, but visible browser
                                     (useful while you're still tuning selectors)
"""
import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright
import db

STATE_PATH = Path.home() / ".loseit-data" / "state.json"
LOSEIT_BASE = "https://www.loseit.com"


def do_login():
    """Opens a headed browser so you can log in manually (handles any
    CAPTCHA/verification LoseIt throws up), then saves the session."""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(f"{LOSEIT_BASE}/login")
        print("Log in manually in the opened browser window.")
        print("Once you're on your LoseIt home/dashboard page, come back here and press Enter.")
        input()
        context.storage_state(path=str(STATE_PATH))
        browser.close()
    print(f"Session saved to {STATE_PATH}")


_DATE_HEADER_RE_JS = r"^[A-Za-z]+\s+[A-Za-z]{3}\s+\d{1,2},\s+\d{4}$"


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


def _goto_date(page, target: date):
    """Click the prev/next day arrow enough times to reach `target`.

    LoseIt has no per-day URL (confirmed: /log/date/YYYY-MM-DD 404s), so
    navigation only happens through these in-page buttons. We verify by
    re-reading the date header rather than assuming N clicks worked.
    """
    current = _read_displayed_date(page)
    delta = (target - current).days
    if delta == 0:
        return
    selector = ".nextArrowButton" if delta > 0 else ".prevArrowButton"
    for _ in range(abs(delta)):
        page.click(selector)
        page.wait_for_timeout(400)

    for _ in range(20):
        if _read_displayed_date(page) == target:
            return
        page.wait_for_timeout(300)
    raise RuntimeError(
        f"Could not navigate to {target.isoformat()}; "
        f"page still shows {_read_displayed_date(page).isoformat()}"
    )


def _parse_quantity(text: str):
    """'2  Servings' -> (2.0, 'Servings'); '1/2 Each' -> (0.5, 'Each')."""
    parts = text.split(None, 1)
    if len(parts) != 2:
        return None, text.strip()
    qty_str, units = parts
    if "/" in qty_str:
        num, _, den = qty_str.partition("/")
        qty = float(num) / float(den)
    else:
        qty = float(qty_str)
    return qty, units.strip()


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
    Breakfast/Lunch/Dinner/Snacks sections of the day log. The Exercise
    section further down the same log is deliberately not read here --
    fitness data is out of scope for this scraper."""
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
            let depth = 0;
            while (el && depth < 15) {
                const t = el.textContent;
                if (t.includes('Breakfast') && t.includes('Lunch') && t.includes('Dinner') && t.includes('Snacks') && t.includes('Exercise')) break;
                el = el.parentElement; depth++;
            }
            if (!el) return [];
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


def fetch_day(page, day: date):
    """Scrape the daily calorie budget/eaten totals, macro totals, daily notes,
    and the individual food log entries for `day` off the LoseIt dashboard
    (which doubles as the per-day log view once navigated to that date)."""
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

    for item in _meal_food_items(page):
        quantity, units = _parse_quantity(item["quantity_text"])
        db.insert_food_log_entry({
            "date": day.isoformat(),
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


def fetch_weight(page, day: date):
    """Scrape the body weight logged for `day`, if any, from the Weight
    widget on the dashboard.

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
    value = page.evaluate(
        """() => {
            // LoseIt uses two shapes for the weight widget:
            // - a disabled gwt-TextBox on some past days / empty states
            // - an editable `gwt-TextBox GPOEIKGBUC` once a weight is logged
            //   (same class family as the water input; safe here because we
            //   are on the Weight tab after the nav dance above)
            const disabled = document.querySelector('input.gwt-TextBox[disabled]');
            if (disabled && disabled.value.trim()) return disabled.value.trim();
            const inputs = Array.from(document.querySelectorAll('input.gwt-TextBox'));
            const el = inputs.find(
                i => i.className.trim() === 'gwt-TextBox GPOEIKGBUC' && !i.disabled
            );
            return el ? el.value.trim() : '';
        }"""
    )
    if value:
        db.upsert_weight(day.isoformat(), float(value), "lb")


def fetch_water(page, day: date):
    """Scrape fluid ounces logged for `day` from the Water Intake widget.

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
    value = page.evaluate(
        """() => {
            // Same widget-value class as the weight input (gwt-TextBox
            // GPOEIKGBUC, no extra classes) -- deliberately more specific
            // than 'input.gwt-TextBox' alone, which also matches the
            // 'search & add food/exercise' boxes elsewhere on the page.
            const inputs = Array.from(document.querySelectorAll('input.gwt-TextBox'));
            const el = inputs.find(i => i.className.trim() === 'gwt-TextBox GPOEIKGBUC' && !i.disabled);
            return el ? el.value.trim() : '';
        }"""
    )
    if value:
        db.upsert_water(day.isoformat(), float(value))


def run(since: date, headed: bool):
    if not STATE_PATH.exists():
        print("No saved session found. Run `python scraper.py login` first.")
        sys.exit(1)

    db.init_db()
    today = date.today()
    d = since
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        context = browser.new_context(storage_state=str(STATE_PATH))
        page = context.new_page()
        page.goto(LOSEIT_BASE, wait_until="networkidle")

        while d < today:
            print(f"Fetching {d.isoformat()}...")
            fetch_day(page, d)
            fetch_weight(page, d)
            fetch_water(page, d)
            d += timedelta(days=1)

        # refresh session in case cookies rotated
        context.storage_state(path=str(STATE_PATH))
        browser.close()
    print("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login")
    run_p = sub.add_parser("run")
    run_p.add_argument("--since", type=str, default=None, help="YYYY-MM-DD, defaults to the last recorded date (re-fetched in case it was edited) through yesterday")
    run_p.add_argument("--headed", action="store_true")
    args = parser.parse_args()

    if args.cmd == "login":
        do_login()
    elif args.cmd == "run":
        if args.since:
            since = date.fromisoformat(args.since)
        else:
            db.init_db()
            last = db.latest_date("daily_summary")
            # Re-fetch from the last recorded date itself (not +1): this
            # both re-confirms that day in case it was edited after the
            # fact, and naturally backfills any gap (e.g. laptop closed
            # for a few days) up through yesterday in the same pass.
            since = date.fromisoformat(last) if last else (date.today() - timedelta(days=7))
        run(since, args.headed)
