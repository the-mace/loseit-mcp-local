"""Water Intake widget lookup and storage."""
from datetime import date

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

import db
import scraper

# Trimmed from the live day log. The decoys are the inputs the
# lookup must not pick: the food search box, and a hidden-tab textbox with
# the class the water input used to have.
_WIDGET_HTML = """
<table><tbody><tr><td>
  <input type="text" class="gwt-TextBox GPOEIKGBWC GPOEIKGCWC GPOEIKGPTC" value="search &amp; add food">
</td></tr></tbody></table>
<div style="display: none"><input type="text" class="gwt-TextBox GPOEIKGBUC" value="99"></div>
<div class="GPOEIKGALC GPOEIKGMKC"><table class="GPOEIKGO1C"><tbody><tr>
  <td><table class="GPOEIKGFOC"><tbody>
    <tr><td><div class="gwt-HTML GPOEIKGDOC">48 fluid ounces</div></td></tr>
    <tr><td><div class="gwt-HTML GPOEIKGGOC">on July 10th</div></td></tr>
  </tbody></table></td>
  <td><div tabindex="0" class="GPOEIKGDTC">
    <input type="text" tabindex="-1" aria-hidden="true" style="opacity: 0">
    <div><table class="GPOEIKGCPC"><tbody>
      <tr><td><div class="gwt-HTML GPOEIKGAPC">today's water intake</div></td></tr>
      <tr><td><table><tbody><tr><td><div class="GPOEIKGDTC GPOEIKGAUC GPOEIKGEPC">
        <input type="text" tabindex="-1" aria-hidden="true" style="opacity: 0">
        <div><input type="text" class="gwt-TextBox GPOEIKGPTC" value="{value}"></div>
      </div></td></tr></tbody></table></td></tr>
    </tbody></table></div>
  </div></td>
</tr></tbody></table></div>
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


@pytest.mark.parametrize("value, expected", [("48", "48"), ("", "")])
def test_water_lookup_finds_input_by_label(browser_page, value, expected):
    browser_page.set_content(_WIDGET_HTML.format(value=value))
    assert browser_page.evaluate(scraper._WATER_INPUT_JS) == expected


def test_water_lookup_returns_none_without_widget(browser_page):
    browser_page.set_content('<input type="text" class="gwt-TextBox GPOEIKGPTC" value="48">')
    assert browser_page.evaluate(scraper._WATER_INPUT_JS) is None


class _Page:
    def __init__(self, value):
        self.value = value

    def wait_for_timeout(self, ms):
        pass

    def evaluate(self, js):
        assert js is scraper._WATER_INPUT_JS
        return self.value


@pytest.fixture
def no_nav(monkeypatch):
    monkeypatch.setattr(scraper, "_click_tab", lambda page, label: None)
    monkeypatch.setattr(scraper, "_goto_date", lambda page, day: None)


def test_fetch_water_stores_value(tmp_db, no_nav):
    assert scraper.fetch_water(_Page("48"), date(2026, 7, 10)) is True
    rows = db.query_water_log("2026-07-10", "2026-07-10")
    assert [r["fluid_ounces"] for r in rows] == [48.0]


def test_fetch_water_empty_day_stores_nothing(tmp_db, no_nav):
    assert scraper.fetch_water(_Page(""), date(2026, 7, 10)) is True
    assert db.query_water_log("2026-07-10", "2026-07-10") == []


def test_fetch_water_missing_widget_reports_failure(tmp_db, no_nav):
    assert scraper.fetch_water(_Page(None), date(2026, 7, 10)) is False
    assert db.query_water_log("2026-07-10", "2026-07-10") == []
