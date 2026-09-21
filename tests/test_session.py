"""Catch-up start date and saved-session expiry (no browser)."""
import json
from datetime import date
from pathlib import Path

import db
from scraper import _auth_session_expired, resolve_since


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
