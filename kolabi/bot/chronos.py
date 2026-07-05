"""Synchronous strategy supervisor shell around the pure Isis reducer.

Purpose: own persistent strategy cache, apply event ordering and deduplication,
activate dependencies/repeats, and return typed runtime commands to the caller.
Inputs: typed `EggMove` values from market/private/account listeners.
Outputs: typed bot commands and supervisor notices.
Side effects: none outside local supervisor state.
Important types: `StrategyState`, `EggMove`, `DragonSong`, `ChronosNotice`.
Role: deterministic orchestration shell.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Iterable

from kolabi.bot.domain import (
    ChainDependencyToken,
    EggMove,
    EggMoveKind,
    HeadState,
    OrderPairSpec,
    OrderRole,
    PairCycleState,
    StrategyState,
    TailState,
)
from kolabi.bot.horus import plan_runtime_commands
from kolabi.bot.isis import step_strategy
from kolabi.bot.pricing import pair_window_is_open
from kolabi.bot.repeat_adjustment import apply_repeat_adjustment
from kolabi.shared.core.runtime_types import DragonSong, RuntimeCommandKind, Symbol


class ChronosNoticeKind(StrEnum):
    DUPLICATE_EVENT_IGNORED = "DuplicateEventIgnored"
    PUBLIC_EVENT_IGNORED = "PublicEventIgnoredByPrivateTerminal"
    PENDING_IDENTITY_TIMEOUT = "PendingIdentityTimeout"


@dataclass(frozen=True)
class ChronosNotice:
    kind: ChronosNoticeKind
    pair_name: str | None
    event_id: str | None
    noted_at: datetime
    detail: str | None = None


@dataclass(frozen=True)
class PendingEggMove:
    event: EggMove
    first_seen_at: datetime


@dataclass(frozen=True)
class PendingRepeat:
    pair_name: str
    ready_at: datetime
    next_attempt: int
    next_pair: OrderPairSpec | None = None
    terminal_event: EggMove | None = None


class HookTargetKind(StrEnum):
    HEAD_FILLED = "head_filled"
    PAIR_CLOSED = "pair_closed"


@dataclass(frozen=True)
class HookTarget:
    origin_pair_name: str
    kind: HookTargetKind


@dataclass
class Chronos:
    """Supervise typed strategy events and forward typed runtime commands."""

    state: StrategyState
    pending_timeout: timedelta = timedelta(seconds=30)
    notices: list[ChronosNotice] = field(default_factory=list)
    pending: dict[str, PendingEggMove] = field(default_factory=dict)
    pending_repeats: dict[str, PendingRepeat] = field(default_factory=dict)
    _seen_event_keys: set[tuple[str, str]] = field(default_factory=set)
    _seen_fallback_keys: set[tuple[str, str, int]] = field(default_factory=set)
    _seen_command_keys: set[tuple[str, str, str | None]] = field(default_factory=set)

    def process_events(
        self,
        events: Iterable[EggMove],
        *,
        now: datetime | None = None,
    ) -> tuple[DragonSong, ...]:
        """Applique precedence et routage sur un lot d'evenements."""
        current_time = now or datetime.now(timezone.utc)
        selected: list[EggMove] = []
        private_terminal_pairs: dict[str, EggMove] = {}
        public_ignored: list[EggMove] = []

        for event in events:
            pair_name = resolve_pair_name(self.state, event) or event.pair_name
            if pair_name and _is_private_terminal(event):
                private_terminal_pairs[pair_name] = event
                continue
            selected.append(event)

        if private_terminal_pairs:
            filtered: list[EggMove] = []
            for event in selected:
                pair_name = resolve_pair_name(self.state, event) or event.pair_name
                if pair_name and pair_name in private_terminal_pairs and not event.is_private:
                    public_ignored.append(event)
                    continue
                filtered.append(event)
            selected = filtered + list(private_terminal_pairs.values())

        for event in public_ignored:
            self.notices.append(
                ChronosNotice(
                    kind=ChronosNoticeKind.PUBLIC_EVENT_IGNORED,
                    pair_name=resolve_pair_name(self.state, event) or event.pair_name,
                    event_id=event.event_id,
                    noted_at=current_time,
                    detail="private terminal event has precedence",
                )
            )

        emitted: list[DragonSong] = []
        for event in selected:
            emitted.extend(self.process_event(event, now=current_time))
        return self._dedupe_commands(emitted)

    def process_event(
        self,
        event: EggMove,
        *,
        now: datetime | None = None,
    ) -> tuple[DragonSong, ...]:
        """Traite un evenement unique avec deduplication et attente d'identite."""
        current_time = now or datetime.now(timezone.utc)
        if self._needs_pending_identity(event):
            pending_key = event.event_id or f"pending:{id(event)}"
            self.pending[pending_key] = PendingEggMove(event=event, first_seen_at=current_time)
            return ()

        pair_name = resolve_pair_name(self.state, event) or event.pair_name
        event_key = self._event_key(pair_name, event)
        if event_key is not None:
            if len(event_key) == 2:
                key = (event_key[0], event_key[1])
                if key in self._seen_event_keys:
                    self._record_duplicate(pair_name, event, current_time)
                    return ()
                self._seen_event_keys.add(key)
            else:
                fallback_key = (event_key[0], event_key[1], int(event_key[2]))
                if fallback_key in self._seen_fallback_keys:
                    self._record_duplicate(pair_name, event, current_time)
                    return ()
                self._seen_fallback_keys.add(fallback_key)

        self.state, intents = step_strategy(self.state, _with_target_pair(event, pair_name))
        pair_state = self.state.pairs.get(pair_name) if pair_name is not None else None
        commands = () if pair_state is None else plan_runtime_commands(
            pair_state,
            intents,
            symbol=Symbol(event.symbol),
        )
        chained_commands = self._activate_dependent_pairs(event)
        repeated_commands = self._schedule_or_activate_repeat(pair_name, event, current_time)
        return tuple(commands) + chained_commands + repeated_commands

    def expire_pending(
        self,
        *,
        now: datetime | None = None,
    ) -> tuple[ChronosNotice, ...]:
        """Expire les evenements sans identite suffisante apres delai."""
        current_time = now or datetime.now(timezone.utc)
        expired: list[str] = []
        notices: list[ChronosNotice] = []
        for key, pending_event in self.pending.items():
            if current_time - pending_event.first_seen_at < self.pending_timeout:
                continue
            notices.append(
                ChronosNotice(
                    kind=ChronosNoticeKind.PENDING_IDENTITY_TIMEOUT,
                    pair_name=pending_event.event.pair_name,
                    event_id=pending_event.event.event_id,
                    noted_at=current_time,
                    detail="missing identity prevents confirmation match",
                )
            )
            expired.append(key)
        for key in expired:
            self.pending.pop(key, None)
        self.notices.extend(notices)
        return tuple(notices)

    def activate_ready_repeats(
        self,
        *,
        symbol: str,
        now: datetime | None = None,
    ) -> tuple[DragonSong, ...]:
        """Reset pairs whose configured repeat wait has elapsed."""
        current_time = now or datetime.now(timezone.utc)
        ready = [
            pending
            for pending in self.pending_repeats.values()
            if pending.ready_at <= current_time
        ]
        emitted: list[DragonSong] = []
        for pending in ready:
            self.pending_repeats.pop(pending.pair_name, None)
            emitted.extend(
                self._activate_repeat_pair(
                    pending.pair_name,
                    next_attempt=pending.next_attempt,
                    next_pair=pending.next_pair,
                    terminal_event=pending.terminal_event,
                )
            )
        return self._dedupe_commands(emitted)

    def _record_duplicate(
        self,
        pair_name: str | None,
        event: EggMove,
        current_time: datetime,
    ) -> None:
        self.notices.append(
            ChronosNotice(
                kind=ChronosNoticeKind.DUPLICATE_EVENT_IGNORED,
                pair_name=pair_name,
                event_id=event.event_id,
                noted_at=current_time,
            )
        )

    def _needs_pending_identity(self, event: EggMove) -> bool:
        if not event.is_private:
            return False
        if resolve_pair_name(self.state, event) or event.pair_name:
            return False
        if event.event_id:
            return False
        return _event_order_id(event) is None

    def _event_key(
        self,
        pair_name: str | None,
        event: EggMove,
    ) -> tuple[str, str] | tuple[str, str, float] | None:
        if pair_name is None:
            return None
        if event.event_id:
            return pair_name, event.event_id
        order_id = _event_order_id(event)
        status = _event_status(event)
        if order_id is None or status is None:
            return None
        ts_bucket = event.occurred_at.timestamp() // 1
        return pair_name, f"{order_id}:{status}", ts_bucket

    def _dedupe_commands(
        self,
        commands: Iterable[DragonSong],
    ) -> tuple[DragonSong, ...]:
        per_pair: dict[str, DragonSong] = {}
        for command in commands:
            pair_name = _command_pair_name(command)
            if pair_name is None:
                continue
            attempt = _pair_attempt(self.state, pair_name)
            command_key = (f"{pair_name}:{attempt}", f"{command.kind}:{command.reason}", _command_dedupe_value(command))
            if command_key in self._seen_command_keys:
                continue
            self._seen_command_keys.add(command_key)
            previous = per_pair.get(pair_name)
            if previous is None or _command_precedence(command) >= _command_precedence(previous):
                per_pair[pair_name] = command
        return tuple(per_pair.values())

    def _activate_dependent_pairs(self, event: EggMove) -> tuple[DragonSong, ...]:
        """Release dependent pairs after a fresh matching origin event."""
        origin_pair = resolve_pair_name(self.state, event) or event.pair_name
        if origin_pair is None:
            return ()
        if not event.is_private:
            return ()
        origin_state = self.state.pairs.get(origin_pair)
        if origin_state is None:
            return ()
        replacements: dict[str, PairCycleState] = {}
        for pair_name, pair_state in self.state.pairs.items():
            if pair_name == origin_pair:
                continue
            if pair_state.head_state != HeadState.LATENT:
                continue
            if pair_state.dependency_token is not None:
                continue
            target = parse_hook_target(pair_state.pair.hook_name)
            if target is None or target.origin_pair_name != origin_pair:
                continue
            if not hook_target_satisfied(target, event, origin_state):
                continue
            if not pair_window_is_open(
                pair_state.pair,
                launched_at=self.state.launched_at,
                now=event.occurred_at,
            ):
                continue
            replacements[pair_name] = replace(
                pair_state,
                dependency_token=ChainDependencyToken(
                    origin_pair_name=origin_pair,
                    origin_attempt_index=origin_state.attempt_index,
                    closed_at=event.occurred_at,
                ),
            )
        if replacements:
            self.state = replace(
                self.state,
                pairs={**self.state.pairs, **replacements},
            )
        return ()

    def _schedule_or_activate_repeat(
        self,
        pair_name: str | None,
        event: EggMove,
        current_time: datetime,
    ) -> tuple[DragonSong, ...]:
        if pair_name is None:
            return ()
        pair_state = self.state.pairs.get(pair_name)
        if pair_state is None or not _pair_terminal_for_repeat(pair_state):
            return ()
        next_attempt = pair_state.attempt_index + 1
        attempts = pair_state.pair.attempts
        if attempts is not None and next_attempt > max(attempts, 1):
            return ()
        if pair_name in self.pending_repeats:
            return ()
        wait_minutes = _repeat_wait_minutes(pair_state, event)
        ready_at = current_time + timedelta(minutes=wait_minutes)
        next_pair = _next_repeat_pair(
            pair_state,
            event,
            next_attempt=next_attempt,
        )
        if not pair_window_is_open(
            next_pair,
            launched_at=self.state.launched_at,
            now=ready_at,
        ):
            return ()
        if ready_at > current_time:
            self.pending_repeats[pair_name] = PendingRepeat(
                pair_name=pair_name,
                ready_at=ready_at,
                next_attempt=next_attempt,
                next_pair=next_pair,
                terminal_event=event,
            )
            return ()
        self._activate_repeat_pair(
            pair_name,
            next_attempt=next_attempt,
            next_pair=next_pair,
            terminal_event=event,
        )
        return ()

    def _activate_repeat_pair(
        self,
        pair_name: str,
        *,
        next_attempt: int,
        next_pair: OrderPairSpec | None = None,
        terminal_event: EggMove | None = None,
    ) -> tuple[DragonSong, ...]:
        pair_state = self.state.pairs.get(pair_name)
        if pair_state is None:
            return ()
        if next_pair is None and terminal_event is not None:
            next_pair = _next_repeat_pair(
                pair_state,
                terminal_event,
                next_attempt=next_attempt,
            )
        if next_pair is None:
            next_pair = pair_state.pair
        reset_state = replace(
            pair_state,
            pair=next_pair,
            head_state=HeadState.LATENT,
            tail_state=None,
            tail_mode=None,
            head_identity=None,
            tail_identity=None,
            tail_trail=None,
            head_trigger_reference_price=None,
            head_trigger_reference_source=None,
            head_trigger_reference_at=None,
            head_order_price=None,
            head_order_stop_price=None,
            head_order_quantity=None,
            dependency_token=None,
            played_quantity=None,
            latest_commands=None,
            last_processed_private_event_id=None,
            last_processed_private_event_ts=None,
            last_emitted_command_id=None,
            last_emitted_command_ts=None,
            attempt_index=next_attempt,
            completed_at=None,
        )
        self.state = replace(
            self.state,
            pairs={**self.state.pairs, pair_name: reset_state},
        )
        return ()


