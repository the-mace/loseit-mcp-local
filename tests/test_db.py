"""Unit tests for the SQLite storage layer (db.py)."""
import db


def _summary(date, **overrides):
    row = {
        "date": date,
        "calorie_budget": 2000.0,
        "calories_eaten": 1500.0,
        "exercise_calories_burned": 200.0,
        "calories_remaining_before_exercise": 500.0,
        "calories_remaining_after_exercise": 700.0,
        "protein_g": 100.0,
        "carbs_g": 150.0,
        "fat_g": 50.0,
        "notes": None,
    }
    row.update(overrides)
    return row


def _food(date="2026-07-10", meal="Breakfast", name="Eggs", **overrides):
    row = {
        "date": date,
        "meal": meal,
        "name": name,
        "brand": None,
        "quantity": 2.0,
        "units": "Each",
        "calories": 140.0,
        "protein_g": 12.0,
        "carbs_g": 1.0,
        "fat_g": 10.0,
    }
    row.update(overrides)
    return row


# --- daily_summary ---


def test_upsert_daily_summary_inserts(tmp_db):
    db.upsert_daily_summary(_summary("2026-07-10", notes="Felt good"))
    rows = db.query_daily_summary("2026-07-10", "2026-07-10")
    assert len(rows) == 1
    assert rows[0]["calorie_budget"] == 2000.0
    assert rows[0]["calories_eaten"] == 1500.0
    assert rows[0]["protein_g"] == 100.0
    assert rows[0]["notes"] == "Felt good"


def test_upsert_daily_summary_overwrites_on_conflict(tmp_db):
    db.upsert_daily_summary(_summary("2026-07-10", calories_eaten=1500.0, notes="old"))
    db.upsert_daily_summary(
        _summary("2026-07-10", calories_eaten=1800.0, protein_g=120.0, notes="updated")
    )
    rows = db.query_daily_summary("2026-07-10", "2026-07-10")
    assert len(rows) == 1
    assert rows[0]["calories_eaten"] == 1800.0
    assert rows[0]["protein_g"] == 120.0
    assert rows[0]["notes"] == "updated"


def test_upsert_daily_summary_notes_cleared_when_absent(tmp_db):
    """Re-scrape with no note must clear a previously stored note."""
    db.upsert_daily_summary(_summary("2026-07-10", notes="had a note"))
    db.upsert_daily_summary(_summary("2026-07-10", notes=None))
    rows = db.query_daily_summary("2026-07-10", "2026-07-10")
    assert rows[0]["notes"] is None


def test_upsert_daily_summary_notes_optional_key(tmp_db):
    """Callers that omit notes entirely still insert successfully."""
    row = _summary("2026-07-10")
    del row["notes"]
    db.upsert_daily_summary(row)
    rows = db.query_daily_summary("2026-07-10", "2026-07-10")
    assert rows[0]["notes"] is None


