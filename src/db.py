"""SQLite storage layer for scraped LoseIt data."""
import sqlite3
from pathlib import Path
from contextlib import contextmanager

DB_PATH = Path.home() / ".loseit-data" / "loseit.db"

_FOOD_LOG_TABLE = """
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
    fetched_at TEXT DEFAULT (datetime('now'))
);
"""

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
    notes TEXT,
    fetched_at TEXT DEFAULT (datetime('now'))
);

{food_log}
CREATE INDEX IF NOT EXISTS food_log_date ON food_log(date);

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
""".replace("{food_log}", _FOOD_LOG_TABLE.strip())


_FOOD_LOG_COLS = "id, date, meal, name, brand, quantity, units, calories, protein_g, carbs_g, fat_g, fetched_at"


def _migrate(conn: sqlite3.Connection):
    """Apply schema changes for DBs created before newer columns."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(daily_summary)")}
    if cols and "notes" not in cols:
        conn.execute("ALTER TABLE daily_summary ADD COLUMN notes TEXT")
    # The old UNIQUE(date, meal, name, quantity, units) merged two identical
    # entries in one meal into one row; days are now replaced whole instead.
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='food_log'"
    ).fetchone()
    if sql and "UNIQUE" in sql[0].upper():
        conn.executescript(f"""
            BEGIN;
            ALTER TABLE food_log RENAME TO food_log_old;
            {_FOOD_LOG_TABLE}
            INSERT INTO food_log ({_FOOD_LOG_COLS}) SELECT {_FOOD_LOG_COLS} FROM food_log_old;
            DROP TABLE food_log_old;
            COMMIT;
        """)


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        # Before SCHEMA, so its food_log index lands on the rebuilt table.
        _migrate(conn)
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
    # Default notes to None so older call sites / partial dicts still work.
    row = {"notes": None, **row}
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO daily_summary
               (date, calorie_budget, calories_eaten, exercise_calories_burned,
                calories_remaining_before_exercise, calories_remaining_after_exercise,
                protein_g, carbs_g, fat_g, notes)
               VALUES (:date, :calorie_budget, :calories_eaten, :exercise_calories_burned,
                       :calories_remaining_before_exercise, :calories_remaining_after_exercise,
                       :protein_g, :carbs_g, :fat_g, :notes)
               ON CONFLICT(date) DO UPDATE SET
                 calorie_budget=excluded.calorie_budget,
                 calories_eaten=excluded.calories_eaten,
                 exercise_calories_burned=excluded.exercise_calories_burned,
                 calories_remaining_before_exercise=excluded.calories_remaining_before_exercise,
                 calories_remaining_after_exercise=excluded.calories_remaining_after_exercise,
                 protein_g=excluded.protein_g,
                 carbs_g=excluded.carbs_g,
                 fat_g=excluded.fat_g,
                 notes=excluded.notes,
                 fetched_at=datetime('now')
            """,
            row,
        )


def replace_food_log(date: str, rows: list[dict]):
    """Make the stored food rows for `date` exactly `rows`, in one transaction."""
    with get_conn() as conn:
        conn.execute("DELETE FROM food_log WHERE date = ?", (date,))
        conn.executemany(
            """INSERT INTO food_log
               (date, meal, name, brand, quantity, units, calories, protein_g, carbs_g, fat_g)
               VALUES (:date, :meal, :name, :brand, :quantity, :units, :calories, :protein_g, :carbs_g, :fat_g)
            """,
            [{**r, "date": date} for r in rows],
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