def _with_target_pair(event: EggMove, pair_name: str | None) -> EggMove:
    if pair_name is None or event.pair_name == pair_name:
        return event
    return EggMove(
        kind=event.kind,
        occurred_at=event.occurred_at,
        symbol=event.symbol,
        order=event.order,
        reply=event.reply,
        event_id=event.event_id,
        pair_name=pair_name,
        role=event.role,
        is_private=event.is_private,
    )


def _command_pair_name(command: DragonSong) -> str | None:
    return command.pair_name or None


def _command_client_order_id(command: DragonSong) -> str | None:
    return getattr(command.request, "clOrdID", None)


def _command_dedupe_value(command: DragonSong) -> str | None:
    client_order_id = _command_client_order_id(command) or ""
    price = getattr(command.request, "newPrice", None)
    if price is None:
        price = getattr(command.request, "stopPx", None)
    return f"{client_order_id}:{price}"


def resolve_pair_name(state: StrategyState, event: EggMove) -> str | None:
    """Resout la paire cible avant delegation au reducer Isis."""
    if event.pair_name and event.pair_name in state.pairs:
        return event.pair_name

    for payload in (event.order, event.reply):
        candidate = _string_or_none(None if payload is None else payload.get("pair_name"))
        if candidate and candidate in state.pairs:
            return candidate

    client_order_id = _identity_field(event, "clOrdID")
    exchange_order_id = _identity_field(event, "orderID")
    if client_order_id is None and exchange_order_id is None:
        return None

    for pair_name, pair_state in state.pairs.items():
        for identity in (pair_state.head_identity, pair_state.tail_identity):
            if identity is None:
                continue
            if client_order_id and identity.client_order_id == client_order_id:
                return pair_name
            if exchange_order_id and identity.exchange_order_id == exchange_order_id:
                return pair_name
    return None


