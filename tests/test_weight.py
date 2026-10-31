"""Weight widget lookup, day check, and storage."""
from datetime import date

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

import db
import scraper

# Trimmed from the live Weight tab. The decoys are the food search box and a
# hidden-tab textbox with the class the weight input used to have.
_WIDGET_HTML = """
<input type="text" class="gwt-TextBox GPOEIKGBWC GPOEIKGCWC GPOEIKGPTC" value="search &amp; add food">
<div style="display: none"><input type="text" class="gwt-TextBox GPOEIKGBUC" value="99"></div>
<table><tbody>
  <tr><td><div class="gwt-HTML GPOEIKGMXB">You'll reach your goal of losing 10 lbs on Jan 01, 2027</div></td></tr>
  <tr><td><div class="gwt-HTML GPOEIKGJ1B">{label}</div></td></tr>
  <tr><td><div class="GPOEIKGL1B"><div><div class="GPOEIKGGQB">
    <div tabindex="0" class="GPOEIKGDTC GPOEIKGAUC GPOEIKGIQB">
      <input type="text" tabindex="-1" aria-hidden="true" style="opacity: 0">
      <div><input type="text" class="gwt-TextBox GPOEIKGPTC" value="{value}" {disabled}></div>
    </div>
    <div class="gwt-HTML GPOEIKGFQB">lb</div>
  </div></div></div></td></tr>
</tbody></table>
"""


@pytest.fixture(scope="module")
def browser_page():
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            yield browser.new_page()
            browser.close()
    except PlaywrightError as exc:
        pytest.skip(f"Chromium not installed: {exc}")


@pytest.mark.parametrize(
    "label, value, disabled",
    [("Jul 10 weight", "150.5", "disabled"), ("today's weight", "150.5", ""), ("Jul 10 weight", "", "disabled")],
)
def test_weight_lookup_finds_input_by_label(browser_page, label, value, disabled):
    browser_page.set_content(_WIDGET_HTML.format(label=label, value=value, disabled=disabled))
    assert browser_page.evaluate(scraper._WEIGHT_INPUT_JS) == {"label": label, "value": value}


def test_weight_lookup_returns_none_without_widget(browser_page):
    browser_page.set_content('<input type="text" class="gwt-TextBox GPOEIKGPTC" value="150.5" disabled>')
    assert browser_page.evaluate(scraper._WEIGHT_INPUT_JS) is None


@pytest.mark.parametrize(
    "label, day, expected",
    [
        ("Jul 10 weight", date(2026, 7, 10), True),
        ("Jul 1 weight", date(2026, 7, 1), True),
        ("Sept 15 weight", date(2026, 9, 15), True),
        ("Sep 15 weight", date(2026, 9, 15), True),
        ("September 15 weight", date(2026, 9, 15), True),
        ("Sept 15 weight", date(2026, 10, 15), False),
        ("Jun 15 weight", date(2026, 7, 15), False),
        ("Dec 31, 2024 weight", date(2025, 12, 31), False),
        ("Dec 31, 2025 weight", date(2025, 12, 31), True),
        ("today's weight", date(2026, 7, 11), True),
        ("Jul 9 weight", date(2026, 7, 10), False),
        ("today's weight", date(2026, 7, 10), False),
        ("Jul 11 weight", date(2026, 7, 11), False),
    ],
)
def test_weight_label_matches(label, day, expected):
    assert scraper._weight_label_matches(label, day, today=date(2026, 7, 11)) is expected


class _Page:
    def __init__(self, found):
        self.found = found

    def wait_for_timeout(self, ms):
        pass

    def click(self, selector):
        pass

    def evaluate(self, js):
        assert js is scraper._WEIGHT_INPUT_JS
        return self.found


@pytest.fixture
def no_nav(monkeypatch):
    monkeypatch.setattr(scraper, "_click_tab", lambda page, label: None)
    monkeypatch.setattr(scraper, "_goto_date", lambda page, day: None)


_DAY = date(2026, 7, 10)
_TODAY = date(2026, 7, 11)


def test_fetch_weight_stores_value(tmp_db, no_nav):
    page = _Page({"label": "Jul 10 weight", "value": "150.5"})
    assert scraper.fetch_weight(page, _DAY, today=_TODAY) is True
    rows = db.query_weight_log("2026-07-10", "2026-07-10")
    assert [(r["weight"], r["units"]) for r in rows] == [(150.5, "lb")]


def test_fetch_weight_empty_day_stores_nothing_and_is_not_a_failure(tmp_db, no_nav, capsys):
    page = _Page({"label": "Jul 10 weight", "value": ""})
    assert scraper.fetch_weight(page, _DAY, today=_TODAY) is True
    assert db.query_weight_log("2026-07-10", "2026-07-10") == []
    assert "No weight logged for 2026-07-10" in capsys.readouterr().out


def _weights():
    return [(r["date"], r["weight"]) for r in db.query_weight_log("2026-07-01", "2026-07-31")]


def test_fetch_weight_skips_repeat_of_previous_day_and_is_not_a_failure(tmp_db, no_nav, capsys):
    db.upsert_weight("2026-07-08", 150.5)
    page = _Page({"label": "Jul 10 weight", "value": "150.5"})
    assert scraper.fetch_weight(page, _DAY, today=_TODAY) is True
    assert _weights() == [("2026-07-08", 150.5)]
    assert "repeats 2026-07-08" in capsys.readouterr().out


def test_fetch_weight_removes_previously_stored_repeat(tmp_db, no_nav):
    db.upsert_weight("2026-07-09", 150.5)
    db.upsert_weight("2026-07-10", 150.5)
    page = _Page({"label": "Jul 10 weight", "value": "150.5"})
    assert scraper.fetch_weight(page, _DAY, today=_TODAY) is True
    assert _weights() == [("2026-07-09", 150.5)]


def test_fetch_weight_stores_changed_weight_over_old_row(tmp_db, no_nav):
    db.upsert_weight("2026-07-09", 150.5)
    db.upsert_weight("2026-07-10", 151.0)
    page = _Page({"label": "Jul 10 weight", "value": "150.0"})
    assert scraper.fetch_weight(page, _DAY, today=_TODAY) is True
    assert _weights() == [("2026-07-09", 150.5), ("2026-07-10", 150.0)]


def test_fetch_weight_compares_with_earlier_days_only(tmp_db, no_nav):
    db.upsert_weight("2026-07-11", 150.5)
    page = _Page({"label": "Jul 10 weight", "value": "150.5"})
    assert scraper.fetch_weight(page, _DAY, today=_TODAY) is True
    assert _weights() == [("2026-07-10", 150.5), ("2026-07-11", 150.5)]


@pytest.mark.parametrize("found", [None, {"label": "today's weight", "value": "150.5"}])
def test_fetch_weight_missing_or_wrong_day_reports_failure(tmp_db, no_nav, found):
    assert scraper.fetch_weight(_Page(found), _DAY, today=_TODAY) is False
    assert db.query_weight_log("2026-07-10", "2026-07-10") == []
