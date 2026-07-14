"""Unit tests for MCP date-range defaults (no MCP server process needed)."""
from datetime import date

import loseit_mcp


def test_default_range_looks_back_n_days(monkeypatch):
    class FakeDate:
        @staticmethod
        def today():
            return date(2026, 7, 14)

    monkeypatch.setattr(loseit_mcp, "date", FakeDate)

    start, end = loseit_mcp._default_range(7)
    assert start == "2026-07-07"
    assert end == "2026-07-14"


def test_default_range_zero_days(monkeypatch):
    class FakeDate:
        @staticmethod
        def today():
            return date(2026, 7, 14)

    monkeypatch.setattr(loseit_mcp, "date", FakeDate)

    start, end = loseit_mcp._default_range(0)
    assert start == "2026-07-14"
    assert end == "2026-07-14"