def test_migrate_adds_notes_column_to_existing_db(tmp_path, monkeypatch):
    """DBs created before the notes column still gain it via init_db()."""
    import sqlite3

    path = tmp_path / "legacy.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """CREATE TABLE daily_summary (
                date TEXT PRIMARY KEY,
                calorie_budget REAL,
                calories_eaten REAL,
                exercise_calories_burned REAL,
                calories_remaining_before_exercise REAL,
                calories_remaining_after_exercise REAL,
                protein_g REAL,
                carbs_g REAL,
                fat_g REAL,
                fetched_at TEXT
            )"""
        )
        conn.execute(
            "INSERT INTO daily_summary (date, calorie_budget) VALUES ('2026-07-10', 2000)"
        )
    db.init_db()
    with sqlite3.connect(path) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(daily_summary)")}
        assert "notes" in cols
        note = conn.execute(
            "SELECT notes FROM daily_summary WHERE date = '2026-07-10'"
        ).fetchone()[0]
        assert note is None


def test_query_daily_summary_date_range_and_order(tmp_db):
    for d in ("2026-07-12", "2026-07-10", "2026-07-11", "2026-07-09"):
        db.upsert_daily_summary(_summary(d))
    rows = db.query_daily_summary("2026-07-10", "2026-07-11")
    assert [r["date"] for r in rows] == ["2026-07-10", "2026-07-11"]


# --- food_log ---


def test_insert_food_log_entry(tmp_db):
    db.insert_food_log_entry(_food())
    rows = db.query_food_log("2026-07-10", "2026-07-10")
    assert len(rows) == 1
    assert rows[0]["name"] == "Eggs"
    assert rows[0]["meal"] == "Breakfast"
    assert rows[0]["calories"] == 140.0


def test_food_log_dedup_on_reinsert(tmp_db):
    """Re-scraping the same day must not create duplicate food rows."""
    entry = _food()
    db.insert_food_log_entry(entry)
    db.insert_food_log_entry(entry)
    rows = db.query_food_log("2026-07-10", "2026-07-10")
    assert len(rows) == 1


def test_food_log_dedup_with_null_brand(tmp_db):
    """brand is always NULL and must not defeat the UNIQUE constraint
    (SQL treats NULL != NULL, so brand was deliberately left out of the
    uniqueness key)."""
    entry = _food(brand=None)
    db.insert_food_log_entry(entry)
    db.insert_food_log_entry(entry)
    assert len(db.query_food_log("2026-07-10", "2026-07-10")) == 1


def test_food_log_allows_distinct_entries(tmp_db):
    db.insert_food_log_entry(_food(name="Eggs", quantity=2.0))
    db.insert_food_log_entry(_food(name="Eggs", quantity=1.0))  # different qty
    db.insert_food_log_entry(_food(name="Toast", quantity=2.0))  # different name
    db.insert_food_log_entry(_food(meal="Lunch", name="Eggs", quantity=2.0))  # different meal
    rows = db.query_food_log("2026-07-10", "2026-07-10")
    assert len(rows) == 4


def test_query_food_log_ordered_by_date_then_meal(tmp_db):
    db.insert_food_log_entry(_food(date="2026-07-11", meal="Lunch", name="Salad"))
    db.insert_food_log_entry(_food(date="2026-07-10", meal="Dinner", name="Steak"))
    db.insert_food_log_entry(_food(date="2026-07-10", meal="Breakfast", name="Eggs"))
    rows = db.query_food_log("2026-07-10", "2026-07-11")
    assert [(r["date"], r["meal"]) for r in rows] == [
        ("2026-07-10", "Breakfast"),
        ("2026-07-10", "Dinner"),
        ("2026-07-11", "Lunch"),
    ]


# --- weight_log ---


def test_upsert_weight_inserts_and_overwrites(tmp_db):
    db.upsert_weight("2026-07-10", 180.0, "lb")
    db.upsert_weight("2026-07-10", 179.5, "lb")
    rows = db.query_weight_log("2026-07-10", "2026-07-10")
    assert len(rows) == 1
    assert rows[0]["weight"] == 179.5
    assert rows[0]["units"] == "lb"


def test_query_weight_log_range(tmp_db):
    db.upsert_weight("2026-07-09", 181.0)
    db.upsert_weight("2026-07-10", 180.0)
    db.upsert_weight("2026-07-12", 179.0)
    rows = db.query_weight_log("2026-07-10", "2026-07-12")
    assert [r["date"] for r in rows] == ["2026-07-10", "2026-07-12"]


# --- water_log ---


def test_upsert_water_inserts_and_overwrites(tmp_db):
    db.upsert_water("2026-07-10", 64.0)
    db.upsert_water("2026-07-10", 80.0)
    rows = db.query_water_log("2026-07-10", "2026-07-10")
    assert len(rows) == 1
    assert rows[0]["fluid_ounces"] == 80.0


def test_query_water_log_range(tmp_db):
    db.upsert_water("2026-07-09", 50.0)
    db.upsert_water("2026-07-10", 64.0)
    db.upsert_water("2026-07-11", 72.0)
    rows = db.query_water_log("2026-07-10", "2026-07-11")
    assert [r["date"] for r in rows] == ["2026-07-10", "2026-07-11"]


# --- latest_date ---


def test_latest_date_empty_table(tmp_db):
    assert db.latest_date("daily_summary") is None


def test_latest_date_returns_max(tmp_db):
    db.upsert_daily_summary(_summary("2026-07-10"))
    db.upsert_daily_summary(_summary("2026-07-12"))
    db.upsert_daily_summary(_summary("2026-07-11"))
    assert db.latest_date("daily_summary") == "2026-07-12"
