"""Pure pricing and time-window helpers for pair-cycle decisions.

Purpose: compute head prices from market snapshots and evaluate absolute pair
activation windows from strategy launch time.
Inputs: `OrderPairSpec`, `PublicMarketState`, strategy launch/current times.
Outputs: deterministic activation booleans and optional head limit prices.
Side effects: none.
Important types: `OrderPairSpec`, `Side`, `PublicMarketState`.
Role: pure logic.
Transitional: yes, extracted from `pair_cycle.py` while old runtime layers remain.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Protocol

from kolabi.bot.domain import OrderPairSpec, PairCycleState, Side
from kolabi.bot.order_codes import order_price_source, parse_order_code
from kolabi.bot.price_units import logbps_price_distance, signed_logbps_move
from kolabi.shared.core.runtime_types import decimal_to_float, to_decimal


class MarketLike(Protocol):
    @property
    def best_bid(self) -> float | None: ...

    @property
    def best_ask(self) -> float | None: ...

    @property
    def mid_price(self) -> float | None: ...
    @property
    def last_price(self) -> float | None: ...
    @property
    def mark_price(self) -> float | None: ...
    @property
    def index_price(self) -> float | None: ...


def pair_window_is_open(
    pair: OrderPairSpec,
    *,
    launched_at: datetime,
    now: datetime,
) -> bool:
    """Return true when current time is inside the pair launch-relative window."""
    elapsed_minutes = _elapsed_minutes(launched_at=launched_at, now=now)
    return pair.window.start_minutes <= elapsed_minutes <= pair.window.end_minutes


def pair_window_has_ended(
    pair: OrderPairSpec,
    *,
    launched_at: datetime,
    now: datetime,
) -> bool:
    """Return true after the pair launch-relative window has ended."""
    elapsed_minutes = _elapsed_minutes(launched_at=launched_at, now=now)
    return elapsed_minutes > pair.window.end_minutes


def _elapsed_minutes(*, launched_at: datetime, now: datetime) -> float:
    return (_as_utc_aware(now) - _as_utc_aware(launched_at)).total_seconds() / 60.0


def _as_utc_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def resolve_head_price(pair: OrderPairSpec, market: MarketLike) -> float | None:
    """Resolve the head order price from pair configuration and live market state."""
    price, _ = resolve_head_order_prices(pair, market)
    return price


def resolve_head_order_prices(
    pair: OrderPairSpec,
    market: MarketLike,
    *,
    gate_reference_price: Decimal | int | float | str | None = None,
) -> tuple[float | None, float | None]:
    """Resolve concrete price/stopPx values for a non-market head order.

    The `pGate` interval is the hook condition. It is not a limit price. Once
    the hook fires, ``hPrice`` is materialised lazily against the gate-opening
    reference.  ``hDelta`` is only a trigger-to-limit offset for SL/LT heads.
    """
    code = parse_order_code(pair.head.order_type)
    if code.base_key == "M":
        return None, None
    reference = (
        to_decimal(gate_reference_price)
        if gate_reference_price is not None
        else to_decimal(executable_head_reference_price(pair, market)[1])
    )
    if reference <= 0:
        return None, None
    if code.base_key == "L":
        price = _plain_limit_head_price(pair, reference, market)
        if code.post_only:
            price = _post_only_passive_limit_head_price(pair, price, market)
        return decimal_to_float(price), None
    if code.base_key == "S":
        return None, decimal_to_float(_stop_head_price(pair, reference, market))
    if code.base_key in {"SL", "LT"}:
        stop_price = _touch_or_stop_head_price(pair, reference, market)
        price = _trigger_limit_head_price(pair, stop_price)
        return (
            None if price is None else decimal_to_float(price),
            decimal_to_float(stop_price),
        )
    if code.base_key == "MT":
        return None, decimal_to_float(_touch_head_price(pair, reference, market))
    return None, None


def reference_price(side: Side, market: MarketLike) -> float:
    """Return the reference side-aware market price used for relative pricing."""
    if side == Side.BUY:
        return market.best_bid or market.mid_price or 0.0
    return market.best_ask or market.mid_price or 0.0


def _plain_limit_head_price(
    pair: OrderPairSpec,
    reference: Decimal,
    market: MarketLike,
) -> Decimal:
    value = _head_order_price_value(pair, reference, market)
    if value.absolute is not None:
        return value.absolute
    if pair.head.side == Side.BUY:
        return reference - value.distance
    return reference + value.distance


def _post_only_passive_limit_head_price(
    pair: OrderPairSpec,
    price: Decimal,
    market: MarketLike,
) -> Decimal:
    if pair.head.side == Side.BUY:
        passive_ceiling = _positive_decimal(market.best_bid)
        if passive_ceiling is None:
            passive_ceiling = _one_tick_inside(
                _positive_decimal(market.best_ask),
                -1,
                market,
            )
        if passive_ceiling is None:
            return price
        return min(price, passive_ceiling)

    passive_floor = _positive_decimal(market.best_ask)
    if passive_floor is None:
        passive_floor = _one_tick_inside(
            _positive_decimal(market.best_bid),
            1,
            market,
        )
    if passive_floor is None:
        return price
    return max(price, passive_floor)


def _one_tick_inside(
    reference: Decimal | None,
    direction: int,
    market: MarketLike,
) -> Decimal | None:
    tick = _tick_size_from_market(market)
    if reference is None or tick is None:
        return None
    value = reference + (tick if direction > 0 else -tick)
    if value <= 0:
        return None
    return value


def _positive_decimal(value: Decimal | int | float | str | None) -> Decimal | None:
    if value is None:
        return None
    parsed = to_decimal(value)
    if parsed <= 0:
        return None
    return parsed


def _stop_head_price(
    pair: OrderPairSpec,
    reference: Decimal,
    market: MarketLike,
) -> Decimal:
    value = _head_order_price_value(pair, reference, market)
    if value.absolute is not None:
        return value.absolute
    if pair.head.side == Side.BUY:
        return reference + value.distance
    return reference - value.distance


def _touch_head_price(
    pair: OrderPairSpec,
    reference: Decimal,
    market: MarketLike,
) -> Decimal:
    value = _head_order_price_value(pair, reference, market)
    if value.absolute is not None:
        return value.absolute
    if pair.head.side == Side.BUY:
        return reference - value.distance
    return reference + value.distance


def _touch_or_stop_head_price(
    pair: OrderPairSpec,
    reference: Decimal,
    market: MarketLike,
) -> Decimal:
    code = parse_order_code(pair.head.order_type)
    if code.base_key == "LT":
        return _touch_head_price(pair, reference, market)
    return _stop_head_price(pair, reference, market)


def _trigger_limit_head_price(
    pair: OrderPairSpec,
    stop_price: Decimal,
) -> Decimal | None:
    distance = _head_limit_offset_distance(pair, stop_price)
    if pair.head.side == Side.BUY:
        return stop_price + distance
    return stop_price - distance


class _HeadOrderPriceValue:
    def __init__(self, *, distance: Decimal, absolute: Decimal | None = None) -> None:
        self.distance = distance
        self.absolute = absolute


def _head_order_price_value(
    pair: OrderPairSpec,
    reference: Decimal,
    market: MarketLike,
) -> _HeadOrderPriceValue:
    if pair.head_order_price_spec is None:
        tick = _tick_size_from_market(market)
        if tick is None:
            raise ValueError(
                f"Order pair '{pair.name}' needs an instrument tick size to materialise "
                "blank hPrice"
            )
        return _HeadOrderPriceValue(distance=tick)
    value = to_decimal(pair.head_order_price_spec)
    price_type = pair.head_order_price_spec_type.lower()
    if price_type == "ha":
        return _HeadOrderPriceValue(distance=Decimal("0"), absolute=value)
    distance = abs(value)
    if price_type == "hb":
        distance = logbps_price_distance(reference, distance)
    return _HeadOrderPriceValue(distance=distance)


def _head_limit_offset_distance(pair: OrderPairSpec, reference: Decimal) -> Decimal:
    if pair.head.delta is None:
        return Decimal("0")
    delta = abs(to_decimal(pair.head.delta or 0))
    if pair.head.delta_type.lower() == "ob":
        return logbps_price_distance(reference, delta)
    return delta


def _tick_size_from_market(market: MarketLike) -> Decimal | None:
    value = getattr(market_as_any(market), "tick_size", None)
    if value is None:
        return None
    tick = to_decimal(value)
    if tick <= 0:
        return None
    return tick


def executable_head_reference_price(
    pair: OrderPairSpec,
    market: MarketLike,
) -> tuple[str, float]:
    """Return the executable public reference for head placement conditions."""
    source = _head_reference_source(pair, market)
    return source, price_from_source(source, market)


def head_price_reference_price(
    pair: OrderPairSpec,
    market: MarketLike,
) -> tuple[str, float]:
    """Return the public reference used to materialise a non-market head price."""
    source = _head_reference_source(pair, market)
    return source, price_from_source(source, market)


def _head_reference_source(pair: OrderPairSpec, market: MarketLike) -> str:
    explicit_source = order_price_source(pair.head.order_type)
    if explicit_source is not None:
        return explicit_source
    market_any = market_as_any(market)
    if _is_positive_price(getattr(market_any, "last_price", None)):
        return "last"
    if _is_positive_price(getattr(market_any, "mark_price", None)):
        return "mark"
    if pair.head.side == Side.BUY:
        return "ask"
    return "bid"


def _is_positive_price(value: Any) -> bool:
    return value is not None and value > 0


def head_price_condition_satisfied(
    pair_state: PairCycleState,
    reference_price: Decimal | int | float | str,
) -> bool:
    """Evaluate the head pGate interval against current and baseline reference."""
    pair = pair_state.pair
    current = to_decimal(reference_price)
    low, high = (to_decimal(pair.head_price[0]), to_decimal(pair.head_price[1]))
    price_type = (pair.head_price_type or "").lower()
    amount_type = pair.amount_type.lower()
    if "pa" in price_type or "pa" in amount_type:
        return low <= current <= high

    baseline = pair_state.head_trigger_reference_price
    if baseline is None or baseline <= 0:
        return False
    if current <= 0:
        return False
    if "pb" in price_type or "pb" in amount_type:
        value = signed_logbps_move(current, baseline)
    else:
        value = current - baseline
    return low <= value <= high


def head_price_condition_needs_baseline(pair: OrderPairSpec) -> bool:
    price_type = (pair.head_price_type or "").lower()
    amount_type = pair.amount_type.lower()
    return not ("pa" in price_type or "pa" in amount_type)


def tail_trigger_source(order_type: str) -> str:
    """Resolve abstract trigger source from legacy tail order suffixes."""
    code = parse_order_code(order_type)
    if code.base_key in {"S", "SL", "MT", "LT"}:
        return order_price_source(order_type, default="last") or "last"
    return "book"


def tail_reference_price(
    pair: OrderPairSpec,
    market: MarketLike,
) -> tuple[str, float]:
    """Return (source, reference) aligned with tail trigger semantics."""
    source = tail_trigger_source(pair.tail.order_type)
    market_any = market_as_any(market)
    if source == "last":
        return source, _price_or_fallback(
            getattr(market_any, "last_price", None), market.mid_price
        )
    if source == "mark":
        return source, _price_or_fallback(
            getattr(market_any, "mark_price", None), market.mid_price
        )
    if source == "index":
        return source, _price_or_fallback(
            getattr(market_any, "index_price", None), market.mid_price
        )
    return "book", reference_price(pair.head.side, market)


def _price_or_fallback(primary: float | None, fallback: float | None) -> float:
    if primary is not None and primary > 0:
        return primary
    if fallback is not None and fallback > 0:
        return fallback
    return 0.0


def price_from_source(source: str, market: MarketLike) -> float:
    market_any = market_as_any(market)
    if source == "last":
        return _price_or_fallback(getattr(market_any, "last_price", None), market.mid_price)
    if source == "mark":
        return _price_or_fallback(getattr(market_any, "mark_price", None), market.mid_price)
    if source == "index":
        return _price_or_fallback(getattr(market_any, "index_price", None), market.mid_price)
    if source == "ask":
        return _price_or_fallback(market.best_ask, market.mid_price)
    if source == "bid":
        return _price_or_fallback(market.best_bid, market.mid_price)
    return reference_price(Side.BUY, market)


def market_as_any(market: MarketLike) -> Any:
    return market
