"""Money helpers.

This module provides ``to_money(value)`` (build a Decimal from a string or int,
never a float), ``quantize(value)`` (round to two decimal places using
ROUND_HALF_UP), and ``format_money(value)`` (format as ``$1,234.56``). All money
in the system is ``decimal.Decimal``; floats are never used for money.

``format_money`` is the single formatter used by templates (via a Jinja filter)
so every money string in the UI is identical in style.
"""

from decimal import Decimal, ROUND_HALF_UP

# The cent quantum: all money is rounded/quantized to this many decimal places.
CENT = Decimal("0.01")

# Largest value any money column (DECIMAL(10,2)) can hold.
MAX_MONEY = Decimal("99999999.99")


def to_money(value) -> Decimal:
    """Build a Decimal from a string (or int). Never from float.

    We go through ``str(value)`` so that an accidental float argument is first
    turned into its decimal text form rather than its binary float value. The
    rule across the whole system is: money is always Decimal, built from text.
    """
    return Decimal(str(value))


def quantize(value: Decimal) -> Decimal:
    """Round to 2 decimal places using ROUND_HALF_UP."""
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def format_money(value: Decimal) -> str:
    """Format as $1,234.56 — leading $, comma thousands, exactly 2 decimals."""
    q = quantize(value)
    return "${:,.2f}".format(q)
