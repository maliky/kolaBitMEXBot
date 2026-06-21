from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from kolabi.bot.chronos import (
    Chronos,
    ChronosNoticeKind,
    PendingRepeat,
    pair_dependency_satisfied,
)
from kolabi.bot.domain import (
    ChainDependencyToken,
    EggMove,
    EggMoveKind,
    HeadSpec,
    HeadState,
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
                dependency_token=ChainDependencyToken(
                    origin_pair_name="main",
                    origin_attempt_index=1,
                    closed_at=datetime(2026, 5, 21, 12, 3, tzinfo=timezone.utc),
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
    assert chain_state.dependency_token is not None
    assert chain_state.dependency_token.origin_attempt_index == 1
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
                dependency_token=ChainDependencyToken(
                    origin_pair_name="main",
                    origin_attempt_index=1,
                    closed_at=datetime(2026, 5, 21, 12, 3, tzinfo=timezone.utc),
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
    token = chronos.state.pairs["chain"].dependency_token
    assert token is not None
    assert token.origin_attempt_index == 1

    ready_commands = chronos.activate_ready_repeats(
        symbol="PI_XBTUSD",
        now=ready_at + timedelta(seconds=1),
    )
    assert ready_commands == ()
    assert chronos.state.pairs["chain"].attempt_index == 2
    assert chronos.state.pairs["chain"].dependency_token is None

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
    assert chain_state.dependency_token is not None
    assert chain_state.dependency_token.origin_pair_name == "main"
    assert chain_state.dependency_token.origin_attempt_index == 3
    assert chain_state.dependency_token.closed_at == datetime(
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
