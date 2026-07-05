"""Pure repeat-adjustment policies for successive pair attempts."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from kolabi.bot.domain import (
    EggMove,
    EggMoveKind,
    OrderPairSpec,
    OrderRole,
    PairCycleState,
    TailState,
)
from kolabi.shared.core.runtime_types import Side


class RepeatAdjustmentError(ValueError):
    """Raised when an rFunc policy is unknown or returns an unsafe pair."""


class RepeatTermination(StrEnum):
    LATENT_TIMEOUT = "latent_timeout"
    UNFILLED_CANCEL = "unfilled_cancel"
    SUCCESSFUL_TAIL_CLOSE = "successful_tail_close"
    FAILED_TAIL = "failed_tail"
    GENERIC_TERMINAL = "generic_terminal"


@dataclass(frozen=True)
class RepeatAdjustmentContext:
    previous_state: PairCycleState
    terminal_event: EggMove
    next_attempt: int
    termination: RepeatTermination
    roi: Decimal | None = None


RepeatAdjustment = Callable[[OrderPairSpec, RepeatAdjustmentContext], OrderPairSpec]
_REPEAT_ADJUSTMENTS: dict[str, RepeatAdjustment] = {}


def register_repeat_adjustment(name: str, func: RepeatAdjustment) -> RepeatAdjustment:
    key = _normalise_name(name)
    _REPEAT_ADJUSTMENTS[key] = func
    return func


def resolve_repeat_adjustment(name: str | None) -> RepeatAdjustment | None:
    if name is None or not name.strip():
        return None
    key = _normalise_name(name)
    try:
        return _REPEAT_ADJUSTMENTS[key]
    except KeyError as exc:
        raise RepeatAdjustmentError(
            f"Unknown repeat adjustment function '{name}'."
        ) from exc


def apply_repeat_adjustment(
    previous_state: PairCycleState,
    terminal_event: EggMove,
    *,
    next_attempt: int,
) -> OrderPairSpec:
    pair = previous_state.pair
    func = resolve_repeat_adjustment(pair.repeat_adjustment)
    if func is None:
        return pair

    context = RepeatAdjustmentContext(
        previous_state=previous_state,
        terminal_event=terminal_event,
        next_attempt=next_attempt,
        termination=classify_repeat_termination(previous_state, terminal_event),
        roi=_best_effort_roi(previous_state, terminal_event),
    )
    adjusted = func(pair, context)
    if not isinstance(adjusted, OrderPairSpec):
        raise RepeatAdjustmentError(
            f"Repeat adjustment '{pair.repeat_adjustment}' did not return an OrderPairSpec."
        )
    _validate_adjusted_pair(pair, adjusted, name=str(pair.repeat_adjustment))
    return adjusted


def classify_repeat_termination(
    previous_state: PairCycleState,
    terminal_event: EggMove,
) -> RepeatTermination:
    if _is_latent_timeout(terminal_event):
        return RepeatTermination.LATENT_TIMEOUT
    if _is_successful_tail_close(previous_state, terminal_event):
        return RepeatTermination.SUCCESSFUL_TAIL_CLOSE
    if _is_failed_tail(previous_state, terminal_event):
        return RepeatTermination.FAILED_TAIL
    if _played_quantity(previous_state) <= Decimal("0"):
        return RepeatTermination.UNFILLED_CANCEL
    return RepeatTermination.GENERIC_TERMINAL


def mm_scurve(pair: OrderPairSpec, context: RepeatAdjustmentContext) -> OrderPairSpec:
    """Small built-in policy: failed/timeout entries move closer, wins further."""

    if context.termination == RepeatTermination.SUCCESSFUL_TAIL_CLOSE:
        factor = Decimal("1.10")
        if context.roi is not None and context.roi < 0:
            factor = Decimal("0.90")
        return _scale_entry_distance(pair, factor)
    if context.termination == RepeatTermination.LATENT_TIMEOUT:
        adjusted = _scale_entry_distance(pair, Decimal("0.80"))
        return replace(adjusted, timeout=_scale_timeout_minutes(pair.timeout, Decimal("0.90")))
    if context.termination == RepeatTermination.FAILED_TAIL:
        adjusted = _scale_entry_distance(pair, Decimal("0.90"))
        return replace(adjusted, dr_pause=_scale_wait_minutes(pair.dr_pause, Decimal("1.20")))
    if context.termination == RepeatTermination.UNFILLED_CANCEL:
        return _scale_entry_distance(pair, Decimal("0.90"))
    return pair


def _normalise_name(name: str) -> str:
    key = name.strip()
    if not key:
        raise RepeatAdjustmentError("Repeat adjustment function name cannot be empty.")
    return key


def _is_latent_timeout(event: EggMove) -> bool:
    if event.event_id and event.event_id.startswith("latent-timeout:"):
        return True
    payload = event.reply or event.order or {}
    return str(payload.get("execType") or "") == "latent_timeout"


def _is_successful_tail_close(state: PairCycleState, event: EggMove) -> bool:
    return (
        event.is_private
        and event.kind == EggMoveKind.PLAYED_AND_CANCELED
        and event.role != OrderRole.HEAD
        and state.tail_state == TailState.CLOSED
        and _played_quantity(state) > Decimal("0")
    )


def _is_failed_tail(state: PairCycleState, event: EggMove) -> bool:
    return (
        event.role == OrderRole.TAIL
        and state.tail_state == TailState.FAILED
        and _played_quantity(state) > Decimal("0")
    )


def _played_quantity(state: PairCycleState) -> Decimal:
    return state.played_quantity or Decimal("0")


def _best_effort_roi(
    state: PairCycleState,
    event: EggMove,
) -> Decimal | None:
    entry = state.head_order_price or state.head_trigger_reference_price
    exit_price = _event_price(event)
    if exit_price is None and state.tail_trail is not None:
        exit_price = (
            state.tail_trail.confirmed_stop_price
            or state.tail_trail.current_stop_price
            or state.tail_trail.last_reference_price
        )
    if entry is None or exit_price is None or entry <= 0:
        return None
    if state.pair.head.side == Side.SELL:
        return (entry - exit_price) / entry
    return (exit_price - entry) / entry


def _event_price(event: EggMove) -> Decimal | None:
    for payload in (event.reply, event.order):
        if payload is None:
            continue
        for key in (
            "price",
            "avgPx",
            "avgPrice",
            "lastPx",
            "stopPx",
            "stop_price",
            "stopPrice",
        ):
            value = payload.get(key)
            parsed = _decimal_or_none(value)
            if parsed is not None:
                return parsed
    return None


def _decimal_or_none(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _scale_entry_distance(pair: OrderPairSpec, factor: Decimal) -> OrderPairSpec:
    head = pair.head
    if _is_relative_type(head.delta_type) and head.delta is not None:
        head = replace(head, delta=_scale_float(head.delta, factor))
    return replace(
        pair,
        head=head,
        head_price=(
            _scale_float(pair.head_price[0], factor),
            _scale_float(pair.head_price[1], factor),
        )
        if _is_relative_type(pair.head_price_type)
        else pair.head_price,
        head_order_price_spec=_scale_optional_float(
            pair.head_order_price_spec,
            pair.head_order_price_spec_type,
            factor,
        ),
    )


def _scale_optional_float(
    value: float | None,
    value_type: str,
    factor: Decimal,
) -> float | None:
    if value is None or not _is_relative_type(value_type):
        return value
    return _scale_float(value, factor)


def _scale_timeout_minutes(value: float | None, factor: Decimal) -> float | None:
    if value is None:
        return None
    return max(0.5, _scale_float(value, factor))


def _scale_wait_minutes(value: float | None, factor: Decimal) -> float | None:
    if value is None:
        return None
    return max(0.0, _scale_float(value, factor))


def _scale_float(value: float, factor: Decimal) -> float:
    return float(Decimal(str(value)) * factor)


def _is_relative_type(value_type: str) -> bool:
    return value_type.endswith(("B", "D"))


def _validate_adjusted_pair(
    original: OrderPairSpec,
    adjusted: OrderPairSpec,
    *,
    name: str,
) -> None:
    protected = {
        "name": (original.name, adjusted.name),
        "window": (original.window, adjusted.window),
        "try_num": (original.try_num, adjusted.try_num),
        "hook_name": (original.hook_name, adjusted.hook_name),
        "symbol": (original.symbol, adjusted.symbol),
        "exchange": (original.exchange, adjusted.exchange),
        "market_type": (original.market_type, adjusted.market_type),
        "head.order_type": (original.head.order_type, adjusted.head.order_type),
        "tail.order_type": (original.tail.order_type, adjusted.tail.order_type),
    }
    typed = {
        "head_price_type": (original.head_price_type, adjusted.head_price_type),
        "head_quantity_type": (
            original.head_quantity_type,
            adjusted.head_quantity_type,
        ),
        "tail_price_spec_type": (
            original.tail_price_spec_type,
            adjusted.tail_price_spec_type,
        ),
        "tail_unblock_spec_type": (
            original.tail_unblock_spec_type,
            adjusted.tail_unblock_spec_type,
        ),
        "head_order_price_spec_type": (
            original.head_order_price_spec_type,
            adjusted.head_order_price_spec_type,
        ),
        "head.delta_type": (original.head.delta_type, adjusted.head.delta_type),
        "amount_type": (original.amount_type, adjusted.amount_type),
    }
    for field, (before, after) in {**protected, **typed}.items():
        if before != after:
            raise RepeatAdjustmentError(
                f"Repeat adjustment '{name}' changed protected field {field}."
            )


register_repeat_adjustment("mm_scurve", mm_scurve)
