from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from kolabi.bot.chronos import (
    Chronos,
    ChronosNoticeKind,
    PendingRepeat,
    pair_dependency_satisfied,
)
from kolabi.bot.domain import (
    EggMove,
    EggMoveKind,
    HeadSpec,
    HeadState,
    HookEvidence,
    HookTarget,
    HookTargetKind,
    OrderIdentity,
    OrderPairSpec,
    OrderRole,
    PairCycleState,
    Side,
    StrategyState,
    TailMode,
    TailSpec,
    TailState,
    TimeWindow,
)
from kolabi.bot.repeat_adjustment import (
    RepeatAdjustmentContext,
    RepeatAdjustmentError,
    RepeatTermination,
    head_timed_out,
    parse_repeat_adjustment,
    register_repeat_adjustment,
)
from kolabi.shared.core.runtime_types import (
    DragonSong,
    PlaceHeadCommand,
    PlaceOrderCommandRequest,
    RuntimeCommandKind,
    Symbol,
)


def sample_pair(name: str) -> OrderPairSpec:
    return OrderPairSpec(
        name=name,
        window=TimeWindow(start_minutes=-1.0, end_minutes=10.0),
        try_num=1,
        dr_pause=None,
        timeout=60,
        head=HeadSpec(side=Side.BUY, order_type="Limit"),
        head_price=(100.0, 101.0),
        head_price_type="pA",
        head_quantity=1,
        head_quantity_type="qA",
        tail=TailSpec(side=Side.SELL, order_type="Stop", delta=0.5),
        tail_price_spec=99.0,
        tail_price_spec_type="tA",
        amount_type="qApD",
    )


def sample_state() -> StrategyState:
    launched_at = datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc)
    submitted_b = PairCycleState(
        pair=sample_pair("pair-b"),
        head_state=HeadState.SUBMITTED,
        head_identity=OrderIdentity(
            pair_name="pair-b",
            role="head",
            client_order_id="CID-B",
            exchange_order_id="OID-B",
        ),
    )
    return StrategyState(
        launched_at=launched_at,
        strategy_id="strategy-1",
        pairs={
            "pair-a": PairCycleState(pair=sample_pair("pair-a")),
            "pair-b": submitted_b,
            "pair-c": PairCycleState(pair=sample_pair("pair-c")),
        },
    )


def close_evidence(
    origin_pair_name: str,
    origin_attempt_index: int,
    satisfied_at: datetime,
) -> HookEvidence:
    return HookEvidence(
        target=HookTarget(origin_pair_name, HookTargetKind.PAIR_CLOSED),
        origin_attempt_index=origin_attempt_index,
        satisfied_at=satisfied_at,
    )


def register_test_scurve(name: str = "test_scurve") -> str:
    def test_scurve(
        pair: OrderPairSpec,
        context: RepeatAdjustmentContext,
    ) -> OrderPairSpec:
        if context.termination == RepeatTermination.SUCCESSFUL_TAIL_CLOSE:
            return _scale_test_head_price(pair, Decimal("1.10"))
        if context.termination in {
            RepeatTermination.LATENT_TIMEOUT,
            RepeatTermination.UNFILLED_CANCEL,
        }:
            return _scale_test_head_price(pair, Decimal("0.80"))
        return pair

    register_repeat_adjustment(name, test_scurve)
    return name


def _scale_test_head_price(pair: OrderPairSpec, factor: Decimal) -> OrderPairSpec:
    return replace(
        pair,
        head_price=(
            float(Decimal(str(pair.head_price[0])) * factor),
            float(Decimal(str(pair.head_price[1])) * factor),
        ),
    )


def test_repeat_adjustment_parser_accepts_colon_arguments() -> None:
    request = parse_repeat_adjustment("head_offset_toggle: 3, -8")

    assert request is not None
    assert request.name == "head_offset_toggle"
    assert request.args == (Decimal("3"), Decimal("-8"))


def test_head_timed_out_reads_runtime_reason_from_private_cancel() -> None:
    occurred_at = datetime(2026, 5, 21, 12, 1, tzinfo=timezone.utc)
    pair = sample_pair("pair-r")
    previous = PairCycleState(
        pair=pair,
        head_state=HeadState.FAILED,
        played_quantity=Decimal("0"),
    )
    event = EggMove(
        kind=EggMoveKind.NOT_PLAYED_CANCELED,
        occurred_at=occurred_at,
        symbol="PI_XBTUSD",
        pair_name="pair-r",
        role=OrderRole.HEAD,
        is_private=True,
        reply={
            "execType": "cancelled_by_user",
            "runtime_reason": "head_timeout",
            "attempt_index": 1,
            "cumQty": 0.0,
        },
    )
    context = RepeatAdjustmentContext(
        previous_state=previous,
        terminal_event=event,
        next_attempt=2,
        termination=RepeatTermination.UNFILLED_CANCEL,
    )

    assert head_timed_out(context)


@pytest.mark.parametrize(
    "raw",
    [
        "head_offset_once:",
        "head_offset_toggle: 1,,2",
        "head_offset_toggle: nope",
    ],
)
def test_repeat_adjustment_parser_rejects_bad_arguments(raw: str) -> None:
    with pytest.raises(RepeatAdjustmentError):
        parse_repeat_adjustment(raw)


def test_chronos_dedupes_duplicate_event() -> None:
    chronos = Chronos(state=sample_state())
    move = EggMove(
        kind=EggMoveKind.NOT_PLAYED_CANCELED,
        occurred_at=datetime(2026, 5, 21, 12, 1, tzinfo=timezone.utc),
        symbol="PI_XBTUSD",
        pair_name="pair-b",
        event_id="evt-1",
        is_private=True,
    )

    first = chronos.process_event(move)
    second = chronos.process_event(move)

    assert first == ()
    assert second == ()
    assert chronos.state.pairs["pair-b"].head_state == HeadState.FAILED
    assert chronos.notices[-1].kind == ChronosNoticeKind.DUPLICATE_EVENT_IGNORED


def test_chronos_private_terminal_precedence() -> None:
    chronos = Chronos(state=sample_state())
    public_move = EggMove(
        kind=EggMoveKind.HEAD_HOOKED,
        occurred_at=datetime(2026, 5, 21, 12, 1, tzinfo=timezone.utc),
        symbol="PI_XBTUSD",
        pair_name="pair-b",
        event_id="evt-public",
    )
    private_terminal = EggMove(
        kind=EggMoveKind.NOT_PLAYED_CANCELED,
        occurred_at=datetime(2026, 5, 21, 12, 1, 1, tzinfo=timezone.utc),
        symbol="PI_XBTUSD",
        pair_name="pair-b",
        event_id="evt-private",
        is_private=True,
    )

    commands = chronos.process_events([public_move, private_terminal])

    assert commands == ()
    assert chronos.state.pairs["pair-b"].head_state == HeadState.FAILED
    assert any(notice.kind == ChronosNoticeKind.PUBLIC_EVENT_IGNORED for notice in chronos.notices)


def test_chronos_dedupes_duplicate_command() -> None:
    chronos = Chronos(state=sample_state())
    commands: list[DragonSong] = [
        PlaceHeadCommand(
            kind=kind,
            symbol=Symbol("PI_XBTUSD"),
            request=PlaceOrderCommandRequest(
                pair_name="pair-b",
                side="buy",
                ordType="Limit",
                clOrdID="CID-B",
            ),
            pair_name="pair-b",
        )
        for kind in (RuntimeCommandKind.PLACE, RuntimeCommandKind.PLACE)
    ]

    deduped = chronos._dedupe_commands(commands)

    assert len(deduped) == 1


def test_chronos_requires_identity_for_confirmation_match() -> None:
    chronos = Chronos(state=sample_state())
    private_move = EggMove(
        kind=EggMoveKind.NOT_PLAYED_NOR_CANCELED,
        occurred_at=datetime(2026, 5, 21, 12, 2, tzinfo=timezone.utc),
        symbol="PI_XBTUSD",
        is_private=True,
    )

    commands = chronos.process_event(private_move)

    assert commands == ()
    assert len(chronos.pending) == 1
    assert chronos.state == sample_state()


