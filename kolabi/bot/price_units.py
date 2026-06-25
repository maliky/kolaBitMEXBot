"""Pure helpers for typed price-distance units."""

from __future__ import annotations

from decimal import Decimal

from kolabi.shared.core.runtime_types import to_decimal

LOG_BPS_SCALE = Decimal("10000")


def logbps_price_distance(
    reference_price: Decimal | int | float | str,
    logbps: Decimal | int | float | str,
) -> Decimal:
    """Return the absolute price distance represented by signed log basis points."""
    reference = to_decimal(reference_price)
    value = abs(to_decimal(logbps))
    if reference <= 0 or value == 0:
        return Decimal("0")
    return reference * ((value / LOG_BPS_SCALE).exp() - Decimal("1"))


def signed_logbps_move(
    current_price: Decimal | int | float | str,
    baseline_price: Decimal | int | float | str,
) -> Decimal:
    """Return ln(current / baseline) * 10000 as signed log basis points."""
    current = to_decimal(current_price)
    baseline = to_decimal(baseline_price)
    if current <= 0 or baseline <= 0:
        raise ValueError("logbps prices must be positive")
    return (current / baseline).ln() * LOG_BPS_SCALE
