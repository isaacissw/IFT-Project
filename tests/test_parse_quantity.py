"""Property-based and example tests for ``app.services.trading.parse_quantity``.

Property 3: Valid quantities parse to their integer.
Validates: Requirements 7.1

Property 4: Invalid quantities are rejected with the greater-than-zero message.
Validates: Requirements 7.2

Property 5: Over-maximum quantities are rejected with the no-more-than message.
Validates: Requirements 7.3

``parse_quantity`` is a pure helper: it takes raw form input and returns a whole
integer from 1 through 1,000,000, or raises ``ValidationError`` with one of two
exact user-facing messages. These tests exercise it directly with no database
involved. They assert the EXACT message strings via ``str(exc.value)``.
"""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.services.trading import ValidationError, parse_quantity

# The two exact user-facing messages parse_quantity may raise.
GREATER_THAN_ZERO_MSG = "Please enter a whole number of shares greater than 0."
NO_MORE_THAN_MSG = "Please enter a whole number of shares no more than 1,000,000."

MAX_QUANTITY = 1_000_000


# ---------------------------------------------------------------------------
# Property 3: Valid quantities parse to their integer.
# ---------------------------------------------------------------------------

# A whole number 1..1,000,000 rendered as a plain base-10 digit string, with
# optional surrounding whitespace (spaces, tabs, newlines) that parse_quantity
# strips before validating.
_whitespace = st.text(alphabet=" \t\r\n", max_size=4)


@st.composite
def _valid_quantity_strings(draw):
    value = draw(st.integers(min_value=1, max_value=MAX_QUANTITY))
    lead = draw(_whitespace)
    trail = draw(_whitespace)
    return value, f"{lead}{value}{trail}"


# Feature: phase-1-buy-stock, Property 3: Valid quantities parse to their integer
@given(pair=_valid_quantity_strings())
@settings(max_examples=200)
def test_valid_quantities_parse_to_their_integer(pair):
    """Any whole number 1..1,000,000 (optionally padded) parses to that int.

    Validates: Requirements 7.1
    """
    value, raw = pair
    assert parse_quantity(raw) == value


# ---------------------------------------------------------------------------
# Property 4: Invalid quantities are rejected with the greater-than-zero message.
# ---------------------------------------------------------------------------

# Dedicated strategies per invalid category so a generated "invalid" input is
# never accidentally a valid 1..1,000,000 digit string.

# Empty / whitespace-only strings (strip to "").
_empty_or_spaces = st.text(alphabet=" \t\r\n", max_size=6)

# "0" and other all-zero digit strings ("00", "000", ...): digit strings whose
# integer value is 0.
_zeros = st.integers(min_value=1, max_value=8).map(lambda n: "0" * n)

# Negative integers like "-5".
_negatives = st.integers(min_value=1, max_value=10**9).map(lambda n: f"-{n}")

# Decimals like "1.5".
_decimals = st.builds(
    lambda whole, frac: f"{whole}.{frac}",
    st.integers(min_value=0, max_value=10**6),
    st.integers(min_value=0, max_value=999),
)

# Scientific notation like "1e3".
_scientific = st.builds(
    lambda mant, exp: f"{mant}e{exp}",
    st.integers(min_value=1, max_value=999),
    st.integers(min_value=0, max_value=9),
)

# Non-numeric text: any text that, after stripping, is NOT a pure ASCII digit
# string. Filtering this way guarantees we never emit a valid digit string.
_non_numeric = st.text(max_size=12).filter(
    lambda s: not s.strip().isdigit() or not all(c in "0123456789" for c in s.strip())
)

_invalid_inputs = st.one_of(
    _empty_or_spaces,
    _zeros,
    _negatives,
    _decimals,
    _scientific,
    _non_numeric,
)


# Feature: phase-1-buy-stock, Property 4: Invalid quantities are rejected with the greater-than-zero message
@given(raw=_invalid_inputs)
@settings(max_examples=300)
def test_invalid_quantities_rejected_with_greater_than_zero_message(raw):
    """Invalid inputs raise ValidationError with the exact >0 message.

    Validates: Requirements 7.2
    """
    with pytest.raises(ValidationError) as exc:
        parse_quantity(raw)
    assert str(exc.value) == GREATER_THAN_ZERO_MSG


# ---------------------------------------------------------------------------
# Property 5: Over-maximum quantities are rejected with the no-more-than message.
# ---------------------------------------------------------------------------

# Whole numbers strictly greater than 1,000,000, rendered as digit strings
# (optionally padded with whitespace).
@st.composite
def _over_max_strings(draw):
    value = draw(st.integers(min_value=MAX_QUANTITY + 1, max_value=10**15))
    lead = draw(_whitespace)
    trail = draw(_whitespace)
    return f"{lead}{value}{trail}"


# Feature: phase-1-buy-stock, Property 5: Over-maximum quantities are rejected with the no-more-than message
@given(raw=_over_max_strings())
@settings(max_examples=200)
def test_over_maximum_quantities_rejected_with_no_more_than_message(raw):
    """Whole numbers over 1,000,000 raise the exact no-more-than message.

    Validates: Requirements 7.3
    """
    with pytest.raises(ValidationError) as exc:
        parse_quantity(raw)
    assert str(exc.value) == NO_MORE_THAN_MSG


# ---------------------------------------------------------------------------
# Explicit example tests.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    ["", "  ", "0", "-1", "1.5", "1e3", "abc"],
)
def test_invalid_examples_raise_greater_than_zero_message(raw):
    """Spot-check the exact >0 message for the called-out invalid inputs."""
    with pytest.raises(ValidationError) as exc:
        parse_quantity(raw)
    assert str(exc.value) == GREATER_THAN_ZERO_MSG


def test_valid_examples_parse_to_their_integer():
    """The boundary valid examples parse to their integer value."""
    assert parse_quantity("1") == 1
    assert parse_quantity("1000000") == 1_000_000


def test_over_maximum_example_raises_no_more_than_message():
    """1,000,001 is just over the maximum and uses the no-more-than message."""
    with pytest.raises(ValidationError) as exc:
        parse_quantity("1000001")
    assert str(exc.value) == NO_MORE_THAN_MSG
