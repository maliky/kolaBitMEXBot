"""Pure quantity materialisation helpers for typed strategy quantities."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
from typing import Mapping

from kolabi.bot.domain import OrderPairSpec
from kolabi.shared.core.runtime_types import to_decimal


USD_NOTIONAL_QUANTITY_TYPE = "qU"
PERCENT_BALANCE_QUANTITY_TYPE = "q%"


class QuantityResolutionError(ValueError):
    """Raised when a declarative strategy quantity cannot become exchange size."""


@dataclass(frozen=True)
class UsdQuantityResolution:
    nominal_usd: Decimal
    mark_price: Decimal
    contract_size: Decimal
    quantity_step: Decimal
    min_quantity: Decimal
    quantity: Decimal

    @property
    def approx_usd(self) -> Decimal:
        return self.quantity * self.contract_size * self.mark_price


@dataclass(frozen=True)
class PercentBalanceQuantityResolution:
    percent: Decimal
    available_usd: Decimal
    nominal_usd: Decimal
    mark_price: Decimal
    contract_size: Decimal
    quantity_step: Decimal
    min_quantity: Decimal
    quantity: Decimal

    @property
    def approx_usd(self) -> Decimal:
        return self.quantity * self.contract_size * self.mark_price


@dataclass(frozen=True)
class AbsoluteQuantityValidation:
    quantity: Decimal
    quantity_step: Decimal
    min_quantity: Decimal


def pair_uses_usd_quantity(pair: OrderPairSpec) -> bool:
    return pair.head_quantity_type == USD_NOTIONAL_QUANTITY_TYPE


def pair_uses_percent_balance_quantity(pair: OrderPairSpec) -> bool:
    return pair.head_quantity_type == PERCENT_BALANCE_QUANTITY_TYPE


def pair_uses_materialized_quantity(pair: OrderPairSpec) -> bool:
    return pair_uses_usd_quantity(pair) or pair_uses_percent_balance_quantity(pair)


def validate_pair_absolute_quantity(
    pair: OrderPairSpec,
    *,
    rules: Mapping[str, object],
) -> AbsoluteQuantityValidation:
    if pair.head_quantity is None:
        raise QuantityResolutionError(
            f"QTY_ABS_INVALID pair={pair.name} missing absolute quantity"
        )
    quantity = _positive_decimal(
        pair.head_quantity,
        label=f"QTY_ABS_INVALID pair={pair.name} qty",
    )
    min_quantity = quantity_minimum_from_rules(rules)
    quantity_step = quantity_step_from_rules(
        rules,
        min_quantity=min_quantity,
        error_code="QTY_ABS_INVALID",
    )
    if min_quantity > 0 and quantity < min_quantity:
        raise QuantityResolutionError(
            "QTY_ABS_INVALID "
            f"pair={pair.name} qty={quantity} min={min_quantity} step={quantity_step}"
        )
    ratio = quantity / quantity_step
    if ratio != ratio.to_integral_value():
        raise QuantityResolutionError(
            "QTY_ABS_INVALID "
            f"pair={pair.name} qty={quantity} min={min_quantity} step={quantity_step}"
        )
    return AbsoluteQuantityValidation(
        quantity=quantity,
        quantity_step=quantity_step,
        min_quantity=min_quantity,
    )


def mark_price_for_usd_quantity(
    pair: OrderPairSpec,
    market: object,
    *,
    error_code: str = "QTY_USD_TOO_SMALL",
) -> object:
    if getattr(market, "ready", True) is False:
        reason = getattr(market, "reason", None) or "public market data is not ready"
        raise QuantityResolutionError(
            f"{error_code} pair={pair.name} mark unavailable: {reason}"
        )
    return getattr(market, "mark_price", None)


def available_usd_for_percent_quantity(pair: OrderPairSpec, balance: object) -> object:
    if getattr(balance, "ready", True) is False:
        reason = getattr(balance, "reason", None) or "account balance is not ready"
        raise QuantityResolutionError(
            f"QTY_PCT_TOO_SMALL pair={pair.name} balance unavailable: {reason}"
        )
    return getattr(balance, "available", None)


def resolve_pair_usd_quantity(
    pair: OrderPairSpec,
    *,
    mark_price: object,
    rules: Mapping[str, object],
) -> UsdQuantityResolution:
    if pair.head_quantity is None:
        raise QuantityResolutionError(
            f"QTY_USD_TOO_SMALL pair={pair.name} missing nominal USD quantity"
        )
    nominal_usd = _positive_decimal(
        pair.head_quantity,
        label=f"QTY_USD_TOO_SMALL pair={pair.name} qty",
    )
    return _resolve_nominal_usd_quantity(
        pair,
        nominal_usd=nominal_usd,
        mark_price=mark_price,
        rules=rules,
        error_code="QTY_USD_TOO_SMALL",
    )


def resolve_pair_percent_balance_quantity(
    pair: OrderPairSpec,
    *,
    available_usd: object,
    mark_price: object,
    rules: Mapping[str, object],
) -> PercentBalanceQuantityResolution:
    if pair.head_quantity is None:
        raise QuantityResolutionError(
            f"QTY_PCT_TOO_SMALL pair={pair.name} missing percent quantity"
        )
    percent = _percentage_decimal(
        pair.head_quantity,
        label=f"QTY_PCT_TOO_SMALL pair={pair.name} percent",
    )
    available = _positive_decimal(
        available_usd,
        label=f"QTY_PCT_TOO_SMALL pair={pair.name} available_usd",
    )
    nominal_usd = available * percent / Decimal("100")
    resolved = _resolve_nominal_usd_quantity(
        pair,
        nominal_usd=nominal_usd,
        mark_price=mark_price,
        rules=rules,
        error_code="QTY_PCT_TOO_SMALL",
    )
    return PercentBalanceQuantityResolution(
        percent=percent,
        available_usd=available,
        nominal_usd=resolved.nominal_usd,
        mark_price=resolved.mark_price,
        contract_size=resolved.contract_size,
        quantity_step=resolved.quantity_step,
        min_quantity=resolved.min_quantity,
        quantity=resolved.quantity,
    )


def _resolve_nominal_usd_quantity(
    pair: OrderPairSpec,
    *,
    nominal_usd: Decimal,
    mark_price: object,
    rules: Mapping[str, object],
    error_code: str,
) -> UsdQuantityResolution:
    mark = _positive_decimal(
        mark_price,
        label=f"{error_code} pair={pair.name} mark",
    )
    contract_size = _positive_rule_decimal(
        rules,
        ("contractSize", "contract_size"),
        label=f"{error_code} pair={pair.name} contract_size",
    )
    min_quantity = quantity_minimum_from_rules(rules)
    quantity_step = quantity_step_from_rules(
        rules,
        min_quantity=min_quantity,
        error_code=error_code,
    )
    raw_quantity = nominal_usd / (mark * contract_size)
    steps = (raw_quantity / quantity_step).to_integral_value(rounding=ROUND_FLOOR)
    quantity = steps * quantity_step
    if quantity <= 0:
        raise QuantityResolutionError(
            f"{error_code} "
            f"pair={pair.name} nominal_usd={nominal_usd} mark={mark} "
            f"contract_size={contract_size} step={quantity_step} resolved=0"
        )
    if min_quantity > 0 and quantity < min_quantity:
        raise QuantityResolutionError(
            f"{error_code} "
            f"pair={pair.name} nominal_usd={nominal_usd} mark={mark} "
            f"contract_size={contract_size} step={quantity_step} "
            f"min={min_quantity} resolved={quantity}"
        )
    return UsdQuantityResolution(
        nominal_usd=nominal_usd,
        mark_price=mark,
        contract_size=contract_size,
        quantity_step=quantity_step,
        min_quantity=min_quantity,
        quantity=quantity,
    )


def quantity_minimum_from_rules(rules: Mapping[str, object]) -> Decimal:
    value = _first_rule_decimal(
        rules,
        (
            "minimumQuantity",
            "minOrderSize",
            "minimumOrderSize",
            "minQuantity",
            "ordermin",
        ),
    )
    return value if value is not None else Decimal("0")


def quantity_step_from_rules(
    rules: Mapping[str, object],
    *,
    min_quantity: Decimal | None = None,
    error_code: str = "QTY_USD_TOO_SMALL",
) -> Decimal:
    step = _first_rule_decimal(
        rules,
        (
            "quantityStep",
            "quantity_step",
            "quantityIncrement",
            "qtyIncrement",
            "orderQtyStep",
            "lotSize",
            "stepSize",
        ),
    )
    if step is None:
        step = (
            min_quantity
            if min_quantity is not None
            else quantity_minimum_from_rules(rules)
        )
    if step is None or step <= 0:
        step = _first_rule_decimal(rules, ("contractSize", "contract_size"))
    if step is None or step <= 0:
        raise QuantityResolutionError(f"{error_code} missing positive quantity step")
    return step


def _positive_rule_decimal(
    rules: Mapping[str, object],
    keys: tuple[str, ...],
    *,
    label: str,
) -> Decimal:
    value = _first_rule_decimal(rules, keys)
    if value is None or value <= 0:
        raise QuantityResolutionError(f"{label} must be positive")
    return value


def _first_rule_decimal(
    rules: Mapping[str, object],
    keys: tuple[str, ...],
) -> Decimal | None:
    for key in keys:
        value = rules.get(key)
        if value in (None, ""):
            continue
        parsed = (
            to_decimal(value)
            if isinstance(value, (int, float, Decimal, str))
            else None
        )
        if parsed is not None and parsed > 0:
            return parsed
    return None


def _positive_decimal(value: object, *, label: str) -> Decimal:
    if not isinstance(value, (int, float, Decimal, str)):
        raise QuantityResolutionError(f"{label} must be numeric")
    parsed = to_decimal(value)
    if parsed <= 0:
        raise QuantityResolutionError(f"{label} must be positive")
    return parsed


def _percentage_decimal(value: object, *, label: str) -> Decimal:
    parsed = _positive_decimal(value, label=label)
    if parsed >= 100:
        raise QuantityResolutionError(f"{label} must be below 100")
    return parsed
