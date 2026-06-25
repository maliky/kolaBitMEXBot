"""Pure order payload builders for the pair-cycle reducer.

Purpose: construct head/tail payloads and typed runtime commands from immutable
pair state without touching exchange clients.
Inputs: `OrderPairSpec`, `PairCycleState`, and reducer context.
Outputs: typed `OrderDict` payloads and `RuntimeCommand` values.
Side effects: none.
Important types: `PairCycleState`, `OrderDict`, `RuntimeCommand`.
Role: pure logic.
Transitional: yes, extracted from `pair_cycle.py` while legacy shells remain.
"""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import cast

from kolabi.bot.domain import OrderPairSpec, PairCycleState, opposite_side
from kolabi.bot.order_codes import base_order_type, order_exec_inst, parse_order_code
from kolabi.bot.quantity import pair_uses_materialized_quantity
from kolabi.shared.core.runtime_types import (
    AmendHeadCommand,
    AmendOrderCommandRequest,
    AmendTailCommand,
    LimitPrice,
    OrderDict,
    OrderQty,
    PlaceHeadCommand,
    PlaceOrderCommandRequest,
    PlaceTailCommand,
    PriceOffset,
    RuntimeCommandKind,
    StopPrice,
    Symbol,
    to_decimal,
)


def _tail_order_type(raw: str) -> str:
    """Return the base order type after legacy tail suffixes are removed."""
    return base_order_type(raw)


def _tail_exec_inst(raw: str) -> str | None:
    """Translate legacy tail suffixes into adapter execution instructions."""
    return order_exec_inst(raw, role="tail")


def _head_order_type(raw: str) -> str:
    """Return the base head type before exchange placement."""
    return base_order_type(raw)


def _head_exec_inst(raw: str) -> str | None:
    """Translate valid head suffixes into adapter execution instructions."""
    return order_exec_inst(raw, role="head")


def head_order_dict(pair: OrderPairSpec, *, client_order_id: str | None = None) -> OrderDict:
    """Build the head order payload from the static pair specification."""
    order: OrderDict = {
        "side": pair.head.side.value,
        "ordType": _head_order_type(pair.head.order_type),
        "pair_name": pair.name,
    }
    exec_inst = _head_exec_inst(pair.head.order_type)
    if exec_inst is not None:
        order["execInst"] = exec_inst
    if pair.head_quantity is not None and not pair_uses_materialized_quantity(pair):
        order["orderQty"] = cast(OrderQty, to_decimal(pair.head_quantity))
    if client_order_id is not None:
        order["clOrdID"] = client_order_id
    return order


def head_place_request(
    state: PairCycleState,
    *,
    client_order_id: str | None = None,
) -> PlaceOrderCommandRequest:
    pair = state.pair
    quantity = _head_order_quantity(state)
    return PlaceOrderCommandRequest(
        pair_name=pair.name,
        side=pair.head.side.value,
        ordType=_head_order_type(pair.head.order_type),
        orderQty=quantity,
        price=(
            None
            if state.head_order_price is None
            else cast(LimitPrice, state.head_order_price)
        ),
        stopPx=(
            None
            if state.head_order_stop_price is None
            else cast(StopPrice, state.head_order_stop_price)
        ),
        clOrdID=client_order_id,
        execInst=_head_exec_inst(pair.head.order_type),
        oDelta=_head_exchange_offset(pair),
    )


def _head_exchange_offset(pair: OrderPairSpec) -> PriceOffset | None:
    """Return only exchange-native nominal offsets for head placement."""
    if base_order_type(pair.head.order_type) not in {"SL", "LT"}:
        return None
    if pair.head.delta is None:
        return None
    if pair.head.delta_type.lower() == "ob":
        return None
    return _distance_offset(pair.head.delta)


def _tail_exchange_offset(state: PairCycleState) -> PriceOffset | None:
    """Return explicit tail offset or one tick for blank stop-limit deltas."""
    if state.pair.tail.delta is not None:
        return _distance_offset(state.pair.tail.delta)
    if base_order_type(state.pair.tail.order_type) not in {"SL", "LT"}:
        return None
    tick = state.instrument_tick_size
    if tick is None or tick <= 0:
        raise ValueError(
            f"Order pair '{state.pair.name}' needs an instrument tick size to materialise "
            "blank tail tDelta"
        )
    return cast(PriceOffset, tick)


def _distance_offset(value: Decimal | float | int | str) -> PriceOffset:
    return cast(PriceOffset, abs(to_decimal(value)))


def _head_order_quantity(state: PairCycleState) -> OrderQty | None:
    if state.head_order_quantity is not None:
        return cast(OrderQty, state.head_order_quantity)
    if state.pair.head_quantity is None or pair_uses_materialized_quantity(state.pair):
        return None
    return cast(OrderQty, to_decimal(state.pair.head_quantity))