def test_chronos_pending_identity_timeout_is_typed() -> None:
    chronos = Chronos(state=sample_state(), pending_timeout=timedelta(seconds=5))
    private_move = EggMove(
        kind=EggMoveKind.NOT_PLAYED_NOR_CANCELED,
        occurred_at=datetime(2026, 5, 21, 12, 2, tzinfo=timezone.utc),
        symbol="PI_XBTUSD",
        is_private=True,
    )
    chronos.process_event(private_move, now=datetime(2026, 5, 21, 12, 2, tzinfo=timezone.utc))

    notices = chronos.expire_pending(now=datetime(2026, 5, 21, 12, 2, 6, tzinfo=timezone.utc))

    assert len(notices) == 1
    assert notices[0].kind == ChronosNoticeKind.PENDING_IDENTITY_TIMEOUT


def test_chronos_emits_no_exchange_payloads_directly() -> None:
    chronos = Chronos(state=sample_state())
    move = EggMove(
        kind=EggMoveKind.HEAD_HOOKED,
        occurred_at=datetime(2026, 5, 21, 12, 3, tzinfo=timezone.utc),
        symbol="PI_XBTUSD",
        pair_name="pair-a",
        event_id="evt-3",
    )

    commands = chronos.process_event(move, now=move.occurred_at)

    assert commands
    assert all(isinstance(command, DragonSong.__args__) for command in commands)
    assert all(not isinstance(command, dict) for command in commands)


def test_chronos_emits_typed_runtime_commands_only() -> None:
    chronos = Chronos(state=sample_state())
    move = EggMove(
        kind=EggMoveKind.HEAD_HOOKED,
        occurred_at=datetime(2026, 5, 21, 12, 4, tzinfo=timezone.utc),
        symbol="PI_XBTUSD",
        pair_name="pair-a",
        event_id="evt-4",
    )

    commands = chronos.process_event(move, now=move.occurred_at)

    assert commands
    assert all(isinstance(command, DragonSong.__args__) for command in commands)


def test_chronos_processes_batch_events() -> None:
    chronos = Chronos(state=sample_state())
    commands = chronos.process_events(
        [
            EggMove(
                kind=EggMoveKind.HEAD_HOOKED,
                occurred_at=datetime(2026, 5, 21, 12, 5, tzinfo=timezone.utc),
                symbol="PI_XBTUSD",
                pair_name="pair-a",
                event_id="evt-5",
            )
        ]
    )

    assert commands
    assert isinstance(commands[0], DragonSong.__args__)


def test_closed_tail_makes_dependent_pair_eligible_without_direct_command() -> None:
    pair_x = sample_pair("pair-x")
    pair_y = replace(sample_pair("pair-y"), hook_name="pair-x")
    state = StrategyState(
        launched_at=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
        strategy_id="strategy-chain",
        pairs={
            "pair-x": PairCycleState(
                pair=pair_x,
                head_state=HeadState.CLOSED,
                tail_state=TailState.CLOSED,
                tail_mode=TailMode.FLYING,
                played_quantity=Decimal("1"),
            ),
            "pair-y": PairCycleState(pair=pair_y),
        },
    )
    chronos = Chronos(state=state)
    move = EggMove(
        kind=EggMoveKind.PLAYED_AND_CANCELED,
        occurred_at=datetime(2026, 5, 21, 12, 6, tzinfo=timezone.utc),
        symbol="PI_XBTUSD",
        pair_name="pair-x",
        event_id="evt-chain",
        is_private=True,
    )

    commands = chronos.process_event(move)

    assert commands == ()
    assert chronos.state.pairs["pair-y"].head_state == HeadState.LATENT
    assert pair_dependency_satisfied(chronos.state, chronos.state.pairs["pair-y"]) is True


def test_tail_closed_hook_does_not_activate_on_head_close_only() -> None:
    pair_x = sample_pair("pair-x")
    pair_y = replace(sample_pair("pair-y"), hook_name="pair-x-tail-closed")
    state = StrategyState(
        launched_at=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
        strategy_id="strategy-chain",
        pairs={
            "pair-x": PairCycleState(
                pair=pair_x,
                head_state=HeadState.SUBMITTED,
                head_identity=OrderIdentity(
                    pair_name="pair-x",
                    role="head",
                    client_order_id="CID-X-H",
                    exchange_order_id="OID-X-H",
                ),
            ),
            "pair-y": PairCycleState(pair=pair_y),
        },
    )
    chronos = Chronos(state=state)

    commands = chronos.process_event(
        EggMove(
            kind=EggMoveKind.PLAYED_AND_CANCELED,
            occurred_at=datetime(2026, 5, 21, 12, 6, tzinfo=timezone.utc),
            symbol="PI_XBTUSD",
            pair_name="pair-x",
            role=None,
            event_id="evt-chain-head-only",
            reply={
                "orderID": "OID-X-H",
                "clOrdID": "CID-X-H",
                "cumQty": 1.0,
                "orderQty": 1.0,
            },
            is_private=True,
        )
    )

    assert commands
    assert {command.pair_name for command in commands} == {"pair-x"}
    assert chronos.state.pairs["pair-x"].head_state == HeadState.CLOSED
    assert chronos.state.pairs["pair-x"].tail_state == TailState.HOOKED
    assert chronos.state.pairs["pair-y"].head_state == HeadState.LATENT
    assert pair_dependency_satisfied(chronos.state, chronos.state.pairs["pair-y"]) is False


def test_head_filled_hook_activates_dependent_pair_on_head_fill() -> None:
    pair_x = sample_pair("pair-x")
    pair_y = replace(sample_pair("pair-y"), hook_name="pair-x-head-filled")
    state = StrategyState(
        launched_at=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
        strategy_id="strategy-head-chain",
        pairs={
            "pair-x": PairCycleState(
                pair=pair_x,
                head_state=HeadState.SUBMITTED,
                head_identity=OrderIdentity(
                    pair_name="pair-x",
                    role="head",
                    client_order_id="CID-X-H",
                    exchange_order_id="OID-X-H",
                ),
            ),
            "pair-y": PairCycleState(pair=pair_y),
        },
    )
    chronos = Chronos(state=state)

    commands = chronos.process_event(
        EggMove(
            kind=EggMoveKind.PLAYED_AND_CANCELED,
            occurred_at=datetime(2026, 5, 21, 12, 6, tzinfo=timezone.utc),
            symbol="PI_XBTUSD",
            pair_name="pair-x",
            role=OrderRole.HEAD,
            event_id="evt-head-filled",
            reply={
                "orderID": "OID-X-H",
                "clOrdID": "CID-X-H",
                "cumQty": 1.0,
                "orderQty": 1.0,
            },
            is_private=True,
        )
    )

    assert commands
    assert {command.pair_name for command in commands} == {"pair-x"}
    assert chronos.state.pairs["pair-y"].head_state == HeadState.LATENT
    assert pair_dependency_satisfied(chronos.state, chronos.state.pairs["pair-y"]) is True


def test_head_filled_hook_ignores_tail_close_event() -> None:
    pair_x = sample_pair("pair-x")
    pair_y = replace(sample_pair("pair-y"), hook_name="pair-x-head-filled")
    state = StrategyState(
        launched_at=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
        strategy_id="strategy-head-chain",
        pairs={
            "pair-x": PairCycleState(
                pair=pair_x,
                head_state=HeadState.CLOSED,
                tail_state=TailState.CLOSED,
                played_quantity=Decimal("1"),
            ),
            "pair-y": PairCycleState(pair=pair_y),
        },
    )
    chronos = Chronos(state=state)

    commands = chronos.process_event(
        EggMove(
            kind=EggMoveKind.PLAYED_AND_CANCELED,
            occurred_at=datetime(2026, 5, 21, 12, 7, tzinfo=timezone.utc),
            symbol="PI_XBTUSD",
            pair_name="pair-x",
            role=OrderRole.TAIL,
            event_id="evt-tail-closed",
            reply={"orderID": "OID-X-T", "cumQty": 1.0, "orderQty": 1.0},
            is_private=True,
        )
    )

    assert commands == ()
    assert pair_dependency_satisfied(chronos.state, chronos.state.pairs["pair-y"]) is False


