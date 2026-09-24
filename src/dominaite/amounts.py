"""Turning a decimal price into the integer minor units the API takes.

Every amount you send is an integer in the currency's minor unit: 2500 is 25.00 EUR, but
2500 is 2500 JPY and 2.500 BHD. How many minor units make one major unit is the number
of decimals the GATEWAY uses for that currency, and getting it wrong charges the wrong
amount by a factor of 100 or 1000 without any error.

That is not always the ISO 4217 exponent. The gateway takes HUF in whole forints (0
decimals, where ISO says 2), so 2500 means 2500 Ft, not 25.00 Ft.

:func:`to_minor_units` does the conversion without float arithmetic, and refuses what it
cannot convert exactly rather than rounding: a currency it does not know, or more
decimal places than the currency has.
"""

import re
from decimal import Decimal
from types import MappingProxyType
from typing import Mapping, Union

#: Decimals per currency, as the gateway's currency registry defines them. A currency
#: that is not here makes :func:`to_minor_units` raise rather than guess: assuming 2
#: would charge a JPY or BHD customer the wrong amount.
CURRENCY_EXPONENTS: Mapping[str, int] = MappingProxyType(
    {
        # Two decimals.
        "EUR": 2,
        "USD": 2,
        "GBP": 2,
        "CAD": 2,
        "AUD": 2,
        "CHF": 2,
        "BGN": 2,
        "RON": 2,
        "PLN": 2,
        "CZK": 2,
        "SEK": 2,
        "DKK": 2,
        "NOK": 2,
        # No decimals. HUF is whole forints on the gateway, not the ISO 4217 two.
        "JPY": 0,
        "HUF": 0,
        # Three decimals.
        "BHD": 3,
        "KWD": 3,
    }
)

#: Currencies where ISO 4217 and the gateway disagree on the decimals (the gateway would
#: read them as two). Converting either way would be off by 100x or 10x for someone, so
#: :func:`to_minor_units` refuses them outright.
UNSUPPORTED_CURRENCIES = frozenset({"ISK", "KRW", "OMR", "JOD", "TND"})

# Digits, optionally a point and at least one more digit. No sign, no exponent, no
# grouping separators, no surrounding whitespace: anything looser invites a silent
# misreading of a price.
_DECIMAL_STRING_RE = re.compile(r"(\d+)(?:\.(\d+))?")


def to_minor_units(amount: Union[str, Decimal], currency: str) -> int:
    """Convert a decimal amount to integer minor units, exactly.

    ``to_minor_units("25.00", "EUR")`` is ``2500``; ``to_minor_units("2500", "JPY")`` and
    ``to_minor_units("2500", "HUF")`` are ``2500``; ``to_minor_units("2.5", "BHD")`` is
    ``2500``.

    Pass the amount as a string or a :class:`decimal.Decimal`, never a float: ``0.1 + 0.2``
    is ``0.30000000000000004`` as a float, while ``to_minor_units("0.30", "EUR")`` is
    exactly ``30``.

    Nothing is rounded. More decimal places than the currency has raises, even when the
    extra digits are zeros (``"25.000"`` for EUR): quantize your value to the currency's
    exponent first if it comes from a wider column.

    :param amount: A non-negative decimal string like ``"25.00"``, or a ``Decimal``.
    :param currency: ISO 4217 code, any case. Must be in :data:`CURRENCY_EXPONENTS`.
    :returns: The amount in minor units, ready for ``amount=``.
    :raises ValueError: A float, int or other type, a malformed or negative amount, an
        unknown or unsupported currency, or more decimal places than the currency allows.
    """
    code = currency.upper() if isinstance(currency, str) else ""
    if code in UNSUPPORTED_CURRENCIES:
        raise ValueError(
            "currency {0} is not supported: ISO 4217 and the gateway disagree on its "
            "decimals, so any conversion would be wrong by 10x or 100x".format(code)
        )
    exponent = CURRENCY_EXPONENTS.get(code)
    if exponent is None:
        raise ValueError("Unknown currency {0}: no minor-unit exponent on record".format(currency))

    if isinstance(amount, Decimal):
        if not amount.is_finite():
            raise ValueError("amount must be a finite number")
        # Fixed-point text, so Decimal("1E+2") reads as "100" and the digit count below
        # sees exactly what the value carries.
        text = format(amount, "f")
    elif isinstance(amount, str):
        text = amount
    else:
        raise ValueError(
            "amount must be a decimal string like '25.00' or a Decimal, not {0}; floats "
            "cannot hold most prices exactly".format(type(amount).__name__)
        )

    match = _DECIMAL_STRING_RE.fullmatch(text)
    if match is None:
        raise ValueError(
            "amount must be a non-negative decimal like '25.00', got {0!r}".format(text)
        )

    whole, fraction = match.group(1), match.group(2) or ""
    if len(fraction) > exponent:
        raise ValueError(
            "{0} has {1} decimal places; {2} has {3}".format(
                text, len(fraction), code, exponent
            )
        )
    # Integer arithmetic only: pad the fraction out to the exponent and read both parts
    # as one whole number of minor units.
    return int(whole + fraction.ljust(exponent, "0"))
