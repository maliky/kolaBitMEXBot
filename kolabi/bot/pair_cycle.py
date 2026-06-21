"""Pure pair-cycle reducer for the active bot runtime.

Purpose: evaluate one head/tail pair lifecycle through typed reducer moves and
emit ordered command intents without side effects.
Inputs: `PairCycleState` and one typed `EggMove`.
Outputs: next immutable `PairCycleState` and ordered `PairIntent` values.
Side effects: none.
Important types: `PairCycleState`, `EggMove`, `PairIntent`.
Role: pure logic.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

from kolabi.bot.domain import (
    ConfirmedOrder,
    EggMove,
    EggMoveKind,
    HeadState,
    OrderIdentity,
    OrderPairSpec,
    OrderReason,
    OrderRole,
    PairCycleState,
    PairIntent,
    PairIntentKind,
    Side,
    TailMode,
    TailState,
    classify_confirmed_move,
)
from kolabi.bot.dragon import reason_from_status_or_reason
from kolabi.bot.order_codes import parse_order_code
from kolabi.bot.tail_tracking import initial_tail_trail, step_tail_trail
from kolabi.shared.core.runtime_types import to_decimal


def step_pair(
    state: PairCycleState,
    move: EggMove,
) -> tuple[PairCycleState, tuple[PairIntent, ...]]:
    """Pure reducer for one pair transition.

    A reducer maps current immutable state plus one typed move to next immutable
    state and emitted command intents, with no side effects.
    """
    if move.kind == EggMoveKind.TAIL_SUBMITTED:
        if state.tail_state in {None, TailState.LATENT, TailState.FAILED, TailState.CLOSED}:
            return state, ()
        next_state = replace(
            state,
            tail_state=TailState.SUBMITTED,
            tail_identity=tail_identity_from_move(state, move),
            tail_trail=tail_trail_confirmed_from_move(state, move),
        )
        return next_state, ()

    if move.kind == EggMoveKind.TAIL_AMENDED:
        if state.tail_trail is None or state.tail_state in {None, TailState.CLOSED, TailState.FAILED}:
            return state, ()
        next_state = replace(
            state,
            tail_identity=tail_identity_from_move(state, move),
            tail_trail=tail_trail_confirmed_from_move(state, move),
        )
        return next_state, ()

    if move.kind == EggMoveKind.TAIL_AMEND_REJECTED:
        if state.tail_trail is None:
            return state, ()
        confirmed_stop = state.tail_trail.confirmed_stop_price
        if confirmed_stop is None:
            return state, ()
        return replace(
            state,
            tail_trail=replace(
                state.tail_trail,
                current_stop_price=confirmed_stop,
                previous_stop_price=confirmed_stop,
            ),
        ), ()

    if (
        state.head_state in {HeadState.FAILED, HeadState.CLOSED}
        and move.kind != EggMoveKind.MARKET_TICK
        and move.role != OrderRole.TAIL
    ):
        return state, ()

    if move.kind == EggMoveKind.HEAD_TRIGGER_BASELINED:
        if state.head_state != HeadState.LATENT:
            return state, ()
        reference_price = reference_price_from_move(state, move)
        if reference_price is None or reference_price <= 0:
            return state, ()
        reply = move.reply or {}
        source = reply.get("reference_source")
        return replace(
            state_with_market_metadata(state, move),
            head_trigger_reference_price=reference_price,
            head_trigger_reference_source=(
                str(source) if isinstance(source, str) and source else None
            ),
            head_trigger_reference_at=move.occurred_at,
        ), ()

    if move.kind == EggMoveKind.HEAD_HOOKED:
        if state.head_state != HeadState.LATENT:
            return state, ()
        next_state = replace(
            state_with_market_metadata(state, move),
            head_state=HeadState.HOOKED,
            head_trigger_reference_at=move.occurred_at,
            head_order_price=_decimal_from_move(move, "head_order_price"),
            head_order_stop_price=_decimal_from_move(move, "head_order_stop_price"),
        )
        return next_state, (PairIntent(PairIntentKind.PLACE_HEAD),)

    if move.kind == EggMoveKind.HEAD_SUBMITTED:
        next_state = replace(
            state,
            head_state=HeadState.SUBMITTED,
            head_identity=head_identity_from_move(state, move),
            head_order_quantity=order_quantity_from_move(state, move),
        )
        return next_state, ()

    if _is_zero_fill_post_only_head_reject(
        state,
        move,
    ) and not _is_zero_fill_post_only_probe_head_reject(state):
        move = replace(move, kind=EggMoveKind.NOT_PLAYED_CANCELED)

    if move.kind == EggMoveKind.MARKET_TICK:
        if state.tail_trail is None or state.tail_state in {
            None,
            TailState.LATENT,
            TailState.HOOKED,
            TailState.CLOSED,
            TailState.FAILED,
        }:
            return state, ()
        reference_price = reference_price_from_move(state, move)
        if reference_price is None:
            return state, ()
        next_trail = step_tail_trail(
            state.pair,
            state.tail_trail,
            reference_price,
            move.occurred_at,
            tick_size=tick_size_from_move(move) or state.instrument_tick_size,
            spread=spread_from_move(move),
            symbol=move.symbol,
        )
        next_state = replace(state_with_market_metadata(state, move), tail_trail=next_trail)
        if (
            next_trail.current_stop_price != state.tail_trail.current_stop_price
            and _has_full_tail_identity(next_state)
            and _has_confirmed_tail_stop(next_state)
        ):
            return next_state, (PairIntent(PairIntentKind.AMEND_TAIL),)
        return next_state, ()

    if move.kind == EggMoveKind.NOT_PLAYED_NOR_CANCELED:
        if move.role == OrderRole.TAIL:
            next_state = replace(
                state,
                # Private open-order confirmation means the tail is now live.
                tail_state=TailState.LIVING,
                tail_identity=tail_identity_from_move(state, move),
                tail_trail=tail_trail_confirmed_from_move(state, move),
            )
            return next_state, ()
        next_state = replace(
            state,
            head_state=HeadState.NEW,
            head_identity=head_identity_from_move(state, move),
            head_order_quantity=order_quantity_from_move(state, move),
            played_quantity=played_quantity_from_move(state, move),
        )
        return next_state, ()

    if move.kind == EggMoveKind.NOT_PLAYED_CANCELED:
        if move.role == OrderRole.TAIL:
            next_state = replace(
                state,
                tail_state=TailState.FAILED,
                tail_identity=tail_identity_from_move(state, move),
                tail_trail=tail_trail_confirmed_from_move(state, move),
                completed_at=move.occurred_at,
            )
            return next_state, ()
        next_state = replace(
            state,
            head_state=HeadState.FAILED,
            head_identity=head_identity_from_move(state, move),
            tail_state=TailState.LATENT,
            tail_mode=None,
            head_order_quantity=order_quantity_from_move(state, move),
            played_quantity=played_quantity_from_move(state, move),
            completed_at=move.occurred_at,
        )
        return next_state, ()

    if move.kind == EggMoveKind.PLAYED_NOT_CANCELED:
        if move.role == OrderRole.TAIL:
            next_state = replace(
                state,
                tail_state=TailState.LIVING,
                tail_identity=tail_identity_from_move(state, move),
                tail_trail=tail_trail_confirmed_from_move(state, move),
            )
            return next_state, ()
        played_quantity = played_quantity_from_move(state, move)
        if (
            state.head_state == HeadState.LIVING
            and state.played_quantity == played_quantity
            and state.tail_state in {TailState.SUBMITTED, TailState.LIVING}
        ):
            return state, ()
        next_state = replace(
            state,
            head_state=HeadState.LIVING,
            head_identity=head_identity_from_move(state, move),
            tail_state=TailState.LIVING,
            tail_mode=TailMode.FLAPPING,
            tail_trail=tail_trail_from_move(state, move),
            head_order_quantity=order_quantity_from_move(state, move),
            played_quantity=played_quantity,
        )
        return next_state, (_tail_intent_for_state(next_state),)

    if move.kind == EggMoveKind.PLAYED_AND_CANCELED:
        if move.role == OrderRole.TAIL:
            next_state = replace(
                state,
                tail_state=TailState.CLOSED,
                tail_mode=TailMode.FLYING,
                tail_identity=tail_identity_from_move(state, move),
                tail_trail=tail_trail_confirmed_from_move(state, move),
                completed_at=move.occurred_at,
            )
            return next_state, ()
        played_quantity = played_quantity_from_move(state, move)
        if (
            state.head_state == HeadState.CLOSED
            and state.played_quantity == played_quantity
            and state.tail_mode == TailMode.FLYING
        ):
            return state, ()
        next_state = replace(
            state,
            head_state=HeadState.CLOSED,
            head_identity=head_identity_from_move(state, move),
            tail_state=_next_closed_tail_state(state),
            tail_mode=TailMode.FLYING,
            tail_trail=tail_trail_from_move(state, move),
            head_order_quantity=order_quantity_from_move(state, move),
            played_quantity=played_quantity,
        )
        return next_state, (_tail_intent_for_state(next_state),)

    return state, ()


def _tail_intent_for_state(state: PairCycleState) -> PairIntent:
    if _has_full_tail_identity(state):
        return PairIntent(PairIntentKind.AMEND_TAIL)
    return PairIntent(PairIntentKind.PLACE_TAIL)


def _has_full_tail_identity(state: PairCycleState) -> bool:
    return (
        state.tail_identity is not None
        and bool(state.tail_identity.client_order_id)
        and bool(state.tail_identity.exchange_order_id)
    )


def _has_confirmed_tail_stop(state: PairCycleState) -> bool:
    return (
        state.tail_trail is not None
        and state.tail_trail.confirmed_stop_price is not None
    )


def _next_closed_tail_state(state: PairCycleState) -> TailState:
    """Choisit l'etat du tail ferme selon son activation precedente."""
    if state.tail_state in {TailState.SUBMITTED, TailState.LIVING}:
        return TailState.LIVING
    return TailState.HOOKED


