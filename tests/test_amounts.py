"""Contract tests for to_minor_units: exact conversion by ISO 4217 exponent, no guessing."""

from decimal import Decimal

import pytest

from dominaite import CURRENCY_EXPONENTS, to_minor_units


@pytest.mark.parametrize(
    "amount, currency, minor",
    [
        ("25.00", "EUR", 2500),
        ("25", "EUR", 2500),
        ("25.5", "EUR", 2550),
        ("0.01", "USD", 1),
        ("0", "GBP", 0),
        ("1234.56", "BGN", 123456),
        ("2500", "JPY", 2500),
        ("2500", "KRW", 2500),
        ("99", "ISK", 99),
        ("2.5", "BHD", 2500),
        ("1.234", "KWD", 1234),
        ("0.001", "TND", 1),
    ],
)
def test_converts_by_the_currency_exponent(amount, currency, minor):
    assert to_minor_units(amount, currency) == minor


def test_the_float_trap_does_not_apply():
    """0.1 + 0.2 is 0.30000000000000004 as a float; the string form is exactly 30 cents."""
    assert 0.1 + 0.2 != 0.3
    assert to_minor_units("0.30", "EUR") == 30
    assert to_minor_units(Decimal("0.1") + Decimal("0.2"), "EUR") == 30


def test_accepts_decimal_values():
    assert to_minor_units(Decimal("25.00"), "EUR") == 2500
    assert to_minor_units(Decimal("1E+2"), "EUR") == 10000
    assert to_minor_units(Decimal("2500"), "JPY") == 2500


def test_currency_is_case_insensitive():
    assert to_minor_units("25.00", "eur") == 2500


@pytest.mark.parametrize(
    "amount, currency",
    [
        ("25.001", "EUR"),
        ("25.000", "EUR"),
        ("100.5", "JPY"),
        ("100.0", "JPY"),
        ("1.2345", "BHD"),
        (Decimal("25.001"), "EUR"),
    ],
)
def test_refuses_more_decimal_places_than_the_currency_has(amount, currency):
    """Rounding would charge a different amount than the caller computed; refuse instead."""
    with pytest.raises(ValueError, match="decimal places"):
        to_minor_units(amount, currency)


@pytest.mark.parametrize("currency", ["XYZ", "", "EURO", None, 978])
def test_an_unknown_currency_is_an_error_not_a_default(currency):
    with pytest.raises(ValueError, match="unknown currency"):
        to_minor_units("25.00", currency)


@pytest.mark.parametrize(
    "amount",
    [25.0, 0.3, 25, True, None],
    ids=["float", "small-float", "int", "bool", "none"],
)
def test_refuses_anything_but_a_string_or_decimal(amount):
    with pytest.raises(ValueError, match="decimal string"):
        to_minor_units(amount, "EUR")


@pytest.mark.parametrize(
    "amount",
    ["-1.00", "+1.00", "1,00", "1,000.00", " 1.00", "1.00 ", "1.", ".5", "1e2", "", "abc",
     Decimal("-1.00"), Decimal("NaN"), Decimal("Infinity")],
)
def test_refuses_malformed_or_negative_amounts(amount):
    with pytest.raises(ValueError):
        to_minor_units(amount, "EUR")


def test_the_exponent_table_covers_the_documented_currencies():
    two = ["EUR", "USD", "GBP", "BGN", "RON", "CHF", "PLN", "CZK", "HUF", "SEK", "DKK", "NOK"]
    assert {code: CURRENCY_EXPONENTS[code] for code in two} == dict.fromkeys(two, 2)
    assert {code: CURRENCY_EXPONENTS[code] for code in ["JPY", "KRW", "ISK"]} == dict.fromkeys(
        ["JPY", "KRW", "ISK"], 0
    )
    three = ["BHD", "KWD", "OMR", "JOD", "TND"]
    assert {code: CURRENCY_EXPONENTS[code] for code in three} == dict.fromkeys(three, 3)


def test_the_exponent_table_cannot_be_edited_at_runtime():
    with pytest.raises(TypeError):
        CURRENCY_EXPONENTS["XYZ"] = 2  # type: ignore[index]