def test_all_hook_waits_for_fresh_evidence_from_every_target() -> None:
    launched_at = datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc)
    dependent = replace(
        sample_pair("dependent"),
        hook_name="all(origin-a-tail-closed, origin-b-tail-closed)",
    )
    state = StrategyState(
        launched_at=launched_at,
        strategy_id="strategy-all-chain",
        pairs={
            "origin-a": PairCycleState(
                pair=sample_pair("origin-a"),
                head_state=HeadState.CLOSED,
                tail_state=TailState.CLOSED,
                played_quantity=Decimal("1"),
            ),
            "origin-b": PairCycleState(
                pair=sample_pair("origin-b"),
                head_state=HeadState.CLOSED,
                tail_state=TailState.CLOSED,
                played_quantity=Decimal("1"),
            ),
            "dependent": PairCycleState(
                pair=dependent,
                dependency_armed_at=launched_at,
            ),
        },
    )
    chronos = Chronos(state=state)

    chronos.process_event(
        EggMove(
            kind=EggMoveKind.PLAYED_AND_CANCELED,
            occurred_at=launched_at + timedelta(minutes=1),
            symbol="PI_XBTUSD",
            pair_name="origin-a",
            event_id="origin-a-closed",
            is_private=True,
        )
    )

    dependent_state = chronos.state.pairs["dependent"]
    assert len(dependent_state.dependency_evidence) == 1
    assert not pair_dependency_satisfied(chronos.state, dependent_state)

    chronos.process_event(
        EggMove(
            kind=EggMoveKind.PLAYED_AND_CANCELED,
            occurred_at=launched_at + timedelta(minutes=2),
            symbol="PI_XBTUSD",
            pair_name="origin-b",
            event_id="origin-b-closed",
            is_private=True,
        )
    )

    dependent_state = chronos.state.pairs["dependent"]
    assert len(dependent_state.dependency_evidence) == 2
    assert pair_dependency_satisfied(chronos.state, dependent_state)


def test_any_hook_releases_on_first_fresh_target() -> None:
    launched_at = datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc)
    dependent = replace(
        sample_pair("dependent"),
        hook_name="any(origin-a-tail-closed,origin-b-head-filled)",
    )
    state = StrategyState(
        launched_at=launched_at,
        strategy_id="strategy-any-chain",
        pairs={
            "origin-a": PairCycleState(
                pair=sample_pair("origin-a"),
                head_state=HeadState.CLOSED,
                tail_state=TailState.CLOSED,
                played_quantity=Decimal("1"),
            ),
            "origin-b": PairCycleState(pair=sample_pair("origin-b")),
            "dependent": PairCycleState(
                pair=dependent,
                dependency_armed_at=launched_at,
            ),
        },
    )
    chronos = Chronos(state=state)

    chronos.process_event(
        EggMove(
            kind=EggMoveKind.PLAYED_AND_CANCELED,
            occurred_at=launched_at + timedelta(minutes=1),
            symbol="PI_XBTUSD",
            pair_name="origin-a",
            event_id="origin-a-closed",
            is_private=True,
        )
    )

    dependent_state = chronos.state.pairs["dependent"]
    assert len(dependent_state.dependency_evidence) == 1
    assert pair_dependency_satisfied(chronos.state, dependent_state)


def test_one_origin_event_can_release_multiple_dependent_pairs() -> None:
    launched_at = datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc)
    origin = sample_pair("origin")
    child_a = replace(sample_pair("child-a"), hook_name="origin-tail-closed")
    child_b = replace(sample_pair("child-b"), hook_name="origin-tail-closed")
    state = StrategyState(
        launched_at=launched_at,
        strategy_id="strategy-fan-out",
        pairs={
            "origin": PairCycleState(
                pair=origin,
                head_state=HeadState.CLOSED,
                tail_state=TailState.CLOSED,
                played_quantity=Decimal("1"),
            ),
            "child-a": PairCycleState(pair=child_a),
            "child-b": PairCycleState(pair=child_b),
        },
    )
    chronos = Chronos(state=state)

    chronos.process_event(
        EggMove(
            kind=EggMoveKind.PLAYED_AND_CANCELED,
            occurred_at=launched_at + timedelta(minutes=1),
            symbol="PI_XBTUSD",
            pair_name="origin",
            event_id="origin-closed",
            is_private=True,
        )
    )

    assert pair_dependency_satisfied(chronos.state, chronos.state.pairs["child-a"])
    assert pair_dependency_satisfied(chronos.state, chronos.state.pairs["child-b"])


def test_dependency_event_before_attempt_epoch_is_ignored() -> None:
    launched_at = datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc)
    origin = sample_pair("origin")
    dependent = replace(sample_pair("dependent"), hook_name="origin")
    state = StrategyState(
        launched_at=launched_at,
        strategy_id="strategy-stale-chain",
        pairs={
            "origin": PairCycleState(
                pair=origin,
                head_state=HeadState.CLOSED,
                tail_state=TailState.CLOSED,
                played_quantity=Decimal("1"),
            ),
            "dependent": PairCycleState(
                pair=dependent,
                attempt_index=2,
                dependency_armed_at=launched_at + timedelta(minutes=2),
            ),
        },
    )
    chronos = Chronos(state=state)

    chronos.process_event(
        EggMove(
            kind=EggMoveKind.PLAYED_AND_CANCELED,
            occurred_at=launched_at + timedelta(minutes=1),
            symbol="PI_XBTUSD",
            pair_name="origin",
            event_id="delayed-origin-close",
            is_private=True,
        ),
        now=launched_at + timedelta(minutes=3),
    )

    assert chronos.state.pairs["dependent"].dependency_evidence == ()


def test_all_hook_repeat_requires_every_target_again() -> None:
    launched_at = datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc)
    ready_at = launched_at + timedelta(minutes=2)
    dependent = replace(
        sample_pair("dependent"),
        hook_name="all(origin-a,origin-b)",
        try_num=2,
    )
    state = StrategyState(
        launched_at=launched_at,
        strategy_id="strategy-repeat-all-chain",
        pairs={
            "origin-a": PairCycleState(
                pair=sample_pair("origin-a"),
                head_state=HeadState.CLOSED,
                tail_state=TailState.CLOSED,
                played_quantity=Decimal("1"),
            ),
            "origin-b": PairCycleState(
                pair=sample_pair("origin-b"),
                head_state=HeadState.CLOSED,
                tail_state=TailState.CLOSED,
                played_quantity=Decimal("1"),
            ),
            "dependent": PairCycleState(
                pair=dependent,
                head_state=HeadState.CLOSED,
                tail_state=TailState.CLOSED,
                played_quantity=Decimal("1"),
                dependency_evidence=(
                    close_evidence("origin-a", 1, launched_at + timedelta(seconds=30)),
                    close_evidence("origin-b", 1, launched_at + timedelta(minutes=1)),
                ),
            ),
        },
    )
    chronos = Chronos(state=state)
    chronos.pending_repeats["dependent"] = PendingRepeat(
        pair_name="dependent",
        ready_at=ready_at,
        next_attempt=2,
    )

    chronos.activate_ready_repeats(symbol="PI_XBTUSD", now=ready_at)

    dependent_state = chronos.state.pairs["dependent"]
    assert dependent_state.attempt_index == 2
    assert dependent_state.dependency_evidence == ()

    chronos.process_event(
        EggMove(
            kind=EggMoveKind.PLAYED_AND_CANCELED,
            occurred_at=launched_at + timedelta(minutes=3),
            symbol="PI_XBTUSD",
            pair_name="origin-a",
            event_id="origin-a-repeat-close",
            is_private=True,
        )
    )
    dependent_state = chronos.state.pairs["dependent"]
    assert not pair_dependency_satisfied(chronos.state, dependent_state)

    chronos.process_event(
        EggMove(
            kind=EggMoveKind.PLAYED_AND_CANCELED,
            occurred_at=launched_at + timedelta(minutes=4),
            symbol="PI_XBTUSD",
            pair_name="origin-b",
            event_id="origin-b-repeat-close",
            is_private=True,
        )
    )

    assert pair_dependency_satisfied(
        chronos.state,
        chronos.state.pairs["dependent"],
    )