def resolve_tail_quantity(state: PairCycleState) -> Decimal | int | float | None:
    """Resolve tail quantity from played runtime state first, then planned size."""
    if state.played_quantity is not None and state.played_quantity > 0:
        return state.played_quantity
    if state.head_order_quantity is not None and state.head_order_quantity > 0:
        return state.head_order_quantity
    if pair_uses_materialized_quantity(state.pair):
        return None
    return state.pair.head_quantity


def tail_order_dict(state: PairCycleState) -> OrderDict:
    """Build a tail payload using runtime played quantity when available."""
    pair = state.pair
    order: OrderDict = {
        "side": opposite_side(pair.head.side).value,
        "ordType": _tail_order_type(pair.tail.order_type),
        "pair_name": pair.name,
    }
    exec_inst = _tail_exec_inst(pair.tail.order_type)
    if exec_inst is not None:
        order["execInst"] = exec_inst
    quantity = resolve_tail_quantity(state)
    if quantity is not None:
        order["orderQty"] = cast(OrderQty, to_decimal(quantity))
    stop_price = tail_trigger_price(state)
    if stop_price is not None:
        order["stopPx"] = cast(StopPrice, to_decimal(stop_price))
    limit_price = tail_limit_price(state)
    if limit_price is not None:
        order["price"] = cast(LimitPrice, to_decimal(limit_price))
    tail_offset = _tail_exchange_offset(state)
    if tail_offset is not None:
        order["oDelta"] = tail_offset
    return order


def tail_place_request(state: PairCycleState) -> PlaceOrderCommandRequest:
    quantity = resolve_tail_quantity(state)
    stop_price = tail_trigger_price(state)
    return PlaceOrderCommandRequest(
        pair_name=state.pair.name,
        side=opposite_side(state.pair.head.side).value,
        ordType=_tail_order_type(state.pair.tail.order_type),
        orderQty=None if quantity is None else cast(OrderQty, to_decimal(quantity)),
        price=(
            None
            if (limit_price := tail_limit_price(state)) is None
            else cast(LimitPrice, to_decimal(limit_price))
        ),
        stopPx=None if stop_price is None else cast(StopPrice, to_decimal(stop_price)),
        execInst=_tail_exec_inst(state.pair.tail.order_type),
        oDelta=_tail_exchange_offset(state),
    )


def tail_amend_order_dict(state: PairCycleState) -> OrderDict:
    """Build an amend payload from runtime tail identity and target price."""
    if state.tail_identity is None:
        raise ValueError("tail amend requires an existing tail identity")
    if not state.tail_identity.client_order_id or not state.tail_identity.exchange_order_id:
        raise ValueError("tail amend requires both client and exchange order IDs")
    stop_price = tail_stop_price(state)
    if stop_price is None:
        raise ValueError("tail amend requires a planned tail price")

    order = tail_order_dict(state)
    order["clOrdID"] = state.tail_identity.client_order_id
    order["orderID"] = state.tail_identity.exchange_order_id
    order["newPrice"] = cast(StopPrice, to_decimal(stop_price))
    quantity = resolve_tail_quantity(state)
    if quantity is not None:
        order["newQty"] = cast(OrderQty, to_decimal(quantity))
    return order


def tail_amend_request(state: PairCycleState) -> AmendOrderCommandRequest:
    if state.tail_identity is None:
        raise ValueError("tail amend requires an existing tail identity")
    if not state.tail_identity.client_order_id or not state.tail_identity.exchange_order_id:
        raise ValueError("tail amend requires both client and exchange order IDs")
    stop_price = tail_stop_price(state)
    if stop_price is None:
        raise ValueError("tail amend requires a planned tail price")
    quantity = resolve_tail_quantity(state)
    return AmendOrderCommandRequest(
        pair_name=state.pair.name,
        side=opposite_side(state.pair.head.side).value,
        ordType=_tail_order_type(state.pair.tail.order_type),
        orderID=state.tail_identity.exchange_order_id,
        clOrdID=state.tail_identity.client_order_id,
        newPrice=cast(StopPrice, to_decimal(stop_price)),
        newQty=None if quantity is None else cast(OrderQty, to_decimal(quantity)),
        oDelta=_tail_exchange_offset(state),
    )


def head_amend_request(state: PairCycleState) -> AmendOrderCommandRequest:
    if state.head_identity is None:
        raise ValueError("head amend requires an existing head identity")
    if not state.head_identity.exchange_order_id:
        raise ValueError("head amend requires an exchange order ID")
    return AmendOrderCommandRequest(
        pair_name=state.pair.name,
        side=state.pair.head.side.value,
        ordType=_head_order_type(state.pair.head.order_type),
        orderID=state.head_identity.exchange_order_id,
        clOrdID=state.head_identity.client_order_id,
        text="head amend",
    )


