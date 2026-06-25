"""Async persistent strategy runtime over Dragon, Chronos, Horus, and Ogun.

Purpose: own the active supervisor loop for simulated and live/demo execution
through the typed bot stack.
Inputs: canonical strategy state, real event sources, and an executor.
Outputs: final strategy state, emitted bot commands, and supervisor notices.
Side effects: async queue flow and optional command execution through an
executor boundary.
Important types: `Chronos`, `EggMove`, algebraic bot commands.
Role: interpreter shell.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from itertools import count
from typing import Mapping, Protocol, assert_never, cast

from kolabi.bot.chronos import (
    Chronos,
    ChronosNotice,
    pair_dependency_satisfied,
    resolve_pair_name,
)
from kolabi.bot.domain import (
    EggMove,
    EggMoveKind,
    HeadState,
    OrderIdentity,
    OrderRole,
    PairCycleState,
    StrategySpec,
    StrategyState,
    TailState,
)
from kolabi.bot.dragon import (
    MarketSnapshotFact,
    head_hooked_event,
    head_hooked_from_market_snapshot,
    head_move_from_private_fact,
    head_submitted_from_ack,
    market_tick_from_market_snapshot,
    private_order_fact_from_record,
    simulated_private_fill_from_submission,
    tail_submitted_from_ack,
)
from kolabi.bot.exchange_routes import ExchangeRoute, pair_route
from kolabi.bot.ids import head_client_order_id, tail_client_order_id
from kolabi.bot.price_units import signed_logbps_move
from kolabi.bot.pricing import (
    executable_head_reference_price,
    pair_window_has_ended,
    pair_window_is_open,
    tail_reference_price,
)
from kolabi.bot.quantity import (
    QuantityResolutionError,
    available_usd_for_percent_quantity,
    mark_price_for_usd_quantity,
    pair_uses_percent_balance_quantity,
    pair_uses_usd_quantity,
    resolve_pair_percent_balance_quantity,
    resolve_pair_usd_quantity,
)
from kolabi.bot.runtime_policy import (
    CommandSlot as _CommandSlot,
)
from kolabi.bot.runtime_policy import (
    active_pair_count,
    active_pair_names,
    append_pending_command,
    command_slot,
    command_slot_still_live,
    head_capacity_available,
)
from kolabi.bot.runtime_policy import (
    pair_runtime_complete as _policy_pair_runtime_complete,
)
from kolabi.bot.tail_tracking import tail_first_jump_guard_width, tail_unblock_requirement
from kolabi.bot.telemetry import TailTelemetryRow
from kolabi.shared.core.models import OrderAck
from kolabi.shared.core.runtime_types import (
    AmendHeadCommand,
    AmendTailCommand,
    CancelCommand,
    CancelOrderCommandRequest,
    DragonSong,
    OrderDict,
    OrderQty,
    PlaceHeadCommand,
    PlaceOrderCommandRequest,
    PlaceTailCommand,
    Price,
    PrivateOrderRecord,
    RuntimeCommandKind,
    Symbol,
    to_decimal,
)

_LOGGER = logging.getLogger("kola")
_DEFAULT_HEAD_FILL_REFERENCE_GRACE_SECONDS = 20.0
_DEFAULT_PRIVATE_PENDING_RECORD_TTL_SECONDS = 120.0
_DEFAULT_PRIVATE_PENDING_RECORD_LIMIT = 512
_DEFAULT_PRIVATE_SUPPRESSED_EVENT_ID_LIMIT = 2048
_MAX_HEAD_CANCEL_RETRIES = 3
_LEGEND_GAP = "    "


def _legend_fields(*fields: object) -> str:
    return _LEGEND_GAP.join(str(field) for field in fields)


def _runtime_fields(*values: object) -> str:
    return " ".join(str(value) for value in values)


def _drop_closed_logger_handlers(logger: logging.Logger) -> None:
    for handler in tuple(logger.handlers):
        stream = getattr(handler, "stream", None)
        if stream is None or not getattr(stream, "closed", False):
            continue
        logger.removeHandler(handler)


class CommandExecutor(Protocol):
    async def execute(self, command: DragonSong) -> OrderAck: ...


class RuntimeEventSource(Protocol):
    async def pump(self, runtime: "RuntimeQueueLike") -> None: ...


class RuntimeQueueLike(Protocol):
    symbol: str
    exchange: str
    market_type: str
    state: StrategyState
    running: bool

    @property
    def all_pairs_terminal(self) -> bool: ...

    @property
    def should_keep_sources_alive(self) -> bool: ...

    async def enqueue(self, event: EggMove) -> None: ...

    def pair_state_for_record(self, record: object) -> tuple[PairCycleState, OrderRole] | None: ...


class PublicMarketStateReader(Protocol):
    """Adapter port for public market facts; the bot core does not know the DB."""

    best_bid: float | None
    best_ask: float | None
    mid_price: float | None
    last_price: float | None
    mark_price: float | None
    index_price: float | None
    tick_size: float | None
    recorded_at: str | None


class PublicRuntimeStateReader(Protocol):
    """Reads strategy-facing public market state from any backing store."""

    def fetch_market_state(
        self,
        symbol: str | None = None,
        exchange: str | None = None,
        market_type: str | None = None,
    ) -> PublicMarketStateReader: ...


class AccountBalanceStateReader(Protocol):
    """Reads strategy-facing account balance state from any backing store."""

    def fetch_account_balance(
        self,
        asset: str = "USD",
        exchange: str | None = None,
        market_type: str | None = None,
    ) -> object: ...


class PrivateOrderStateReader(Protocol):
    """Reads strategy-facing private order records from any backing store."""

    def fetch_private_orders_since(
        self,
        *,
        after_local_timestamp: datetime | None = None,
        after_local_id: int | None = None,
        symbol: str | None = None,
        exchange: str | None = None,
        market_type: str | None = None,
        limit: int = 200,
    ) -> tuple[PrivateOrderRecord, ...]: ...

    def fetch_private_fills_since(
        self,
        *,
        after_local_timestamp: datetime | None = None,
        after_local_id: int | None = None,
        symbol: str | None = None,
        exchange: str | None = None,
        market_type: str | None = None,
        limit: int = 200,
    ) -> tuple[PrivateOrderRecord, ...]: ...


class StrategyRuntimeLike(RuntimeQueueLike, Protocol):
    strategy: StrategySpec


class TailTelemetryWriter(Protocol):
    def record_rows(self, rows: tuple[TailTelemetryRow, ...]) -> None: ...


@dataclass(frozen=True)
class _PendingPrivateRecord:
    record: PrivateOrderRecord
    first_seen_at: datetime
    is_fill: bool = False


@dataclass
class _PrivateCursor:
    after_local_timestamp: datetime | None = None
    after_local_id: int | None = None
    after_fill_timestamp: datetime | None = None
    after_fill_id: int | None = None
    initialised: bool = False


@dataclass(frozen=True)
class _InFlightCommand:
    command: DragonSong
    task: asyncio.Task[None]


@dataclass(frozen=True)
class _OrderLifecycleSnapshot:
    side: str | None = None
    filled_qty: Decimal | None = None
    filled_price: Decimal | None = None
    filled_at: datetime | None = None


@dataclass(frozen=True)
class _TailVisibilityWindow:
    pair_name: str
    attempt_index: int
    client_order_id: str | None
    exchange_order_id: str | None
    started_at: datetime
    deadline_at: datetime
    last_warned_at: datetime | None = None


@dataclass(frozen=True)
class _TailAmendPending:
    pair_name: str
    attempt_index: int
    desired_stop_price: Decimal
    client_order_id: str | None
    exchange_order_id: str | None
    started_at: datetime
    deadline_at: datetime


@dataclass(frozen=True)
class _HeadFillDeadline:
    pair_name: str
    attempt_index: int
    client_order_id: str | None
    exchange_order_id: str | None
    started_at: datetime
    deadline_at: datetime
    source: str = "ack"
    cancel_dispatched_at: datetime | None = None


@dataclass(frozen=True)
class _LatentHeadDeadline:
    pair_name: str
    attempt_index: int
    started_at: datetime
    deadline_at: datetime


_LEASE_PENDING_PLACE = "PENDING_PLACE"
_LEASE_ACKED = "ACKED"
_LEASE_LIVE = "LIVE"
_LEASE_CANCEL_REQUESTED = "CANCEL_REQUESTED"
_LEASE_CLOSED = "CLOSED"


@dataclass(frozen=True)
class _OrderLease:
    pair_name: str
    attempt_index: int
    role: str
    symbol: str
    client_order_id: str | None
    exchange_order_id: str | None
    side: str | None
    quantity: Decimal | None
    price: Decimal | None
    stop_price: Decimal | None
    status: str
    created_at: datetime
    last_seen_at: datetime | None = None
    cancel_sent_at: datetime | None = None
    cancel_retry_count: int = 0
    visibility_warned_at: datetime | None = None
    exchange: str | None = None
    market_type: str | None = None


@dataclass(frozen=True)
class StrategyRunResult:
    state: StrategyState
    commands: tuple[DragonSong, ...]
    notices: tuple[ChronosNotice, ...]


class SimulatedExecutor:
    """Deterministic executor used by `--simulate`."""

    def __init__(self) -> None:
        self._ids = count(1)

    async def execute(self, command: DragonSong) -> OrderAck:
        qty: OrderQty | float | None = None
        price: Price | float | None = None
        side = None
        if isinstance(command, (PlaceHeadCommand, PlaceTailCommand)):
            qty = _ack_quantity(command.request.orderQty)
            price = _ack_price(
                command.request.price
                if command.request.price is not None
                else command.request.stopPx
            )
            side = command.request.side
        return OrderAck(
            order_id=f"SIM-{next(self._ids)}",
            status="New",
            price=price,
            orig_qty=qty,
            executed_qty=0.0,
            side=side,
        )


class StaticHookSource:
    """One-shot source for planner/tests/simulation fallback."""

    async def pump(self, runtime: StrategyRuntimeLike) -> None:
        current_time = datetime.now(timezone.utc)
        for pair in runtime.strategy.pairs:
            await runtime.enqueue(
                head_hooked_event(
                    pair_name=pair.name,
                    symbol=_pair_symbol_from_pair(pair, runtime.symbol),
                    occurred_at=current_time,
                )
            )


class KrakenPublicTriggerSource:
    def __init__(
        self,
        client: PublicRuntimeStateReader,
        *,
        poll_seconds: float = 0.25,
    ) -> None:
        self.client = client
        self.poll_seconds = poll_seconds
        self._seen_event_ids: set[str] = set()

    async def pump(self, runtime: RuntimeQueueLike) -> None:
        while runtime.running:
            for route in _active_runtime_routes(runtime):
                market = _fetch_market_state_for_route(self.client, route)
                snapshot = MarketSnapshotFact(
                    symbol=route.symbol,
                    best_bid=market.best_bid,
                    best_ask=market.best_ask,
                    mid_price=market.mid_price,
                    occurred_at=datetime.now(timezone.utc),
                    last_price=getattr(market, "last_price", None),
                    mark_price=getattr(market, "mark_price", None),
                    index_price=getattr(market, "index_price", None),
                    tick_size=getattr(market, "tick_size", None),
                    spread=getattr(market, "spread", None),
                )
                for pair_name, pair_state in runtime.state.pairs.items():
                    if _pair_route(pair_state, runtime) != route:
                        continue
                    if pair_state.head_state == HeadState.LATENT:
                        if not pair_dependency_satisfied(runtime.state, pair_state):
                            continue
                        move = head_hooked_from_market_snapshot(
                            pair_state=pair_state,
                            launched_at=runtime.state.launched_at,
                            snapshot=snapshot,
                        )
                        event_prefix = "public-hook"
                    else:
                        if pair_state.tail_trail is None or pair_state.tail_state not in {
                            TailState.HOOKED,
                            TailState.SUBMITTED,
                            TailState.LIVING,
                        }:
                            continue
                        move = market_tick_from_market_snapshot(
                            pair=pair_state.pair,
                            snapshot=snapshot,
                        )
                        event_prefix = "public-market"
                    if move is None:
                        continue
                    reference_key = ""
                    if move.reply is not None:
                        reference_key = f":{move.reply.get('reference_source', '')}:{move.reply.get('reference_price', '')}"
                    event_id = (
                        f"{event_prefix}:{route.label}:{pair_name}:{pair_state.attempt_index}:"
                        f"{market.recorded_at or snapshot.occurred_at.isoformat()}{reference_key}"
                    )
                    if event_id in self._seen_event_ids:
                        continue
                    self._seen_event_ids.add(event_id)
                    await runtime.enqueue(replace(move, event_id=event_id))
            if _runtime_sources_should_stop(runtime):
                return
            await asyncio.sleep(self.poll_seconds)


class KrakenPrivateOrderPollingSource:
    def __init__(
        self,
        client: PrivateOrderStateReader,
        *,
        poll_seconds: float = 0.25,
        head_fill_reference_grace_seconds: float = _DEFAULT_HEAD_FILL_REFERENCE_GRACE_SECONDS,
        pending_record_ttl_seconds: float = _DEFAULT_PRIVATE_PENDING_RECORD_TTL_SECONDS,
        max_pending_records: int = _DEFAULT_PRIVATE_PENDING_RECORD_LIMIT,
        max_suppressed_event_ids: int = _DEFAULT_PRIVATE_SUPPRESSED_EVENT_ID_LIMIT,
    ) -> None:
        self.client = client
        self.poll_seconds = poll_seconds
        self.head_fill_reference_grace_seconds = max(
            0.0, head_fill_reference_grace_seconds
        )
        self.pending_record_ttl_seconds = max(1.0, pending_record_ttl_seconds)
        self.max_pending_records = max(1, max_pending_records)
        self.max_suppressed_event_ids = max(1, max_suppressed_event_ids)
        self._cursors: dict[str, _PrivateCursor] = {}
        self._pending_records: list[_PendingPrivateRecord] = []
        self._suppressed_event_ids: deque[str] = deque()
        self._suppressed_event_id_set: set[str] = set()

    async def pump(self, runtime: RuntimeQueueLike) -> None:
        while runtime.running:
            now = datetime.now(timezone.utc)
            candidates = tuple(self._pending_records)
            self._pending_records = []
            for route in _active_runtime_routes(runtime):
                cursor = self._cursor_for(route.label, runtime.state.launched_at)
                records = self.client.fetch_private_orders_since(
                    after_local_timestamp=cursor.after_local_timestamp,
                    after_local_id=cursor.after_local_id,
                    symbol=route.symbol,
                    exchange=route.exchange,
                    market_type=route.market_type,
                )
                fill_records = self.client.fetch_private_fills_since(
                    after_local_timestamp=cursor.after_fill_timestamp,
                    after_local_id=cursor.after_fill_id,
                    symbol=route.symbol,
                    exchange=route.exchange,
                    market_type=route.market_type,
                )
                active_client_ids, active_exchange_ids = self._active_identity_sets(
                    runtime,
                    route,
                )
                if active_client_ids or active_exchange_ids:
                    fetch_orders_by_identity = getattr(
                        self.client, "fetch_private_orders_for_identities", None
                    )
                    if callable(fetch_orders_by_identity):
                        identity_orders = fetch_orders_by_identity(
                            client_order_ids=active_client_ids,
                            exchange_order_ids=active_exchange_ids,
                            symbol=route.symbol,
                            exchange=route.exchange,
                            market_type=route.market_type,
                        )
                        records = self._merge_unique_private_records(records, identity_orders)
                    fetch_fills_by_identity = getattr(
                        self.client, "fetch_private_fills_for_identities", None
                    )
                    if callable(fetch_fills_by_identity):
                        identity_fills = fetch_fills_by_identity(
                            client_order_ids=active_client_ids,
                            exchange_order_ids=active_exchange_ids,
                            symbol=route.symbol,
                            exchange=route.exchange,
                            market_type=route.market_type,
                        )
                        fill_records = self._merge_unique_private_records(
                            fill_records,
                            identity_fills,
                        )
                candidates = candidates + tuple(
                    _PendingPrivateRecord(record=record, first_seen_at=now)
                    for record in records
                ) + tuple(
                    _PendingPrivateRecord(
                        record=record,
                        first_seen_at=now,
                        is_fill=True,
                    )
                    for record in fill_records
                )
                self._advance_cursor(cursor, records, fill_records)
            candidates, _ = self._dedupe_pending_candidates(candidates)
            for pending_record in candidates:
                record = pending_record.record
                resolved = runtime.pair_state_for_record(record)
                if resolved is None:
                    if self._pending_record_still_fresh(pending_record, now):
                        self._pending_records.append(pending_record)
                    continue
                pair_state, role = resolved
                fact = private_order_fact_from_record(
                    record,
                    pair_name=pair_state.pair.name,
                )
                move = head_move_from_private_fact(fact)
                move = replace(move, role=role)
                event_id = (
                    self._private_record_event_id(
                        record,
                        is_fill=pending_record.is_fill,
                    )
                )
                if _is_mechanical_tail_amend_cancel(runtime, pair_state, role, move):
                    if event_id is not None and event_id in self._suppressed_event_id_set:
                        continue
                    if event_id is not None:
                        self._remember_suppressed_event_id(event_id)
                    _LOGGER.info(
                        "AMEND_REPLACE_CANCEL_SUPPRESSED (%s#%s): %s",
                        pair_state.pair.name,
                        pair_state.attempt_index,
                        _runtime_fields(
                            (move.reply or {}).get("clOrdID", "-"),
                            (move.reply or {}).get("orderID", "-"),
                        ),
                    )
                    continue
                move = self._with_reference_price(
                    move,
                    pair_state,
                    _pair_symbol(pair_state, runtime.symbol),
                )
                if self._must_wait_for_private_fill_reference(move, pair_state, role):
                    if self._reference_price_from_move(move) is None:
                        elapsed_seconds = max(
                            0.0,
                            (now - pending_record.first_seen_at).total_seconds(),
                        )
                        if elapsed_seconds < self.head_fill_reference_grace_seconds:
                            self._pending_records.append(pending_record)
                            continue
                        order_id = record.exchange_order_id or "-"
                        client_id = record.client_order_id or "-"
                        raise RuntimeError(
                            "head fill reference price missing for relative tail after grace window: "
                            f"pair={pair_state.pair.name} clOrdID={client_id} "
                            f"orderID={order_id} grace_seconds={self.head_fill_reference_grace_seconds:g}"
                        )
                await runtime.enqueue(replace(move, event_id=event_id))
            self._trim_pending_records()
            if _runtime_sources_should_stop(runtime):
                return
            await asyncio.sleep(self.poll_seconds)

    def _cursor_for(self, symbol: str, launched_at: datetime) -> _PrivateCursor:
        cursor = self._cursors.setdefault(symbol, _PrivateCursor())
        if not cursor.initialised:
            cursor.after_local_timestamp = launched_at - timedelta(seconds=5)
            cursor.after_local_id = None
            cursor.after_fill_timestamp = launched_at - timedelta(seconds=5)
            cursor.after_fill_id = None
            cursor.initialised = True
        return cursor

    @staticmethod
    def _advance_cursor(
        cursor: _PrivateCursor,
        records: tuple[PrivateOrderRecord, ...],
        fill_records: tuple[PrivateOrderRecord, ...],
    ) -> None:
        for record in records:
            occurred_at = _record_timestamp(record)
            if occurred_at is not None:
                cursor.after_local_timestamp = occurred_at
            cursor.after_local_id = record.local_id
        for record in fill_records:
            occurred_at = _record_timestamp(record)
            if occurred_at is not None:
                cursor.after_fill_timestamp = occurred_at
            cursor.after_fill_id = record.local_id

    @staticmethod
    def _active_identity_sets(
        runtime: RuntimeQueueLike,
        route: ExchangeRoute,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        client_ids: set[str] = set()
        exchange_ids: set[str] = set()
        for pair_state in runtime.state.pairs.values():
            if _pair_route(pair_state, runtime) != route:
                continue
            for identity in (pair_state.head_identity, pair_state.tail_identity):
                if identity is None:
                    continue
                if identity.client_order_id:
                    client_ids.add(identity.client_order_id)
                if identity.exchange_order_id:
                    exchange_ids.add(identity.exchange_order_id)
        if isinstance(runtime, StrategyRuntime):
            for identity in runtime._command_identities().values():
                if not _identity_route_matches(identity, route):
                    continue
                if identity.client_order_id:
                    client_ids.add(identity.client_order_id)
                if identity.exchange_order_id:
                    exchange_ids.add(identity.exchange_order_id)
        return tuple(sorted(client_ids)), tuple(sorted(exchange_ids))

    @staticmethod
    def _merge_unique_private_records(
        primary: tuple[PrivateOrderRecord, ...],
        extra: tuple[PrivateOrderRecord, ...],
    ) -> tuple[PrivateOrderRecord, ...]:
        merged: dict[tuple[object, ...], PrivateOrderRecord] = {}
        for record in primary + extra:
            key = (
                record.exchange,
                record.market_type,
                record.symbol,
                record.local_id,
                record.exchange_order_id,
                record.client_order_id,
                record.local_timestamp,
                record.status,
                record.order_type,
            )
            merged[key] = record
        return tuple(merged.values())

    def _dedupe_pending_candidates(
        self,
        candidates: tuple[_PendingPrivateRecord, ...],
    ) -> tuple[tuple[_PendingPrivateRecord, ...], int]:
        seen: set[str] = set()
        kept: list[_PendingPrivateRecord] = []
        pruned = 0
        for pending_record in candidates:
            key = self._pending_record_key(pending_record)
            if key in seen:
                pruned += 1
                continue
            seen.add(key)
            kept.append(pending_record)
        return tuple(kept), pruned

    def _pending_record_key(self, pending_record: _PendingPrivateRecord) -> str:
        event_id = self._private_record_event_id(
            pending_record.record,
            is_fill=pending_record.is_fill,
        )
        if event_id is not None:
            return event_id
        record = pending_record.record
        prefix = "fill" if pending_record.is_fill else "order"
        return ":".join(
            (
                prefix,
                _event_atom(record.exchange),
                _event_atom(record.market_type),
                _event_atom(record.symbol),
                _event_atom(record.exchange_order_id),
                _event_atom(record.client_order_id),
                _event_atom(record.local_timestamp),
                _event_atom(record.status),
            )
        )

    def _pending_record_still_fresh(
        self,
        pending_record: _PendingPrivateRecord,
        now: datetime,
    ) -> bool:
        elapsed = (now - _as_utc_aware(pending_record.first_seen_at)).total_seconds()
        return elapsed <= self.pending_record_ttl_seconds

    def _trim_pending_records(self) -> int:
        overflow = len(self._pending_records) - self.max_pending_records
        if overflow <= 0:
            return 0
        self._pending_records = sorted(
            self._pending_records,
            key=lambda item: item.first_seen_at,
        )[overflow:]
        return overflow

    def _remember_suppressed_event_id(self, event_id: str) -> None:
        if event_id in self._suppressed_event_id_set:
            return
        self._suppressed_event_ids.append(event_id)
        self._suppressed_event_id_set.add(event_id)
        while len(self._suppressed_event_ids) > self.max_suppressed_event_ids:
            stale = self._suppressed_event_ids.popleft()
            self._suppressed_event_id_set.discard(stale)

    @staticmethod
    def _private_record_event_id(
        record: PrivateOrderRecord,
        *,
        is_fill: bool,
    ) -> str | None:
        prefix = "private-fill" if is_fill else "private-order"
        identity = (
            str(record.local_id)
            if record.local_id is not None
            else (record.exchange_order_id or record.client_order_id)
        )
        if identity is None:
            return None
        # One DB row can represent multiple lifecycle snapshots over time.
        # Include state fingerprint so open -> amend -> close emits distinct events.
        fingerprint = ":".join(
            (
                _event_atom(record.local_timestamp),
                _event_atom(record.source_timestamp),
                _event_atom(record.status),
                _event_atom(record.reason),
                _event_atom(record.price),
                _event_atom(record.stop_price),
                _event_atom(record.filled_quantity),
                _event_atom(record.quantity),
            )
        )
        route = ":".join(
            (
                _event_atom(record.exchange),
                _event_atom(record.market_type),
                _event_atom(record.symbol),
            )
        )
        return f"{prefix}:{route}:{identity}:{fingerprint}"

    def _with_reference_price(
        self,
        move: EggMove,
        pair_state: PairCycleState,
        symbol: str,
    ) -> EggMove:
        """Attach role-sensitive execution/trigger reference from private payload."""
        del symbol
        reply = dict(move.reply or {})
        role = _role_from_move(move)
        fill_price = None
        if role == OrderRole.HEAD:
            for key in ("price", "fillPrice", "avgPx", "lastPx", "executed_price"):
                if key in reply and isinstance(reply[key], (int, float, Decimal, str)):
                    fill_price = reply[key]
                    break
        else:
            for key in ("stopPx", "stop_price", "stopPrice"):
                if key in reply and isinstance(reply[key], (int, float, Decimal, str)):
                    fill_price = reply[key]
                    break
        del pair_state
        if isinstance(fill_price, (int, float, Decimal, str)) and to_decimal(fill_price) > 0:
            reply["reference_price"] = fill_price
            return replace(move, reply=reply)
        return move

    def _must_wait_for_private_fill_reference(
        self,
        move: EggMove,
        pair_state: PairCycleState,
        role: OrderRole,
    ) -> bool:
        if role != OrderRole.HEAD:
            return False
        if move.kind not in {EggMoveKind.PLAYED_NOT_CANCELED, EggMoveKind.PLAYED_AND_CANCELED}:
            return False
        if not _pair_uses_relative_tail(pair_state):
            return False
        if pair_state.tail_trail is not None:
            return False
        played = _played_quantity_from_move(move)
        if played is None or played <= 0:
            return False
        return True

    def _reference_price_from_move(self, move: EggMove) -> Decimal | None:
        payload = move.reply or {}
        value = payload.get("reference_price")
        if isinstance(value, (int, float, Decimal, str)):
            parsed = to_decimal(value)
            if parsed > 0:
                return parsed
        return None


class StrategyRuntime:
    """Persistent async supervisor that owns the active strategy path."""

    def __init__(
        self,
        *,
        strategy: StrategySpec,
        symbol: str,
        executor: CommandExecutor | None = None,
        public_source: RuntimeEventSource | None = None,
        private_source: RuntimeEventSource | None = None,
        public_state_reader: PublicRuntimeStateReader | None = None,
        account_state_reader: AccountBalanceStateReader | None = None,
        tail_telemetry_writer: TailTelemetryWriter | None = None,
        tail_telemetry_interval_seconds: float = 30.0,
        exchange: str = "kraken",
        environment: str = "demo",
        market_type: str = "futures",
        account_scope: str = "default",
        tail_visibility_timeout_seconds: float = 30.0,
        max_active_pairs: int = 4,
        simulate: bool = False,
        instrument_rules_by_route: Mapping[ExchangeRoute, Mapping[str, object]] | None = None,
    ) -> None:
        self.strategy = strategy
        self.symbol = symbol
        self.executor = executor
        self.public_source = public_source
        self.private_source = private_source
        self.public_state_reader = public_state_reader
        self.account_state_reader = account_state_reader
        self.tail_telemetry_writer = tail_telemetry_writer
        self.tail_telemetry_interval_seconds = tail_telemetry_interval_seconds
        self.exchange = exchange
        self.environment = environment
        self.market_type = market_type
        self.account_scope = account_scope
        self.tail_visibility_timeout_seconds = max(0.1, float(tail_visibility_timeout_seconds))
        self.max_active_pairs = max(0, int(max_active_pairs))
        self.simulate = simulate
        self.instrument_rules_by_route = dict(instrument_rules_by_route or {})
        launched_at = datetime.now(timezone.utc)
        self.state = StrategyState(
            launched_at=launched_at,
            pairs={pair.name: self._pair_state(pair) for pair in strategy.pairs},
            strategy_id=strategy.name,
        )
        self.chronos = Chronos(state=self.state)
        self.event_queue: asyncio.Queue[EggMove] = asyncio.Queue()
        self.commands: list[DragonSong] = []
        self.running = False
        self._tasks: list[asyncio.Task[None]] = []
        self._inflight_commands: dict[_CommandSlot, _InFlightCommand] = {}
        self._pending_commands: dict[_CommandSlot, deque[DragonSong]] = {}
        self._pending_head_commands: deque[DragonSong] = deque()
        self._command_errors: list[BaseException] = []
        self._legend_logged = False
        self._last_pair_updates: dict[str, tuple[str, ...]] = {}
        self._last_tail_metrics: dict[str, tuple[str, ...]] = {}
        self._head_order_lifecycle: dict[str, _OrderLifecycleSnapshot] = {}
        self._tail_order_lifecycle: dict[str, _OrderLifecycleSnapshot] = {}
        self._live_command_identities: dict[str, OrderIdentity] = {}
        self._head_fill_deadlines: dict[_CommandSlot, _HeadFillDeadline] = {}
        self._latent_head_deadlines: dict[tuple[str, int], _LatentHeadDeadline] = {}
        self._order_leases: dict[_CommandSlot, _OrderLease] = {}
        self._pending_tail_visibility: dict[_CommandSlot, _TailVisibilityWindow] = {}
        self._pending_tail_amends: dict[_CommandSlot, _TailAmendPending] = {}
        self._last_gate_logs: dict[str, datetime] = {}
        self._last_gate_signatures: dict[str, tuple[str, ...]] = {}
        self._gate_log_interval_seconds = 300.0

    def _pair_state(self, pair):
        from kolabi.bot.domain import PairCycleState

        return PairCycleState(pair=pair)

    @property
    def all_pairs_terminal(self) -> bool:
        now = datetime.now(timezone.utc)
        return all(
            _pair_runtime_complete(
                pair_state,
                launched_at=self.state.launched_at,
                now=now,
            )
            for pair_state in self.state.pairs.values()
        )

    @property
    def should_keep_sources_alive(self) -> bool:
        return bool(self.chronos.pending_repeats or self.active_order_leases())

    def active_order_leases(self) -> tuple[_OrderLease, ...]:
        """Return live exchange-order leases that still need DB evidence."""

        return tuple(
            lease
            for lease in self._order_leases.values()
            if lease.status != _LEASE_CLOSED
        )

    def active_order_identities(self) -> tuple[OrderIdentity, ...]:
        """Expose active live order identities for service-level cleanup."""

        identities: list[OrderIdentity] = []
        for lease in self.active_order_leases():
            if lease.client_order_id is None and lease.exchange_order_id is None:
                continue
            identities.append(
                OrderIdentity(
                    pair_name=lease.pair_name,
                    role=lease.role,
                    client_order_id=lease.client_order_id,
                    exchange_order_id=lease.exchange_order_id,
                    symbol=lease.symbol,
                    exchange=lease.exchange,
                    market_type=lease.market_type,
                )
            )
        return tuple(identities)

    async def enqueue(self, event: EggMove) -> None:
        await self.event_queue.put(event)

    async def start(self) -> None:
        if self.running:
            return
        self.running = True
        _drop_closed_logger_handlers(_LOGGER)
        self._log_runtime_legend_once()
        sources = [
            self.public_source or (StaticHookSource() if self.simulate else None),
            None if self.simulate else self.private_source,
        ]
        self._tasks = [
            asyncio.create_task(source.pump(self))
            for source in sources
            if source is not None
        ]
        if (
            not self.simulate
            and self.public_state_reader is not None
            and self.tail_telemetry_writer is not None
        ):
            self._tasks.append(asyncio.create_task(self._pump_tail_telemetry()))

    async def stop(self) -> None:
        self.running = False
        for task in self._tasks:
            task.cancel()
        for entry in self._inflight_commands.values():
            entry.task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._inflight_commands:
            await asyncio.gather(
                *(entry.task for entry in self._inflight_commands.values()),
                return_exceptions=True,
            )
        self._tasks.clear()
        self._inflight_commands.clear()
        self._pending_commands.clear()
        self._pending_head_commands.clear()
        self._head_fill_deadlines.clear()
        self._latent_head_deadlines.clear()
        self._pending_tail_visibility.clear()
        self._pending_tail_amends.clear()

    async def run(self) -> StrategyRunResult:
        await self.start()
        try:
            while self.running:
                current_time = datetime.now(timezone.utc)
                self.chronos.expire_pending(now=current_time)
                self._reap_source_tasks()
                self._reap_command_tasks()
                if self._command_errors:
                    raise self._command_errors[0]
                self._prune_live_command_identities()
                self._check_latent_head_deadlines(current_time)
                self._check_head_fill_deadlines(current_time)
                self._check_order_lease_deadlines(current_time)
                self._check_tail_visibility_deadlines(current_time)
                self._check_tail_amend_deadlines(current_time)
                self._log_gate_waits(current_time)
                repeat_commands = self.chronos.activate_ready_repeats(
                    symbol=self.symbol,
                    now=current_time,
                )
                if repeat_commands or self.chronos.state is not self.state:
                    previous_pairs = dict(self.state.pairs)
                    previous_state = self.state
                    self.state = self.chronos.state
                    self._prune_latent_head_deadlines()
                    self._log_repeat_attempts(previous_state.pairs)
                    self._check_latent_head_deadlines(current_time)
                    self._log_gate_waits(current_time)
                if repeat_commands:
                    self._log_repeat_start(repeat_commands)
                    self._dispatch_commands(repeat_commands)
                    self._log_living_updates(previous_pairs)
                    self._drain_pending_head_commands()
                if (
                    self.all_pairs_terminal
                    and self.event_queue.empty()
                    and not self._inflight_commands
                    and not self._pending_commands
                    and not self._pending_head_commands
                    and not self._latent_head_deadlines
                    and not self.active_order_leases()
                    and not self.chronos.pending_repeats
                ):
                    break
                try:
                    event = await asyncio.wait_for(self.event_queue.get(), timeout=0.2)
                except TimeoutError:
                    continue
                if self._should_ignore_stale_runtime_cancel(event):
                    continue
                previous_pairs = dict(self.state.pairs)
                previous_repeats = dict(self.chronos.pending_repeats)
                self._record_private_event_for_leases(event)
                self._record_head_lifecycle(event)
                commands = self.chronos.process_event(event)
                self.state = self.chronos.state
                self._prune_latent_head_deadlines()
                self._prune_order_leases()
                self._sync_head_fill_deadline(event)
                self._log_new_pending_repeats(previous_repeats)
                self._log_chain_releases(previous_pairs)
                self._dispatch_commands(commands)
                self._log_living_updates(previous_pairs)
                self._drain_pending_head_commands()
        finally:
            await self.stop()
        return StrategyRunResult(
            state=self.chronos.state,
            commands=tuple(self.commands),
            notices=tuple(self.chronos.notices),
        )

    def _dispatch_commands(self, commands: tuple[DragonSong, ...]) -> None:
        for command in commands:
            try:
                prepared = self._prepare_command(command)
            except QuantityResolutionError as exc:
                self._fail_command_before_dispatch(command, exc)
                continue
            self.commands.append(prepared)
            if self.executor is None:
                continue
            slot = self._command_slot(prepared)
            if isinstance(prepared, PlaceHeadCommand) and not self._head_capacity_available(prepared):
                self._pending_head_commands.append(prepared)
                _LOGGER.info(
                    "HEAD_WAIT (%s#%s): %s",
                    prepared.pair_name,
                    slot.attempt_index,
                    _runtime_fields(self._active_pair_count(), self.max_active_pairs),
                )
                continue
            if slot not in self._inflight_commands:
                self._launch_command(slot, prepared)
                continue
            pending = self._pending_commands.setdefault(slot, deque())
            self._pending_commands[slot] = append_pending_command(pending, prepared)

    def _launch_command(self, slot: _CommandSlot, prepared: DragonSong) -> None:
        identity = self._command_identity_from_command(prepared)
        if identity is not None:
            self._live_command_identities[_identity_key(identity)] = identity
        self._on_command_dispatched(slot, prepared, identity)
        self._inflight_commands[slot] = _InFlightCommand(
            command=prepared,
            task=asyncio.create_task(self._execute_and_enqueue(prepared, slot)),
        )

    async def _execute_and_enqueue(
        self,
        prepared: DragonSong,
        slot: _CommandSlot,
    ) -> None:
        if self.executor is None:
            return
        try:
            ack = await self.executor.execute(prepared)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            if isinstance(prepared, CancelCommand):
                _LOGGER.warning(
                    "COMMAND_FAILED (%s#%s): %s",
                    prepared.pair_name,
                    self._command_slot(prepared).attempt_index,
                    _runtime_fields(prepared.reason, _compact_error(exc)),
                )
                return
            failure = _command_failure_event(
                prepared,
                symbol=str(prepared.symbol),
                occurred_at=datetime.now(timezone.utc),
                error=exc,
            )
            if failure is None:
                raise
            _LOGGER.warning(
                "COMMAND_FAILED (%s#%s): %s",
                prepared.pair_name,
                self._command_slot(prepared).attempt_index,
                _runtime_fields(prepared.reason, _compact_error(exc)),
            )
            await self.enqueue(failure)
            return
        self._record_live_ack(prepared, ack)
        for followup in self._followup_events(prepared, ack, slot=slot):
            await self.enqueue(followup)

    def _reap_command_tasks(self) -> None:
        done_slots: list[_CommandSlot] = []
        for slot, entry in tuple(self._inflight_commands.items()):
            if not entry.task.done():
                continue
            try:
                entry.task.result()
            except asyncio.CancelledError:
                pass
            except BaseException as exc:
                self._command_errors.append(exc)
            done_slots.append(slot)
        for slot in done_slots:
            self._inflight_commands.pop(slot, None)
            self._dispatch_next_pending(slot)

    def _reap_source_tasks(self) -> None:
        alive_tasks: list[asyncio.Task[None]] = []
        for task in self._tasks:
            if not task.done():
                alive_tasks.append(task)
                continue
            try:
                task.result()
            except asyncio.CancelledError:
                continue
            except BaseException as exc:
                self._command_errors.append(exc)
                continue
            if self.running and not self.all_pairs_terminal and not self.simulate:
                self._command_errors.append(
                    RuntimeError("runtime event source stopped before strategy completed")
                )
        self._tasks = alive_tasks

    def _dispatch_next_pending(self, slot: _CommandSlot) -> None:
        pending = self._pending_commands.get(slot)
        if pending is None:
            return
        while pending:
            next_command = pending.popleft()
            if not self._command_slot_still_live(slot):
                continue
            self._launch_command(slot, next_command)
            if pending:
                self._pending_commands[slot] = pending
            else:
                self._pending_commands.pop(slot, None)
            return
        self._pending_commands.pop(slot, None)

    def _drain_pending_head_commands(self) -> None:
        if not self._pending_head_commands:
            return
        deferred: deque[DragonSong] = deque()
        while self._pending_head_commands:
            command = self._pending_head_commands.popleft()
            if not isinstance(command, PlaceHeadCommand):
                deferred.append(command)
                continue
            slot = self._command_slot(command)
            if not self._command_slot_still_live(slot):
                continue
            if not self._head_capacity_available(command):
                deferred.append(command)
                continue
            self._launch_command(slot, command)
        self._pending_head_commands = deferred

    def _head_capacity_available(self, command: PlaceHeadCommand) -> bool:
        return head_capacity_available(
            command,
            state=self.state,
            max_active_pairs=self.max_active_pairs,
            inflight_commands=self._inflight_command_policy_items(),
            now=datetime.now(timezone.utc),
        )

    def _active_pair_count(self) -> int:
        return active_pair_count(
            self.state,
            inflight_commands=self._inflight_command_policy_items(),
            now=datetime.now(timezone.utc),
        )

    def _active_pair_names(self) -> set[str]:
        return set(
            active_pair_names(
                self.state,
                inflight_commands=self._inflight_command_policy_items(),
                now=datetime.now(timezone.utc),
            )
        )

    def _command_slot(self, command: DragonSong) -> _CommandSlot:
        return command_slot(command, state=self.state)

    def _command_slot_still_live(self, slot: _CommandSlot) -> bool:
        return command_slot_still_live(slot, state=self.state)

    def _inflight_command_policy_items(self) -> tuple[tuple[_CommandSlot, DragonSong], ...]:
        return tuple(
            (slot, entry.command)
            for slot, entry in self._inflight_commands.items()
        )

    async def _pump_tail_telemetry(self) -> None:
        interval = max(self.tail_telemetry_interval_seconds, 1.0)
        while self.running:
            now = datetime.now(timezone.utc)
            rows = self._collect_tail_telemetry_rows(now)
            if rows and self.tail_telemetry_writer is not None:
                try:
                    self.tail_telemetry_writer.record_rows(rows)
                except Exception as exc:
                    _LOGGER.warning(
                        "tail telemetry persistence failed rows=%s error=%s",
                        len(rows),
                        _compact_error(exc),
                    )
            for row in rows:
                source = "unknown"
                pair_state = self.state.pairs[row.pair_name]
                market = (
                    None
                    if self.public_state_reader is None
                    else _fetch_market_state_for_route(
                        self.public_state_reader,
                        _pair_route(pair_state, self),
                    )
                )
                if market is not None:
                    source, _ = tail_reference_price(pair_state.pair, market)
                signature = (
                    str(self.state.pairs[row.pair_name].attempt_index),
                    row.head_state,
                    row.tail_state,
                    _fmt_compact_price(row.reference_price),
                    _fmt_compact_price(row.stop_price),
                    _fmt_compact_price(row.initial_distance),
                    _fmt_compact_price(row.current_distance),
                    _fmt_compact_price(row.spread_guard),
                    _fmt_compact_price(row.unblock_requirement),
                    row.last_tail_update_at.isoformat() if row.last_tail_update_at is not None else "-",
                    source,
                    _fmt_compact_price(None if market is None else getattr(market, "best_bid", None)),
                    _fmt_compact_price(None if market is None else getattr(market, "best_ask", None)),
                    _fmt_compact_price(None if market is None else getattr(market, "mid_price", None)),
                    _fmt_compact_price(None if market is None else getattr(market, "last_price", None)),
                    _fmt_compact_price(None if market is None else getattr(market, "mark_price", None)),
                    _fmt_compact_price(None if market is None else getattr(market, "index_price", None)),
                )
                if self._last_tail_metrics.get(row.pair_name) == signature:
                    continue
                self._last_tail_metrics[row.pair_name] = signature
                _LOGGER.info(
                    "METRICS (%s#%s): %s",
                    row.pair_name,
                    signature[0],
                    _runtime_fields(
                        f"{row.head_state}--{row.tail_state}",
                        signature[3],
                        signature[4],
                        signature[5],
                        signature[6],
                        signature[7],
                        signature[8],
                        signature[9],
                        signature[10],
                        signature[11],
                        signature[12],
                        signature[13],
                        signature[14],
                        signature[15],
                        signature[16],
                    ),
                )
            await asyncio.sleep(interval)

    def _collect_tail_telemetry_rows(self, now: datetime) -> tuple[TailTelemetryRow, ...]:
        reader = self.public_state_reader
        if reader is None:
            return ()
        rows: list[TailTelemetryRow] = []
        for pair_name, pair_state in self.state.pairs.items():
            if (
                pair_state.tail_trail is None
                or pair_state.tail_state not in {TailState.HOOKED, TailState.SUBMITTED, TailState.LIVING}
            ):
                continue
            route = _pair_route(pair_state, self)
            symbol = route.symbol
            market = _fetch_market_state_for_route(reader, route)
            _, ref = tail_reference_price(pair_state.pair, market)
            if ref <= 0:
                continue
            stop = pair_state.tail_trail.confirmed_stop_price
            if stop is None:
                continue
            current_distance = _tail_signed_distance(pair_state, to_decimal(ref), stop)
            spread_guard = tail_first_jump_guard_width(
                pair_state.tail_trail,
                getattr(market, "tick_size", None) or pair_state.instrument_tick_size,
                getattr(market, "spread", None),
            )
            unblock_requirement = tail_unblock_requirement(
                pair_state.pair,
                pair_state.tail_trail,
                spread_guard,
            )
            rows.append(
                TailTelemetryRow(
                    exchange=route.exchange,
                    environment=self.environment,
                    market_type=route.market_type,
                    account_scope=self.account_scope,
                    strategy_id=self.state.strategy_id,
                    pair_name=pair_name,
                    symbol=symbol,
                    head_state=pair_state.head_state.value,
                    tail_state=pair_state.tail_state.value,
                    tail_mode=None if pair_state.tail_mode is None else pair_state.tail_mode.value,
                    reference_price=float(ref),
                    stop_price=float(stop),
                    initial_distance=float(pair_state.tail_trail.baseline_width),
                    current_distance=float(current_distance),
                    spread_guard=float(spread_guard),
                    unblock_requirement=float(unblock_requirement),
                    last_tail_update_at=pair_state.tail_trail.last_confirmed_at,
                    recorded_at=now,
                )
            )
        return tuple(rows)

    def _log_living_updates(self, previous_pairs: dict[str, PairCycleState]) -> None:
        for pair_name, current in self.state.pairs.items():
            previous = previous_pairs.get(pair_name)
            if previous is None:
                continue
            if (
                current.head_state == HeadState.FAILED
                and previous.head_state != HeadState.FAILED
            ):
                identity = current.head_identity
                played_qty = (
                    str(current.played_quantity)
                    if current.played_quantity is not None
                    else "-"
                )
                head_cancel_signature = (
                    str(current.attempt_index),
                    current.head_state.value,
                    played_qty,
                    "-"
                    if identity is None or identity.client_order_id is None
                    else identity.client_order_id,
                    "-"
                    if identity is None or identity.exchange_order_id is None
                    else identity.exchange_order_id,
                )
                if self._last_pair_updates.get(pair_name) != head_cancel_signature:
                    self._last_pair_updates[pair_name] = head_cancel_signature
                    _LOGGER.info(
                        "HEAD_CANCELLED (%s#%s): %s",
                        pair_name,
                        head_cancel_signature[0],
                        _runtime_fields(
                            head_cancel_signature[2],
                            head_cancel_signature[3],
                            head_cancel_signature[4],
                        ),
                    )
                continue
            if current.head_state not in {HeadState.LIVING, HeadState.CLOSED} and current.tail_state not in {
                TailState.LIVING,
                TailState.SUBMITTED,
            }:
                continue
            quantity_changed = current.played_quantity != previous.played_quantity
            stop_previous = _confirmed_tail_stop(previous)
            stop_current = _confirmed_tail_stop(current)
            desired_stop_previous = (
                None if previous.tail_trail is None else previous.tail_trail.current_stop_price
            )
            desired_stop = (
                None if current.tail_trail is None else current.tail_trail.current_stop_price
            )
            stop_changed = stop_current != stop_previous
            desired_stop_changed = desired_stop != desired_stop_previous
            state_changed = (
                current.head_state != previous.head_state
                or current.tail_state != previous.tail_state
            )
            if not (quantity_changed or stop_changed or desired_stop_changed or state_changed):
                continue
            head_lifecycle = self._head_order_lifecycle.get(pair_name, _OrderLifecycleSnapshot())
            update_signature: tuple[str, ...]
            message: str
            message_args: tuple[object, ...]
            transition = (
                current.head_state.value,
                current.tail_state.value if current.tail_state is not None else "-",
            )
            played_qty = str(current.played_quantity) if current.played_quantity is not None else "-"
            if transition == ("closed", "hooked"):
                update_signature = (
                    str(current.attempt_index),
                    transition[0],
                    transition[1],
                    played_qty,
                    _fmt_compact_price(desired_stop),
                    head_lifecycle.side or "-",
                    _fmt_compact_price(head_lifecycle.filled_qty),
                    _fmt_compact_price(head_lifecycle.filled_price),
                    head_lifecycle.filled_at.isoformat()
                    if head_lifecycle.filled_at is not None
                    else "-",
                )
                message = "UPDATE (%s#%s): %s"
                message_args = (
                    pair_name,
                    update_signature[0],
                    _runtime_fields(
                        f"{update_signature[1]}--{update_signature[2]}",
                        update_signature[3],
                        update_signature[4],
                        update_signature[5],
                        update_signature[6],
                        update_signature[7],
                        update_signature[8],
                    ),
                )
            elif transition[0] == "closed" and transition[1] in {"closed", "failed"}:
                tail_lifecycle = self._tail_order_lifecycle.get(
                    pair_name,
                    _OrderLifecycleSnapshot(),
                )
                update_signature = (
                    str(current.attempt_index),
                    transition[0],
                    transition[1],
                    played_qty,
                    _fmt_compact_price(stop_current),
                    _fmt_compact_price(desired_stop),
                    tail_lifecycle.side or "-",
                    _fmt_compact_price(tail_lifecycle.filled_qty),
                    _fmt_compact_price(tail_lifecycle.filled_price),
                    tail_lifecycle.filled_at.isoformat()
                    if tail_lifecycle.filled_at is not None
                    else "-",
                )
                message = "UPDATE (%s#%s): %s"
                message_args = (
                    pair_name,
                    update_signature[0],
                    _runtime_fields(
                        f"{update_signature[1]}--{update_signature[2]}",
                        update_signature[3],
                        update_signature[4],
                        update_signature[5],
                        update_signature[6],
                        update_signature[7],
                        update_signature[8],
                        update_signature[9],
                    ),
                )
            elif transition in {("closed", "submitted"), ("closed", "living")}:
                tail_identity = current.tail_identity
                update_signature = (
                    str(current.attempt_index),
                    transition[0],
                    transition[1],
                    played_qty,
                    _fmt_compact_price(stop_current),
                    _fmt_compact_price(desired_stop),
                    "-"
                    if tail_identity is None or tail_identity.client_order_id is None
                    else tail_identity.client_order_id,
                    "-"
                    if tail_identity is None or tail_identity.exchange_order_id is None
                    else tail_identity.exchange_order_id,
                    "-"
                    if current.tail_trail is None or current.tail_trail.last_confirmed_at is None
                    else current.tail_trail.last_confirmed_at.isoformat(),
                )
                message = "UPDATE (%s#%s): %s"
                message_args = (
                    pair_name,
                    update_signature[0],
                    _runtime_fields(
                        f"{update_signature[1]}--{update_signature[2]}",
                        update_signature[3],
                        update_signature[4],
                        update_signature[5],
                        update_signature[6],
                        update_signature[7],
                        update_signature[8],
                    ),
                )
            else:
                update_signature = (
                    str(current.attempt_index),
                    transition[0],
                    transition[1],
                    played_qty,
                    _fmt_compact_price(stop_current),
                    _fmt_compact_price(desired_stop),
                )
                message = "UPDATE (%s#%s): %s"
                message_args = (
                    pair_name,
                    update_signature[0],
                    _runtime_fields(
                        f"{update_signature[1]}--{update_signature[2]}",
                        update_signature[3],
                        update_signature[4],
                        update_signature[5],
                    ),
                )
            if self._last_pair_updates.get(pair_name) == update_signature:
                continue
            self._last_pair_updates[pair_name] = update_signature
            _LOGGER.info(message, *message_args)

    def _log_runtime_legend_once(self) -> None:
        if self._legend_logged:
            return
        self._legend_logged = True
        legends = (
            (
                "GENERAL",
                (
                    "AI=attempt_index",
                    "PU=pair_update",
                    "HS=head_state",
                    "TS=tail_state",
                    "PQ=played_qty",
                    "CS=confirmed_stop",
                    "DS=desired_stop",
                    "ID=initial_dist",
                    "CD=current_dist",
                    "LU=last_update",
                    "TCID=tail_client_id",
                    "TOID=tail_order_id",
                    "TLU=tail_last_update",
                    "HCID=head_client_id",
                    "HOID=head_order_id",
                    "HFS/HFQ/HFP/HFT=head_fill_fields(closed--hooked)",
                    "TFS/TFQ/TFP/TFT=tail_fill_fields(closed--closed)",
                ),
            ),
            ("UPDATE", ("HS--TS", "PQ", "CS/DS", "ids/fill_fields/as_applicable")),
            (
                "METRICS",
                (
                    "HS--TS",
                    "ref",
                    "stop",
                    "ID",
                    "CD",
                    "SG=spread_guard",
                    "UR=unblock_requirement",
                    "LU",
                    "src",
                    "bid",
                    "ask",
                    "mid",
                    "last",
                    "mark",
                    "index",
                ),
            ),
            ("GATE_WAIT-1", ("status", "hook/window", "oType", "hPrice", "tOut")),
            (
                "GATE_WAIT-2",
                ("status", "src", "ref", "base", "move", "gate", "unit", "oType", "hPrice", "tOut"),
            ),
            ("GATE_WAIT-ERR", ("status", "symbol", "error")),
            ("LATENT_TIMEOUT_ARMED", ("deadline",)),
            ("LATENT_TIMEOUT", ("waited", "reason")),
            ("HEAD_WAIT", ("active_pairs", "max_active_pairs")),
            ("HEAD_SENT", ("HCID", "side", "type", "qty", "price", "stop")),
            ("HEAD_ACK", ("HCID", "HOID", "tOut_deadline")),
            ("HEAD_TIMEOUT", ("status", "waited", "HCID", "HOID")),
            ("HEAD_VISIBILITY_TIMEOUT_ARMED", ("HCID", "HOID", "tOut_deadline")),
            ("HEAD_VISIBILITY_TIMEOUT", ("status", "waited", "HCID", "HOID")),
            ("HEAD_CANCEL_SENT", ("CID", "reason")),
            ("CANCEL_SENT", ("CID", "reason")),
            ("HEAD_CANCELLED", ("PQ", "HCID", "HOID")),
            ("HEAD_CANCEL_ACK_STALE", ("current_attempt", "CID")),
            ("LEASE_OPEN", ("role", "HCID/TCID", "HOID/TOID", "status")),
            ("LEASE_CLOSED", ("role", "HCID/TCID", "HOID/TOID", "status")),
            ("CANCEL_PENDING", ("role", "CID", "reason")),
            ("CANCEL_RETRY", ("role", "CID", "reason")),
            ("ORDER_SAFETY_BLOCKED", ("role", "status", "reason")),
            ("TAIL_PENDING", ("TCID", "TOID", "waited", "timeout")),
            ("TAIL_VISIBLE", ("TCID", "TOID", "waited")),
            ("AMEND_SENT", ("CS", "DS", "ref", "src", "TCID", "TOID")),
            ("AMEND_PENDING", ("CS", "DS", "TCID", "TOID", "waited")),
            ("AMEND_REJECTED", ("status", "TCID", "TOID")),
            ("COMMAND_FAILED", ("reason", "error")),
            ("REPEAT_WAIT", ("ready_at",)),
            ("REPEAT_READY", ("status", "window", "baseline")),
            ("REPEAT_START", ("commands",)),
            ("CHAIN_READY", ("origin", "status", "closed_at")),
        )
        for name, fields in legends:
            _LOGGER.info("LEGEND--%s: %s", name, _legend_fields(*fields))

    def _log_gate_waits(self, now: datetime) -> None:
        """Log latent head gates so quiet strategies are observable."""
        if self.public_state_reader is None:
            return
        for pair_name, pair_state in self.state.pairs.items():
            if pair_state.head_state != HeadState.LATENT:
                continue
            pair = pair_state.pair
            if pair.hook_name and not pair_dependency_satisfied(self.state, pair_state):
                self._emit_gate_wait(
                    pair_name=pair_name,
                    pair_state=pair_state,
                    now=now,
                    signature=("chain_wait", pair.hook_name),
                    label="GATE_WAIT-1",
                    values=(
                        "chain_wait",
                        pair.hook_name,
                        pair.head.order_type,
                        _fmt_compact_price(
                            None
                            if pair.head_order_price_spec is None
                            else Decimal(str(pair.head_order_price_spec))
                        ),
                        "-" if pair.timeout_minutes is None else pair.timeout_minutes,
                    ),
                )
                continue
            if not pair_window_is_open(pair, launched_at=self.state.launched_at, now=now):
                status = "window_closed" if pair_window_has_ended(
                    pair,
                    launched_at=self.state.launched_at,
                    now=now,
                ) else "window_wait"
                self._emit_gate_wait(
                    pair_name=pair_name,
                    pair_state=pair_state,
                    now=now,
                    signature=(status,),
                    label="GATE_WAIT-1",
                    values=(
                        status,
                        f"{pair.window.start_minutes}..{pair.window.end_minutes}",
                        pair.head.order_type,
                        _fmt_compact_price(
                            None
                            if pair.head_order_price_spec is None
                            else Decimal(str(pair.head_order_price_spec))
                        ),
                        "-" if pair.timeout_minutes is None else pair.timeout_minutes,
                    ),
                )
                continue
            route = _pair_route(pair_state, self)
            symbol = route.symbol
            try:
                market = _fetch_market_state_for_route(self.public_state_reader, route)
                source, reference = executable_head_reference_price(pair, market)
            except Exception as exc:
                _LOGGER.warning(
                    "GATE_WAIT-ERR (%s#%s): %s",
                    pair_name,
                    pair_state.attempt_index,
                    _runtime_fields(
                        "market_unavailable",
                        symbol,
                        " ".join(str(exc).split()),
                    ),
                )
                continue
            if reference <= 0:
                continue
            current = Decimal(str(reference))
            measurement = _head_gate_measure(pair_state, current)
            low = Decimal(str(pair.head_price[0]))
            high = Decimal(str(pair.head_price[1]))
            unit = _head_gate_unit(pair_state)
            status = _head_gate_status(measurement, low, high)
            baseline = pair_state.head_trigger_reference_price
            signature = (
                status,
                source,
                _fmt_compact_price(current),
                _fmt_compact_price(measurement),
                _fmt_compact_price(baseline),
                _fmt_compact_price(low),
                _fmt_compact_price(high),
                unit,
            )
            self._emit_gate_wait(
                pair_name=pair_name,
                pair_state=pair_state,
                now=now,
                signature=signature,
                label="GATE_WAIT-2",
                values=(
                    status,
                    source,
                    _fmt_compact_price(current),
                    _fmt_compact_price(baseline),
                    _fmt_compact_price(measurement),
                    f"{_fmt_compact_price(low)}..{_fmt_compact_price(high)}",
                    unit,
                    pair.head.order_type,
                    _fmt_compact_price(
                        None
                        if pair.head_order_price_spec is None
                        else Decimal(str(pair.head_order_price_spec))
                    ),
                    "-" if pair.timeout_minutes is None else pair.timeout_minutes,
                ),
            )

    def _emit_gate_wait(
        self,
        *,
        pair_name: str,
        pair_state: PairCycleState,
        now: datetime,
        signature: tuple[str, ...],
        label: str,
        values: tuple[object, ...],
    ) -> None:
        key = f"{pair_name}#{pair_state.attempt_index}"
        last_at = self._last_gate_logs.get(key)
        previous_signature = self._last_gate_signatures.get(key)
        if (
            last_at is not None
            and previous_signature == signature
            and (now - last_at).total_seconds() < self._gate_log_interval_seconds
        ):
            return
        self._last_gate_logs[key] = now
        self._last_gate_signatures[key] = signature
        _LOGGER.info(
            "%s (%s#%s): %s",
            label,
            pair_name,
            pair_state.attempt_index,
            _runtime_fields(*values),
        )

    def _record_head_lifecycle(self, move: EggMove) -> None:
        if move.role not in {OrderRole.HEAD, OrderRole.TAIL} or move.reply is None:
            return
        pair_name = move.pair_name or resolve_pair_name(self.state, move)
        if pair_name is None:
            return
        reply = move.reply
        side = reply.get("side")
        side_value = side.lower() if isinstance(side, str) and side else None
        filled_qty = _filled_quantity_from_move_payload(reply)
        filled_price = _head_fill_price_from_move_payload(reply)
        if side_value is None:
            pair_state = self.state.pairs.get(pair_name)
            if pair_state is not None:
                side = (
                    pair_state.pair.head.side
                    if move.role == OrderRole.HEAD
                    else pair_state.pair.tail.side
                )
                side_value = side.value
        snapshots = (
            self._head_order_lifecycle
            if move.role == OrderRole.HEAD
            else self._tail_order_lifecycle
        )
        previous = snapshots.get(pair_name, _OrderLifecycleSnapshot())
        snapshots[pair_name] = _OrderLifecycleSnapshot(
            side=side_value,
            filled_qty=filled_qty if filled_qty is not None else previous.filled_qty,
            filled_price=filled_price if filled_price is not None else previous.filled_price,
            filled_at=(
                move.occurred_at
                if (filled_qty is not None or filled_price is not None)
                else previous.filled_at
            ),
        )

    def _record_private_event_for_leases(self, move: EggMove) -> None:
        if not move.is_private or move.role not in {OrderRole.HEAD, OrderRole.TAIL}:
            return
        pair_name = move.pair_name or resolve_pair_name(self.state, move)
        if pair_name is None:
            return
        pair_state = self.state.pairs.get(pair_name)
        if pair_state is None:
            return
        role = "head" if move.role == OrderRole.HEAD else "tail"
        slot = _CommandSlot(
            pair_name=pair_name,
            attempt_index=pair_state.attempt_index,
            role=role,
        )
        lease = self._order_leases.get(slot)
        if lease is None:
            lease = self._lease_from_private_event(slot, move)
        if lease is None:
            return
        updated = self._lease_with_private_event(lease, move)
        self._order_leases[slot] = updated
        if lease.status != updated.status:
            log = _LOGGER.info if updated.status == _LEASE_CLOSED else _LOGGER.debug
            log(
                "LEASE_%s (%s#%s): %s",
                "CLOSED" if updated.status == _LEASE_CLOSED else "OPEN",
                updated.pair_name,
                updated.attempt_index,
                _runtime_fields(
                    updated.role,
                    updated.client_order_id or "-",
                    updated.exchange_order_id or "-",
                    updated.status,
                ),
            )

    def _lease_from_private_event(
        self,
        slot: _CommandSlot,
        move: EggMove,
    ) -> _OrderLease | None:
        reply = move.reply or {}
        client_order_id = _string_payload(reply.get("clOrdID"))
        exchange_order_id = _string_payload(reply.get("orderID"))
        if client_order_id is None and exchange_order_id is None:
            return None
        return _OrderLease(
            pair_name=slot.pair_name,
            attempt_index=slot.attempt_index,
            role=slot.role,
            symbol=move.symbol or self.symbol,
            client_order_id=client_order_id,
            exchange_order_id=exchange_order_id,
            side=_string_payload(reply.get("side")),
            quantity=_decimal_payload(reply.get("orderQty")),
            price=_decimal_payload(reply.get("price")),
            stop_price=_decimal_payload(reply.get("stopPx")),
            status=_LEASE_PENDING_PLACE,
            created_at=_as_utc_aware(move.occurred_at),
            exchange=self.state.pairs[slot.pair_name].pair.exchange,
            market_type=self.state.pairs[slot.pair_name].pair.market_type,
        )

    def _lease_with_private_event(
        self,
        lease: _OrderLease,
        move: EggMove,
    ) -> _OrderLease:
        reply = move.reply or {}
        status = _private_order_lease_status(_string_payload(reply.get("ordStatus")))
        if lease.status == _LEASE_CANCEL_REQUESTED and status == _LEASE_LIVE:
            status = _LEASE_CANCEL_REQUESTED
        return replace(
            lease,
            client_order_id=_string_payload(reply.get("clOrdID")) or lease.client_order_id,
            exchange_order_id=_string_payload(reply.get("orderID")) or lease.exchange_order_id,
            side=_string_payload(reply.get("side")) or lease.side,
            quantity=_decimal_payload(reply.get("orderQty")) or lease.quantity,
            price=_decimal_payload(reply.get("price")) or lease.price,
            stop_price=_decimal_payload(reply.get("stopPx")) or lease.stop_price,
            status=status or lease.status,
            last_seen_at=_as_utc_aware(move.occurred_at),
        )

    def _upsert_order_lease_from_place_command(
        self,
        slot: _CommandSlot,
        command: PlaceHeadCommand | PlaceTailCommand,
        identity: OrderIdentity | None,
    ) -> None:
        if self.simulate:
            return
        now = datetime.now(timezone.utc)
        request = command.request
        role = "head" if isinstance(command, PlaceHeadCommand) else "tail"
        lease = _OrderLease(
            pair_name=command.pair_name,
            attempt_index=slot.attempt_index,
            role=role,
            symbol=str(command.symbol),
            client_order_id=None if identity is None else identity.client_order_id,
            exchange_order_id=None if identity is None else identity.exchange_order_id,
            side=request.side,
            quantity=_decimal_payload(request.orderQty),
            price=_decimal_payload(request.price),
            stop_price=_decimal_payload(request.stopPx),
            status=_LEASE_PENDING_PLACE,
            created_at=now,
            exchange=command.exchange or self.exchange,
            market_type=command.market_type or self.market_type,
        )
        self._order_leases[_CommandSlot(command.pair_name, slot.attempt_index, role)] = lease
        _LOGGER.info(
            "LEASE_OPEN (%s#%s): %s",
            lease.pair_name,
            lease.attempt_index,
            _runtime_fields(
                lease.role,
                lease.client_order_id or "-",
                lease.exchange_order_id or "-",
                lease.status,
            ),
        )

    def _enrich_order_lease_from_ack(self, command: DragonSong, ack: OrderAck) -> None:
        if self.simulate or not isinstance(command, (PlaceHeadCommand, PlaceTailCommand)):
            return
        pair_state = self.state.pairs.get(command.pair_name)
        attempt_index = 1 if pair_state is None else pair_state.attempt_index
        role = "head" if isinstance(command, PlaceHeadCommand) else "tail"
        slot = _CommandSlot(command.pair_name, attempt_index, role)
        lease = self._order_leases.get(slot)
        if lease is None:
            return
        next_status = lease.status
        if lease.status == _LEASE_PENDING_PLACE and not _ack_is_rejected(ack):
            next_status = _LEASE_ACKED
        updated = replace(
            lease,
            client_order_id=ack.client_order_id or lease.client_order_id,
            exchange_order_id=str(ack.order_id) if ack.order_id else lease.exchange_order_id,
            status=next_status,
            last_seen_at=datetime.now(timezone.utc),
        )
        self._order_leases[slot] = updated
        if role == "head" and isinstance(command, PlaceHeadCommand):
            self._arm_head_fill_deadline_from_ack(slot, command, ack, updated)

    def _arm_head_fill_deadline_from_ack(
        self,
        slot: _CommandSlot,
        command: PlaceHeadCommand,
        ack: OrderAck,
        lease: _OrderLease,
    ) -> None:
        pair_state = self.state.pairs.get(command.pair_name)
        if pair_state is None or pair_state.attempt_index != slot.attempt_index:
            return
        timeout_minutes = pair_state.pair.timeout_minutes
        if timeout_minutes is None or timeout_minutes <= 0:
            return
        if slot in self._head_fill_deadlines:
            return
        if _ack_is_rejected(ack):
            return
        started_at = _as_utc_aware(lease.last_seen_at or datetime.now(timezone.utc))
        deadline = _HeadFillDeadline(
            pair_name=command.pair_name,
            attempt_index=slot.attempt_index,
            client_order_id=lease.client_order_id or command.request.clOrdID,
            exchange_order_id=lease.exchange_order_id,
            started_at=started_at,
            deadline_at=started_at + timedelta(minutes=float(timeout_minutes)),
        )
        self._head_fill_deadlines[slot] = deadline
        _LOGGER.info(
            "HEAD_ACK (%s#%s): %s",
            command.pair_name,
            slot.attempt_index,
            _runtime_fields(
                deadline.client_order_id or "-",
                deadline.exchange_order_id or "-",
                deadline.deadline_at.isoformat(),
            ),
        )

    def _mark_cancel_requested_from_command(self, command: CancelCommand) -> None:
        cancel_id = command.request.clOrdID
        now = datetime.now(timezone.utc)
        for slot, lease in tuple(self._order_leases.items()):
            if lease.status == _LEASE_CLOSED:
                continue
            if lease.pair_name != command.pair_name:
                continue
            if not _command_route_matches_lease(
                command,
                lease,
                self.exchange,
                self.market_type,
            ):
                continue
            if not _lease_matches_identity(
                lease,
                client_order_id=cancel_id,
                exchange_order_id=cancel_id,
            ):
                continue
            self._order_leases[slot] = replace(
                lease,
                status=_LEASE_CANCEL_REQUESTED,
                cancel_sent_at=now,
                cancel_retry_count=lease.cancel_retry_count + 1,
            )
            _LOGGER.info(
                "CANCEL_PENDING (%s#%s): %s",
                lease.pair_name,
                lease.attempt_index,
                _runtime_fields(lease.role, cancel_id, command.reason),
            )
            return

    def _record_cancel_ack(self, command: CancelCommand, ack: OrderAck) -> None:
        if self.simulate:
            return
        cancel_id = command.request.clOrdID
        slot, lease = self._lease_for_cancel(command, cancel_id)
        if lease is None or slot is None:
            return
        terminal_notfound = command.reason == "head_timeout" and _ack_is_zero_fill_notfound_cancel(
            ack
        )
        status = _LEASE_CLOSED if terminal_notfound else lease.status
        self._order_leases[slot] = replace(
            lease,
            status=status,
            last_seen_at=datetime.now(timezone.utc),
        )
        if terminal_notfound:
            self._head_fill_deadlines.pop(slot, None)
        label = "HEAD_CANCEL_NOT_FOUND" if terminal_notfound else "HEAD_CANCEL_ACK"
        _LOGGER.info(
            "%s (%s#%s): %s",
            label,
            lease.pair_name,
            lease.attempt_index,
            _runtime_fields(
                ack.status or "-",
                cancel_id,
                f"retry={lease.cancel_retry_count}",
                _fmt_compact_price(ack.executed_qty),
                ack.reason or "-",
            ),
        )

    def _lease_for_cancel(
        self,
        command: CancelCommand,
        cancel_id: str,
    ) -> tuple[_CommandSlot | None, _OrderLease | None]:
        for slot, lease in tuple(self._order_leases.items()):
            if lease.status == _LEASE_CLOSED:
                continue
            if lease.pair_name != command.pair_name:
                continue
            if not _command_route_matches_lease(
                command,
                lease,
                self.exchange,
                self.market_type,
            ):
                continue
            if _lease_matches_identity(
                lease,
                client_order_id=cancel_id,
                exchange_order_id=cancel_id,
            ):
                return slot, lease
        return None, None

    def _check_order_lease_deadlines(self, now: datetime) -> None:
        if self.simulate:
            return
        now = _as_utc_aware(now)
        for lease in tuple(self._order_leases.values()):
            if lease.status == _LEASE_CLOSED:
                continue
            pair_state = self.state.pairs.get(lease.pair_name)
            if pair_state is None or pair_state.attempt_index != lease.attempt_index:
                continue
            if lease.role == "head" and lease.status == _LEASE_PENDING_PLACE:
                waited = (now - lease.created_at).total_seconds()
                if waited >= self.tail_visibility_timeout_seconds:
                    if lease.visibility_warned_at is None:
                        _LOGGER.warning(
                            "HEAD_VISIBILITY_PENDING (%s#%s): %s",
                            lease.pair_name,
                            lease.attempt_index,
                            _runtime_fields(
                                lease.role,
                                lease.client_order_id or "-",
                                lease.exchange_order_id or "-",
                                f"{waited:.1f}s",
                                f"{self.tail_visibility_timeout_seconds:.1f}s",
                            ),
                        )
                        slot = _CommandSlot(
                            pair_name=lease.pair_name,
                            attempt_index=lease.attempt_index,
                            role=lease.role,
                        )
                        current = self._order_leases.get(slot, lease)
                        self._order_leases[slot] = replace(
                            current,
                            visibility_warned_at=now,
                        )
                    self._arm_head_fill_deadline_from_visibility(lease, pair_state, now)
                    continue
            if lease.role == "head" and lease.status == _LEASE_ACKED:
                waited = (now - lease.created_at).total_seconds()
                if (
                    waited >= self.tail_visibility_timeout_seconds
                    and lease.visibility_warned_at is None
                ):
                    _LOGGER.warning(
                        "HEAD_VISIBILITY_PENDING (%s#%s): %s",
                        lease.pair_name,
                        lease.attempt_index,
                        _runtime_fields(
                            lease.role,
                            lease.client_order_id or "-",
                            lease.exchange_order_id or "-",
                            f"{waited:.1f}s",
                            f"{self.tail_visibility_timeout_seconds:.1f}s",
                        ),
                    )
                    slot = _CommandSlot(
                        pair_name=lease.pair_name,
                        attempt_index=lease.attempt_index,
                        role=lease.role,
                    )
                    current = self._order_leases.get(slot, lease)
                    self._order_leases[slot] = replace(
                        current,
                        visibility_warned_at=now,
                    )
                    continue
            if lease.status != _LEASE_CANCEL_REQUESTED:
                continue
            if lease.cancel_sent_at is not None and (
                now - _as_utc_aware(lease.cancel_sent_at)
            ).total_seconds() < self._head_cancel_retry_seconds():
                continue
            if (
                lease.role == "head"
                and lease.cancel_retry_count >= _MAX_HEAD_CANCEL_RETRIES
            ):
                self._terminate_unconfirmed_head_cancel(lease, now)
                continue
            self._dispatch_lease_cancel(lease, reason="cancel_retry", now=now)

    def _terminate_unconfirmed_head_cancel(
        self,
        lease: _OrderLease,
        now: datetime,
    ) -> None:
        pair_state = self.state.pairs.get(lease.pair_name)
        if pair_state is None or pair_state.attempt_index != lease.attempt_index:
            return
        cancel_id = lease.exchange_order_id or lease.client_order_id
        if not cancel_id:
            return
        command = _head_timeout_cancel_command(
            pair_state,
            _pair_symbol(pair_state, self.symbol),
            cancel_id,
        )
        ack = OrderAck(
            order_id=cancel_id,
            status="NotFound",
            executed_qty=0.0,
            reason="head_unconfirmed_timeout",
        )
        event = _head_timeout_cancel_event_from_ack(
            command,
            ack,
            symbol=lease.symbol,
            pair_state=pair_state,
            attempt_index=lease.attempt_index,
            runtime_reason="head_unconfirmed_timeout",
        )
        if event is None:
            return
        slot = _CommandSlot(lease.pair_name, lease.attempt_index, lease.role)
        self._order_leases[slot] = replace(
            lease,
            status=_LEASE_CLOSED,
            last_seen_at=now,
        )
        self._head_fill_deadlines.pop(slot, None)
        _LOGGER.warning(
            "HEAD_CANCEL_GAVE_UP (%s#%s): %s",
            lease.pair_name,
            lease.attempt_index,
            _runtime_fields(
                cancel_id,
                f"retry={lease.cancel_retry_count}",
                "head_unconfirmed_timeout",
            ),
        )
        self.event_queue.put_nowait(event)

    def _dispatch_lease_cancel(
        self,
        lease: _OrderLease,
        *,
        reason: str,
        now: datetime,
    ) -> None:
        cancel_id = lease.exchange_order_id or lease.client_order_id
        if not cancel_id:
            _LOGGER.warning(
                "ORDER_SAFETY_BLOCKED (%s#%s): %s",
                lease.pair_name,
                lease.attempt_index,
                _runtime_fields(lease.role, "missing_identity", reason),
            )
            return
        cancel_slot = _CommandSlot(
            pair_name=lease.pair_name,
            attempt_index=lease.attempt_index,
            role="cancel",
        )
        if cancel_slot in self._inflight_commands:
            return
        command = CancelCommand(
            kind=RuntimeCommandKind.CANCEL,
            symbol=Symbol(lease.symbol),
            pair_name=lease.pair_name,
            request=CancelOrderCommandRequest(
                pair_name=lease.pair_name,
                clOrdID=cancel_id,
            ),
            reason=reason,
            exchange=lease.exchange or self.exchange,
            market_type=lease.market_type or self.market_type,
        )
        _LOGGER.warning(
            "CANCEL_RETRY (%s#%s): %s",
            lease.pair_name,
            lease.attempt_index,
            _runtime_fields(lease.role, cancel_id, reason),
        )
        self._dispatch_commands((command,))
        slot = _CommandSlot(lease.pair_name, lease.attempt_index, lease.role)
        current = self._order_leases.get(slot, lease)
        self._order_leases[slot] = replace(
            current,
            status=_LEASE_CANCEL_REQUESTED,
            cancel_sent_at=now,
            cancel_retry_count=current.cancel_retry_count + 1,
        )

    def _arm_head_fill_deadline_from_visibility(
        self,
        lease: _OrderLease,
        pair_state: PairCycleState,
        now: datetime,
    ) -> None:
        if lease.role != "head":
            return
        slot = _CommandSlot(lease.pair_name, lease.attempt_index, "head")
        if slot in self._head_fill_deadlines:
            return
        timeout_minutes = pair_state.pair.timeout_minutes
        if timeout_minutes is None or timeout_minutes <= 0:
            return
        client_order_id = lease.client_order_id
        exchange_order_id = lease.exchange_order_id
        if not client_order_id and not exchange_order_id:
            _LOGGER.warning(
                "ORDER_SAFETY_BLOCKED (%s#%s): %s",
                lease.pair_name,
                lease.attempt_index,
                _runtime_fields(lease.role, "missing_identity", "head_visibility_timeout"),
            )
            return
        started_at = _as_utc_aware(lease.created_at)
        deadline = _HeadFillDeadline(
            pair_name=lease.pair_name,
            attempt_index=lease.attempt_index,
            client_order_id=client_order_id,
            exchange_order_id=exchange_order_id,
            started_at=started_at,
            deadline_at=started_at + timedelta(minutes=float(timeout_minutes)),
            source="visibility",
        )
        self._head_fill_deadlines[slot] = deadline
        _LOGGER.warning(
            "HEAD_VISIBILITY_TIMEOUT_ARMED (%s#%s): %s",
            lease.pair_name,
            lease.attempt_index,
            _runtime_fields(
                client_order_id or "-",
                exchange_order_id or "-",
                deadline.deadline_at.isoformat(),
            ),
        )

    def _prune_order_leases(self) -> None:
        for slot, lease in tuple(self._order_leases.items()):
            if lease.status == _LEASE_CLOSED:
                continue
            pair_state = self.state.pairs.get(lease.pair_name)
            if pair_state is None or pair_state.attempt_index != lease.attempt_index:
                continue
            if lease.role == "head" and pair_state.head_state in {
                HeadState.CLOSED,
                HeadState.FAILED,
            }:
                self._order_leases[slot] = replace(lease, status=_LEASE_CLOSED)
            elif lease.role == "tail" and pair_state.tail_state in {
                TailState.CLOSED,
                TailState.FAILED,
            }:
                self._order_leases[slot] = replace(lease, status=_LEASE_CLOSED)

    def _prune_live_command_identities(self) -> None:
        stale: list[str] = []
        for key, identity in self._live_command_identities.items():
            pair_state = self.state.pairs.get(identity.pair_name)
            if pair_state is None:
                stale.append(key)
                continue
            if identity.role == "head":
                if _identity_confirmed(identity, pair_state.head_identity):
                    stale.append(key)
                    continue
                if pair_state.head_state == HeadState.LATENT and pair_state.head_identity is None:
                    stale.append(key)
                    continue
                if pair_state.head_state in {HeadState.CLOSED, HeadState.FAILED}:
                    stale.append(key)
                    continue
            elif identity.role == "tail":
                if _identity_confirmed(identity, pair_state.tail_identity):
                    stale.append(key)
                    continue
                if pair_state.tail_state in {None, TailState.CLOSED, TailState.FAILED}:
                    stale.append(key)
                    continue
            else:
                # Cancel correlation can be dropped once it cannot affect live exposure.
                if pair_state.head_state in {HeadState.CLOSED, HeadState.FAILED} and pair_state.tail_state in {
                    None,
                    TailState.LATENT,
                    TailState.CLOSED,
                    TailState.FAILED,
                }:
                    stale.append(key)
        for key in stale:
            self._live_command_identities.pop(key, None)

    def pair_state_for_record(self, record) -> tuple[PairCycleState, OrderRole] | None:
        for slot, lease in self._order_leases.items():
            if lease.status == _LEASE_CLOSED:
                continue
            if lease.role not in {"head", "tail"}:
                continue
            pair_state = self.state.pairs.get(lease.pair_name)
            if pair_state is None or pair_state.attempt_index != lease.attempt_index:
                continue
            if not _record_route_matches_pair(record, pair_state, self):
                continue
            if _record_matches_lease(record, lease):
                role = OrderRole.HEAD if lease.role == "head" else OrderRole.TAIL
                return pair_state, role
        for identity in self._identity_map_from_pair_and_commands().values():
            if identity.role not in {"head", "tail"}:
                continue
            pair_state = self.state.pairs.get(identity.pair_name)
            if pair_state is None:
                continue
            if not _record_route_matches_pair(record, pair_state, self):
                continue
            if _record_matches_identity(record, identity):
                role = OrderRole.HEAD if identity.role == "head" else OrderRole.TAIL
                return pair_state, role
        for pair_state in self.state.pairs.values():
            if not _record_route_matches_pair(record, pair_state, self):
                continue
            tail_identity = pair_state.tail_identity
            if tail_identity is not None and _record_matches_identity(
                record, tail_identity
            ):
                return pair_state, OrderRole.TAIL
            head_identity = pair_state.head_identity
            if head_identity is not None and _record_matches_identity(
                record, head_identity
            ):
                return pair_state, OrderRole.HEAD
        return None

    def _command_identities(self) -> dict[str, OrderIdentity]:
        return self._identity_map_from_pair_and_commands()

    def _identity_map_from_pair_and_commands(self) -> dict[str, OrderIdentity]:
        identities: dict[str, OrderIdentity] = {}
        for pair_state in self.state.pairs.values():
            if pair_state.head_identity is not None:
                identities[_identity_key(pair_state.head_identity)] = pair_state.head_identity
            if pair_state.tail_identity is not None:
                identities[_identity_key(pair_state.tail_identity)] = pair_state.tail_identity
        for lease in self.active_order_leases():
            identity = OrderIdentity(
                pair_name=lease.pair_name,
                role=lease.role,
                client_order_id=lease.client_order_id,
                exchange_order_id=lease.exchange_order_id,
                symbol=lease.symbol,
                exchange=lease.exchange,
                market_type=lease.market_type,
            )
            identities[_identity_key(identity)] = identity
        identities.update(self._live_command_identities)
        for entry in self._inflight_commands.values():
            identity = self._command_identity_from_command(entry.command)
            if identity is not None:
                identities[_identity_key(identity)] = identity
        for queue in self._pending_commands.values():
            for command in queue:
                identity = self._command_identity_from_command(command)
                if identity is not None:
                    identities[_identity_key(identity)] = identity
        return identities

    @staticmethod
    def _command_identity_from_command(command: DragonSong) -> OrderIdentity | None:
        pair_name = command.pair_name
        if isinstance(command, CancelCommand):
            cancel_id = getattr(command.request, "clOrdID", None)
            if not cancel_id:
                return None
            return OrderIdentity(
                pair_name=pair_name,
                role="cancel",
                client_order_id=cancel_id,
                symbol=str(command.symbol),
                exchange=command.exchange or None,
                market_type=command.market_type or None,
            )
        client_order_id = getattr(command.request, "clOrdID", None)
        if not client_order_id:
            return None
        if isinstance(command, (PlaceHeadCommand, AmendHeadCommand)):
            role = "head"
        elif isinstance(command, (PlaceTailCommand, AmendTailCommand)):
            role = "tail"
        else:
            assert_never(command)
        exchange_order_id = getattr(command.request, "orderID", None)
        return OrderIdentity(
            pair_name=pair_name,
            role=role,
            client_order_id=client_order_id,
            exchange_order_id=exchange_order_id,
            symbol=str(command.symbol),
            exchange=command.exchange or None,
            market_type=command.market_type or None,
        )

    def _prepare_command(self, command: DragonSong) -> DragonSong:
        command = self._with_command_route(command)
        if isinstance(command, PlaceHeadCommand):
            command = self._with_materialized_head_quantity(command)
        if isinstance(command, PlaceHeadCommand) and command.request.clOrdID is None:
            pair_state = self.state.pairs[command.request.pair_name]
            pair = pair_state.pair
            clordid = head_client_order_id(
                pair,
                attempt_index=pair_state.attempt_index,
                at=datetime.now(timezone.utc),
            )
            request = replace(command.request, clOrdID=clordid)
            legacy_order = dict(command.legacy_order or {})
            legacy_order["clOrdID"] = clordid
            return replace(
                command,
                request=request,
                legacy_order=cast(OrderDict, legacy_order),
            )
        if isinstance(command, PlaceTailCommand) and command.request.clOrdID is None:
            pair_state = self.state.pairs[command.request.pair_name]
            clordid = tail_client_order_id(
                pair_state.pair,
                attempt_index=pair_state.attempt_index,
                at=datetime.now(timezone.utc),
            )
            request = replace(command.request, clOrdID=clordid)
            legacy_order = dict(command.legacy_order or {})
            legacy_order["clOrdID"] = clordid
            return replace(
                command,
                request=request,
                legacy_order=cast(OrderDict, legacy_order),
            )
        return command

    def _with_materialized_head_quantity(self, command: PlaceHeadCommand) -> PlaceHeadCommand:
        pair_state = self.state.pairs[command.request.pair_name]
        pair = pair_state.pair
        if not pair_uses_usd_quantity(pair) and not pair_uses_percent_balance_quantity(
            pair
        ):
            return command
        route = _pair_route(pair_state, self)
        rules = self.instrument_rules_by_route.get(route)
        if rules is None:
            code = (
                "QTY_PCT_TOO_SMALL"
                if pair_uses_percent_balance_quantity(pair)
                else "QTY_USD_TOO_SMALL"
            )
            raise QuantityResolutionError(
                f"{code} pair={pair.name} route={route.label} missing instrument rules"
            )
        if self.public_state_reader is None:
            code = (
                "QTY_PCT_TOO_SMALL"
                if pair_uses_percent_balance_quantity(pair)
                else "QTY_USD_TOO_SMALL"
            )
            raise QuantityResolutionError(
                f"{code} pair={pair.name} route={route.label} missing market reader"
            )
        market = _fetch_market_state_for_route(self.public_state_reader, route)
        if pair_uses_percent_balance_quantity(pair):
            if self.account_state_reader is None:
                raise QuantityResolutionError(
                    f"QTY_PCT_TOO_SMALL pair={pair.name} route={route.label} missing account balance reader"
                )
            balance = _fetch_account_balance_for_route(self.account_state_reader, route)
            resolution = resolve_pair_percent_balance_quantity(
                pair,
                available_usd=available_usd_for_percent_quantity(pair, balance),
                mark_price=mark_price_for_usd_quantity(
                    pair,
                    market,
                    error_code="QTY_PCT_TOO_SMALL",
                ),
                rules=rules,
            )
            quantity = cast(OrderQty, resolution.quantity)
            request = replace(command.request, orderQty=quantity)
            legacy_order = dict(command.legacy_order or {})
            legacy_order["orderQty"] = quantity
            _LOGGER.info(
                "QTY_PCT_RESOLVED (%s#%s): %s",
                command.pair_name,
                pair_state.attempt_index,
                _runtime_fields(
                    route.label,
                    f"%{resolution.percent}",
                    f"available={resolution.available_usd}",
                    f"nominal_usd={resolution.nominal_usd}",
                    f"mark={resolution.mark_price}",
                    f"contract={resolution.contract_size}",
                    f"step={resolution.quantity_step}",
                    f"min={resolution.min_quantity}",
                    f"qty={resolution.quantity}",
                    f"usd={resolution.approx_usd}",
                ),
            )
            return replace(
                command,
                request=request,
                legacy_order=cast(OrderDict, legacy_order),
            )
        resolution = resolve_pair_usd_quantity(
            pair,
            mark_price=mark_price_for_usd_quantity(pair, market),
            rules=rules,
        )
        quantity = cast(OrderQty, resolution.quantity)
        request = replace(command.request, orderQty=quantity)
        legacy_order = dict(command.legacy_order or {})
        legacy_order["orderQty"] = quantity
        _LOGGER.info(
            "QTY_USD_RESOLVED (%s#%s): %s",
            command.pair_name,
            pair_state.attempt_index,
            _runtime_fields(
                route.label,
                f"U{resolution.nominal_usd}",
                f"mark={resolution.mark_price}",
                f"contract={resolution.contract_size}",
                f"step={resolution.quantity_step}",
                f"min={resolution.min_quantity}",
                f"qty={resolution.quantity}",
                f"usd={resolution.approx_usd}",
            ),
        )
        return replace(command, request=request, legacy_order=cast(OrderDict, legacy_order))

    def _fail_command_before_dispatch(
        self,
        command: DragonSong,
        error: QuantityResolutionError,
    ) -> None:
        failure = _command_failure_event(
            command,
            symbol=str(command.symbol),
            occurred_at=datetime.now(timezone.utc),
            error=error,
        )
        if failure is None:
            raise error
        slot = self._command_slot(command)
        _LOGGER.warning(
            "COMMAND_FAILED (%s#%s): %s",
            command.pair_name,
            slot.attempt_index,
            _runtime_fields(_quantity_error_code(error), _compact_error(error)),
        )
        followups = self.chronos.process_event(failure)
        self.state = self.chronos.state
        self._prune_order_leases()
        if followups:
            self._dispatch_commands(followups)

    def _with_command_route(self, command: DragonSong) -> DragonSong:
        pair_state = self.state.pairs.get(command.pair_name)
        if pair_state is None:
            exchange = command.exchange or self.exchange
            market_type = command.market_type or self.market_type
        else:
            route = _pair_route(pair_state, self)
            exchange = command.exchange or route.exchange
            market_type = command.market_type or route.market_type
        if command.exchange == exchange and command.market_type == market_type:
            return command
        return replace(command, exchange=exchange, market_type=market_type)

    def _record_live_ack(self, command: DragonSong, ack: OrderAck) -> None:
        if self.simulate:
            return
        self._enrich_order_lease_from_ack(command, ack)
        identity = self._command_identity_from_command(command)
        if identity is None:
            return
        merged = OrderIdentity(
            pair_name=identity.pair_name,
            role=identity.role,
            client_order_id=ack.client_order_id or identity.client_order_id,
            exchange_order_id=str(ack.order_id) if ack.order_id else identity.exchange_order_id,
            symbol=identity.symbol,
            exchange=identity.exchange,
            market_type=identity.market_type,
        )
        self._live_command_identities[_identity_key(merged)] = merged
        if isinstance(command, (PlaceHeadCommand, PlaceTailCommand)) and _ack_is_rejected(ack):
            self._close_rejected_place_lease(command)
            _LOGGER.warning(
                "COMMAND_FAILED (%s#%s): %s",
                command.pair_name,
                self._command_slot(command).attempt_index,
                _runtime_fields(
                    "rejected",
                    _ack_rejection_reason(ack) or ack.status,
                    merged.client_order_id or "-",
                    merged.exchange_order_id or "-",
                ),
            )
            return
        if isinstance(command, CancelCommand):
            self._record_cancel_ack(command, ack)
            return
        if isinstance(command, AmendTailCommand) and _ack_is_rejected(ack):
            pair_state = self.state.pairs.get(command.pair_name)
            attempt_index = 1 if pair_state is None else pair_state.attempt_index
            slot = _CommandSlot(
                pair_name=command.pair_name,
                attempt_index=attempt_index,
                role="tail",
            )
            self._pending_tail_amends.pop(slot, None)
            _LOGGER.warning(
                "AMEND_REJECTED (%s#%s): %s",
                command.pair_name,
                attempt_index,
                _runtime_fields(
                    ack.status,
                    merged.client_order_id or "-",
                    merged.exchange_order_id or "-",
                ),
            )
        if not isinstance(command, PlaceTailCommand):
            return
        pair_state = self.state.pairs.get(command.pair_name)
        attempt_index = 1 if pair_state is None else pair_state.attempt_index
        slot = _CommandSlot(
            pair_name=command.pair_name,
            attempt_index=attempt_index,
            role="tail",
        )
        now = datetime.now(timezone.utc)
        self._pending_tail_visibility[slot] = _TailVisibilityWindow(
            pair_name=command.pair_name,
            attempt_index=attempt_index,
            client_order_id=merged.client_order_id,
            exchange_order_id=merged.exchange_order_id,
            started_at=now,
            deadline_at=now + timedelta(seconds=self.tail_visibility_timeout_seconds),
        )

    def _close_rejected_place_lease(self, command: PlaceHeadCommand | PlaceTailCommand) -> None:
        pair_state = self.state.pairs.get(command.pair_name)
        attempt_index = 1 if pair_state is None else pair_state.attempt_index
        role = "head" if isinstance(command, PlaceHeadCommand) else "tail"
        slot = _CommandSlot(command.pair_name, attempt_index, role)
        lease = self._order_leases.get(slot)
        if lease is None:
            return
        self._order_leases[slot] = replace(lease, status=_LEASE_CLOSED)
        self._head_fill_deadlines.pop(slot, None)
        if role == "tail":
            self._pending_tail_visibility.pop(slot, None)

    def _check_latent_head_deadlines(self, now: datetime) -> None:
        now = _as_utc_aware(now)
        stale_keys: list[tuple[str, int]] = []
        expired_keys: set[tuple[str, int]] = set()
        for key, deadline in tuple(self._latent_head_deadlines.items()):
            pair_state = self.state.pairs.get(deadline.pair_name)
            if not self._latent_head_deadline_still_applies(pair_state, deadline):
                stale_keys.append(key)
                continue
            if now < deadline.deadline_at:
                continue
            _LOGGER.warning(
                "LATENT_TIMEOUT (%s#%s): %s",
                deadline.pair_name,
                deadline.attempt_index,
                _runtime_fields(
                    f"{(now - deadline.started_at).total_seconds():.1f}s",
                    "price_gate",
                ),
            )
            self.event_queue.put_nowait(
                _latent_head_timeout_event(
                    deadline,
                    symbol=_pair_symbol(cast(PairCycleState, pair_state), self.symbol),
                    occurred_at=now,
                )
            )
            stale_keys.append(key)
            expired_keys.add(key)

        for key in stale_keys:
            self._latent_head_deadlines.pop(key, None)

        for pair_name, pair_state in self.state.pairs.items():
            key = (pair_name, pair_state.attempt_index)
            if key in self._latent_head_deadlines or key in expired_keys:
                continue
            if not self._latent_head_timeout_can_arm(pair_state, now):
                continue
            timeout_minutes = cast(float, pair_state.pair.timeout_minutes)
            deadline = _LatentHeadDeadline(
                pair_name=pair_name,
                attempt_index=pair_state.attempt_index,
                started_at=now,
                deadline_at=now + timedelta(minutes=float(timeout_minutes)),
            )
            self._latent_head_deadlines[key] = deadline
            _LOGGER.info(
                "LATENT_TIMEOUT_ARMED (%s#%s): %s",
                pair_name,
                pair_state.attempt_index,
                deadline.deadline_at.isoformat(),
            )

    def _latent_head_timeout_can_arm(
        self,
        pair_state: PairCycleState,
        now: datetime,
    ) -> bool:
        if pair_state.head_state != HeadState.LATENT:
            return False
        if pair_state.head_identity is not None:
            return False
        timeout_minutes = pair_state.pair.timeout_minutes
        if timeout_minutes is None or timeout_minutes <= 0:
            return False
        if not pair_window_is_open(
            pair_state.pair,
            launched_at=self.state.launched_at,
            now=now,
        ):
            return False
        if not pair_dependency_satisfied(self.state, pair_state):
            return False
        return True

    def _latent_head_deadline_still_applies(
        self,
        pair_state: PairCycleState | None,
        deadline: _LatentHeadDeadline,
    ) -> bool:
        if pair_state is None:
            return False
        if pair_state.attempt_index != deadline.attempt_index:
            return False
        if pair_state.head_state != HeadState.LATENT:
            return False
        if pair_state.head_identity is not None:
            return False
        timeout_minutes = pair_state.pair.timeout_minutes
        return timeout_minutes is not None and timeout_minutes > 0

    def _prune_latent_head_deadlines(self) -> None:
        stale_keys = [
            key
            for key, deadline in self._latent_head_deadlines.items()
            if not self._latent_head_deadline_still_applies(
                self.state.pairs.get(deadline.pair_name),
                deadline,
            )
        ]
        for key in stale_keys:
            self._latent_head_deadlines.pop(key, None)

    def _discard_latent_head_deadline(self, pair_name: str, attempt_index: int) -> None:
        self._latent_head_deadlines.pop((pair_name, attempt_index), None)

    def _should_ignore_stale_runtime_cancel(self, event: EggMove) -> bool:
        is_latent_timeout = _is_latent_head_timeout_event(event)
        is_head_timeout_cancel = _is_head_timeout_cancel_event(event)
        if not (is_latent_timeout or is_head_timeout_cancel):
            return False
        pair_name = event.pair_name
        if pair_name is None:
            return True
        pair_state = self.state.pairs.get(pair_name)
        if pair_state is None:
            return True
        attempt_index = _runtime_cancel_attempt_index(event)
        if attempt_index is not None and pair_state.attempt_index != attempt_index:
            return True
        if is_latent_timeout:
            return pair_state.head_state != HeadState.LATENT
        return pair_state.head_state not in {
            HeadState.HOOKED,
            HeadState.SUBMITTED,
            HeadState.NEW,
            HeadState.LIVING,
        }

    def _sync_head_fill_deadline(self, move: EggMove) -> None:
        if move.role != OrderRole.HEAD:
            return
        pair_name = move.pair_name or resolve_pair_name(self.state, move)
        if pair_name is None:
            return
        pair_state = self.state.pairs.get(pair_name)
        if pair_state is None:
            return
        slot = _CommandSlot(
            pair_name=pair_name,
            attempt_index=pair_state.attempt_index,
            role="head",
        )
        if pair_state.head_state in {HeadState.CLOSED, HeadState.FAILED}:
            self._head_fill_deadlines.pop(slot, None)
            return
        if pair_state.head_state not in {
            HeadState.HOOKED,
            HeadState.SUBMITTED,
            HeadState.NEW,
            HeadState.LIVING,
        }:
            return
        timeout_minutes = pair_state.pair.timeout_minutes
        if timeout_minutes is None or timeout_minutes <= 0:
            return
        if slot in self._head_fill_deadlines:
            return
        identity = pair_state.head_identity
        if identity is None:
            return
        started_at = _as_utc_aware(move.occurred_at)
        deadline = _HeadFillDeadline(
            pair_name=pair_name,
            attempt_index=pair_state.attempt_index,
            client_order_id=identity.client_order_id,
            exchange_order_id=identity.exchange_order_id,
            started_at=started_at,
            deadline_at=started_at + timedelta(minutes=float(timeout_minutes)),
        )
        self._head_fill_deadlines[slot] = deadline
        _LOGGER.info(
            "HEAD_ACK (%s#%s): %s",
            pair_name,
            pair_state.attempt_index,
            _runtime_fields(
                identity.client_order_id or "-",
                identity.exchange_order_id or "-",
                deadline.deadline_at.isoformat(),
            ),
        )

    def _check_head_fill_deadlines(self, now: datetime) -> None:
        stale_slots: list[_CommandSlot] = []
        updated: dict[_CommandSlot, _HeadFillDeadline] = {}
        for slot, deadline in tuple(self._head_fill_deadlines.items()):
            pair_state = self.state.pairs.get(deadline.pair_name)
            if pair_state is None or pair_state.attempt_index != deadline.attempt_index:
                stale_slots.append(slot)
                continue
            if pair_state.head_state in {HeadState.CLOSED, HeadState.FAILED}:
                stale_slots.append(slot)
                continue
            if pair_state.head_state not in {
                HeadState.HOOKED,
                HeadState.SUBMITTED,
                HeadState.NEW,
                HeadState.LIVING,
            }:
                continue
            if _as_utc_aware(now) < deadline.deadline_at:
                continue
            cancel_id = deadline.exchange_order_id or deadline.client_order_id
            if not cancel_id:
                _LOGGER.warning(
                    "HEAD_TIMEOUT (%s#%s): %s",
                    deadline.pair_name,
                    deadline.attempt_index,
                    _runtime_fields(
                        "missing_identity",
                        f"{(_as_utc_aware(now) - deadline.started_at).total_seconds():.1f}s",
                        "-",
                        "-",
                    ),
                )
                continue
            cancel_slot = _CommandSlot(
                pair_name=deadline.pair_name,
                attempt_index=deadline.attempt_index,
                role="cancel",
            )
            if cancel_slot in self._inflight_commands:
                continue
            if (
                deadline.cancel_dispatched_at is not None
                and (_as_utc_aware(now) - deadline.cancel_dispatched_at).total_seconds()
                < self._head_cancel_retry_seconds()
            ):
                continue
            if deadline.cancel_dispatched_at is None:
                waited = f"{(_as_utc_aware(now) - deadline.started_at).total_seconds():.1f}s"
                if deadline.source == "visibility":
                    _LOGGER.warning(
                        "HEAD_VISIBILITY_TIMEOUT (%s#%s): %s",
                        deadline.pair_name,
                        deadline.attempt_index,
                        _runtime_fields(
                            "expired",
                            waited,
                            deadline.client_order_id or "-",
                            deadline.exchange_order_id or "-",
                        ),
                    )
                else:
                    _LOGGER.warning(
                        "HEAD_TIMEOUT (%s#%s): %s",
                        deadline.pair_name,
                        deadline.attempt_index,
                        _runtime_fields(
                            "expired",
                            waited,
                            deadline.client_order_id or "-",
                            deadline.exchange_order_id or "-",
                        ),
                    )
            command = _head_timeout_cancel_command(
                pair_state,
                _pair_symbol(pair_state, self.symbol),
                cancel_id,
            )
            self._dispatch_commands((command,))
            updated[slot] = replace(deadline, cancel_dispatched_at=_as_utc_aware(now))
        for slot in stale_slots:
            self._head_fill_deadlines.pop(slot, None)
        self._head_fill_deadlines.update(updated)

    def _head_cancel_retry_seconds(self) -> float:
        return max(5.0, self.tail_visibility_timeout_seconds)

    def _check_tail_visibility_deadlines(self, now: datetime) -> None:
        stale_slots: list[_CommandSlot] = []
        delayed_slots: dict[_CommandSlot, _TailVisibilityWindow] = {}
        for slot, window in self._pending_tail_visibility.items():
            pair_state = self.state.pairs.get(window.pair_name)
            if pair_state is None or pair_state.attempt_index != window.attempt_index:
                stale_slots.append(slot)
                continue
            tail_identity = pair_state.tail_identity
            if tail_identity is not None and _identities_overlap(
                tail_identity,
                window.client_order_id,
                window.exchange_order_id,
            ):
                if window.last_warned_at is not None:
                    _LOGGER.info(
                        "TAIL_VISIBLE (%s#%s): %s",
                        window.pair_name,
                        window.attempt_index,
                        _runtime_fields(
                            window.client_order_id or "-",
                            window.exchange_order_id or "-",
                            f"{(now - window.started_at).total_seconds():.1f}s",
                        ),
                    )
                stale_slots.append(slot)
                continue
            if pair_state.tail_state in {TailState.CLOSED, TailState.FAILED}:
                stale_slots.append(slot)
                continue
            if now < window.deadline_at:
                continue
            warn_after = max(5.0, self.tail_visibility_timeout_seconds)
            if (
                window.last_warned_at is not None
                and (now - window.last_warned_at).total_seconds() < warn_after
            ):
                continue
            _LOGGER.warning(
                "TAIL_PENDING (%s#%s): %s",
                window.pair_name,
                window.attempt_index,
                _runtime_fields(
                    window.client_order_id or "-",
                    window.exchange_order_id or "-",
                    f"{(now - window.started_at).total_seconds():.1f}s",
                    f"{self.tail_visibility_timeout_seconds:.1f}s",
                ),
            )
            delayed_slots[slot] = replace(window, last_warned_at=now)
        for slot in stale_slots:
            self._pending_tail_visibility.pop(slot, None)
        self._pending_tail_visibility.update(delayed_slots)

    def _check_tail_amend_deadlines(self, now: datetime) -> None:
        stale_slots: list[_CommandSlot] = []
        for slot, pending in self._pending_tail_amends.items():
            pair_state = self.state.pairs.get(pending.pair_name)
            if pair_state is None or pair_state.attempt_index != pending.attempt_index:
                stale_slots.append(slot)
                continue
            if pair_state.tail_state in {None, TailState.CLOSED, TailState.FAILED}:
                stale_slots.append(slot)
                continue
            confirmed_stop = _confirmed_tail_stop(pair_state)
            if _prices_close(confirmed_stop, pending.desired_stop_price):
                stale_slots.append(slot)
                continue
            if now < pending.deadline_at:
                continue
            _LOGGER.warning(
                "AMEND_PENDING (%s#%s): %s",
                pending.pair_name,
                pending.attempt_index,
                _runtime_fields(
                    _fmt_compact_price(confirmed_stop),
                    _fmt_compact_price(pending.desired_stop_price),
                    pending.client_order_id or "-",
                    pending.exchange_order_id or "-",
                    f"{(now - pending.started_at).total_seconds():.1f}s",
                ),
            )
            stale_slots.append(slot)
        for slot in stale_slots:
            self._pending_tail_amends.pop(slot, None)

    def _on_command_dispatched(
        self,
        slot: _CommandSlot,
        command: DragonSong,
        identity: OrderIdentity | None,
    ) -> None:
        if isinstance(command, PlaceHeadCommand):
            pair_state = self.state.pairs.get(command.pair_name)
            attempt_index = slot.attempt_index if pair_state is None else pair_state.attempt_index
            self._discard_latent_head_deadline(command.pair_name, attempt_index)
            self._upsert_order_lease_from_place_command(slot, command, identity)
            _LOGGER.info(
                "HEAD_SENT (%s#%s): %s",
                command.pair_name,
                attempt_index,
                _runtime_fields(
                    "-" if identity is None else identity.client_order_id or "-",
                    command.request.side,
                    command.request.ordType,
                    _fmt_compact_price(command.request.orderQty),
                    _fmt_compact_price(command.request.price),
                    _fmt_compact_price(command.request.stopPx),
                ),
            )
            return
        if isinstance(command, PlaceTailCommand):
            self._upsert_order_lease_from_place_command(slot, command, identity)
            return
        if isinstance(command, CancelCommand):
            label = "HEAD_CANCEL_SENT" if command.reason == "head_timeout" else "CANCEL_SENT"
            self._mark_cancel_requested_from_command(command)
            _LOGGER.info(
                "%s (%s#%s): %s",
                label,
                command.pair_name,
                slot.attempt_index,
                _runtime_fields(command.request.clOrdID, command.reason),
            )
            return
        if not isinstance(command, AmendTailCommand):
            return
        pair_state = self.state.pairs.get(command.pair_name)
        if pair_state is None:
            return
        now = datetime.now(timezone.utc)
        if command.request.newPrice is None:
            return
        desired_stop = to_decimal(command.request.newPrice)
        confirmed_stop = _confirmed_tail_stop(pair_state)
        ref_source = "-"
        ref_price: Decimal | None = None
        if self.public_state_reader is not None:
            market = _fetch_market_state_for_route(
                self.public_state_reader,
                _pair_route(pair_state, self),
            )
            ref_source, ref_price_value = tail_reference_price(pair_state.pair, market)
            ref_price = to_decimal(ref_price_value)
        _LOGGER.info(
            "AMEND_SENT (%s#%s): %s",
            command.pair_name,
            pair_state.attempt_index,
            _runtime_fields(
                _fmt_compact_price(confirmed_stop),
                _fmt_compact_price(desired_stop),
                _fmt_compact_price(ref_price),
                ref_source,
                "-" if identity is None else identity.client_order_id or "-",
                "-" if identity is None else identity.exchange_order_id or "-",
            ),
        )
        self._pending_tail_amends[slot] = _TailAmendPending(
            pair_name=command.pair_name,
            attempt_index=pair_state.attempt_index,
            desired_stop_price=desired_stop,
            client_order_id=None if identity is None else identity.client_order_id,
            exchange_order_id=None if identity is None else identity.exchange_order_id,
            started_at=now,
            deadline_at=now + timedelta(seconds=self.tail_visibility_timeout_seconds),
        )

    def _log_new_pending_repeats(
        self,
        previous_repeats: Mapping[str, object],
    ) -> None:
        for pair_name, pending in self.chronos.pending_repeats.items():
            if previous_repeats.get(pair_name) == pending:
                continue
            _LOGGER.info(
                "REPEAT_WAIT (%s#%s): %s",
                pair_name,
                pending.next_attempt,
                pending.ready_at.isoformat(),
            )

    def _log_chain_releases(self, previous_pairs: Mapping[str, PairCycleState]) -> None:
        for pair_name, current in self.state.pairs.items():
            previous = previous_pairs.get(pair_name)
            if previous is None:
                continue
            token = current.dependency_token
            if token is None or previous.dependency_token == token:
                continue
            _LOGGER.info(
                "CHAIN_READY (%s#%s): %s",
                pair_name,
                current.attempt_index,
                _runtime_fields(
                    f"{token.origin_pair_name}#{token.origin_attempt_index}",
                    "waiting_for_price_gate",
                    token.closed_at.isoformat(),
                ),
            )

    def _log_repeat_attempts(self, previous_pairs: Mapping[str, PairCycleState]) -> None:
        for pair_name, current in self.state.pairs.items():
            previous = previous_pairs.get(pair_name)
            if previous is None:
                continue
            if current.attempt_index <= previous.attempt_index:
                continue
            _LOGGER.info(
                "REPEAT_READY (%s#%s): %s",
                pair_name,
                current.attempt_index,
                _runtime_fields(
                    "waiting_for_price_gate",
                    f"{current.pair.window.start_minutes}..{current.pair.window.end_minutes}",
                    "-",
                ),
            )

    def _log_repeat_start(self, commands: tuple[DragonSong, ...]) -> None:
        started: set[tuple[str, int]] = set()
        for command in commands:
            pair_state = self.state.pairs.get(command.pair_name)
            if pair_state is None:
                continue
            key = (command.pair_name, pair_state.attempt_index)
            if key in started:
                continue
            started.add(key)
            _LOGGER.info(
                "REPEAT_START (%s#%s): %s",
                key[0],
                key[1],
                sum(1 for item in commands if item.pair_name == key[0]),
            )

    def _followup_events(
        self,
        command: DragonSong,
        ack: OrderAck,
        *,
        slot: _CommandSlot | None = None,
    ) -> tuple[EggMove, ...]:
        if isinstance(command, AmendTailCommand) and _ack_is_rejected(ack):
            return (
                _tail_amend_event_from_ack(
                    command,
                    ack,
                    symbol=str(command.symbol),
                    kind=EggMoveKind.TAIL_AMEND_REJECTED,
                ),
            )
        if (
            not self.simulate
            and isinstance(command, (PlaceHeadCommand, PlaceTailCommand))
            and _ack_is_rejected(ack)
        ):
            return (
                _place_rejected_event_from_ack(
                    command,
                    ack,
                    symbol=str(command.symbol),
                ),
            )
        if isinstance(command, CancelCommand) and command.reason == "head_timeout":
            if not self.simulate and not _ack_is_zero_fill_notfound_cancel(ack):
                # Live/demo lifecycle is DB-grounded; ordinary cancel ACKs are
                # intent correlation only. A private DB row must close the order.
                return ()
            pair_state = self.state.pairs.get(command.pair_name)
            expected_attempt = (
                slot.attempt_index
                if slot is not None
                else 1 if pair_state is None else pair_state.attempt_index
            )
            if pair_state is None or pair_state.attempt_index != expected_attempt:
                _LOGGER.info(
                    "HEAD_CANCEL_ACK_STALE (%s#%s): %s",
                    command.pair_name,
                    expected_attempt,
                    _runtime_fields(
                        "-" if pair_state is None else pair_state.attempt_index,
                        command.request.clOrdID,
                    ),
                )
                return ()
            timeout_cancel = _head_timeout_cancel_event_from_ack(
                command,
                ack,
                symbol=str(command.symbol),
                pair_state=pair_state,
                attempt_index=expected_attempt,
            )
            if timeout_cancel is not None:
                return (timeout_cancel,)
        if not self.simulate and isinstance(command, CancelCommand):
            # Live/demo lifecycle is DB-grounded; cancel ACKs are intent
            # correlation only. A private DB row must close the order.
            return ()
        if not self.simulate:
            # Live/demo lifecycle is DB-grounded; adapter ACKs are correlation only.
            return ()
        if isinstance(command, PlaceTailCommand):
            submitted_tail = tail_submitted_from_ack(
                pair_name=command.pair_name,
                symbol=str(command.symbol),
                ack=ack,
                client_order_id=command.request.clOrdID,
                stop_price=command.request.stopPx,
                occurred_at=datetime.now(timezone.utc),
            )
            return (submitted_tail,)
        if isinstance(command, AmendTailCommand):
            kind = EggMoveKind.TAIL_AMENDED
            return (
                _tail_amend_event_from_ack(
                    command,
                    ack,
                    symbol=str(command.symbol),
                    kind=kind,
                ),
            )
        if not isinstance(command, PlaceHeadCommand):
            return ()
        submitted = head_submitted_from_ack(
            pair_name=command.pair_name,
            symbol=str(command.symbol),
            ack=ack,
            client_order_id=command.request.clOrdID,
            occurred_at=datetime.now(timezone.utc),
        )
        simulated_played_quantity = _played_quantity_from_request(command.request)
        submitted_with_reference = _with_simulated_reference_price(submitted, command)
        confirmed = simulated_private_fill_from_submission(
            submitted_with_reference,
            played_quantity=simulated_played_quantity,
            closed=True,
        )
        confirmed = _copy_reference_price(confirmed, submitted_with_reference)
        return (submitted, confirmed)


def plan_strategy_once(
    *,
    strategy: StrategySpec,
    symbol: str,
) -> StrategyRunResult:
    state = StrategyState(
        launched_at=datetime.now(timezone.utc),
        pairs={pair.name: _pair_state_from_spec(pair) for pair in strategy.pairs},
        strategy_id=strategy.name,
    )
    chronos = Chronos(state=state)
    commands = chronos.process_events(
        tuple(
            head_hooked_event(
                pair_name=pair_state.pair.name,
                symbol=_pair_symbol(pair_state, symbol),
                occurred_at=state.launched_at,
            )
            for pair_state in state.pairs.values()
            if pair_dependency_satisfied(state, pair_state)
        )
    )
    return StrategyRunResult(
        state=chronos.state,
        commands=commands,
        notices=tuple(chronos.notices),
    )


def _pair_state_from_spec(pair):
    from kolabi.bot.domain import PairCycleState

    return PairCycleState(pair=pair)


def _pair_symbol_from_pair(pair: object, default_symbol: str) -> str:
    symbol = str(getattr(pair, "symbol", "") or "").strip()
    return symbol or default_symbol


def _pair_symbol(pair_state: PairCycleState, default_symbol: str) -> str:
    return _pair_symbol_from_pair(pair_state.pair, default_symbol)


def _pair_route(pair_state: PairCycleState, runtime: RuntimeQueueLike) -> ExchangeRoute:
    return pair_route(
        pair_state.pair,
        default_exchange=getattr(runtime, "exchange", "kraken"),
        default_symbol=runtime.symbol,
        default_market_type=getattr(runtime, "market_type", "futures"),
    )


def _active_runtime_routes(runtime: RuntimeQueueLike) -> tuple[ExchangeRoute, ...]:
    routes = {
        _pair_route(pair_state, runtime)
        for pair_state in runtime.state.pairs.values()
    }
    if not routes:
        return (
            ExchangeRoute(
                exchange=getattr(runtime, "exchange", "kraken"),
                market_type=getattr(runtime, "market_type", "futures"),
                symbol=runtime.symbol,
            ),
        )
    return tuple(sorted(routes))


def _active_runtime_symbols(runtime: RuntimeQueueLike) -> tuple[str, ...]:
    symbols = {route.symbol for route in _active_runtime_routes(runtime)}
    if not symbols:
        return (runtime.symbol,)
    return tuple(sorted(symbols))


def _fetch_market_state_for_route(
    reader: PublicRuntimeStateReader,
    route: ExchangeRoute,
) -> PublicMarketStateReader:
    try:
        return reader.fetch_market_state(
            symbol=route.symbol,
            exchange=route.exchange,
            market_type=route.market_type,
        )
    except TypeError as exc:
        message = str(exc)
        if "exchange" not in message and "market_type" not in message:
            raise
        return reader.fetch_market_state(route.symbol)


def _fetch_account_balance_for_route(
    reader: AccountBalanceStateReader,
    route: ExchangeRoute,
) -> object:
    try:
        return reader.fetch_account_balance(
            asset="USD",
            exchange=route.exchange,
            market_type=route.market_type,
        )
    except TypeError as exc:
        message = str(exc)
        if "exchange" not in message and "market_type" not in message:
            raise
        return reader.fetch_account_balance("USD")


def _pair_runtime_complete(
    pair_state: PairCycleState,
    *,
    launched_at: datetime,
    now: datetime,
) -> bool:
    return _policy_pair_runtime_complete(
        pair_state,
        launched_at=launched_at,
        now=now,
    )


def _head_timeout_cancel_command(
    pair_state: PairCycleState,
    symbol: str,
    cancel_id: str,
) -> CancelCommand:
    request = CancelOrderCommandRequest(
        pair_name=pair_state.pair.name,
        clOrdID=cancel_id,
    )
    return CancelCommand(
        kind=RuntimeCommandKind.CANCEL,
        symbol=Symbol(symbol),
        pair_name=pair_state.pair.name,
        request=request,
        reason="head_timeout",
        legacy_order=cast(
            OrderDict,
            {
                "pair_name": pair_state.pair.name,
                "ordType": "cancel",
                "clOrdID": cancel_id,
                "text": "head_timeout",
            },
        ),
        exchange=pair_state.pair.exchange or "",
        market_type=pair_state.pair.market_type or "futures",
    )


def _latent_head_timeout_event(
    deadline: _LatentHeadDeadline,
    *,
    symbol: str,
    occurred_at: datetime,
) -> EggMove:
    return EggMove(
        kind=EggMoveKind.NOT_PLAYED_CANCELED,
        occurred_at=occurred_at,
        symbol=symbol,
        pair_name=deadline.pair_name,
        role=OrderRole.HEAD,
        reply={
            "ordStatus": "Canceled",
            "execType": "latent_timeout",
            "reason": "latent_timeout",
            "cumQty": 0.0,
            "attempt_index": deadline.attempt_index,
        },
        event_id=(
            f"latent-timeout:{deadline.pair_name}:"
            f"{deadline.attempt_index}:{deadline.deadline_at.isoformat()}"
        ),
        is_private=False,
    )


def _is_latent_head_timeout_event(event: EggMove) -> bool:
    if event.kind != EggMoveKind.NOT_PLAYED_CANCELED or event.role != OrderRole.HEAD:
        return False
    if event.event_id is not None and event.event_id.startswith("latent-timeout:"):
        return True
    reply = event.reply or {}
    return reply.get("execType") == "latent_timeout" or reply.get("reason") == "latent_timeout"


def _is_head_timeout_cancel_event(event: EggMove) -> bool:
    if event.kind != EggMoveKind.NOT_PLAYED_CANCELED or event.role != OrderRole.HEAD:
        return False
    if event.event_id is not None and event.event_id.startswith("head-timeout-cancel:"):
        return True
    reply = event.reply or {}
    return reply.get("runtime_reason") in {"head_timeout", "head_unconfirmed_timeout"}


def _runtime_cancel_attempt_index(event: EggMove) -> int | None:
    reply = event.reply or {}
    value = reply.get("attempt_index")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _command_failure_event(
    command: DragonSong,
    *,
    symbol: str,
    occurred_at: datetime,
    error: BaseException,
) -> EggMove | None:
    reply: dict[str, object] = {
        "ordStatus": HeadState.FAILED.value,
        "execType": "command_failed",
        "error": _compact_error(error),
    }
    client_order_id = getattr(command.request, "clOrdID", None)
    if client_order_id is not None:
        reply["clOrdID"] = client_order_id
    order_id = getattr(command.request, "orderID", None)
    if order_id is not None:
        reply["orderID"] = order_id
    price = getattr(command.request, "newPrice", None)
    if price is None:
        price = getattr(command.request, "price", None)
    if price is None:
        price = getattr(command.request, "stopPx", None)
    if price is not None:
        reply["price"] = float(to_decimal(price))
        reply["stopPx"] = float(to_decimal(price))
    quantity = _command_request_quantity(command)
    if quantity is not None:
        reply["orderQty"] = float(quantity)
        reply["cumQty"] = 0.0
    if isinstance(command, PlaceHeadCommand):
        return EggMove(
            kind=EggMoveKind.NOT_PLAYED_CANCELED,
            occurred_at=occurred_at,
            symbol=symbol,
            pair_name=command.pair_name,
            role=OrderRole.HEAD,
            reply=reply,
        )
    if isinstance(command, PlaceTailCommand):
        return EggMove(
            kind=EggMoveKind.NOT_PLAYED_CANCELED,
            occurred_at=occurred_at,
            symbol=symbol,
            pair_name=command.pair_name,
            role=OrderRole.TAIL,
            reply=reply,
        )
    if isinstance(command, AmendTailCommand):
        return EggMove(
            kind=EggMoveKind.TAIL_AMEND_REJECTED,
            occurred_at=occurred_at,
            symbol=symbol,
            pair_name=command.pair_name,
            role=OrderRole.TAIL,
            reply=reply,
        )
    return None


def _place_rejected_event_from_ack(
    command: PlaceHeadCommand | PlaceTailCommand,
    ack: OrderAck,
    *,
    symbol: str,
) -> EggMove:
    reply: dict[str, object] = {
        "ordStatus": ack.status or HeadState.FAILED.value,
        "execType": "Rejected",
        "cumQty": 0.0,
    }
    reason = _ack_rejection_reason(ack)
    if reason is not None:
        reply["error"] = reason
    client_order_id = ack.client_order_id or command.request.clOrdID
    if client_order_id is not None:
        reply["clOrdID"] = client_order_id
    if ack.order_id:
        reply["orderID"] = str(ack.order_id)
    price = ack.price
    if price is None:
        price = command.request.price
    if price is None:
        price = command.request.stopPx
    if price is not None:
        parsed_price = float(to_decimal(price))
        reply["price"] = parsed_price
        reply["stopPx"] = parsed_price
    quantity = ack.orig_qty
    if quantity is None:
        quantity = _command_request_quantity(command)
    if quantity is not None:
        reply["orderQty"] = float(to_decimal(quantity))
    side = ack.side or command.request.side
    if side is not None:
        reply["side"] = side
    role = OrderRole.HEAD if isinstance(command, PlaceHeadCommand) else OrderRole.TAIL
    return EggMove(
        kind=EggMoveKind.NOT_PLAYED_CANCELED,
        occurred_at=datetime.now(timezone.utc),
        symbol=symbol,
        pair_name=command.pair_name,
        role=role,
        reply=reply,
        is_private=False,
    )


def _tail_amend_event_from_ack(
    command: AmendTailCommand,
    ack: OrderAck,
    *,
    symbol: str,
    kind: EggMoveKind,
) -> EggMove:
    status = str(ack.status or "")
    reply: dict[str, object] = {
        "orderID": str(ack.order_id),
        "ordStatus": status,
    }
    if command.request.clOrdID is not None:
        reply["clOrdID"] = command.request.clOrdID
    confirmed_price = ack.price if ack.price is not None else command.request.newPrice
    if confirmed_price is not None:
        reply["stopPx"] = float(to_decimal(confirmed_price))
    if ack.orig_qty is not None:
        reply["orderQty"] = float(to_decimal(ack.orig_qty))
    if ack.executed_qty is not None:
        reply["cumQty"] = float(to_decimal(ack.executed_qty))
    if ack.side is not None:
        reply["side"] = ack.side
    return EggMove(
        kind=kind,
        occurred_at=datetime.now(timezone.utc),
        symbol=symbol,
        pair_name=command.pair_name,
        role=OrderRole.TAIL,
        reply=reply,
        is_private=False,
    )


def _head_timeout_cancel_event_from_ack(
    command: CancelCommand,
    ack: OrderAck,
    *,
    symbol: str,
    pair_state: PairCycleState | None,
    attempt_index: int,
    runtime_reason: str = "head_timeout",
) -> EggMove | None:
    if not _ack_is_zero_fill_cancel(ack):
        return None
    head_identity = None if pair_state is None else pair_state.head_identity
    client_order_id = (
        None if head_identity is None else head_identity.client_order_id
    )
    exchange_order_id = (
        str(ack.order_id)
        if ack.order_id
        else None if head_identity is None else head_identity.exchange_order_id
    )
    reply: dict[str, object] = {
        "ordStatus": "Canceled",
        "execType": "Canceled",
        "runtime_reason": runtime_reason,
        "attempt_index": attempt_index,
        "cumQty": 0.0,
    }
    if exchange_order_id:
        reply["orderID"] = exchange_order_id
    else:
        reply["orderID"] = command.request.clOrdID
    if client_order_id:
        reply["clOrdID"] = client_order_id
    if ack.orig_qty is not None:
        reply["orderQty"] = float(to_decimal(ack.orig_qty))
    elif pair_state is not None and pair_state.pair.head_quantity is not None:
        reply["orderQty"] = float(to_decimal(pair_state.pair.head_quantity))
    if ack.price is not None:
        price = float(to_decimal(ack.price))
        reply["price"] = price
        reply["stopPx"] = price
    if ack.side is not None:
        reply["side"] = ack.side
    return EggMove(
        kind=EggMoveKind.NOT_PLAYED_CANCELED,
        occurred_at=datetime.now(timezone.utc),
        symbol=symbol,
        pair_name=command.pair_name,
        role=OrderRole.HEAD,
        reply=reply,
        event_id=f"head-timeout-cancel:{command.pair_name}:{attempt_index}:{command.request.clOrdID}",
        is_private=False,
    )


def _command_request_quantity(command: DragonSong) -> Decimal | None:
    quantity = getattr(command.request, "orderQty", None)
    if quantity is None:
        quantity = getattr(command.request, "newQty", None)
    if isinstance(quantity, (int, float, Decimal, str)):
        parsed = to_decimal(quantity)
        if parsed >= 0:
            return parsed
    return None


def _with_simulated_reference_price(
    submitted: EggMove,
    command: PlaceHeadCommand,
) -> EggMove:
    """Give relative tails a deterministic reference in simulation mode."""
    reply = dict(submitted.reply or {})
    if "reference_price" not in reply:
        reference = command.request.price or command.request.stopPx or Decimal("100")
        reply["reference_price"] = float(reference)
    return replace(submitted, reply=reply)


def _copy_reference_price(move: EggMove, source: EggMove) -> EggMove:
    source_reply = source.reply or {}
    reference = source_reply.get("reference_price", source_reply.get("price"))
    if reference is None:
        return move
    reply = dict(move.reply or {})
    reply["reference_price"] = reference
    return replace(move, reply=reply)


def _played_quantity_from_ack(ack: OrderAck) -> Decimal | None:
    if ack.executed_qty is None:
        return None
    return Decimal(str(ack.executed_qty))


def _ack_is_terminal(ack: OrderAck) -> bool:
    return ack.status.replace(" ", "_").replace("-", "_").lower() in {
        "filled",
        "closed",
        "fully_filled",
        "full_fill",
    }


def _ack_is_rejected(ack: OrderAck) -> bool:
    return ack.status.replace(" ", "").replace("_", "").replace("-", "").lower() in {
        "rejected",
        "reject",
        "failed",
        "invalidprice",
    }


def _ack_rejection_reason(ack: OrderAck) -> str | None:
    reason = str(ack.reason or "").strip()
    if reason:
        return reason
    status = str(ack.status or "").strip()
    if status and _ack_is_rejected(ack):
        return status
    return None


def _ack_is_zero_fill_cancel(ack: OrderAck) -> bool:
    status = ack.status.replace(" ", "").replace("_", "").replace("-", "").lower()
    if ack.executed_qty is None:
        return status in {"canceled", "cancelled"}
    if status not in {"canceled", "cancelled", "notfound", "notfoundcancelled"}:
        return False
    return to_decimal(ack.executed_qty) == Decimal("0")


def _ack_is_zero_fill_notfound_cancel(ack: OrderAck) -> bool:
    status = ack.status.replace(" ", "").replace("_", "").replace("-", "").lower()
    if status not in {"notfound", "notfoundcancelled"}:
        return False
    if ack.executed_qty is None:
        return False
    return to_decimal(ack.executed_qty) == Decimal("0")


def _private_order_lease_status(status: str | None) -> str | None:
    key = _status_key(status)
    if key in {
        "new",
        "open",
        "untouched",
        "partiallyfilled",
        "partialfill",
        "partially_filled",
        "living",
    }:
        return _LEASE_LIVE
    if key in {
        "canceled",
        "cancelled",
        "closed",
        "filled",
        "failed",
        "rejected",
        "notfound",
        "notfoundcancelled",
    }:
        return _LEASE_CLOSED
    return None


def _status_key(status: str | None) -> str:
    return str(status or "").replace(" ", "").replace("-", "").lower()


def _string_payload(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _decimal_payload(value: object) -> Decimal | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float, Decimal, str)):
        return to_decimal(value)
    return None


def _record_matches_lease(record: object, lease: _OrderLease) -> bool:
    if not _record_route_matches_lease(record, lease):
        return False
    return _lease_matches_identity(
        lease,
        client_order_id=getattr(record, "client_order_id", None),
        exchange_order_id=getattr(record, "exchange_order_id", None),
    )


def _lease_matches_identity(
    lease: _OrderLease,
    *,
    client_order_id: str | None,
    exchange_order_id: str | None,
) -> bool:
    if client_order_id and lease.client_order_id == client_order_id:
        return True
    if exchange_order_id and lease.exchange_order_id == exchange_order_id:
        return True
    return False


def _is_mechanical_tail_amend_cancel(
    runtime: RuntimeQueueLike,
    pair_state: PairCycleState,
    role: OrderRole,
    move: EggMove,
) -> bool:
    """Drop the cancel half of cancel-replace tail amendments."""

    if role != OrderRole.TAIL or move.kind != EggMoveKind.NOT_PLAYED_CANCELED:
        return False
    reply = move.reply or {}
    filled = _decimal_payload(reply.get("cumQty")) or Decimal("0")
    if filled != Decimal("0"):
        return False
    pending_by_slot = getattr(runtime, "_pending_tail_amends", None)
    if not isinstance(pending_by_slot, Mapping):
        return False
    pending = pending_by_slot.get(
        _CommandSlot(pair_state.pair.name, pair_state.attempt_index, "tail")
    )
    if pending is None:
        return False
    client_order_id = _string_payload(reply.get("clOrdID"))
    exchange_order_id = _string_payload(reply.get("orderID"))
    if client_order_id and client_order_id == pending.client_order_id:
        return True
    if exchange_order_id and exchange_order_id == pending.exchange_order_id:
        return True
    return False


def _record_matches_identity(record, identity: OrderIdentity) -> bool:
    if not _record_route_matches_identity(record, identity):
        return False
    client_order_id = getattr(record, "client_order_id", None)
    exchange_order_id = getattr(record, "exchange_order_id", None)
    if client_order_id and identity.client_order_id == client_order_id:
        return True
    if (
        exchange_order_id
        and identity.exchange_order_id == exchange_order_id
    ):
        return True
    return False


def _record_symbol(record: object) -> str | None:
    symbol = getattr(record, "symbol", None)
    if symbol is None:
        return None
    text = str(symbol).strip()
    return text or None


def _record_exchange(record: object) -> str | None:
    exchange = getattr(record, "exchange", None)
    if exchange is None:
        return None
    text = str(exchange).strip().lower()
    return text or None


def _record_market_type(record: object) -> str | None:
    market_type = getattr(record, "market_type", None)
    if market_type is None:
        return None
    text = str(market_type).strip().lower()
    return text or None


def _record_route_matches_pair(
    record: object,
    pair_state: PairCycleState,
    runtime: RuntimeQueueLike,
) -> bool:
    route = _pair_route(pair_state, runtime)
    record_symbol = _record_symbol(record)
    if record_symbol is not None and record_symbol != route.symbol:
        return False
    record_exchange = _record_exchange(record)
    if record_exchange is not None and record_exchange != route.exchange:
        return False
    record_market_type = _record_market_type(record)
    if record_market_type is not None and record_market_type != route.market_type:
        return False
    return True


def _record_route_matches_lease(record: object, lease: _OrderLease) -> bool:
    record_symbol = _record_symbol(record)
    if record_symbol is not None and record_symbol != lease.symbol:
        return False
    record_exchange = _record_exchange(record)
    if (
        record_exchange is not None
        and lease.exchange is not None
        and record_exchange != lease.exchange
    ):
        return False
    record_market_type = _record_market_type(record)
    if (
        record_market_type is not None
        and lease.market_type is not None
        and record_market_type != lease.market_type
    ):
        return False
    return True


def _record_route_matches_identity(record: object, identity: OrderIdentity) -> bool:
    record_symbol = _record_symbol(record)
    if identity.symbol is not None:
        if record_symbol is None or record_symbol != identity.symbol:
            return False
    record_exchange = _record_exchange(record)
    if (
        identity.exchange is not None
        and record_exchange is not None
        and record_exchange != identity.exchange
    ):
        return False
    record_market_type = _record_market_type(record)
    if (
        identity.market_type is not None
        and record_market_type is not None
        and record_market_type != identity.market_type
    ):
        return False
    return True


def _identity_route_matches(identity: OrderIdentity, route: ExchangeRoute) -> bool:
    if identity.symbol is not None and identity.symbol != route.symbol:
        return False
    if identity.exchange is not None and identity.exchange != route.exchange:
        return False
    if identity.market_type is not None and identity.market_type != route.market_type:
        return False
    return True


def _command_route_matches_lease(
    command: DragonSong,
    lease: _OrderLease,
    default_exchange: str,
    default_market_type: str,
) -> bool:
    if str(command.symbol) != lease.symbol:
        return False
    command_exchange = command.exchange or default_exchange
    if lease.exchange is not None and command_exchange != lease.exchange:
        return False
    command_market_type = command.market_type or default_market_type
    if lease.market_type is not None and command_market_type != lease.market_type:
        return False
    return True


def _identity_key(identity: OrderIdentity) -> str:
    return (
        f"{identity.exchange or '-'}|{identity.market_type or '-'}|"
        f"{identity.symbol or '-'}|{identity.pair_name}|{identity.role}|"
        f"{identity.client_order_id or '-'}|{identity.exchange_order_id or '-'}"
    )


def _identity_confirmed(expected: OrderIdentity, current: OrderIdentity | None) -> bool:
    if current is None:
        return False
    return _identities_overlap(
        current,
        expected.client_order_id,
        expected.exchange_order_id,
    )


def _identities_overlap(
    identity: OrderIdentity,
    client_order_id: str | None,
    exchange_order_id: str | None,
) -> bool:
    if client_order_id and identity.client_order_id == client_order_id:
        return True
    if exchange_order_id and identity.exchange_order_id == exchange_order_id:
        return True
    return False


def _role_from_move(move: EggMove) -> OrderRole:
    return OrderRole.HEAD if move.role == OrderRole.HEAD else OrderRole.TAIL


def _confirmed_tail_stop(pair_state: PairCycleState) -> Decimal | None:
    if pair_state.tail_trail is None:
        return None
    return pair_state.tail_trail.confirmed_stop_price


def _tail_signed_distance(
    pair_state: PairCycleState,
    reference: Decimal,
    stop: Decimal,
) -> Decimal:
    """Return positive distance only while the platform stop is live-side safe."""
    if pair_state.pair.tail.side.value.lower() == "sell":
        return reference - stop
    return stop - reference


def _pair_uses_relative_tail(pair_state: PairCycleState) -> bool:
    tail_type = (pair_state.pair.tail_price_spec_type or "").lower()
    amount_type = pair_state.pair.amount_type.lower()
    return "tb" in tail_type or "td" in tail_type or "tb" in amount_type or "td" in amount_type


def _runtime_sources_should_stop(runtime: RuntimeQueueLike) -> bool:
    keep_alive = bool(getattr(runtime, "should_keep_sources_alive", False))
    queue = getattr(runtime, "event_queue", None)
    if queue is not None and not queue.empty():
        return False
    return runtime.all_pairs_terminal and not keep_alive


def _played_quantity_from_move(move: EggMove) -> Decimal | None:
    payload = move.reply or {}
    for key in ("cumQty", "executedQty", "filledQty", "filled_quantity"):
        value = payload.get(key)
        if isinstance(value, (int, float, Decimal, str)):
            return to_decimal(value)
    return None


def _filled_quantity_from_move_payload(payload: Mapping[str, object]) -> Decimal | None:
    for key in ("cumQty", "executedQty", "filledQty", "filled_quantity"):
        value = payload.get(key)
        if isinstance(value, (int, float, Decimal, str)):
            return to_decimal(value)
    return None


def _head_fill_price_from_move_payload(payload: Mapping[str, object]) -> Decimal | None:
    for key in ("price", "avgPx", "lastPx", "fillPrice", "executed_price"):
        value = payload.get(key)
        if isinstance(value, (int, float, Decimal, str)):
            return to_decimal(value)
    return None


def _fmt_compact_price(value: float | Decimal | None) -> str:
    if value is None:
        return "-"
    as_float = float(value)
    decimals = 2 if abs(as_float) >= 1 else 4
    return f"{as_float:.{decimals}f}"


def _head_gate_unit(pair_state: PairCycleState) -> str:
    pair = pair_state.pair
    price_type = (pair.head_price_type or "").lower()
    amount_type = pair.amount_type.lower()
    if "pa" in price_type or "pa" in amount_type:
        return "pA"
    if "pb" in price_type or "pb" in amount_type:
        return "pB"
    return "pD"


def _head_gate_measure(
    pair_state: PairCycleState,
    current: Decimal,
) -> Decimal | None:
    unit = _head_gate_unit(pair_state)
    if unit == "pA":
        return current
    baseline = pair_state.head_trigger_reference_price
    if baseline is None or baseline <= 0:
        return None
    if current <= 0:
        return None
    if unit == "pB":
        return signed_logbps_move(current, baseline)
    return current - baseline


def _head_gate_status(
    measurement: Decimal | None,
    low: Decimal,
    high: Decimal,
) -> str:
    if measurement is None:
        return "awaiting_baseline"
    if measurement < low:
        return "below"
    if measurement > high:
        return "above"
    return "ready"


def _event_atom(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else "-"
    if isinstance(value, (int, float)):
        return str(value)
    return str(value)


def _compact_error(exc: BaseException) -> str:
    return " ".join(str(exc).split())


def _quantity_error_code(exc: BaseException) -> str:
    text = _compact_error(exc)
    if not text:
        return "QTY_INVALID"
    token = text.split()[0]
    return token if token.startswith("QTY_") else "QTY_INVALID"


def _prices_close(
    left: Decimal | None,
    right: Decimal | None,
    *,
    epsilon: Decimal = Decimal("0.00000001"),
) -> bool:
    if left is None or right is None:
        return False
    return abs(left - right) <= epsilon


def _record_timestamp(record) -> datetime | None:
    if record.local_timestamp is not None:
        return _as_utc_aware(datetime.fromisoformat(record.local_timestamp))
    if record.source_timestamp is not None:
        return _as_utc_aware(datetime.fromisoformat(record.source_timestamp))
    return None


def _as_utc_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _played_quantity_from_request(
    request: PlaceOrderCommandRequest | object | None,
) -> float:
    if isinstance(request, PlaceOrderCommandRequest) and request.orderQty is not None:
        return float(request.orderQty)
    return 0.0


def _ack_price(value: Price | float | object | None) -> Price | float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, (Decimal, str)):
        return Price(to_decimal(value))
    raise TypeError(f"Unsupported ack price value type: {type(value)!r}")


def _ack_quantity(value: object | None) -> OrderQty | float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, (Decimal, str)):
        return OrderQty(to_decimal(value))
    raise TypeError(f"Unsupported ack quantity value type: {type(value)!r}")
