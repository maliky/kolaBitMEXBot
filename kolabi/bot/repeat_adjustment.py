"""Registry and safety checks for repeat-adjustment policies.

Strategy rows name policies through the optional ``rFunc`` column.  This module
keeps the typed runtime boundary: it builds the repeat context, loads the
operator-local strategy module when needed, and validates the returned
``OrderPairSpec``.  Strategy-specific policy functions belong in the ignored
``orders/rfunc.py`` file, or in the path named by ``KOLABI_STRATEGY_RFUNC``.
"""

from __future__ import annotations

import importlib.util
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path

from kolabi.bot.domain import (
    EggMove,
    EggMoveKind,
    OrderPairSpec,
    OrderRole,
    PairCycleState,
    TailState,
)
from kolabi.bot.order_codes import parse_order_code
from kolabi.shared.core.runtime_types import Side


class RepeatAdjustmentError(ValueError):
    """Raised when an rFunc policy is unknown or returns an unsafe pair."""


class RepeatTermination(StrEnum):
    """Policy-level terminal categories derived from event and pair state."""

    LATENT_TIMEOUT = "latent_timeout"
    UNFILLED_CANCEL = "unfilled_cancel"
    SUCCESSFUL_TAIL_CLOSE = "successful_tail_close"
    FAILED_TAIL = "failed_tail"
    GENERIC_TERMINAL = "generic_terminal"


@dataclass(frozen=True)
class RepeatAdjustmentContext:
    """Evidence available to one repeat policy.

    ``previous_state`` is the terminal state of the attempt that just finished.
    ``terminal_event`` is the event that caused the repeat decision.
    ``termination`` is a compact classification for simple policies, and ``roi``
    is best effort because exact fee/fill evidence may not be available at this
    pure Chronos boundary.
    """

    previous_state: PairCycleState
    terminal_event: EggMove
    next_attempt: int
    termination: RepeatTermination
    args: tuple[Decimal, ...] = ()
    roi: Decimal | None = None


RepeatAdjustment = Callable[[OrderPairSpec, RepeatAdjustmentContext], OrderPairSpec]
RepeatAdjustmentValidator = Callable[
    [OrderPairSpec, tuple[Decimal, ...], str],
    None,
]
LOCAL_RFUNC_ENV = "KOLABI_STRATEGY_RFUNC"
DEFAULT_STRATEGY_RFUNC = Path("orders/rfunc.py")
HEAD_OFFSET_BASES = frozenset({"L", "S", "SL", "LT", "MT"})
HEAD_OFFSET_FLOOR = Decimal("25")
_LOADED_STRATEGY_RFUNC_PATHS: set[Path] = set()


@dataclass(frozen=True)
class RepeatAdjustmentRequest:
    raw: str
    name: str
    args: tuple[Decimal, ...] = ()


@dataclass(frozen=True)
class _RepeatAdjustmentRegistration:
    func: RepeatAdjustment
    validator: RepeatAdjustmentValidator | None = None


_REPEAT_ADJUSTMENTS: dict[str, _RepeatAdjustmentRegistration] = {}


def register_repeat_adjustment(
    name: str,
    func: RepeatAdjustment,
    *,
    validator: RepeatAdjustmentValidator | None = None,
) -> RepeatAdjustment:
    """Register one operator-facing ``rFunc`` name."""

    key = _normalise_name(name)
    _REPEAT_ADJUSTMENTS[key] = _RepeatAdjustmentRegistration(func, validator)
    return func


def resolve_repeat_adjustment(name: str | None) -> RepeatAdjustment | None:
    request = parse_repeat_adjustment(name)
    if request is None:
        return None
    return _resolve_repeat_adjustment_registration(request).func


def validate_repeat_adjustments(pairs: Iterable[OrderPairSpec]) -> None:
    """Fail strategy startup when an ``rFunc`` name or argument set is invalid."""

    for pair in pairs:
        request = parse_repeat_adjustment(pair.repeat_adjustment)
        if request is None:
            continue
        registration = _resolve_repeat_adjustment_registration(request)
        if registration.validator is not None:
            registration.validator(pair, request.args, request.raw)


