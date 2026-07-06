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
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
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
    roi: Decimal | None = None


RepeatAdjustment = Callable[[OrderPairSpec, RepeatAdjustmentContext], OrderPairSpec]
LOCAL_RFUNC_ENV = "KOLABI_STRATEGY_RFUNC"
DEFAULT_STRATEGY_RFUNC = Path("orders/rfunc.py")
_REPEAT_ADJUSTMENTS: dict[str, RepeatAdjustment] = {}
_LOADED_STRATEGY_RFUNC_PATHS: set[Path] = set()


def register_repeat_adjustment(name: str, func: RepeatAdjustment) -> RepeatAdjustment:
    """Register one operator-facing ``rFunc`` name."""

    key = _normalise_name(name)
    _REPEAT_ADJUSTMENTS[key] = func
    return func


def resolve_repeat_adjustment(name: str | None) -> RepeatAdjustment | None:
    if name is None or not name.strip():
        return None
    key = _normalise_name(name)
    func = _REPEAT_ADJUSTMENTS.get(key)
    if func is not None:
        return func

    _load_strategy_repeat_adjustments()
    func = _REPEAT_ADJUSTMENTS.get(key)
    if func is None:
        raise RepeatAdjustmentError(
            f"Unknown repeat adjustment function '{name}'."
        )
    return func


def apply_repeat_adjustment(
    previous_state: PairCycleState,
    terminal_event: EggMove,
    *,
    next_attempt: int,
) -> OrderPairSpec:
    """Return the next pair spec, after applying the optional row policy."""

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
    # Strategy modules are dynamic Python, so keep the runtime fail-closed guard.
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


def _normalise_name(name: str) -> str:
    key = name.strip()
    if not key:
        raise RepeatAdjustmentError("Repeat adjustment function name cannot be empty.")
    return key


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
