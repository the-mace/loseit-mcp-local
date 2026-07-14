"""SQLite storage layer for scraped LoseIt data."""
import sqlite3
from pathlib import Path
from contextlib import contextmanager

DB_PATH = Path.home() / ".loseit-data" / "loseit.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_summary (
    date TEXT PRIMARY KEY,
    calorie_budget REAL,
    calories_eaten REAL,
    exercise_calories_burned REAL,
    calories_remaining_before_exercise REAL,
    calories_remaining_after_exercise REAL,
    protein_g REAL,
    carbs_g REAL,
    fat_g REAL,
    fetched_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS food_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    meal TEXT,
    name TEXT,
    brand TEXT,
    quantity REAL,
    units TEXT,
    calories REAL,
    protein_g REAL,
    carbs_g REAL,
    fat_g REAL,
    fetched_at TEXT DEFAULT (datetime('now')),
    -- brand is deliberately excluded from this constraint: it's never
    -- populated (always NULL, not exposed in the row DOM), and SQL
    -- treats NULL != NULL for uniqueness purposes, so including it here
    -- would silently defeat the dedup entirely on every re-scrape.
    UNIQUE(date, meal, name, quantity, units)
);

CREATE TABLE IF NOT EXISTS weight_log (
    date TEXT PRIMARY KEY,
    weight REAL,
    units TEXT,
    fetched_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS water_log (
    date TEXT PRIMARY KEY,
    fluid_ounces REAL,
    fetched_at TEXT DEFAULT (datetime('now'))
);
"""


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(SCHEMA)


@contextmanager
def get_conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def upsert_daily_summary(row: dict):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO daily_summary
               (date, calorie_budget, calories_eaten, exercise_calories_burned,
                calories_remaining_before_exercise, calories_remaining_after_exercise,
                protein_g, carbs_g, fat_g)
               VALUES (:date, :calorie_budget, :calories_eaten, :exercise_calories_burned,
                       :calories_remaining_before_exercise, :calories_remaining_after_exercise,
                       :protein_g, :carbs_g, :fat_g)
               ON CONFLICT(date) DO UPDATE SET
                 calorie_budget=excluded.calorie_budget,
                 calories_eaten=excluded.calories_eaten,
                 exercise_calories_burned=excluded.exercise_calories_burned,
                 calories_remaining_before_exercise=excluded.calories_remaining_before_exercise,
                 calories_remaining_after_exercise=excluded.calories_remaining_after_exercise,
                 protein_g=excluded.protein_g,
                 carbs_g=excluded.carbs_g,
                 fat_g=excluded.fat_g,
                 fetched_at=datetime('now')
            """,
            row,
        )


def insert_food_log_entry(row: dict):
    with get_conn() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO food_log
               (date, meal, name, brand, quantity, units, calories, protein_g, carbs_g, fat_g)
               VALUES (:date, :meal, :name, :brand, :quantity, :units, :calories, :protein_g, :carbs_g, :fat_g)
            """,
            row,
        )


def upsert_weight(date: str, weight: float, units: str = "lb"):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO weight_log (date, weight, units) VALUES (?, ?, ?)
               ON CONFLICT(date) DO UPDATE SET weight=excluded.weight, units=excluded.units,
                 fetched_at=datetime('now')""",
            (date, weight, units),
        )


def upsert_water(date: str, fluid_ounces: float):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO water_log (date, fluid_ounces) VALUES (?, ?)
               ON CONFLICT(date) DO UPDATE SET fluid_ounces=excluded.fluid_ounces,
                 fetched_at=datetime('now')""",
            (date, fluid_ounces),
        )


def query_daily_summary(start: str, end: str):
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT * FROM daily_summary WHERE date BETWEEN ? AND ? ORDER BY date", (start, end)
        )
        return [dict(r) for r in cur.fetchall()]


def query_food_log(start: str, end: str):
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT * FROM food_log WHERE date BETWEEN ? AND ? ORDER BY date, meal", (start, end)
        )
        return [dict(r) for r in cur.fetchall()]


def query_weight_log(start: str, end: str):
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT * FROM weight_log WHERE date BETWEEN ? AND ? ORDER BY date", (start, end)
        )
        return [dict(r) for r in cur.fetchall()]


def query_water_log(start: str, end: str):
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT * FROM water_log WHERE date BETWEEN ? AND ? ORDER BY date", (start, end)
        )
        return [dict(r) for r in cur.fetchall()]


def latest_date(table: str):
    with get_conn() as conn:
        cur = conn.execute(f"SELECT MAX(date) as d FROM {table}")
        row = cur.fetchone()
        return row["d"] if row else None