def apply_repeat_adjustment(
    previous_state: PairCycleState,
    terminal_event: EggMove,
    *,
    next_attempt: int,
) -> OrderPairSpec:
    """Return the next pair spec, after applying the optional row policy."""

    pair = previous_state.pair
    request = parse_repeat_adjustment(pair.repeat_adjustment)
    if request is None:
        return pair
    registration = _resolve_repeat_adjustment_registration(request)

    context = RepeatAdjustmentContext(
        previous_state=previous_state,
        terminal_event=terminal_event,
        next_attempt=next_attempt,
        termination=classify_repeat_termination(previous_state, terminal_event),
        args=request.args,
        roi=_best_effort_roi(previous_state, terminal_event),
    )
    adjusted = registration.func(pair, context)
    # Strategy modules are dynamic Python, so keep the runtime fail-closed guard.
    if not isinstance(adjusted, OrderPairSpec):
        raise RepeatAdjustmentError(
            f"Repeat adjustment '{request.raw}' did not return an OrderPairSpec."
        )
    _validate_adjusted_pair(pair, adjusted, name=request.raw)
    return adjusted


def classify_repeat_termination(
    previous_state: PairCycleState,
    terminal_event: EggMove,
) -> RepeatTermination:
    """Condense terminal state/event evidence into simple policy categories."""
    if _is_latent_timeout(terminal_event):
        return RepeatTermination.LATENT_TIMEOUT
    if _is_successful_tail_close(previous_state, terminal_event):
        return RepeatTermination.SUCCESSFUL_TAIL_CLOSE
    if _is_failed_tail(previous_state, terminal_event):
        return RepeatTermination.FAILED_TAIL
    if _played_quantity(previous_state) <= Decimal("0"):
        return RepeatTermination.UNFILLED_CANCEL
    return RepeatTermination.GENERIC_TERMINAL


def parse_repeat_adjustment(raw: str | None) -> RepeatAdjustmentRequest | None:
    if raw is None or not raw.strip():
        return None
    expression = raw.strip()
    name, separator, raw_args = expression.partition(":")
    key = _normalise_name(name)
    if not separator:
        return RepeatAdjustmentRequest(raw=expression, name=key)
    return RepeatAdjustmentRequest(
        raw=expression,
        name=key,
        args=_parse_repeat_adjustment_args(raw_args, raw=expression),
    )


def require_arg_count(raw: str, args: tuple[Decimal, ...], expected: int) -> None:
    if len(args) != expected:
        raise RepeatAdjustmentError(
            f"Repeat adjustment '{raw}' expects {expected} argument(s), got {len(args)}."
        )


def require_positive_arg(raw: str, value: Decimal, label: str) -> None:
    if value <= 0:
        raise RepeatAdjustmentError(
            f"Repeat adjustment '{raw}' requires positive {label}."
        )


def single_arg(context: RepeatAdjustmentContext, name: str) -> Decimal:
    require_arg_count(name, context.args, 1)
    return context.args[0]


def two_args(context: RepeatAdjustmentContext, name: str) -> tuple[Decimal, Decimal]:
    require_arg_count(name, context.args, 2)
    return context.args[0], context.args[1]


def validate_head_offset_order_type(pair: OrderPairSpec, raw: str) -> None:
    base = parse_order_code(pair.head.order_type).base_key
    if base not in HEAD_OFFSET_BASES:
        raise RepeatAdjustmentError(
            f"Repeat adjustment '{raw}' requires L, S, SL, LT, or MT head order type; "
            f"pair '{pair.name}' uses {base}."
        )


def head_filled_before_timeout(context: RepeatAdjustmentContext) -> bool:
    if context.termination == RepeatTermination.LATENT_TIMEOUT:
        return False
    return (context.previous_state.played_quantity or Decimal("0")) > Decimal("0")