def resolve_quantity(pair: OrderPairSpec) -> float:
    quantity = pair.head_quantity
    if quantity is None or quantity <= 0:
        raise ValueError(f"Order pair '{pair.name}' needs a positive head quantity")
    return float(to_decimal(quantity))


def head_identity_from_move(
    state: PairCycleState,
    move: EggMove,
) -> OrderIdentity | None:
    reply = move.reply or {}
    order = move.order or {}
    order_id = reply.get("orderID")
    client_order_id = reply.get("clOrdID") or order.get("clOrdID")
    if order_id is None and client_order_id is None:
        return state.head_identity
    return OrderIdentity(
        pair_name=state.pair.name,
        role="head",
        client_order_id=str(client_order_id) if client_order_id is not None else None,
        exchange_order_id=str(order_id) if order_id is not None else None,
        symbol=move.symbol or state.pair.symbol,
        exchange=state.pair.exchange,
        market_type=state.pair.market_type,
    )


def tail_identity_from_move(
    state: PairCycleState,
    move: EggMove,
) -> OrderIdentity | None:
    reply = move.reply or {}
    order = move.order or {}
    order_id = reply.get("orderID")
    client_order_id = reply.get("clOrdID") or order.get("clOrdID")
    if order_id is None and client_order_id is None:
        return state.tail_identity
    return OrderIdentity(
        pair_name=state.pair.name,
        role="tail",
        client_order_id=str(client_order_id) if client_order_id is not None else None,
        exchange_order_id=str(order_id) if order_id is not None else None,
        symbol=move.symbol or state.pair.symbol,
        exchange=state.pair.exchange,
        market_type=state.pair.market_type,
    )