def test_dependency_event_before_dependent_window_is_ignored() -> None:
    launched_at = datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc)
    origin = sample_pair("origin")
    dependent = replace(
        sample_pair("dependent"),
        hook_name="origin",
        window=TimeWindow(start_minutes=2.0, end_minutes=10.0),
    )
    state = StrategyState(
        launched_at=launched_at,
        strategy_id="strategy-window-chain",
        pairs={
            "origin": PairCycleState(
                pair=origin,
                head_state=HeadState.CLOSED,
                tail_state=TailState.CLOSED,
                played_quantity=Decimal("1"),
            ),
            "dependent": PairCycleState(pair=dependent),
        },
    )
    chronos = Chronos(state=state)

    chronos.process_event(
        EggMove(
            kind=EggMoveKind.PLAYED_AND_CANCELED,
            occurred_at=launched_at + timedelta(minutes=1),
            symbol="PI_XBTUSD",
            pair_name="origin",
            event_id="origin-close-before-window",
            is_private=True,
        )
    )

    assert chronos.state.pairs["dependent"].dependency_evidence == ()


def test_public_origin_event_cannot_satisfy_dependency() -> None:
    launched_at = datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc)
    origin = sample_pair("origin")
    dependent = replace(sample_pair("dependent"), hook_name="origin")
    state = StrategyState(
        launched_at=launched_at,
        strategy_id="strategy-public-chain",
        pairs={
            "origin": PairCycleState(
                pair=origin,
                head_state=HeadState.CLOSED,
                tail_state=TailState.CLOSED,
                played_quantity=Decimal("1"),
            ),
            "dependent": PairCycleState(pair=dependent),
        },
    )
    chronos = Chronos(state=state)

    chronos.process_event(
        EggMove(
            kind=EggMoveKind.PLAYED_AND_CANCELED,
            occurred_at=launched_at + timedelta(minutes=1),
            symbol="PI_XBTUSD",
            pair_name="origin",
            event_id="public-origin-close",
            is_private=False,
        )
    )

    assert chronos.state.pairs["dependent"].dependency_evidence == ()


def test_chain_release_ignores_origin_close_while_dependent_is_living() -> None:
    origin = replace(sample_pair("main"), try_num=4)
    chained = replace(sample_pair("chain"), hook_name="main", try_num=4)
    state = StrategyState(
        launched_at=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
        strategy_id="strategy-chain-repeat",
        pairs={
            "main": PairCycleState(
                pair=origin,
                head_state=HeadState.CLOSED,
                tail_state=TailState.CLOSED,
                played_quantity=Decimal("1"),
                attempt_index=2,
            ),
            "chain": PairCycleState(
                pair=chained,
                head_state=HeadState.CLOSED,
                tail_state=TailState.LIVING,
                played_quantity=Decimal("1"),
                attempt_index=1,
                dependency_evidence=(
                    close_evidence(
                        "main",
                        1,
                        datetime(2026, 5, 21, 12, 3, tzinfo=timezone.utc),
                    ),
                ),
            ),
        },
    )
    chronos = Chronos(state=state)

    commands = chronos.process_event(
        EggMove(
            kind=EggMoveKind.PLAYED_AND_CANCELED,
            occurred_at=datetime(2026, 5, 21, 12, 6, tzinfo=timezone.utc),
            symbol="PI_XBTUSD",
            pair_name="main",
            event_id="main-2-closed",
            is_private=True,
        )
    )

    assert commands == ()
    chain_state = chronos.state.pairs["chain"]
    assert chain_state.attempt_index == 1
    assert len(chain_state.dependency_evidence) == 1
    assert chain_state.dependency_evidence[0].origin_attempt_index == 1
    assert chain_state.head_state == HeadState.CLOSED
    assert chain_state.tail_state == TailState.LIVING


def test_chain_release_drops_origin_close_during_dependent_pause() -> None:
    origin = replace(sample_pair("main"), try_num=5)
    chained = replace(sample_pair("chain"), hook_name="main", try_num=3, dr_pause=1.0)
    launched_at = datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc)
    ready_at = datetime(2026, 5, 21, 12, 7, tzinfo=timezone.utc)
    state = StrategyState(
        launched_at=launched_at,
        strategy_id="strategy-chain-repeat",
        pairs={
            "main": PairCycleState(
                pair=origin,
                head_state=HeadState.CLOSED,
                tail_state=TailState.CLOSED,
                played_quantity=Decimal("1"),
                attempt_index=2,
            ),
            "chain": PairCycleState(
                pair=chained,
                head_state=HeadState.CLOSED,
                tail_state=TailState.CLOSED,
                played_quantity=Decimal("1"),
                attempt_index=1,
                dependency_evidence=(
                    close_evidence(
                        "main",
                        1,
                        datetime(2026, 5, 21, 12, 3, tzinfo=timezone.utc),
                    ),
                ),
            ),
        },
    )
    chronos = Chronos(state=state)
    chronos.pending_repeats["chain"] = PendingRepeat(
        pair_name="chain",
        ready_at=ready_at,
        next_attempt=2,
    )

    chronos.process_event(
        EggMove(
            kind=EggMoveKind.PLAYED_AND_CANCELED,
            occurred_at=datetime(2026, 5, 21, 12, 6, tzinfo=timezone.utc),
            symbol="PI_XBTUSD",
            pair_name="main",
            event_id="main-2-closed",
            is_private=True,
        )
    )
    evidence = chronos.state.pairs["chain"].dependency_evidence
    assert len(evidence) == 1
    assert evidence[0].origin_attempt_index == 1

    ready_commands = chronos.activate_ready_repeats(
        symbol="PI_XBTUSD",
        now=ready_at + timedelta(seconds=1),
    )
    assert ready_commands == ()
    assert chronos.state.pairs["chain"].attempt_index == 2
    assert chronos.state.pairs["chain"].dependency_evidence == ()

    chronos.state = replace(
        chronos.state,
        pairs={
            **chronos.state.pairs,
            "main": replace(chronos.state.pairs["main"], attempt_index=3),
        },
    )
    chronos.process_event(
        EggMove(
            kind=EggMoveKind.PLAYED_AND_CANCELED,
            occurred_at=datetime(2026, 5, 21, 12, 8, tzinfo=timezone.utc),
            symbol="PI_XBTUSD",
            pair_name="main",
            event_id="main-3-closed",
            is_private=True,
        )
    )

    chain_state = chronos.state.pairs["chain"]
    assert chain_state.attempt_index == 2
    assert len(chain_state.dependency_evidence) == 1
    assert chain_state.dependency_evidence[0].target.origin_pair_name == "main"
    assert chain_state.dependency_evidence[0].origin_attempt_index == 3
    assert chain_state.dependency_evidence[0].satisfied_at == datetime(
        2026,
        5,
        21,
        12,
        8,
        tzinfo=timezone.utc,
    )


def test_chronos_repeats_terminal_pair_with_fresh_attempt_key() -> None:
    pair = replace(
        sample_pair("pair-r"),
        try_num=2,
        head_price=(5.0, 50.0),
        head_price_type="pD",
        amount_type="qAtDpD",
    )
    state = StrategyState(
        launched_at=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
        strategy_id="strategy-repeat",
        pairs={
            "pair-r": PairCycleState(
                pair=pair,
                head_state=HeadState.CLOSED,
                tail_state=TailState.CLOSED,
                tail_mode=TailMode.FLYING,
                head_trigger_reference_price=Decimal("100"),
                head_trigger_reference_source="bid",
                head_trigger_reference_at=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
                played_quantity=Decimal("1"),
                attempt_index=1,
            ),
        },
    )
    chronos = Chronos(state=state)
    move = EggMove(
        kind=EggMoveKind.PLAYED_AND_CANCELED,
        occurred_at=datetime(2026, 5, 21, 12, 6, tzinfo=timezone.utc),
        symbol="PI_XBTUSD",
        pair_name="pair-r",
        event_id="evt-repeat-1",
        is_private=True,
    )

    commands = chronos.process_event(move, now=move.occurred_at)

    assert commands == ()
    assert chronos.state.pairs["pair-r"].attempt_index == 2
    assert chronos.state.pairs["pair-r"].head_state == HeadState.LATENT
    assert chronos.state.pairs["pair-r"].head_trigger_reference_price is None