def head_timed_out(context: RepeatAdjustmentContext) -> bool:
    if context.termination == RepeatTermination.LATENT_TIMEOUT:
        return True
    event = context.terminal_event
    if event.event_id and (
        event.event_id.startswith("head-timeout-cancel:")
        or event.event_id.startswith("latent-timeout:")
    ):
        return True
    payload = event.reply or event.order or {}
    tokens = {
        str(payload.get("execType") or ""),
        str(payload.get("reason") or ""),
        str(payload.get("runtime_reason") or ""),
        str(payload.get("text") or ""),
    }
    return bool(tokens & {"head_timeout", "head_unconfirmed_timeout", "latent_timeout"})


def pair_has_negative_roi(context: RepeatAdjustmentContext) -> bool:
    return (
        context.termination
        not in {
            RepeatTermination.LATENT_TIMEOUT,
            RepeatTermination.UNFILLED_CANCEL,
        }
        and context.roi is not None
        and context.roi < Decimal("0")
    )


def pair_has_positive_roi(context: RepeatAdjustmentContext) -> bool:
    return (
        context.termination
        not in {
            RepeatTermination.LATENT_TIMEOUT,
            RepeatTermination.UNFILLED_CANCEL,
        }
        and context.roi is not None
        and context.roi > Decimal("0")
    )


def pair_has_positive_roi_after_tail_update(context: RepeatAdjustmentContext) -> bool:
    return pair_has_positive_roi(context) and tail_was_updated(context)


def tail_was_updated(context: RepeatAdjustmentContext) -> bool:
    trail = context.previous_state.tail_trail
    return trail is not None and (
        trail.local_amend_count > 0
        or trail.last_amended_at is not None
        or trail.last_confirmed_at is not None
    )


def step_timeout(
    value: float | None,
    step: Decimal,
    *,
    floor: Decimal,
    cap: Decimal,
) -> float | None:
    if value is None:
        return None
    adjusted = Decimal(str(value)) + step
    adjusted = max(floor, min(cap, adjusted))
    return float(round_down_to_step(adjusted, Decimal("0.1")))


def adapt_timeout_by_timeout_or_negative_roi(
    pair: OrderPairSpec,
    context: RepeatAdjustmentContext,
    *,
    base_timeout: Decimal,
    step: Decimal,
) -> OrderPairSpec:
    """Move tOut toward the objective that every emitted head gets filled."""

    if head_timed_out(context):
        return replace(
            pair,
            timeout=step_timeout(
                pair.timeout,
                step,
                floor=Decimal("0.1"),
                cap=base_timeout * Decimal("2"),
            ),
        )
    if pair_has_negative_roi(context):
        return replace(
            pair,
            timeout=step_timeout(
                pair.timeout,
                -step,
                floor=Decimal("0.1"),
                cap=base_timeout * Decimal("2"),
            ),
        )
    return pair


def adapt_head_offset_by_roi(
    pair: OrderPairSpec,
    context: RepeatAdjustmentContext,
    *,
    base_hprice: Decimal,
    step: Decimal,
) -> OrderPairSpec:
    """Move hPrice from timeout/ROI evidence.

    Timeout means the head was too far from the market, so reduce the offset
    down to the S3/S4 floor.
    Negative ROI means the pair was too close/aggressive, so increase it.
    Positive ROI keeps the current offset.
    """

    hprice = decimal_or_none(pair.head_order_price_spec)
    if hprice is None:
        return pair
    if head_timed_out(context):
        return replace_head_offset(pair, max(HEAD_OFFSET_FLOOR, hprice - step))
    if pair_has_negative_roi(context):
        cap = round_down_to_step(base_hprice * Decimal("2"), step)
        return replace_head_offset(pair, min(cap, hprice + step))
    return pair


def replace_head_offset(pair: OrderPairSpec, value: Decimal) -> OrderPairSpec:
    return replace(pair, head_order_price_spec=float(abs(value)))


