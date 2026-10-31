"""Food log mirroring: only a confirmed day replaces the stored rows."""
from datetime import date

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

import db
import scraper

_DAY = date(2026, 7, 10)


def _item(name, calories, meal="Breakfast", qty="1 Each"):
    return {"meal": meal, "name": name, "quantity_text": qty, "calories_text": calories}


@pytest.mark.parametrize(
    "items, total, expected",
    [
        ([_item("Eggs", "140"), _item("Toast", "1,060")], 1200.0, True),
        ([_item("Eggs", "140"), _item("Toast", "61")], 201.0, True),
        ([_item("Eggs", "140"), _item("Toast", "61")], 200.0, False),
        ([], 0.0, True),
        ([], 1500.0, False),
        ([_item("Eggs", "140")], 1500.0, False),
        (None, 0.0, False),
    ],
)
def test_food_confirmed(items, total, expected):
    assert scraper._food_confirmed(items, total) is expected


@pytest.fixture
def fake_day(monkeypatch):
    """Stub the page reads fetch_day makes; returns a dict the test edits."""
    state = {"items": [], "food": "0", "header": _DAY}
    summary = {"Budget": "2000", "Exercise": "0"}
    monkeypatch.setattr(scraper, "_goto_date", lambda page, day: None)
    monkeypatch.setattr(scraper, "_click_tab", lambda page, label: None)
    monkeypatch.setattr(scraper, "_day_notes", lambda page: None)
    monkeypatch.setattr(scraper, "_nutrient_value", lambda page, label: "0g")
    monkeypatch.setattr(
        scraper, "_summary_value", lambda page, label: state["food"] if label == "Food" else summary[label]
    )
    monkeypatch.setattr(scraper, "_meal_food_items", lambda page: state["items"])
    monkeypatch.setattr(scraper, "_read_displayed_date", lambda page: state["header"])
    return state


class _Page:
    def wait_for_timeout(self, ms):
        pass


def _stored():
    return [(r["name"], r["calories"]) for r in db.query_food_log("2026-07-10", "2026-07-10")]


def _seed():
    db.replace_food_log("2026-07-10", [{
        "meal": "Breakfast", "name": "Old", "brand": None, "quantity": 1.0, "units": "Each",
        "calories": 300.0, "protein_g": None, "carbs_g": None, "fat_g": None,
    }])


def test_fetch_day_replaces_confirmed_day(tmp_db, fake_day):
    _seed()
    fake_day.update(items=[_item("Eggs", "140"), _item("Soup", "200", meal="Lunch")], food="340")
    assert scraper.fetch_day(_Page(), _DAY) is True
    assert _stored() == [("Eggs", 140.0), ("Soup", 200.0)]


def test_fetch_day_confirmed_empty_day_mirrors_to_empty(tmp_db, fake_day):
    _seed()
    fake_day.update(items=[], food="0")
    assert scraper.fetch_day(_Page(), _DAY) is True
    assert _stored() == []


@pytest.mark.parametrize(
    "update",
    [
        {"items": None, "food": "0"},
        {"items": [], "food": "1500"},
        {"items": [_item("Eggs", "140")], "food": "140", "header": date(2026, 7, 9)},
    ],
)
def test_fetch_day_unconfirmed_keeps_rows(tmp_db, fake_day, update):
    _seed()
    fake_day.update(update)
    assert scraper.fetch_day(_Page(), _DAY) is False
    assert _stored() == [("Old", 300.0)]


@pytest.fixture(scope="module")
def browser_page():
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            yield browser.new_page()
            browser.close()
    except PlaywrightError as exc:
        pytest.skip(f"Chromium not installed: {exc}")


def test_meal_food_items_none_without_meal_log(browser_page):
    browser_page.set_content("<div>Loading...</div>")
    assert scraper._meal_food_items(browser_page) is None