def test_chronos_repeats_not_played_canceled_head_with_fresh_attempt_key() -> None:
    pair = replace(
        sample_pair("pair-r"),
        try_num=2,
        head_price=(5.0, 50.0),
        head_price_type="pD",
        amount_type="qAtDpD",
    )
    state = StrategyState(
        launched_at=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
        strategy_id="strategy-latent-repeat",
        pairs={
            "pair-r": PairCycleState(
                pair=pair,
                head_state=HeadState.LATENT,
                head_trigger_reference_price=Decimal("100"),
                head_trigger_reference_source="bid",
                head_trigger_reference_at=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
                attempt_index=1,
            ),
        },
    )
    chronos = Chronos(state=state)
    move = EggMove(
        kind=EggMoveKind.NOT_PLAYED_CANCELED,
        occurred_at=datetime(2026, 5, 21, 12, 1, tzinfo=timezone.utc),
        symbol="PI_XBTUSD",
        pair_name="pair-r",
        role=None,
        event_id="latent-timeout:pair-r:1",
        reply={"cumQty": 0.0, "execType": "latent_timeout"},
    )

    commands = chronos.process_event(move, now=move.occurred_at)

    assert commands == ()
    repeated = chronos.state.pairs["pair-r"]
    assert repeated.attempt_index == 2
    assert repeated.head_state == HeadState.LATENT
    assert repeated.head_trigger_reference_price is None


def test_chronos_applies_repeat_adjustment_to_immediate_repeat() -> None:
    adjustment_name = register_test_scurve("test_scurve_immediate")
    pair = replace(
        sample_pair("pair-r"),
        try_num=2,
        dr_pause=0.0,
        head_price=(10.0, 20.0),
        head_price_type="pD",
        repeat_adjustment=adjustment_name,
    )
    state = StrategyState(
        launched_at=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
        strategy_id="strategy-rfunc",
        pairs={
            "pair-r": PairCycleState(
                pair=pair,
                head_state=HeadState.FAILED,
                attempt_index=1,
            ),
        },
    )
    chronos = Chronos(state=state)
    occurred_at = datetime(2026, 5, 21, 12, 1, tzinfo=timezone.utc)

    commands = chronos.process_event(
        EggMove(
            kind=EggMoveKind.NOT_PLAYED_CANCELED,
            occurred_at=occurred_at,
            symbol="PI_XBTUSD",
            pair_name="pair-r",
            event_id="latent-timeout:pair-r:1",
            reply={"execType": "latent_timeout"},
        ),
        now=occurred_at,
    )

    repeated = chronos.state.pairs["pair-r"]
    assert commands == ()
    assert repeated.attempt_index == 2
    assert repeated.pair.head_price == (8.0, 16.0)
    assert repeated.pair.head_price_type == "pD"
    assert repeated.pair.repeat_adjustment == adjustment_name


def test_chronos_applies_repeat_adjustment_to_delayed_repeat() -> None:
    adjustment_name = register_test_scurve("test_scurve_delayed")
    pair = replace(
        sample_pair("pair-r"),
        try_num=2,
        dr_pause=1.0,
        head_price=(10.0, 20.0),
        head_price_type="pD",
        repeat_adjustment=adjustment_name,
    )
    occurred_at = datetime(2026, 5, 21, 12, 1, tzinfo=timezone.utc)
    state = StrategyState(
        launched_at=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
        strategy_id="strategy-rfunc-delay",
        pairs={
            "pair-r": PairCycleState(
                pair=pair,
                head_state=HeadState.FAILED,
                attempt_index=1,
            ),
        },
    )
    chronos = Chronos(state=state)

    commands = chronos.process_event(
        EggMove(
            kind=EggMoveKind.NOT_PLAYED_CANCELED,
            occurred_at=occurred_at,
            symbol="PI_XBTUSD",
            pair_name="pair-r",
            event_id="latent-timeout:pair-r:1",
            reply={"execType": "latent_timeout"},
        ),
        now=occurred_at,
    )

    assert commands == ()
    assert chronos.state.pairs["pair-r"].attempt_index == 1
    assert chronos.pending_repeats["pair-r"].ready_at == occurred_at + timedelta(minutes=1)

    chronos.activate_ready_repeats(
        symbol="PI_XBTUSD",
        now=occurred_at + timedelta(minutes=1),
    )

    repeated = chronos.state.pairs["pair-r"]
    assert repeated.attempt_index == 2
    assert repeated.pair.head_price == (8.0, 16.0)


def test_chronos_repeat_adjustment_moves_successful_tail_further() -> None:
    adjustment_name = register_test_scurve("test_scurve_success")
    pair = replace(
        sample_pair("pair-r"),
        try_num=2,
        dr_pause=0.0,
        head_price=(10.0, 20.0),
        head_price_type="pD",
        repeat_adjustment=adjustment_name,
    )
    state = StrategyState(
        launched_at=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
        strategy_id="strategy-rfunc-success",
        pairs={
            "pair-r": PairCycleState(
                pair=pair,
                head_state=HeadState.CLOSED,
                tail_state=TailState.CLOSED,
                tail_mode=TailMode.FLYING,
                head_order_price=Decimal("100"),
                played_quantity=Decimal("1"),
                attempt_index=1,
            ),
        },
    )
    chronos = Chronos(state=state)
    occurred_at = datetime(2026, 5, 21, 12, 1, tzinfo=timezone.utc)

    chronos.process_event(
        EggMove(
            kind=EggMoveKind.PLAYED_AND_CANCELED,
            occurred_at=occurred_at,
            symbol="PI_XBTUSD",
            pair_name="pair-r",
            role=OrderRole.TAIL,
            event_id="evt-success-rfunc",
            reply={"price": "105", "cumQty": "1"},
            is_private=True,
        ),
        now=occurred_at,
    )

    repeated = chronos.state.pairs["pair-r"]
    assert repeated.attempt_index == 2
    assert repeated.pair.head_price == (11.0, 22.0)