def _identity_field(event: EggMove, field: str) -> str | None:
    for payload in (event.order, event.reply):
        if payload is None:
            continue
        candidate = _string_or_none(payload.get(field))
        if candidate:
            return candidate
    return None


def _string_or_none(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _command_precedence(command: DragonSong) -> int:
    if command.kind == RuntimeCommandKind.CANCEL:
        return 30
    if command.kind == RuntimeCommandKind.AMEND:
        return 20
    if command.kind == RuntimeCommandKind.PLACE:
        return 10
    return 0


def _event_order_id(event: EggMove) -> str | None:
    for payload in (event.order, event.reply):
        if payload is None:
            continue
        for key_name in ("orderID", "clOrdID"):
            candidate = payload.get(key_name)
            if isinstance(candidate, str) and candidate:
                return candidate
    return None


def _event_status(event: EggMove) -> str | None:
    for payload in (event.reply, event.order):
        if payload is None:
            continue
        for key_name in ("ordStatus", "status"):
            candidate = payload.get(key_name)
            if isinstance(candidate, str) and candidate:
                return candidate
    return None


def _is_private_terminal(event: EggMove) -> bool:
    return event.is_private and event.kind in {
        EggMoveKind.NOT_PLAYED_CANCELED,
        EggMoveKind.PLAYED_AND_CANCELED,
    }


def _pair_attempt(state: StrategyState, pair_name: str) -> int:
    pair_state = state.pairs.get(pair_name)
    return 1 if pair_state is None else pair_state.attempt_index


def _pair_terminal_for_repeat(pair_state) -> bool:
    if pair_state.head_state == HeadState.FAILED:
        return True
    if pair_state.head_state != HeadState.CLOSED:
        return False
    played = pair_state.played_quantity is not None and pair_state.played_quantity > 0
    if not played:
        return True
    return pair_state.tail_state in {TailState.CLOSED, TailState.FAILED}


def _repeat_wait_minutes(pair_state: PairCycleState, event: EggMove) -> float:
    pause_minutes = max(pair_state.pair.pause_minutes or 0.0, 0.0)
    if not _tail_fill_closed_repeat_event(pair_state, event):
        return pause_minutes
    return pause_minutes + max(pair_state.pair.cooldown_minutes or 0.0, 0.0)


def _next_repeat_pair(
    pair_state: PairCycleState,
    event: EggMove,
    *,
    next_attempt: int,
) -> OrderPairSpec:
    return apply_repeat_adjustment(
        pair_state,
        event,
        next_attempt=next_attempt,
    )


def _tail_fill_closed_repeat_event(pair_state: PairCycleState, event: EggMove) -> bool:
    if not event.is_private:
        return False
    if event.kind != EggMoveKind.PLAYED_AND_CANCELED:
        return False
    if event.role == OrderRole.HEAD:
        return False
    if pair_state.tail_state != TailState.CLOSED:
        return False
    return pair_state.played_quantity is not None and pair_state.played_quantity > 0


def pair_dependency_satisfied(state: StrategyState, pair_state: PairCycleState) -> bool:
    """Return true when a pair has no hook or has consumed a fresh close token."""
    hook_name = (pair_state.pair.hook_name or "").strip()
    if not hook_name:
        return True
    del state
    return pair_state.dependency_token is not None


def parse_hook_target(raw: str | None) -> HookTarget | None:
    """Parse one strategy hook dependency into a typed activation target."""

    hook_name = (raw or "").strip()
    if not hook_name:
        return None
    if hook_name.endswith("-head-filled"):
        origin_name = hook_name[: -len("-head-filled")]
        return None if not origin_name else HookTarget(origin_name, HookTargetKind.HEAD_FILLED)
    for suffix in ("-tail-closed", "-closed"):
        if hook_name.endswith(suffix):
            origin_name = hook_name[: -len(suffix)]
            return None if not origin_name else HookTarget(origin_name, HookTargetKind.PAIR_CLOSED)
    return HookTarget(hook_name, HookTargetKind.PAIR_CLOSED)


def hook_target_satisfied(
    target: HookTarget,
    event: EggMove,
    origin_state: PairCycleState,
) -> bool:
    """Return true when a private origin event satisfies a hook target."""

    if target.kind == HookTargetKind.HEAD_FILLED:
        return _is_private_head_filled(event, origin_state)
    if target.kind == HookTargetKind.PAIR_CLOSED:
        return _is_private_terminal(event) and _pair_closed_successfully(origin_state)


def _pair_closed_successfully(pair_state: PairCycleState) -> bool:
    if pair_state.head_state != HeadState.CLOSED:
        return False
    played = pair_state.played_quantity is not None and pair_state.played_quantity > 0
    if not played:
        return True
    return pair_state.tail_state == TailState.CLOSED


def _is_private_head_filled(event: EggMove, pair_state: PairCycleState) -> bool:
    if not event.is_private:
        return False
    if event.role not in {None, OrderRole.HEAD}:
        return False
    if event.kind not in {
        EggMoveKind.PLAYED_NOT_CANCELED,
        EggMoveKind.PLAYED_AND_CANCELED,
    }:
        return False
    return pair_state.played_quantity is not None and pair_state.played_quantity > 0
