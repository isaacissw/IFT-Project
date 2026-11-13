"""Portfolio helpers shared by several pages."""

from decimal import Decimal

from app.money import to_money


def portfolio_value(holdings):
    """Sum of quantity x current price over a list of holding rows, as an exact Decimal."""
    total = Decimal("0.00")
    for h in holdings:
        total += to_money(h["current_price"]) * int(h["quantity"])
    return total
