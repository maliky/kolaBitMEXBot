"""Bot domain model and tagged state vocabulary.

Purpose: define pure domain enums/dataclasses for strategy parsing, pair
lifecycle, and reducer events used by the active pair-cycle runtime.
Inputs: normalized side/order/reason strings and strategy fields.
Outputs: typed domain records and pure classification helpers.
Side effects: none.
Important types: `StrategySpec`, `OrderPairSpec`, `OrderState`, `TailMode`,
`OrderReason`, `ExecutionOutcome`, `EggMove`, `PairCycleState`,
`StrategyState`.
Role: pure logic.
Transitional: yes, legacy pair properties remain available while the active
runtime shell still consumes historic names.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping, Protocol

from kolabi.shared.core.runtime_types import Side

NumberPair = tuple[float, float]


class OrderState(StrEnum):
    """Lifecycle state shared by head and tail orders."""

    LATENT = "latent"
    HOOKED = "hooked"
    SUBMITTED = "submitted"
    UNADMITTED = "unadmitted"
    ADMITTED = "admitted"
    CONFIRMED = "confirmed"
    NEW = "new"
    LIVING = "living"
    CLOSED = "closed"
    FAILED = "failed"


HeadState = OrderState
TailState = OrderState


class OrderRole(StrEnum):
    """Pair role labels."""

    HEAD = "head"
    TAIL = "tail"


PairRole = OrderRole


class TailMode(StrEnum):
    """Tail relative mode driven by head state."""

    FLAPPING = "flapping"
    FLYING = "flying"


TailRole = TailMode


class PairIntentKind(StrEnum):
    PLACE_HEAD = "place_head"
    AMEND_HEAD = "amend_head"
    PLACE_TAIL = "place_tail"
    AMEND_TAIL = "amend_tail"


class HookMode(StrEnum):
    """Boolean mode used by a strategy pair dependency expression."""

    ALL = "all"
    ANY = "any"


class HookTargetKind(StrEnum):
    """Lifecycle conditions that can release a dependent pair."""

    HEAD_FILLED = "head_filled"
    PAIR_CLOSED = "pair_closed"


class OrderMove(StrEnum):
    """Canonical transition vocabulary for lifecycle events."""

    LATENT_TO_HOOKED = "latent_to_hooked"
    HOOKED_TO_SUBMITTED = "hooked_to_submitted"
    SUBMITTED_TO_UNADMITTED = "submitted_to_unadmitted"
    SUBMITTED_TO_ADMITTED = "submitted_to_admitted"
    ADMITTED_TO_CONFIRMED = "admitted_to_confirmed"
    CONFIRMED_TO_NEW = "confirmed_to_new"
    CONFIRMED_TO_LIVING = "confirmed_to_living"
    CONFIRMED_TO_CLOSED = "confirmed_to_closed"
    CONFIRMED_TO_FAILED = "confirmed_to_failed"
    NEW_TO_FAILED = "new_to_failed"
    NEW_TO_LIVING = "new_to_living"
    NEW_TO_NEW = "new_to_new"
    NEW_TO_CLOSED = "new_to_closed"
    LIVING_TO_FAILED = "living_to_failed"
    LIVING_TO_LIVING = "living_to_living"
    LIVING_TO_CLOSE = "living_to_close"


class OrderReason(StrEnum):
    """Raw exchange reasons as emitted by order/execution feeds."""

    LIMIT_ORDER_FROM_STOP = "limit_order_from_stop"
    NEW_PLACED_ORDER_BY_USER = "new_placed_order_by_user"
    CANCELLED_BY_ADMIN = "cancelled_by_admin"
    CANCELLED_BY_USER = "cancelled_by_user"
    CONTRACT_EXPIRED = "contract_expired"
    DEAD_MAN_SWITCH = "dead_man_switch"
    LIQUIDATION = "liquidation"
    MARKET_INACTIVE = "market_inactive"
    NOT_ENOUGH_MARGIN = "not_enough_margin"
    ORDER_FOR_EDIT_NOT_FOUND = "order_for_edit_not_found"
    PARTIAL_FILL = "partial_fill"
    FULL_FILL = "full_fill"
    IOC_WOULD_NOT_EXECUTE = "ioc_order_failed_because_it_would_not_be_executed"
    POST_ONLY_WOULD_FILL = "post_order_failed_because_it_would_filled"
    STOP_ORDER_TRIGGERED = "stop_order_triggered"
    WOULD_EXECUTE_SELF = "would_execute_self"
    WOULD_NOT_REDUCE_POSITION = "would_not_reduce_position"
    UNKNOWN = "unknown"


class ExecutionOutcome(StrEnum):
    NEW = "new"
    PLAYED = "played"
    CANCELED_UNPLAYED = "canceled_unplayed"
    CANCELED_PLAYED = "canceled_played"


class EggMoveKind(StrEnum):
    HEAD_TRIGGER_BASELINED = "head_trigger_baselined"
    HEAD_HOOKED = "head_hooked"
    HEAD_SUBMITTED = "head_submitted"
    TAIL_SUBMITTED = "tail_submitted"
    TAIL_AMENDED = "tail_amended"
    TAIL_AMEND_REJECTED = "tail_amend_rejected"
    MARKET_TICK = "market_tick"
    HEAD_UNADMITTED = "head_unadmitted"
    HEAD_ADMITTED = "head_admitted"
    NOT_PLAYED_NOR_CANCELED = "not_played_nor_canceled"
    NOT_PLAYED_CANCELED = "not_played_canceled"
    PLAYED_NOT_CANCELED = "played_not_canceled"
    PLAYED_AND_CANCELED = "played_and_canceled"


PLAYED_REASONS = frozenset(
    {
        OrderReason.FULL_FILL,
        OrderReason.PARTIAL_FILL,
        OrderReason.IOC_WOULD_NOT_EXECUTE,
        OrderReason.POST_ONLY_WOULD_FILL,
        OrderReason.STOP_ORDER_TRIGGERED,
        OrderReason.WOULD_EXECUTE_SELF,
    }
)


@dataclass(frozen=True)
class TimeWindow:
    start_minutes: float
    end_minutes: float


@dataclass(frozen=True)
class HeadSpec:
    side: Side
    order_type: str
    delta: float | None = None
    delta_type: str = "oD"


@dataclass(frozen=True)
class TailSpec:
    side: Side
    order_type: str
    delta: float | None = None


@dataclass(frozen=True)
class OrderPairSpec:
    name: str
    window: TimeWindow
    try_num: int | None
    dr_pause: float | None
    timeout: float | None
    head: HeadSpec
    head_price: NumberPair
    head_price_type: str
    head_quantity: int | float | Decimal | None
    head_quantity_type: str
    tail: TailSpec
    tail_price_spec: float | None
    tail_price_spec_type: str
    amount_type: str
    hook_name: str | None = None
    symbol: str | None = None
    exchange: str | None = None
    market_type: str | None = None
    tail_unblock_spec: float | None = None
    tail_unblock_spec_type: str = "uD"
    tail_second_update_wait_seconds: float = 6.0
    head_order_price_spec: float | None = None
    head_order_price_spec_type: str = "hD"
    cooldown_minutes: float | None = None
    repeat_adjustment: str | None = None

    @property
    def attempts(self) -> int | None:
        return self.try_num

    @property
    def repeats_until_window(self) -> bool:
        return self.try_num is None

    @property
    def pause_minutes(self) -> float | None:
        return self.dr_pause

    @property
    def timeout_minutes(self) -> float | None:
        return self.timeout

    @property
    def hook(self) -> str | None:
        return self.hook_name

    @property
    def price_interval(self) -> NumberPair:
        return self.head_price


@dataclass(frozen=True)
class StrategySpec:
    name: str
    pairs: tuple[OrderPairSpec, ...]


@dataclass(frozen=True)
class OrderIdentity:
    pair_name: str
    role: str
    client_order_id: str | None = None
    exchange_order_id: str | None = None
    symbol: str | None = None
    exchange: str | None = None
    market_type: str | None = None


@dataclass(frozen=True)
class ConfirmedOrder:
    identity: OrderIdentity
    state: HeadState
    reason: OrderReason
    filled_quantity: Decimal
    total_quantity: Decimal

    @property
    def is_played(self) -> bool:
        """Le jeu est rempli semantiquement, meme avec zero fill."""
        return self.reason in PLAYED_REASONS

    @property
    def is_canceled(self) -> bool:
        """Le broker a termine l'ordre sans le laisser ouvert."""
        return self.state in {HeadState.FAILED, HeadState.CLOSED}

    @property
    def is_terminal(self) -> bool:
        return self.state in {HeadState.FAILED, HeadState.CLOSED}