def head_amend_order_dict(state: PairCycleState) -> OrderDict:
    request = head_amend_request(state)
    order: OrderDict = {
        "pair_name": request.pair_name,
        "ordType": request.ordType,
        "side": request.side,
        "orderID": request.orderID,
    }
    if request.clOrdID is not None:
        order["clOrdID"] = request.clOrdID
    if request.text is not None:
        order["text"] = request.text
    return order


def head_command(
    state: PairCycleState,
    *,
    symbol: Symbol,
    kind: RuntimeCommandKind,
) -> PlaceHeadCommand | AmendHeadCommand:
    if kind == RuntimeCommandKind.AMEND:
        request = head_amend_request(state)
        return AmendHeadCommand(
            kind=kind,
            symbol=symbol,
            request=request,
            pair_name=state.pair.name,
            legacy_order=head_amend_order_dict(state),
            exchange=state.pair.exchange or "",
            market_type=state.pair.market_type or "futures",
        )
    return PlaceHeadCommand(
        kind=kind,
        symbol=symbol,
        request=head_place_request(state),
        pair_name=state.pair.name,
        legacy_order=head_order_dict(state.pair),
        exchange=state.pair.exchange or "",
        market_type=state.pair.market_type or "futures",
    )


def tail_command(
    state: PairCycleState,
    *,
    symbol: Symbol,
    kind: RuntimeCommandKind,
) -> PlaceTailCommand | AmendTailCommand:
    """Build a tail command for placement or amendment from runtime state."""
    request: PlaceOrderCommandRequest | AmendOrderCommandRequest
    if kind == RuntimeCommandKind.AMEND:
        request = tail_amend_request(state)
        order = tail_amend_order_dict(state)
    else:
        request = tail_place_request(state)
        order = tail_order_dict(state)
    if kind == RuntimeCommandKind.AMEND:
        return AmendTailCommand(
            kind=kind,
            symbol=symbol,
            request=cast(AmendOrderCommandRequest, request),
            pair_name=state.pair.name,
            legacy_order=order,
            exchange=state.pair.exchange or "",
            market_type=state.pair.market_type or "futures",
        )
    return PlaceTailCommand(
        kind=kind,
        symbol=symbol,
        request=cast(PlaceOrderCommandRequest, request),
        pair_name=state.pair.name,
        legacy_order=order,
        exchange=state.pair.exchange or "",
        market_type=state.pair.market_type or "futures",
    )


def with_played_quantity(state: PairCycleState, quantity: Decimal) -> PairCycleState:
    """Return updated pair state with a normalized played quantity."""
    return replace(state, played_quantity=max(quantity, Decimal("0")))


def tail_stop_price(state: PairCycleState) -> Decimal | float | None:
    """Resolve dynamic trail stop first, then the static transitional value."""
    if state.tail_trail is not None:
        return state.tail_trail.current_stop_price
    tail_type = (state.pair.tail_price_spec_type or "").lower()
    amount_type = state.pair.amount_type.lower()
    if "tb" in tail_type or "tb" in amount_type or "td" in tail_type or "td" in amount_type:
        raise ValueError(
            f"Order pair '{state.pair.name}' needs an initialised tail trail "
            "before placing or amending a relative tail"
        )
    return state.pair.tail_price_spec


def tail_trigger_price(state: PairCycleState) -> Decimal | float | None:
    """Resolve stopPx only for trigger-family tails."""

    if parse_order_code(state.pair.tail.order_type).base_key not in {
        "S",
        "SL",
        "MT",
        "LT",
    }:
        return None
    return tail_stop_price(state)


def tail_limit_price(state: PairCycleState) -> Decimal | float | None:
    """Resolve the concrete price for plain limit tails.

    A post-only zero-distance limit tail is an explicit terminal-cancel
    convention: materialise it one tick beyond the head fill/reference so the
    exchange should reject/cancel it as taker, while preserving post-only.
    """

    code = parse_order_code(state.pair.tail.order_type)
    if code.base_key != "L":
        return None
    price = tail_stop_price(state)
    if price is None:
        raise ValueError(f"Order pair '{state.pair.name}' limit tail needs a price")
    resolved = to_decimal(price)
    if not (code.post_only and _is_zero_relative_tail(state.pair)):
        return resolved
    tick = state.instrument_tick_size
    if tick is None or tick <= 0:
        raise ValueError(
            f"Order pair '{state.pair.name}' needs an instrument tick size to materialise "
            "post-only zero-distance tail"
        )
    if opposite_side(state.pair.head.side).value == "buy":
        return resolved + tick
    return resolved - tick


def _is_zero_relative_tail(pair: OrderPairSpec) -> bool:
    spec = pair.tail_price_spec
    if spec is None or to_decimal(spec) != Decimal("0"):
        return False
    tail_type = (pair.tail_price_spec_type or "").lower()
    amount_type = pair.amount_type.lower()
    return "tb" in tail_type or "tb" in amount_type or "td" in tail_type or "td" in amount_type