def played_quantity_from_move(state: PairCycleState, move: EggMove) -> Decimal | None:
    """Read played quantity from move payload and return unknown when missing."""
    reply = move.reply or {}
    for key in ("cumQty", "executedQty", "filledQty", "filled_quantity"):
        value = reply.get(key)
        if isinstance(value, (int, float, Decimal, str)):
            parsed = to_decimal(value)
            return parsed if parsed >= Decimal("0") else Decimal("0")
    return None


def order_quantity_from_move(state: PairCycleState, move: EggMove) -> Decimal | None:
    """Read the exchange order quantity from a move, preserving existing truth."""
    reply = move.reply or {}
    for key in ("orderQty", "quantity", "qty"):
        value = reply.get(key)
        if isinstance(value, (int, float, Decimal, str)):
            parsed = to_decimal(value)
            return parsed if parsed > 0 else state.head_order_quantity
    return state.head_order_quantity


def _is_zero_fill_post_only_head_reject(
    state: PairCycleState,
    move: EggMove,
) -> bool:
    if move.role == OrderRole.TAIL:
        return False
    if move.kind not in {
        EggMoveKind.PLAYED_NOT_CANCELED,
        EggMoveKind.PLAYED_AND_CANCELED,
    }:
        return False
    reply = move.reply or {}
    raw_reason = reply.get("execType") or reply.get("reason")
    reason = reason_from_status_or_reason(
        str(reply.get("ordStatus") or reply.get("status") or ""),
        str(raw_reason) if raw_reason is not None else None,
    )
    if reason != OrderReason.POST_ONLY_WOULD_FILL:
        return False
    played_quantity = played_quantity_from_move(state, move)
    return played_quantity is None or played_quantity <= Decimal("0")


def _is_zero_fill_post_only_probe_head_reject(state: PairCycleState) -> bool:
    code = parse_order_code(state.pair.head.order_type)
    if code.base_key not in {"LT", "SL"} or not code.post_only:
        return False
    if (state.pair.head_order_price_spec_type or "").lower() != "hd":
        return False
    if state.pair.head_order_price_spec is None:
        return False
    return abs(to_decimal(state.pair.head_order_price_spec)) == Decimal("0")


