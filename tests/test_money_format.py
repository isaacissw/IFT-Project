# Feature: phase-1-buy-stock, Property 2: Money formatting is consistent
"""Property-based tests for money formatting.

Property 2: Money formatting is consistent.
Validates: Requirements 13.5, 14.1, 17.4

Every string produced by ``app.money.format_money`` must have the same shape:
a leading ``$``, a comma as the thousands separator in the integer part, and
exactly two decimal places (for example ``$12,500.00``). These tests exercise
``format_money`` (together with its inputs built via ``to_money``/``quantize``)
directly, with no database involved.

Money values are always built from strings/ints and kept as ``decimal.Decimal``
— never from ``float`` — matching the system-wide rule in ``app/money.py``.
"""

import re
from decimal import Decimal

from hypothesis import example, given, settings
from hypothesis import strategies as st

from app.money import format_money, quantize, to_money

# A correctly formatted money string: a leading "$", an integer part written as
# 1-3 digits optionally followed by comma-separated groups of exactly 3 digits,
# then a dot and exactly two decimal digits.
MONEY_RE = re.compile(r"^\$\d{1,3}(,\d{3})*\.\d{2}$")

# Non-negative money values generated as integer cents, then turned into a
# Decimal via a string. This never touches float: we build the decimal text
# ("12345" cents -> "123.45") ourselves and hand it to ``to_money``. The upper
# bound stays within MAX_MONEY (99999999.99 == 9_999_999_999 cents).
money_decimals = st.integers(min_value=0, max_value=9_999_999_999).map(
    lambda cents: to_money(f"{cents // 100}.{cents % 100:02d}")
)


@given(value=money_decimals)
@settings(max_examples=200)
@example(value=Decimal("12500"))
@example(value=Decimal("10000"))
@example(value=Decimal("0"))
@example(value=Decimal("0.005"))
@example(value=Decimal("999999.999"))
@example(value=Decimal("1234567.89"))
def test_format_money_shape_is_consistent(value):
    """format_money always yields $, comma thousands, and exactly two decimals.

    Validates: Requirements 13.5, 14.1, 17.4
    """
    result = format_money(value)

    # Leading dollar sign.
    assert result.startswith("$"), result

    # Overall shape: $, comma-grouped integer part, exactly two decimals.
    assert MONEY_RE.match(result), result

    # Exactly two decimal places (belt-and-suspenders on top of the regex).
    assert len(result.split(".")[1]) == 2, result

    # The formatted number equals the quantized input (formatting changes the
    # presentation, not the value).
    numeric = Decimal(result[1:].replace(",", ""))
    assert numeric == quantize(value), (result, value)


def test_format_money_known_examples():
    """Spot-check the exact strings the acceptance criteria call out."""
    assert format_money(Decimal("12500")) == "$12,500.00"
    assert format_money(Decimal("10000")) == "$10,000.00"
    assert format_money(to_money("0")) == "$0.00"
    assert format_money(to_money("1234567.5")) == "$1,234,567.50"
    assert format_money(to_money("99999999.99")) == "$99,999,999.99"