def scale_entry_distance(pair: OrderPairSpec, factor: Decimal) -> OrderPairSpec:
    head = pair.head
    if is_relative_type(head.delta_type) and head.delta is not None:
        head = replace(head, delta=scale_float(head.delta, factor))
    return replace(
        pair,
        head=head,
        head_price=(
            scale_float(pair.head_price[0], factor),
            scale_float(pair.head_price[1], factor),
        )
        if is_relative_type(pair.head_price_type)
        else pair.head_price,
        head_order_price_spec=scale_optional_float(
            pair.head_order_price_spec,
            pair.head_order_price_spec_type,
            factor,
        ),
    )


def scale_optional_float(
    value: float | None,
    value_type: str,
    factor: Decimal,
) -> float | None:
    if value is None or not is_relative_type(value_type):
        return value
    return scale_float(value, factor)


def scale_minutes(
    value: float | None,
    factor: Decimal,
    *,
    floor: Decimal,
) -> float | None:
    if value is None:
        return None
    return float(max(floor, Decimal(str(value)) * factor))


def scale_float(value: float, factor: Decimal) -> float:
    return float(Decimal(str(value)) * factor)


def is_relative_type(value_type: str) -> bool:
    return value_type.endswith(("B", "D"))


def decimal_or_none(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def quantity_value_like(
    original: object,
    quantity: Decimal,
) -> int | float | Decimal:
    if isinstance(original, int) and quantity == quantity.to_integral_value():
        return int(quantity)
    if isinstance(original, float):
        return float(quantity)
    return quantity


def round_down_to_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def _normalise_name(name: str) -> str:
    key = name.strip()
    if not key:
        raise RepeatAdjustmentError("Repeat adjustment function name cannot be empty.")
    return key


def _parse_repeat_adjustment_args(raw_args: str, *, raw: str) -> tuple[Decimal, ...]:
    if not raw_args.strip():
        raise RepeatAdjustmentError(
            f"Repeat adjustment '{raw}' must provide arguments after ':'."
        )
    parsed: list[Decimal] = []
    for item in raw_args.split(","):
        candidate = item.strip()
        if not candidate:
            raise RepeatAdjustmentError(
                f"Repeat adjustment '{raw}' has an empty argument."
            )
        try:
            parsed.append(Decimal(candidate))
        except (InvalidOperation, ValueError) as exc:
            raise RepeatAdjustmentError(
                f"Repeat adjustment '{raw}' has non-numeric argument '{candidate}'."
            ) from exc
    return tuple(parsed)


def _resolve_repeat_adjustment_registration(
    request: RepeatAdjustmentRequest,
) -> _RepeatAdjustmentRegistration:
    registration = _REPEAT_ADJUSTMENTS.get(request.name)
    if registration is not None:
        return registration

    _load_strategy_repeat_adjustments()
    registration = _REPEAT_ADJUSTMENTS.get(request.name)
    if registration is None:
        raise RepeatAdjustmentError(
            f"Unknown repeat adjustment function '{request.name}' in rFunc '{request.raw}'."
        )
    return registration


def _load_strategy_repeat_adjustments() -> None:
    path = _strategy_rfunc_path()
    if path is None:
        return
    if path in _LOADED_STRATEGY_RFUNC_PATHS:
        return
    if not path.is_file():
        return

    module_name = f"kolabi_strategy_rfunc_{len(_LOADED_STRATEGY_RFUNC_PATHS)}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RepeatAdjustmentError(f"Cannot load repeat adjustment file '{path}'.")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # pragma: no cover - exact exception belongs to local policy.
        raise RepeatAdjustmentError(
            f"Failed to load repeat adjustment file '{path}': {exc}"
        ) from exc
    _LOADED_STRATEGY_RFUNC_PATHS.add(path)


def _strategy_rfunc_path() -> Path | None:
    raw = os.environ.get(LOCAL_RFUNC_ENV)
    if raw is not None and not raw.strip():
        return None
    return _normalise_path(Path(raw)) if raw else _normalise_path(DEFAULT_STRATEGY_RFUNC)


def _normalise_path(path: Path) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        expanded = Path.cwd() / expanded
    return expanded.resolve(strict=False)


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
            parsed = decimal_or_none(value)
            if parsed is not None:
                return parsed
    return None


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