@dataclass(frozen=True)
class TailTrailSample:
    occurred_at: datetime
    reference_price: Decimal
    spread: Decimal | None = None


@dataclass(frozen=True)
class TailTrailState:
    entry_reference_price: Decimal
    baseline_width: Decimal
    current_stop_price: Decimal
    previous_stop_price: Decimal
    samples: tuple[TailTrailSample, ...]
    initial_stop_price: Decimal | None = None
    confirmed_stop_price: Decimal | None = None
    last_reference_price: Decimal | None = None
    last_amended_at: datetime | None = None
    last_stop_update_at: datetime | None = None
    first_unblocked_at: datetime | None = None
    last_confirmed_at: datetime | None = None
    max_observed_spread: Decimal = Decimal("0")
    local_amend_count: int = 0
    catch_basis_width: Decimal | None = None


@dataclass(frozen=True)
class HookTarget:
    """One upstream pair lifecycle condition."""

    origin_pair_name: str
    kind: HookTargetKind


@dataclass(frozen=True)
class HookExpression:
    """Typed ALL/ANY dependency expression declared by a strategy pair."""

    mode: HookMode
    targets: tuple[HookTarget, ...]


@dataclass(frozen=True)
class HookEvidence:
    """Fresh evidence collected for one downstream pair attempt."""

    target: HookTarget
    origin_attempt_index: int
    satisfied_at: datetime


