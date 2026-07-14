"""Shared fixtures. Every DB test gets an isolated temp SQLite file so we
never touch ~/.loseit-data/loseit.db."""
import pytest
import db


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    path = tmp_path / "loseit.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    return path
