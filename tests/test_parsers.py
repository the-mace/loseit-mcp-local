"""Unit tests for pure text parsers in scraper.py (no browser needed)."""
import pytest
from scraper import _parse_number, _parse_quantity


# --- _parse_quantity ---


@pytest.mark.parametrize(
    "text, expected",
    [
        ("2  Servings", (2.0, "Servings")),
        ("2 Servings", (2.0, "Servings")),
        ("1/2 Each", (0.5, "Each")),
        ("3/4 cup", (0.75, "cup")),
        ("1.5 oz", (1.5, "oz")),
        ("10 g", (10.0, "g")),
    ],
)
def test_parse_quantity_happy_paths(text, expected):
    assert _parse_quantity(text) == expected


def test_parse_quantity_missing_units_returns_none_qty():
    qty, units = _parse_quantity("plain")
    assert qty is None
    assert units == "plain"


def test_parse_quantity_empty_string():
    qty, units = _parse_quantity("")
    assert qty is None
    assert units == ""


def test_parse_quantity_whitespace_only():
    qty, units = _parse_quantity("   ")
    assert qty is None
    assert units == ""


# --- _parse_number ---


@pytest.mark.parametrize(
    "text, expected",
    [
        ("0", 0.0),
        ("42", 42.0),
        ("1,234", 1234.0),
        ("1,234.5", 1234.5),
        ("  88  ", 88.0),
        ("-", 0.0),
        ("", 0.0),
        ("   ", 0.0),
        ("3.14", 3.14),
    ],
)
def test_parse_number(text, expected):
    assert _parse_number(text) == expected


def test_parse_number_invalid_raises():
    with pytest.raises(ValueError):
        _parse_number("n/a")