@dataclass(frozen=True)
class PairCycleState:
    pair: OrderPairSpec
    head_state: HeadState = HeadState.LATENT
    tail_state: TailState | None = None
    tail_mode: TailMode | None = None
    head_identity: OrderIdentity | None = None
    tail_identity: OrderIdentity | None = None
    tail_trail: TailTrailState | None = None
    head_trigger_reference_price: Decimal | None = None
    head_trigger_reference_source: str | None = None
    head_trigger_reference_at: datetime | None = None
    head_order_price: Decimal | None = None
    head_order_stop_price: Decimal | None = None
    head_order_quantity: Decimal | None = None
    dependency_armed_at: datetime | None = None
    dependency_evidence: tuple[HookEvidence, ...] = ()
    played_quantity: Decimal | None = None
    latest_commands: Mapping[str, tuple[str, ...]] | None = None
    pair_id: str | None = None
    last_processed_private_event_id: str | None = None
    last_processed_private_event_ts: datetime | None = None
    last_emitted_command_id: str | None = None
    last_emitted_command_ts: datetime | None = None
    attempt_index: int = 1
    completed_at: datetime | None = None
    instrument_tick_size: Decimal | None = None

    @property
    def head_client_order_id(self) -> str | None:
        return None if self.head_identity is None else self.head_identity.client_order_id

    @property
    def tail_client_order_id(self) -> str | None:
        return None if self.tail_identity is None else self.tail_identity.client_order_id

    def __post_init__(self) -> None:
        if self.latest_commands is not None and not isinstance(self.latest_commands, MappingProxyType):
            object.__setattr__(
                self,
                "latest_commands",
                MappingProxyType(dict(self.latest_commands)),
            )


@dataclass(frozen=True)
class EggMove:
    """Typed reducer input for one pair transition step."""

    kind: EggMoveKind
    occurred_at: datetime
    symbol: str
    order: Mapping[str, object] | None = None
    reply: Mapping[str, object] | None = None
    event_id: str | None = None
    pair_name: str | None = None
    role: OrderRole | None = None
    is_private: bool = False

    def __post_init__(self) -> None:
        if self.order is not None and not isinstance(self.order, MappingProxyType):
            object.__setattr__(self, "order", MappingProxyType(dict(self.order)))
        if self.reply is not None and not isinstance(self.reply, MappingProxyType):
            object.__setattr__(self, "reply", MappingProxyType(dict(self.reply)))


@dataclass(frozen=True)
class PairIntent:
    kind: PairIntentKind


@dataclass(frozen=True)
class StrategyState:
    """Persistent strategy memory owned by the Chronos supervisor layer."""

    launched_at: datetime
    pairs: Mapping[str, PairCycleState]
    strategy_id: str | None = None
    last_event_id: str | None = None
    last_event_ts: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.pairs, MappingProxyType):
            object.__setattr__(self, "pairs", MappingProxyType(dict(self.pairs)))


@dataclass(frozen=True)
class PairCycleEvent:
    pair_name: str
    state: PairCycleState
    message: str


class ExchangeOrderAck(Protocol):
    order_id: str
    status: str
    price: float | None
    orig_qty: float | None
    executed_qty: float | None
    side: str | None


def normalize_side(raw: str) -> Side:
    value = raw.strip().lower()
    if value == "buy":
        return Side.BUY
    if value == "sell":
        return Side.SELL
    raise ValueError(f"Unsupported side '{raw}'")


def opposite_side(side: Side) -> Side:
    """Retourne le cote oppose pour le tail de fermeture."""
    if side == Side.BUY:
        return Side.SELL
    return Side.BUY


def normalize_reason(raw: str | None) -> OrderReason:
    if not raw:
        return OrderReason.UNKNOWN
    value = raw.strip().lower()
    for reason in OrderReason:
        if reason.value == value:
            return reason
    return OrderReason.UNKNOWN


def classify_confirmed_state(outcome: ExecutionOutcome) -> HeadState:
    if outcome == ExecutionOutcome.CANCELED_PLAYED:
        return HeadState.CLOSED
    if outcome == ExecutionOutcome.PLAYED:
        return HeadState.LIVING
    if outcome == ExecutionOutcome.CANCELED_UNPLAYED:
        return HeadState.FAILED
    return HeadState.NEW


def can_hook_tail(head: ConfirmedOrder) -> bool:
    return head.is_played and head.state != HeadState.FAILED


def tail_mode_for_head(head: ConfirmedOrder) -> TailMode | None:
    if not can_hook_tail(head):
        return None
    if head.state == HeadState.LIVING:
        return TailMode.FLAPPING
    if head.state == HeadState.CLOSED:
        return TailMode.FLYING
    return None


def classify_confirmed_move(head: ConfirmedOrder) -> EggMoveKind:
    """Classe un ordre confirme selon la table canonique jeu/annulation."""
    if head.is_played and head.is_canceled:
        return EggMoveKind.PLAYED_AND_CANCELED
    if head.is_played:
        return EggMoveKind.PLAYED_NOT_CANCELED
    if head.is_canceled:
        return EggMoveKind.NOT_PLAYED_CANCELED
    return EggMoveKind.NOT_PLAYED_NOR_CANCELED
