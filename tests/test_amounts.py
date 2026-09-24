"""Contract tests for to_minor_units: exact conversion by the gateway's decimals, no guessing."""

from decimal import Decimal

import pytest

from dominaite import CURRENCY_EXPONENTS, UNSUPPORTED_CURRENCIES, to_minor_units


@pytest.mark.parametrize(
    "amount, currency, minor",
    [
        ("25.00", "EUR", 2500),
        ("25", "EUR", 2500),
        ("25.5", "EUR", 2550),
        ("0.01", "USD", 1),
        ("0", "GBP", 0),
        ("1234.56", "BGN", 123456),
        ("19.99", "CAD", 1999),
        ("19.99", "AUD", 1999),
        ("2500", "JPY", 2500),
        ("2500", "HUF", 2500),
        ("2.5", "BHD", 2500),
        ("1.234", "KWD", 1234),
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
        ("25.50", "HUF"),
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


def test_the_exponent_table_is_the_gateway_registry():
    """Exact: a currency added, dropped or moved must be a deliberate change here."""
    two = ["EUR", "USD", "GBP", "CAD", "AUD", "CHF", "BGN", "RON", "PLN", "CZK", "SEK", "DKK", "NOK"]
    expected = dict.fromkeys(two, 2)
    expected.update({"JPY": 0, "HUF": 0, "BHD": 3, "KWD": 3})
    assert dict(CURRENCY_EXPONENTS) == expected


def test_huf_is_whole_forints_not_the_iso_two_decimals():
    """ISO 4217 says 2, the gateway says 0. Following ISO would charge 100x too much."""
    assert to_minor_units("2500", "HUF") == 2500
    with pytest.raises(ValueError, match="decimal places"):
        to_minor_units("2500.00", "HUF")


@pytest.mark.parametrize("currency", ["ISK", "KRW", "OMR", "JOD", "TND", "krw"])
def test_currencies_where_iso_and_the_gateway_disagree_are_refused(currency):
    with pytest.raises(ValueError, match="not supported"):
        to_minor_units("100", currency)


def test_unsupported_currencies_have_no_exponent_on_record():
    assert set(UNSUPPORTED_CURRENCIES) == {"ISK", "KRW", "OMR", "JOD", "TND"}
    assert not set(UNSUPPORTED_CURRENCIES) & set(CURRENCY_EXPONENTS)


def test_the_exponent_table_cannot_be_edited_at_runtime():
    with pytest.raises(TypeError):
        CURRENCY_EXPONENTS["XYZ"] = 2  # type: ignore[index]