def reference_price_from_move(state: PairCycleState, move: EggMove) -> Decimal | None:
    """Read the side-aware public reference price from a reducer move."""
    for payload in (move.reply, move.order):
        if payload is None:
            continue
        for key in ("reference_price", "refPrice", "markPrice", "price", "lastPx"):
            value = payload.get(key)
            if isinstance(value, (int, float, Decimal, str)):
                return to_decimal(value)
    if state.tail_trail is not None:
        return state.tail_trail.entry_reference_price
    return None


def _decimal_from_move(move: EggMove, key: str) -> Decimal | None:
    for payload in (move.reply, move.order):
        if payload is None:
            continue
        value = payload.get(key)
        if isinstance(value, (int, float, Decimal, str)):
            parsed = to_decimal(value)
            if parsed > 0:
                return parsed
    return None


def tick_size_from_move(move: EggMove) -> Decimal | None:
    for payload in (move.reply, move.order):
        if payload is None:
            continue
        value = payload.get("tick_size")
        if isinstance(value, (int, float, Decimal, str)):
            tick = to_decimal(value)
            if tick > 0:
                return tick
    return None


def spread_from_move(move: EggMove) -> Decimal | None:
    for payload in (move.reply, move.order):
        if payload is None:
            continue
        value = payload.get("spread")
        if isinstance(value, (int, float, Decimal, str)):
            spread = to_decimal(value)
            if spread >= 0:
                return spread
    return None


def state_with_market_metadata(state: PairCycleState, move: EggMove) -> PairCycleState:
    tick_size = tick_size_from_move(move)
    if tick_size is None:
        return state
    return replace(state, instrument_tick_size=tick_size)


def tail_trail_from_move(state: PairCycleState, move: EggMove):
    if state.tail_trail is not None:
        return state.tail_trail
    reference_price = reference_price_from_move(state, move)
    if reference_price is None:
        return None
    return initial_tail_trail(state.pair, reference_price, move.occurred_at)


def tail_trail_confirmed_from_move(state: PairCycleState, move: EggMove):
    trail = state.tail_trail
    if trail is None:
        return None
    stop_price = _stop_price_from_move(move)
    if stop_price is None:
        return trail
    current_stop_price = stop_price
    previous_stop_price = trail.current_stop_price
    if _should_preserve_tail_desired_stop(state, move, stop_price):
        current_stop_price = trail.current_stop_price
        previous_stop_price = trail.previous_stop_price
    return replace(
        trail,
        current_stop_price=current_stop_price,
        previous_stop_price=previous_stop_price,
        confirmed_stop_price=stop_price,
        last_confirmed_at=move.occurred_at,
    )


def _should_preserve_tail_desired_stop(
    state: PairCycleState,
    move: EggMove,
    confirmed_stop: Decimal,
) -> bool:
    trail = state.tail_trail
    if trail is None or trail.confirmed_stop_price is None:
        return False
    if move.kind != EggMoveKind.NOT_PLAYED_NOR_CANCELED or move.role != OrderRole.TAIL:
        return False
    if not _has_full_tail_identity(state):
        return False
    if state.tail_state not in {TailState.SUBMITTED, TailState.LIVING}:
        return False
    desired_stop = trail.current_stop_price
    if state.pair.tail.side == Side.SELL:
        return desired_stop > confirmed_stop
    return desired_stop < confirmed_stop


def _stop_price_from_move(move: EggMove) -> Decimal | None:
    for payload in (move.reply, move.order):
        if payload is None:
            continue
        for key in ("stopPx", "stop_price", "stopPrice"):
            value = payload.get(key)
            if isinstance(value, (int, float, Decimal, str)):
                return to_decimal(value)
        order_type = str(payload.get("ordType") or payload.get("order_type") or "").lower()
        if "stop" in order_type:
            price = payload.get("price")
            if isinstance(price, (int, float, Decimal, str)):
                return to_decimal(price)
    return None


def egg_move_from_confirmed_head(
    pair: OrderPairSpec,
    head: ConfirmedOrder,
    *,
    symbol: str,
) -> EggMove:
    return EggMove(
        kind=classify_confirmed_move(head),
        occurred_at=datetime.now(timezone.utc),
        symbol=symbol,
        reply={
            "orderID": head.identity.exchange_order_id or "",
            "clOrdID": head.identity.client_order_id or "",
            "ordStatus": head.state.value,
            "execType": head.reason.value,
            "cumQty": float(head.filled_quantity),
            "orderQty": float(head.total_quantity),
        },
    )


def reason_from_status(status: str) -> OrderReason:
    return reason_from_status_or_reason(status, None)
