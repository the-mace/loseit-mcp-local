"""Catch-up start date, saved-session expiry, and home-page open (no browser)."""
import json
from datetime import date
from pathlib import Path

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

import db
import scraper
from scraper import _auth_session_expired, _open_logged_in_home, resolve_since


def _summary(day: str) -> dict:
    return {
        "date": day,
        "calorie_budget": 2000,
        "calories_eaten": 1500,
        "exercise_calories_burned": 0,
        "calories_remaining_before_exercise": 500,
        "calories_remaining_after_exercise": 500,
        "protein_g": 100,
        "carbs_g": 150,
        "fat_g": 50,
        "notes": None,
    }


def _state(tmp_path: Path, cookies: list[dict]) -> Path:
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"cookies": cookies, "origins": []}))
    return path


def test_resolve_since_explicit_overrides_db(tmp_db):
    db.upsert_daily_summary(_summary("2026-07-26"))
    assert resolve_since("2026-08-01", today=date(2026, 9, 21)) == date(2026, 8, 1)


def test_resolve_since_defaults_to_last_stored_day(tmp_db):
    db.upsert_daily_summary(_summary("2026-07-10"))
    db.upsert_daily_summary(_summary("2026-07-26"))
    assert resolve_since(None, today=date(2026, 9, 21)) == date(2026, 7, 26)


def test_resolve_since_empty_db_looks_back_seven_days(tmp_db):
    assert resolve_since(None, today=date(2026, 9, 21)) == date(2026, 9, 14)


def test_auth_session_expired_when_all_auth_cookies_past(tmp_path):
    path = _state(
        tmp_path,
        [
            {"name": "fn_auth", "expires": 1000.0},
            {"name": "liauth", "expires": 1000.0},
            {"name": "fn_authed", "expires": 1000.0},
        ],
    )
    assert _auth_session_expired(path, now_ts=2000.0) is True


def test_auth_session_not_expired_if_any_auth_cookie_future(tmp_path):
    path = _state(
        tmp_path,
        [
            {"name": "fn_auth", "expires": 1000.0},
            {"name": "liauth", "expires": 5000.0},
        ],
    )
    assert _auth_session_expired(path, now_ts=2000.0) is False


def test_auth_session_session_cookie_counts_as_valid(tmp_path):
    path = _state(tmp_path, [{"name": "liauth", "expires": -1}])
    assert _auth_session_expired(path, now_ts=2000.0) is False


def test_auth_session_expired_when_file_missing(tmp_path):
    assert _auth_session_expired(tmp_path / "missing.json", now_ts=1.0) is True


def test_auth_session_expired_when_no_auth_cookies(tmp_path):
    path = _state(tmp_path, [{"name": "_ga", "expires": 9999999999.0}])
    assert _auth_session_expired(path, now_ts=1.0) is True


class _Page:
    def __init__(self, effects):
        self.effects = list(effects)
        self.calls = []
        self.waits = []

    def goto(self, url, **kwargs):
        self.calls.append((url, kwargs))
        effect = self.effects.pop(0)
        if isinstance(effect, Exception):
            raise effect

    def wait_for_timeout(self, ms):
        self.waits.append(ms)


def test_open_home_retries_timeout_then_uses_dashboard(monkeypatch):
    page = _Page([PlaywrightTimeoutError("timeout"), None])
    monkeypatch.setattr(scraper, "_await_dashboard", lambda p: None)
    _open_logged_in_home(page)
    assert len(page.calls) == 2
    assert page.calls[0][1]["wait_until"] == "domcontentloaded"
    assert page.waits == [2000]


def test_open_home_retries_disconnected_network(monkeypatch):
    page = _Page([PlaywrightError("net::ERR_INTERNET_DISCONNECTED"), None])
    monkeypatch.setattr(scraper, "_await_dashboard", lambda p: None)
    _open_logged_in_home(page)
    assert len(page.calls) == 2


def test_open_home_does_not_retry_logged_out(monkeypatch):
    page = _Page([None])

    def logged_out(p):
        raise RuntimeError("LoseIt session expired or is not logged in.")

    monkeypatch.setattr(scraper, "_await_dashboard", logged_out)
    with pytest.raises(RuntimeError, match="session expired"):
        _open_logged_in_home(page)
    assert len(page.calls) == 1
    assert page.waits == []


def test_open_home_raises_after_three_timeouts(monkeypatch):
    page = _Page([PlaywrightTimeoutError("timeout")] * 3)
    monkeypatch.setattr(scraper, "_await_dashboard", lambda p: None)
    with pytest.raises(PlaywrightTimeoutError):
        _open_logged_in_home(page)
    assert len(page.calls) == 3
    assert page.waits == [2000, 2000]