def test_chronos_loads_strategy_rfunc_module_for_timeout(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rfunc_path = tmp_path / "rfunc.py"
    rfunc_path.write_text(
        "\n".join(
            [
                "from dataclasses import replace",
                "from decimal import Decimal",
                "from kolabi.bot.domain import OrderPairSpec",
                "from kolabi.bot.repeat_adjustment import RepeatAdjustmentContext, RepeatTermination, register_repeat_adjustment",
                "",
                "def local_timeout_x1_1(pair: OrderPairSpec, context: RepeatAdjustmentContext) -> OrderPairSpec:",
                "    if context.termination == RepeatTermination.LATENT_TIMEOUT:",
                "        return pair",
                "    if (context.previous_state.played_quantity or Decimal('0')) <= 0:",
                "        return pair",
                "    if pair.timeout is None:",
                "        return pair",
                "    return replace(pair, timeout=float(Decimal(str(pair.timeout)) * Decimal('1.10')))",
                "",
                "register_repeat_adjustment('local_timeout_x1_1', local_timeout_x1_1)",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("KOLABI_STRATEGY_RFUNC", str(rfunc_path))
    pair = replace(
        sample_pair("pair-r"),
        try_num=2,
        dr_pause=0.0,
        timeout=10.0,
        repeat_adjustment="local_timeout_x1_1",
    )
    state = StrategyState(
        launched_at=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
        strategy_id="strategy-rfunc-timeout",
        pairs={
            "pair-r": PairCycleState(
                pair=pair,
                head_state=HeadState.CLOSED,
                tail_state=TailState.CLOSED,
                tail_mode=TailMode.FLYING,
                head_order_price=Decimal("100"),
                played_quantity=Decimal("1"),
                attempt_index=1,
            ),
        },
    )
    chronos = Chronos(state=state)
    occurred_at = datetime(2026, 5, 21, 12, 1, tzinfo=timezone.utc)

    chronos.process_event(
        EggMove(
            kind=EggMoveKind.PLAYED_AND_CANCELED,
            occurred_at=occurred_at,
            symbol="PI_XBTUSD",
            pair_name="pair-r",
            role=OrderRole.TAIL,
            event_id="evt-timeout-rfunc",
            reply={"price": "105", "cumQty": "1"},
            is_private=True,
        ),
        now=occurred_at,
    )

    repeated = chronos.state.pairs["pair-r"]
    assert repeated.attempt_index == 2
    assert repeated.pair.timeout == 11.0


def test_chronos_strategy_rfunc_can_reduce_quantity_by_percent(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rfunc_path = tmp_path / "rfunc.py"
    rfunc_path.write_text(
        "\n".join(
            [
                "from dataclasses import replace",
                "from decimal import Decimal",
                "from kolabi.bot.domain import OrderPairSpec",
                "from kolabi.bot.repeat_adjustment import RepeatAdjustmentContext, RepeatTermination, register_repeat_adjustment",
                "",
                "def local_qty_x0_98(pair: OrderPairSpec, context: RepeatAdjustmentContext) -> OrderPairSpec:",
                "    if context.termination == RepeatTermination.LATENT_TIMEOUT:",
                "        return pair",
                "    if (context.previous_state.played_quantity or Decimal('0')) <= 0:",
                "        return pair",
                "    if pair.head_quantity is None:",
                "        return pair",
                "    return replace(pair, head_quantity=Decimal(str(pair.head_quantity)) * Decimal('0.98'))",
                "",
                "register_repeat_adjustment('local_qty_x0_98', local_qty_x0_98)",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("KOLABI_STRATEGY_RFUNC", str(rfunc_path))
    pair = replace(
        sample_pair("pair-r"),
        try_num=2,
        dr_pause=0.0,
        head_quantity=Decimal("15"),
        repeat_adjustment="local_qty_x0_98",
    )
    state = StrategyState(
        launched_at=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
        strategy_id="strategy-rfunc-qty-usd",
        pairs={
            "pair-r": PairCycleState(
                pair=pair,
                head_state=HeadState.CLOSED,
                tail_state=TailState.CLOSED,
                tail_mode=TailMode.FLYING,
                head_order_price=Decimal("100"),
                played_quantity=Decimal("1"),
                attempt_index=1,
            ),
        },
    )
    chronos = Chronos(state=state)
    occurred_at = datetime(2026, 5, 21, 12, 1, tzinfo=timezone.utc)

    chronos.process_event(
        EggMove(
            kind=EggMoveKind.PLAYED_AND_CANCELED,
            occurred_at=occurred_at,
            symbol="PI_XBTUSD",
            pair_name="pair-r",
            role=OrderRole.TAIL,
            event_id="evt-qty-usd-rfunc",
            reply={"price": "105", "cumQty": "1"},
            is_private=True,
        ),
        now=occurred_at,
    )

    repeated = chronos.state.pairs["pair-r"]
    assert repeated.attempt_index == 2
    assert repeated.pair.head_quantity == Decimal("14.70")


def test_chronos_passes_rfunc_arguments_to_strategy_module(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rfunc_path = tmp_path / "rfunc.py"
    rfunc_path.write_text(
        "\n".join(
            [
                "from dataclasses import replace",
                "from kolabi.bot.domain import OrderPairSpec",
                "from kolabi.bot.repeat_adjustment import RepeatAdjustmentContext, RepeatTermination, register_repeat_adjustment",
                "",
                "def local_head_offset(pair: OrderPairSpec, context: RepeatAdjustmentContext) -> OrderPairSpec:",
                "    if context.termination != RepeatTermination.SUCCESSFUL_TAIL_CLOSE:",
                "        return pair",
                "    return replace(pair, head_order_price_spec=float(abs(context.args[0])))",
                "",
                "register_repeat_adjustment('local_head_offset', local_head_offset)",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("KOLABI_STRATEGY_RFUNC", str(rfunc_path))
    pair = replace(
        sample_pair("pair-r"),
        try_num=2,
        dr_pause=0.0,
        head_order_price_spec=1.0,
        head_order_price_spec_type="hD",
        repeat_adjustment="local_head_offset: -7",
    )
    state = StrategyState(
        launched_at=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
        strategy_id="strategy-rfunc-head-offset",
        pairs={
            "pair-r": PairCycleState(
                pair=pair,
                head_state=HeadState.CLOSED,
                tail_state=TailState.CLOSED,
                tail_mode=TailMode.FLYING,
                head_order_price=Decimal("100"),
                played_quantity=Decimal("1"),
                attempt_index=1,
            ),
        },
    )
    chronos = Chronos(state=state)
    occurred_at = datetime(2026, 5, 21, 12, 1, tzinfo=timezone.utc)

    chronos.process_event(
        EggMove(
            kind=EggMoveKind.PLAYED_AND_CANCELED,
            occurred_at=occurred_at,
            symbol="PI_XBTUSD",
            pair_name="pair-r",
            role=OrderRole.TAIL,
            event_id="evt-head-offset-rfunc",
            reply={"price": "105", "cumQty": "1"},
            is_private=True,
        ),
        now=occurred_at,
    )

    repeated = chronos.state.pairs["pair-r"]
    assert repeated.attempt_index == 2
    assert repeated.pair.head_order_price_spec == 7.0
    assert repeated.pair.head_order_price_spec_type == "hD"


def test_chronos_rfunc_toggle_uses_attempt_index_parity() -> None:
    def test_toggle(
        pair: OrderPairSpec,
        context: RepeatAdjustmentContext,
    ) -> OrderPairSpec:
        if context.termination != RepeatTermination.SUCCESSFUL_TAIL_CLOSE:
            return pair
        first, second = context.args
        return replace(
            pair,
            head_order_price_spec=float(first if context.next_attempt % 2 == 0 else second),
        )

    register_repeat_adjustment("test_head_offset_toggle", test_toggle)
    occurred_at = datetime(2026, 5, 21, 12, 1, tzinfo=timezone.utc)

    def repeated_offset(attempt_index: int) -> float | None:
        pair = replace(
            sample_pair("pair-r"),
            try_num=4,
            dr_pause=0.0,
            head_order_price_spec=1.0,
            repeat_adjustment="test_head_offset_toggle: 3,8",
        )
        chronos = Chronos(
            state=StrategyState(
                launched_at=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
                strategy_id=f"strategy-rfunc-toggle-{attempt_index}",
                pairs={
                    "pair-r": PairCycleState(
                        pair=pair,
                        head_state=HeadState.CLOSED,
                        tail_state=TailState.CLOSED,
                        tail_mode=TailMode.FLYING,
                        head_order_price=Decimal("100"),
                        played_quantity=Decimal("1"),
                        attempt_index=attempt_index,
                    ),
                },
            )
        )
        chronos.process_event(
            EggMove(
                kind=EggMoveKind.PLAYED_AND_CANCELED,
                occurred_at=occurred_at,
                symbol="PI_XBTUSD",
                pair_name="pair-r",
                role=OrderRole.TAIL,
                event_id=f"evt-toggle-rfunc-{attempt_index}",
                reply={"price": "105", "cumQty": "1"},
                is_private=True,
            ),
            now=occurred_at,
        )
        return chronos.state.pairs["pair-r"].pair.head_order_price_spec

    assert repeated_offset(1) == 3.0
    assert repeated_offset(2) == 8.0


def test_chronos_rfunc_head_offset_ignores_latent_timeout() -> None:
    def test_success_only(
        pair: OrderPairSpec,
        context: RepeatAdjustmentContext,
    ) -> OrderPairSpec:
        if context.termination != RepeatTermination.SUCCESSFUL_TAIL_CLOSE:
            return pair
        return replace(pair, head_order_price_spec=float(context.args[0]))

    register_repeat_adjustment("test_head_offset_success_only", test_success_only)
    pair = replace(
        sample_pair("pair-r"),
        try_num=2,
        dr_pause=0.0,
        head_order_price_spec=1.0,
        repeat_adjustment="test_head_offset_success_only: 7",
    )
    state = StrategyState(
        launched_at=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
        strategy_id="strategy-rfunc-latent",
        pairs={
            "pair-r": PairCycleState(
                pair=pair,
                head_state=HeadState.FAILED,
                attempt_index=1,
            ),
        },
    )
    chronos = Chronos(state=state)
    occurred_at = datetime(2026, 5, 21, 12, 1, tzinfo=timezone.utc)

    chronos.process_event(
        EggMove(
            kind=EggMoveKind.NOT_PLAYED_CANCELED,
            occurred_at=occurred_at,
            symbol="PI_XBTUSD",
            pair_name="pair-r",
            event_id="latent-timeout:pair-r:1",
            reply={"execType": "latent_timeout"},
        ),
        now=occurred_at,
    )

    repeated = chronos.state.pairs["pair-r"]
    assert repeated.attempt_index == 2
    assert repeated.pair.head_order_price_spec == 1.0


def test_chronos_repeat_adjustment_unknown_name_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KOLABI_STRATEGY_RFUNC", "")
    pair = replace(
        sample_pair("pair-r"),
        try_num=2,
        dr_pause=0.0,
        repeat_adjustment="missing_policy",
    )
    state = StrategyState(
        launched_at=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
        strategy_id="strategy-rfunc-missing",
        pairs={
            "pair-r": PairCycleState(
                pair=pair,
                head_state=HeadState.FAILED,
                attempt_index=1,
            ),
        },
    )
    chronos = Chronos(state=state)
    occurred_at = datetime(2026, 5, 21, 12, 1, tzinfo=timezone.utc)

    with pytest.raises(RepeatAdjustmentError, match="Unknown repeat adjustment"):
        chronos.process_event(
            EggMove(
                kind=EggMoveKind.NOT_PLAYED_CANCELED,
                occurred_at=occurred_at,
                symbol="PI_XBTUSD",
                pair_name="pair-r",
                event_id="evt-missing-rfunc",
                is_private=True,
            ),
            now=occurred_at,
        )

    assert chronos.state.pairs["pair-r"].attempt_index == 1


def test_chronos_repeat_adjustment_rejects_protected_field_change() -> None:
    def bad_adjustment(
        pair: OrderPairSpec,
        _context: RepeatAdjustmentContext,
    ) -> OrderPairSpec:
        return replace(pair, symbol="PI_ETHUSD")

    register_repeat_adjustment("test_bad_symbol_change", bad_adjustment)
    pair = replace(
        sample_pair("pair-r"),
        try_num=2,
        dr_pause=0.0,
        repeat_adjustment="test_bad_symbol_change",
    )
    state = StrategyState(
        launched_at=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
        strategy_id="strategy-rfunc-invalid",
        pairs={
            "pair-r": PairCycleState(
                pair=pair,
                head_state=HeadState.FAILED,
                attempt_index=1,
            ),
        },
    )
    chronos = Chronos(state=state)
    occurred_at = datetime(2026, 5, 21, 12, 1, tzinfo=timezone.utc)

    with pytest.raises(RepeatAdjustmentError, match="symbol"):
        chronos.process_event(
            EggMove(
                kind=EggMoveKind.NOT_PLAYED_CANCELED,
                occurred_at=occurred_at,
                symbol="PI_XBTUSD",
                pair_name="pair-r",
                event_id="evt-bad-rfunc",
                is_private=True,
            ),
            now=occurred_at,
        )

    assert chronos.state.pairs["pair-r"].attempt_index == 1


def test_chronos_repeat_adjustment_rejects_typed_suffix_change() -> None:
    def bad_adjustment(
        pair: OrderPairSpec,
        _context: RepeatAdjustmentContext,
    ) -> OrderPairSpec:
        return replace(pair, head_price_type="pB")

    register_repeat_adjustment("test_bad_suffix_change", bad_adjustment)
    pair = replace(
        sample_pair("pair-r"),
        try_num=2,
        dr_pause=0.0,
        head_price_type="pD",
        repeat_adjustment="test_bad_suffix_change",
    )
    state = StrategyState(
        launched_at=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
        strategy_id="strategy-rfunc-bad-suffix",
        pairs={
            "pair-r": PairCycleState(
                pair=pair,
                head_state=HeadState.FAILED,
                attempt_index=1,
            ),
        },
    )
    chronos = Chronos(state=state)
    occurred_at = datetime(2026, 5, 21, 12, 1, tzinfo=timezone.utc)

    with pytest.raises(RepeatAdjustmentError, match="head_price_type"):
        chronos.process_event(
            EggMove(
                kind=EggMoveKind.NOT_PLAYED_CANCELED,
                occurred_at=occurred_at,
                symbol="PI_XBTUSD",
                pair_name="pair-r",
                event_id="evt-bad-suffix-rfunc",
                is_private=True,
            ),
            now=occurred_at,
        )

    assert chronos.state.pairs["pair-r"].attempt_index == 1


def test_chronos_star_attempts_repeat_until_pair_window_closes() -> None:
    pair = replace(
        sample_pair("pair-r"),
        try_num=None,
        dr_pause=0.0,
        head_price=(5.0, 50.0),
        head_price_type="pD",
        amount_type="qAtDpD",
    )
    state = StrategyState(
        launched_at=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
        strategy_id="strategy-star-repeat",
        pairs={
            "pair-r": PairCycleState(
                pair=pair,
                head_state=HeadState.CLOSED,
                tail_state=TailState.CLOSED,
                tail_mode=TailMode.FLYING,
                played_quantity=Decimal("1"),
                attempt_index=244,
            ),
        },
    )
    chronos = Chronos(state=state)
    occurred_at = datetime(2026, 5, 21, 12, 6, tzinfo=timezone.utc)

    commands = chronos.process_event(
        EggMove(
            kind=EggMoveKind.PLAYED_AND_CANCELED,
            occurred_at=occurred_at,
            symbol="PI_XBTUSD",
            pair_name="pair-r",
            event_id="evt-star-repeat",
            is_private=True,
        ),
        now=occurred_at,
    )

    assert commands == ()
    repeated = chronos.state.pairs["pair-r"]
    assert repeated.attempt_index == 245
    assert repeated.head_state == HeadState.LATENT
    assert repeated.head_trigger_reference_price is None

    chronos.state = replace(
        chronos.state,
        pairs={
            "pair-r": replace(
                repeated,
                head_state=HeadState.CLOSED,
                tail_state=TailState.CLOSED,
                tail_mode=TailMode.FLYING,
                played_quantity=Decimal("1"),
            )
        },
    )
    after_window = datetime(2026, 5, 21, 12, 11, tzinfo=timezone.utc)
    chronos.process_event(
        EggMove(
            kind=EggMoveKind.PLAYED_AND_CANCELED,
            occurred_at=after_window,
            symbol="PI_XBTUSD",
            pair_name="pair-r",
            event_id="evt-star-after-window",
            is_private=True,
        ),
        now=after_window,
    )

    assert chronos.pending_repeats == {}
    assert chronos.state.pairs["pair-r"].attempt_index == 245


def test_chronos_star_attempt_pause_must_still_fit_pair_window() -> None:
    pair = replace(sample_pair("pair-r"), try_num=None, dr_pause=1.0)
    state = StrategyState(
        launched_at=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
        strategy_id="strategy-star-repeat-pause",
        pairs={
            "pair-r": PairCycleState(
                pair=pair,
                head_state=HeadState.CLOSED,
                tail_state=TailState.CLOSED,
                tail_mode=TailMode.FLYING,
                played_quantity=Decimal("1"),
                attempt_index=244,
            ),
        },
    )
    chronos = Chronos(state=state)
    occurred_at = datetime(2026, 5, 21, 12, 9, 30, tzinfo=timezone.utc)

    commands = chronos.process_event(
        EggMove(
            kind=EggMoveKind.PLAYED_AND_CANCELED,
            occurred_at=occurred_at,
            symbol="PI_XBTUSD",
            pair_name="pair-r",
            event_id="evt-star-pause-after-window",
            is_private=True,
        ),
        now=occurred_at,
    )

    assert commands == ()
    assert chronos.pending_repeats == {}
    assert chronos.state.pairs["pair-r"].attempt_index == 244


def test_chronos_delays_repeat_until_pause_has_elapsed() -> None:
    pair = replace(sample_pair("pair-r"), try_num=2, dr_pause=1.0)
    state = StrategyState(
        launched_at=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
        strategy_id="strategy-repeat",
        pairs={
            "pair-r": PairCycleState(
                pair=pair,
                head_state=HeadState.CLOSED,
                tail_state=TailState.CLOSED,
                tail_mode=TailMode.FLYING,
                played_quantity=Decimal("1"),
            ),
        },
    )
    chronos = Chronos(state=state)
    occurred_at = datetime(2026, 5, 21, 12, 6, tzinfo=timezone.utc)

    commands = chronos.process_event(
        EggMove(
            kind=EggMoveKind.PLAYED_AND_CANCELED,
            occurred_at=occurred_at,
            symbol="PI_XBTUSD",
            pair_name="pair-r",
            event_id="evt-repeat-delay",
            is_private=True,
        ),
        now=occurred_at,
    )
    early = chronos.activate_ready_repeats(
        symbol="PI_XBTUSD",
        now=occurred_at + timedelta(seconds=30),
    )
    ready = chronos.activate_ready_repeats(
        symbol="PI_XBTUSD",
        now=occurred_at + timedelta(seconds=61),
    )

    assert commands == ()
    assert early == ()
    assert ready == ()
    assert chronos.state.pairs["pair-r"].attempt_index == 2
    assert chronos.state.pairs["pair-r"].head_state == HeadState.LATENT


def test_chronos_adds_cool_after_successful_tail_fill() -> None:
    pair = replace(sample_pair("pair-r"), try_num=2, dr_pause=1.0, cooldown_minutes=2.0)
    occurred_at = datetime(2026, 5, 21, 12, 6, tzinfo=timezone.utc)
    state = StrategyState(
        launched_at=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
        strategy_id="strategy-cool-repeat",
        pairs={
            "pair-r": PairCycleState(
                pair=pair,
                head_state=HeadState.CLOSED,
                tail_state=TailState.CLOSED,
                tail_mode=TailMode.FLYING,
                played_quantity=Decimal("1"),
            ),
        },
    )
    chronos = Chronos(state=state)

    commands = chronos.process_event(
        EggMove(
            kind=EggMoveKind.PLAYED_AND_CANCELED,
            occurred_at=occurred_at,
            symbol="PI_XBTUSD",
            pair_name="pair-r",
            role=OrderRole.TAIL,
            event_id="evt-repeat-cool",
            is_private=True,
        ),
        now=occurred_at,
    )

    assert commands == ()
    assert chronos.pending_repeats["pair-r"].ready_at == occurred_at + timedelta(minutes=3)


def test_chronos_ignores_cool_after_failed_tail() -> None:
    pair = replace(sample_pair("pair-r"), try_num=2, dr_pause=0.5, cooldown_minutes=2.0)
    occurred_at = datetime(2026, 5, 21, 12, 6, tzinfo=timezone.utc)
    state = StrategyState(
        launched_at=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
        strategy_id="strategy-failed-tail-repeat",
        pairs={
            "pair-r": PairCycleState(
                pair=pair,
                head_state=HeadState.CLOSED,
                tail_state=TailState.LIVING,
                tail_mode=TailMode.FLYING,
                played_quantity=Decimal("1"),
            ),
        },
    )
    chronos = Chronos(state=state)

    commands = chronos.process_event(
        EggMove(
            kind=EggMoveKind.NOT_PLAYED_CANCELED,
            occurred_at=occurred_at,
            symbol="PI_XBTUSD",
            pair_name="pair-r",
            role=OrderRole.TAIL,
            event_id="evt-repeat-tail-failed",
            is_private=True,
        ),
        now=occurred_at,
    )

    assert commands == ()
    assert chronos.pending_repeats["pair-r"].ready_at == occurred_at + timedelta(seconds=30)


def test_chronos_cool_only_waits_after_tail_fill() -> None:
    pair = replace(sample_pair("pair-r"), try_num=2, dr_pause=0.0, cooldown_minutes=0.5)
    occurred_at = datetime(2026, 5, 21, 12, 6, tzinfo=timezone.utc)
    state = StrategyState(
        launched_at=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
        strategy_id="strategy-cool-only-repeat",
        pairs={
            "pair-r": PairCycleState(
                pair=pair,
                head_state=HeadState.CLOSED,
                tail_state=TailState.CLOSED,
                tail_mode=TailMode.FLYING,
                played_quantity=Decimal("1"),
            ),
        },
    )
    chronos = Chronos(state=state)

    chronos.process_event(
        EggMove(
            kind=EggMoveKind.PLAYED_AND_CANCELED,
            occurred_at=occurred_at,
            symbol="PI_XBTUSD",
            pair_name="pair-r",
            role=OrderRole.TAIL,
            event_id="evt-repeat-cool-only",
            is_private=True,
        ),
        now=occurred_at,
    )

    assert chronos.pending_repeats["pair-r"].ready_at == occurred_at + timedelta(seconds=30)
    assert chronos.state.pairs["pair-r"].attempt_index == 1


def test_chronos_tail_cool_does_not_delay_dependent_hook() -> None:
    origin = replace(sample_pair("main"), try_num=2, dr_pause=1.0, cooldown_minutes=2.0)
    chained = replace(sample_pair("chain"), hook_name="main-tail-closed")
    occurred_at = datetime(2026, 5, 21, 12, 6, tzinfo=timezone.utc)
    state = StrategyState(
        launched_at=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
        strategy_id="strategy-cool-hook",
        pairs={
            "main": PairCycleState(
                pair=origin,
                head_state=HeadState.CLOSED,
                tail_state=TailState.CLOSED,
                tail_mode=TailMode.FLYING,
                played_quantity=Decimal("1"),
            ),
            "chain": PairCycleState(pair=chained),
        },
    )
    chronos = Chronos(state=state)

    commands = chronos.process_event(
        EggMove(
            kind=EggMoveKind.PLAYED_AND_CANCELED,
            occurred_at=occurred_at,
            symbol="PI_XBTUSD",
            pair_name="main",
            role=OrderRole.TAIL,
            event_id="evt-cool-hook",
            is_private=True,
        ),
        now=occurred_at,
    )

    assert commands == ()
    assert pair_dependency_satisfied(chronos.state, chronos.state.pairs["chain"]) is True
    assert chronos.pending_repeats["main"].ready_at == occurred_at + timedelta(minutes=3)


def test_chronos_cool_delay_must_still_fit_pair_window() -> None:
    pair = replace(sample_pair("pair-r"), try_num=None, dr_pause=0.0, cooldown_minutes=2.0)
    state = StrategyState(
        launched_at=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
        strategy_id="strategy-cool-window",
        pairs={
            "pair-r": PairCycleState(
                pair=pair,
                head_state=HeadState.CLOSED,
                tail_state=TailState.CLOSED,
                tail_mode=TailMode.FLYING,
                played_quantity=Decimal("1"),
                attempt_index=5,
            ),
        },
    )
    chronos = Chronos(state=state)
    occurred_at = datetime(2026, 5, 21, 12, 9, tzinfo=timezone.utc)

    commands = chronos.process_event(
        EggMove(
            kind=EggMoveKind.PLAYED_AND_CANCELED,
            occurred_at=occurred_at,
            symbol="PI_XBTUSD",
            pair_name="pair-r",
            role=OrderRole.TAIL,
            event_id="evt-repeat-cool-window",
            is_private=True,
        ),
        now=occurred_at,
    )

    assert commands == ()
    assert chronos.pending_repeats == {}
    assert chronos.state.pairs["pair-r"].attempt_index == 5


def test_chronos_does_not_repeat_after_pair_window_ends() -> None:
    pair = replace(sample_pair("pair-r"), try_num=2, dr_pause=0.0)
    state = StrategyState(
        launched_at=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
        strategy_id="strategy-repeat",
        pairs={
            "pair-r": PairCycleState(
                pair=pair,
                head_state=HeadState.CLOSED,
                tail_state=TailState.CLOSED,
                tail_mode=TailMode.FLYING,
                played_quantity=Decimal("1"),
            ),
        },
    )
    chronos = Chronos(state=state)
    occurred_at = datetime(2026, 5, 21, 12, 11, tzinfo=timezone.utc)

    commands = chronos.process_event(
        EggMove(
            kind=EggMoveKind.PLAYED_AND_CANCELED,
            occurred_at=occurred_at,
            symbol="PI_XBTUSD",
            pair_name="pair-r",
            event_id="evt-repeat-after-window",
            is_private=True,
        ),
        now=occurred_at,
    )

    assert commands == ()
    assert chronos.pending_repeats == {}
    assert chronos.state.pairs["pair-r"].attempt_index == 1
    assert chronos.state.pairs["pair-r"].head_state == HeadState.CLOSED
