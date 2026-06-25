from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from kolabi.bot.domain import (
    EggMove,
    EggMoveKind,
    HeadSpec,
    HeadState,
    OrderIdentity,
    OrderPairSpec,
    OrderReason,
    OrderRole,
    PairCycleState,
    PairIntentKind,
    Side,
    TailMode,
    TailSpec,
    TailState,
    TimeWindow,
)
from kolabi.bot.horus import plan_runtime_commands
from kolabi.bot.pair_cycle import step_pair
from kolabi.bot.tail_tracking import initial_tail_trail
from kolabi.shared.core.runtime_types import PlaceTailCommand, Symbol


def sample_pair() -> OrderPairSpec:
    return OrderPairSpec(
        name="pair-a",
        window=TimeWindow(start_minutes=-1.0, end_minutes=1.0),
        try_num=1,
        dr_pause=None,
        timeout=None,
        head=HeadSpec(
            side=Side.BUY,
            order_type="Limit",
            delta=None,
        ),
        head_price=(100.0, 101.0),
        head_price_type="pA",
        head_quantity=2,
        head_quantity_type="qA",
        tail=TailSpec(
            side=Side.SELL,
            order_type="Stop",
            delta=0.5,
        ),
        tail_price_spec=99.0,
        tail_price_spec_type="tA",
        amount_type="qApD",
        hook_name=None,
    )


def logbps_tail_pair() -> OrderPairSpec:
    pair = sample_pair()
    return OrderPairSpec(
        name=pair.name,
        window=pair.window,
        try_num=pair.try_num,
        dr_pause=pair.dr_pause,
        timeout=pair.timeout,
        head=pair.head,
        head_price=pair.head_price,
        head_price_type="pB",
        head_quantity=pair.head_quantity,
        head_quantity_type=pair.head_quantity_type,
        tail=pair.tail,
        tail_price_spec=148.89,
        tail_price_spec_type="tB",
        amount_type="qAtBpB",
        hook_name=pair.hook_name,
    )


def egg_move(kind: EggMoveKind, *, played_quantity: float = 0.0) -> EggMove:
    return EggMove(
        kind=kind,
        occurred_at=datetime.now(timezone.utc),
        symbol="PI_XBTUSD",
        reply={
            "orderID": "OID-1",
            "clOrdID": "CID-1",
            "cumQty": played_quantity,
            "orderQty": 2.0,
            "reference_price": 100.0,
        },
    )


def post_only_would_fill_move(*, role: OrderRole = OrderRole.HEAD) -> EggMove:
    return EggMove(
        kind=EggMoveKind.PLAYED_AND_CANCELED,
        role=role,
        occurred_at=datetime.now(timezone.utc),
        symbol="PI_XBTUSD",
        reply={
            "orderID": "OID-1",
            "clOrdID": "CID-1",
            "cumQty": 0.0,
            "orderQty": 2.0,
            "execType": OrderReason.POST_ONLY_WOULD_FILL.value,
            "reference_price": 100.0,
        },
    )


def submitted_state() -> PairCycleState:
    return PairCycleState(
        pair=sample_pair(),
        head_state=HeadState.SUBMITTED,
    )


def submitted_logbps_tail_state() -> PairCycleState:
    return PairCycleState(
        pair=logbps_tail_pair(),
        head_state=HeadState.SUBMITTED,
    )


def test_step_pair_played_open_head_keeps_flapping_tail_living() -> None:
    next_state, intents = step_pair(
        submitted_state(),
        egg_move(EggMoveKind.PLAYED_NOT_CANCELED, played_quantity=2.0),
    )

    assert next_state.head_state == HeadState.LIVING
    assert next_state.tail_state == TailState.LIVING
    assert next_state.tail_mode == TailMode.FLAPPING
    assert next_state.tail_trail is not None
    assert next_state.played_quantity == Decimal("2.0")
    assert len(intents) == 1
    assert intents[0].kind == PairIntentKind.PLACE_TAIL


def test_step_pair_head_failed_keeps_tail_latent() -> None:
    next_state, intents = step_pair(
        submitted_state(),
        egg_move(EggMoveKind.NOT_PLAYED_CANCELED),
    )

    assert next_state.head_state == HeadState.FAILED
    assert next_state.tail_state == TailState.LATENT
    assert intents == ()


def test_partial_fill_tail_uses_played_quantity_not_planned_quantity() -> None:
    next_state, intents = step_pair(
        submitted_state(),
        egg_move(EggMoveKind.PLAYED_NOT_CANCELED, played_quantity=1.0),
    )

    assert next_state.head_state == HeadState.LIVING
    assert next_state.tail_state == TailState.LIVING
    assert next_state.tail_mode == TailMode.FLAPPING
    assert next_state.played_quantity == Decimal("1.0")
    assert len(intents) == 1
    assert intents[0].kind == PairIntentKind.PLACE_TAIL


def test_logbps_tail_uses_reference_derived_stop() -> None:
    pair = OrderPairSpec(
        name="pair-a",
        window=TimeWindow(start_minutes=-1.0, end_minutes=1.0),
        try_num=1,
        dr_pause=None,
        timeout=None,
        head=HeadSpec(side=Side.SELL, order_type="Market", delta=None),
        head_price=(-1.0, 1.0),
        head_price_type="pB",
        head_quantity=2,
        head_quantity_type="qA",
        tail=TailSpec(side=Side.BUY, order_type="Stop", delta=None),
        tail_price_spec=49.88,
        tail_price_spec_type="tB",
        amount_type="qAtBpB",
    )
    next_state, intents = step_pair(
        PairCycleState(pair=pair, head_state=HeadState.SUBMITTED),
        EggMove(
            kind=EggMoveKind.PLAYED_NOT_CANCELED,
            occurred_at=datetime.now(timezone.utc),
            symbol="PI_XBTUSD",
            reply={
                "orderID": "OID-1",
                "clOrdID": "CID-1",
                "cumQty": 1.0,
                "orderQty": 2.0,
                "reference_price": 100.0,
            },
        ),
    )

    assert next_state.tail_trail is not None
    assert abs(next_state.tail_trail.current_stop_price - Decimal("100.5")) < Decimal("0.001")
    assert len(intents) == 1
    assert intents[0].kind == PairIntentKind.PLACE_TAIL


def test_step_pair_zero_fill_cancel_fails_without_tail() -> None:
    next_state, intents = step_pair(
        submitted_state(),
        egg_move(EggMoveKind.NOT_PLAYED_CANCELED, played_quantity=0.0),
    )

    assert next_state.head_state == HeadState.FAILED
    assert next_state.tail_state == TailState.LATENT
    assert next_state.tail_mode is None
    assert intents == ()


def test_would_not_reduce_position_is_not_treated_as_played() -> None:
    next_state, intents = step_pair(
        submitted_state(),
        EggMove(
            kind=EggMoveKind.NOT_PLAYED_CANCELED,
            occurred_at=datetime.now(timezone.utc),
            symbol="PI_XBTUSD",
            reply={
                "orderID": "OID-1",
                "clOrdID": "CID-1",
                "cumQty": 0.0,
                "orderQty": 2.0,
                "execType": OrderReason.WOULD_NOT_REDUCE_POSITION.value,
                "reference_price": 100.0,
            },
        ),
    )

    assert next_state.head_state == HeadState.FAILED
    assert next_state.tail_state == TailState.LATENT
    assert next_state.tail_mode is None
    assert intents == ()


def test_post_only_would_fill_head_reject_is_not_treated_as_played() -> None:
    pair = sample_pair()
    state = PairCycleState(
        pair=replace(pair, head=replace(pair.head, order_type="L!")),
        head_state=HeadState.SUBMITTED,
    )

    next_state, intents = step_pair(
        state,
        post_only_would_fill_move(),
    )

    assert next_state.head_state == HeadState.FAILED
    assert next_state.tail_state == TailState.LATENT
    assert next_state.tail_mode is None
    assert intents == ()


def test_zero_offset_post_only_trigger_limit_probe_hooks_tail() -> None:
    pair = sample_pair()
    state = PairCycleState(
        pair=replace(
            pair,
            head=replace(pair.head, order_type="LT!"),
            head_order_price_spec=0.0,
            head_order_price_spec_type="hD",
        ),
        head_state=HeadState.SUBMITTED,
    )

    next_state, intents = step_pair(state, post_only_would_fill_move())

    assert next_state.head_state == HeadState.CLOSED
    assert next_state.tail_state == TailState.HOOKED
    assert next_state.tail_mode == TailMode.FLYING
    assert next_state.tail_trail is not None
    assert next_state.played_quantity == Decimal("0.0")
    assert len(intents) == 1
    assert intents[0].kind == PairIntentKind.PLACE_TAIL

    commands = plan_runtime_commands(next_state, intents, symbol=Symbol("PI_XBTUSD"))
    assert len(commands) == 1
    assert isinstance(commands[0], PlaceTailCommand)
    assert commands[0].request.orderQty == Decimal("2")


def test_nonzero_post_only_trigger_limit_reject_fails_without_tail() -> None:
    pair = sample_pair()
    state = PairCycleState(
        pair=replace(
            pair,
            head=replace(pair.head, order_type="LT!"),
            head_order_price_spec=0.1,
            head_order_price_spec_type="hD",
        ),
        head_state=HeadState.SUBMITTED,
    )

    next_state, intents = step_pair(state, post_only_would_fill_move())

    assert next_state.head_state == HeadState.FAILED
    assert next_state.tail_state == TailState.LATENT
    assert next_state.tail_mode is None
    assert intents == ()


def test_tail_role_post_only_would_fill_reject_is_unchanged() -> None:
    played, _ = step_pair(
        submitted_state(),
        egg_move(EggMoveKind.PLAYED_NOT_CANCELED, played_quantity=2.0),
    )

    next_state, intents = step_pair(
        played,
        post_only_would_fill_move(role=OrderRole.TAIL),
    )

    assert next_state.head_state == HeadState.LIVING
    assert next_state.tail_state == TailState.CLOSED
    assert next_state.tail_mode == TailMode.FLYING
    assert intents == ()


def test_step_pair_canceled_head_after_fill_leaves_flying_tail() -> None:
    next_state, intents = step_pair(
        submitted_state(),
        egg_move(EggMoveKind.PLAYED_AND_CANCELED, played_quantity=1.0),
    )

    assert next_state.head_state == HeadState.CLOSED
    assert next_state.tail_state == TailState.HOOKED
    assert next_state.tail_mode == TailMode.FLYING
    assert next_state.tail_trail is not None
    assert next_state.played_quantity == Decimal("1.0")
    assert len(intents) == 1
    assert intents[0].kind == PairIntentKind.PLACE_TAIL


def test_failed_head_ignores_later_played_event() -> None:
    failed = PairCycleState(
        pair=sample_pair(),
        head_state=HeadState.FAILED,
        tail_state=TailState.LATENT,
        played_quantity=Decimal("0"),
    )
    next_state, intents = step_pair(
        failed,
        egg_move(EggMoveKind.PLAYED_NOT_CANCELED, played_quantity=2.0),
    )

    assert next_state == failed
    assert intents == ()


def test_closed_head_ignores_later_partial_fill_event() -> None:
    closed = PairCycleState(
        pair=sample_pair(),
        head_state=HeadState.CLOSED,
        tail_state=TailState.LIVING,
        tail_mode=TailMode.FLYING,
        played_quantity=Decimal("1.0"),
    )
    next_state, intents = step_pair(
        closed,
        egg_move(EggMoveKind.PLAYED_NOT_CANCELED, played_quantity=1.5),
    )

    assert next_state == closed
    assert intents == ()


def test_duplicate_head_filled_does_not_submit_second_tail() -> None:
    first_state, first_intents = step_pair(
        submitted_state(),
        egg_move(EggMoveKind.PLAYED_NOT_CANCELED, played_quantity=2.0),
    )
    second_state, second_intents = step_pair(
        first_state,
        egg_move(EggMoveKind.PLAYED_NOT_CANCELED, played_quantity=2.0),
    )

    assert len(first_intents) == 1
    assert first_intents[0].kind == PairIntentKind.PLACE_TAIL
    assert second_state == first_state
    assert second_intents == ()


def test_closed_played_head_keeps_existing_flying_tail_living() -> None:
    partially_played = PairCycleState(
        pair=sample_pair(),
        head_state=HeadState.LIVING,
        tail_state=TailState.LIVING,
        tail_mode=TailMode.FLAPPING,
        played_quantity=Decimal("1.0"),
    )
    next_state, intents = step_pair(
        partially_played,
        egg_move(EggMoveKind.PLAYED_AND_CANCELED, played_quantity=1.0),
    )

    assert next_state.head_state == HeadState.CLOSED
    assert next_state.tail_state == TailState.LIVING
    assert next_state.tail_mode == TailMode.FLYING
    assert len(intents) == 1
    assert intents[0].kind == PairIntentKind.PLACE_TAIL


def test_rest_ack_marks_submitted_only() -> None:
    hooked = PairCycleState(pair=sample_pair(), head_state=HeadState.HOOKED)
    next_state, intents = step_pair(
        hooked,
        egg_move(EggMoveKind.HEAD_SUBMITTED, played_quantity=0.0),
    )

    assert next_state.head_state == HeadState.SUBMITTED
    assert next_state.tail_state is None
    assert intents == ()


def test_private_confirmation_required_for_tail_hook() -> None:
    submitted = submitted_state()
    next_state, intents = step_pair(
        submitted,
        egg_move(EggMoveKind.NOT_PLAYED_NOR_CANCELED, played_quantity=0.0),
    )

    assert next_state.head_state == HeadState.NEW
    assert intents == ()


def test_logbps_tail_place_uses_reference_price_not_raw_tail_spec() -> None:
    next_state, intents = step_pair(
        submitted_logbps_tail_state(),
        egg_move(EggMoveKind.PLAYED_NOT_CANCELED, played_quantity=3.0),
    )

    commands = plan_runtime_commands(
        next_state,
        intents,
        symbol=Symbol("PI_XBTUSD"),
    )

    assert next_state.tail_trail is not None
    assert abs(next_state.tail_trail.current_stop_price - Decimal("98.500")) < Decimal("0.001")
    assert len(commands) == 1
    assert isinstance(commands[0], PlaceTailCommand)
    assert abs(commands[0].request.stopPx - Decimal("98.500")) < Decimal("0.001")


def test_market_tick_with_tail_identity_amends_on_first_unblock() -> None:
    played, _ = step_pair(
        submitted_state(),
        egg_move(EggMoveKind.PLAYED_NOT_CANCELED, played_quantity=2.0),
    )
    played = replace(
        played,
        pair=replace(played.pair, tail_second_update_wait_seconds=6.0),
    )
    first_unblocked_at = datetime.now(timezone.utc)
    identified = PairCycleState(
        pair=played.pair,
        head_state=played.head_state,
        tail_state=played.tail_state,
        tail_mode=played.tail_mode,
        tail_identity=OrderIdentity(
            pair_name="pair-a",
            role="tail",
            client_order_id="CID-T",
            exchange_order_id="OID-T",
        ),
        tail_trail=(
            None
            if played.tail_trail is None
            else replace(played.tail_trail, confirmed_stop_price=played.tail_trail.current_stop_price)
        ),
        played_quantity=played.played_quantity,
    )

    amended, amended_intents = step_pair(
        identified,
        EggMove(
            kind=EggMoveKind.MARKET_TICK,
            occurred_at=first_unblocked_at,
            symbol="PI_XBTUSD",
            pair_name="pair-a",
            reply={"reference_price": 102.0},
        ),
    )
    repeated, repeated_intents = step_pair(
        amended,
        EggMove(
            kind=EggMoveKind.MARKET_TICK,
            occurred_at=first_unblocked_at + timedelta(seconds=3),
            symbol="PI_XBTUSD",
            pair_name="pair-a",
            reply={"reference_price": 102.3},
        ),
    )

    assert amended.tail_trail is not None
    assert amended.tail_trail.current_stop_price > Decimal("100")
    assert amended.tail_trail.first_unblocked_at == first_unblocked_at
    assert len(amended_intents) == 1
    assert amended_intents[0].kind == PairIntentKind.AMEND_TAIL
    assert repeated.tail_trail is not None
    assert repeated.tail_trail.current_stop_price == amended.tail_trail.current_stop_price
    assert repeated_intents == ()


def test_market_tick_tublk_controls_first_tail_amend_threshold() -> None:
    pair = replace(sample_pair(), tail_unblock_spec=0.5, tail_unblock_spec_type="uD")
    played, _ = step_pair(
        PairCycleState(pair=pair, head_state=HeadState.SUBMITTED),
        egg_move(EggMoveKind.PLAYED_NOT_CANCELED, played_quantity=2.0),
    )
    first_unblocked_at = datetime.now(timezone.utc)
    assert played.tail_trail is not None
    identified = replace(
        played,
        tail_identity=OrderIdentity(
            pair_name="pair-a",
            role="tail",
            client_order_id="CID-T",
            exchange_order_id="OID-T",
        ),
        tail_trail=replace(
            played.tail_trail,
            confirmed_stop_price=played.tail_trail.current_stop_price,
        ),
    )

    before_threshold, before_intents = step_pair(
        identified,
        EggMove(
            kind=EggMoveKind.MARKET_TICK,
            occurred_at=first_unblocked_at,
            symbol="PI_XBTUSD",
            pair_name="pair-a",
            reply={"reference_price": 100.4},
        ),
    )
    amended, amended_intents = step_pair(
        before_threshold,
        EggMove(
            kind=EggMoveKind.MARKET_TICK,
            occurred_at=first_unblocked_at + timedelta(seconds=7),
            symbol="PI_XBTUSD",
            pair_name="pair-a",
            reply={"reference_price": 100.6},
        ),
    )

    assert before_threshold.tail_trail is not None
    assert before_threshold.tail_trail.first_unblocked_at is None
    assert before_intents == ()
    assert amended.tail_trail is not None
    assert amended.tail_trail.first_unblocked_at == first_unblocked_at + timedelta(seconds=7)
    assert amended.tail_trail.current_stop_price > before_threshold.tail_trail.current_stop_price
    assert len(amended_intents) == 1
    assert amended_intents[0].kind == PairIntentKind.AMEND_TAIL


def test_market_tick_before_tail_identity_updates_state_without_amend() -> None:
    played, _ = step_pair(
        submitted_state(),
        egg_move(EggMoveKind.PLAYED_NOT_CANCELED, played_quantity=2.0),
    )
    first_unblocked_at = datetime.now(timezone.utc)

    moved, intents = step_pair(
        played,
        EggMove(
            kind=EggMoveKind.MARKET_TICK,
            occurred_at=first_unblocked_at,
            symbol="PI_XBTUSD",
            pair_name="pair-a",
            reply={"reference_price": 102.0},
        ),
    )

    assert moved.tail_trail is not None
    assert moved.tail_trail.current_stop_price > Decimal("100")
    assert moved.tail_trail.first_unblocked_at == first_unblocked_at
    assert intents == ()


def test_closed_pair_ignores_later_market_tick() -> None:
    closed = PairCycleState(
        pair=sample_pair(),
        head_state=HeadState.CLOSED,
        tail_state=TailState.LIVING,
        tail_mode=TailMode.FLYING,
    )

    next_state, intents = step_pair(
        closed,
        EggMove(
            kind=EggMoveKind.MARKET_TICK,
            occurred_at=datetime.now(timezone.utc),
            symbol="PI_XBTUSD",
            pair_name="pair-a",
            reply={"reference_price": 102.0},
        ),
    )

    assert next_state == closed
    assert intents == ()


def test_closed_head_with_active_tail_still_trails_on_market_tick() -> None:
    first_unblocked_at = datetime.now(timezone.utc) - timedelta(seconds=50)
    trail = initial_tail_trail(sample_pair(), Decimal("100"), first_unblocked_at)
    trail = replace(
        trail,
        confirmed_stop_price=trail.current_stop_price,
        first_unblocked_at=first_unblocked_at,
    )
    closed = PairCycleState(
        pair=sample_pair(),
        head_state=HeadState.CLOSED,
        tail_state=TailState.LIVING,
        tail_mode=TailMode.FLYING,
        tail_identity=OrderIdentity(
            pair_name="pair-a",
            role="tail",
            client_order_id="CID-T",
            exchange_order_id="OID-T",
        ),
        tail_trail=trail,
        played_quantity=Decimal("1"),
    )

    next_state, intents = step_pair(
        closed,
        EggMove(
            kind=EggMoveKind.MARKET_TICK,
            occurred_at=datetime.now(timezone.utc),
            symbol="PI_XBTUSD",
            pair_name="pair-a",
            reply={"reference_price": 102.0},
        ),
    )

    assert next_state.tail_trail is not None
    assert next_state.tail_trail.current_stop_price > trail.current_stop_price
    assert len(intents) == 1
    assert intents[0].kind == PairIntentKind.AMEND_TAIL


def test_closed_head_hooked_tail_does_not_trail_before_private_confirmation() -> None:
    closed, first_intents = step_pair(
        submitted_state(),
        egg_move(EggMoveKind.PLAYED_AND_CANCELED, played_quantity=1.0),
    )

    next_state, intents = step_pair(
        closed,
        EggMove(
            kind=EggMoveKind.MARKET_TICK,
            occurred_at=datetime.now(timezone.utc),
            symbol="PI_XBTUSD",
            pair_name="pair-a",
            reply={"reference_price": 102.0},
        ),
    )

    assert len(first_intents) == 1
    assert next_state == closed
    assert intents == ()


def test_closed_pair_accepts_tail_submission_identity() -> None:
    closed = PairCycleState(
        pair=sample_pair(),
        head_state=HeadState.CLOSED,
        tail_state=TailState.HOOKED,
        tail_mode=TailMode.FLYING,
    )

    next_state, intents = step_pair(
        closed,
        EggMove(
            kind=EggMoveKind.TAIL_SUBMITTED,
            occurred_at=datetime.now(timezone.utc),
            symbol="PI_XBTUSD",
            pair_name="pair-a",
            reply={"orderID": "OID-T", "clOrdID": "CID-T"},
        ),
    )

    assert next_state.tail_state == TailState.SUBMITTED
    assert next_state.tail_identity == OrderIdentity(
        pair_name="pair-a",
        role="tail",
        client_order_id="CID-T",
        exchange_order_id="OID-T",
        symbol="PI_XBTUSD",
    )
    assert intents == ()


def test_closed_pair_marks_tail_living_on_private_open_confirmation() -> None:
    closed = PairCycleState(
        pair=sample_pair(),
        head_state=HeadState.CLOSED,
        tail_state=TailState.SUBMITTED,
        tail_mode=TailMode.FLYING,
        tail_identity=OrderIdentity(
            pair_name="pair-a",
            role="tail",
            client_order_id="CID-T",
            exchange_order_id="OID-T",
        ),
    )

    next_state, intents = step_pair(
        closed,
        EggMove(
            kind=EggMoveKind.NOT_PLAYED_NOR_CANCELED,
            occurred_at=datetime.now(timezone.utc),
            symbol="PI_XBTUSD",
            pair_name="pair-a",
            role=OrderRole.TAIL,
            reply={"orderID": "OID-T", "clOrdID": "CID-T"},
            is_private=True,
        ),
    )

    assert next_state.tail_state == TailState.LIVING
    assert intents == ()


def test_stale_sell_tail_open_confirmation_does_not_roll_back_desired_stop() -> None:
    now = datetime.now(timezone.utc)
    trail = replace(
        initial_tail_trail(sample_pair(), Decimal("100"), now),
        confirmed_stop_price=Decimal("99"),
        current_stop_price=Decimal("105"),
        previous_stop_price=Decimal("99"),
        last_amended_at=now,
        last_confirmed_at=now,
    )
    state = PairCycleState(
        pair=sample_pair(),
        head_state=HeadState.CLOSED,
        tail_state=TailState.LIVING,
        tail_mode=TailMode.FLYING,
        tail_identity=OrderIdentity(
            pair_name="pair-a",
            role="tail",
            client_order_id="CID-T",
            exchange_order_id="OID-T",
        ),
        tail_trail=trail,
        played_quantity=Decimal("2"),
    )

    next_state, intents = step_pair(
        state,
        EggMove(
            kind=EggMoveKind.NOT_PLAYED_NOR_CANCELED,
            occurred_at=now,
            symbol="PI_XBTUSD",
            pair_name="pair-a",
            role=OrderRole.TAIL,
            reply={
                "orderID": "OID-T",
                "clOrdID": "CID-T",
                "stopPx": 99.0,
            },
            is_private=True,
        ),
    )

    assert next_state.tail_trail is not None
    assert next_state.tail_trail.confirmed_stop_price == Decimal("99.0")
    assert next_state.tail_trail.current_stop_price == Decimal("105")
    assert intents == ()


def test_sell_tail_amend_confirmation_advances_confirmed_and_desired_stop() -> None:
    now = datetime.now(timezone.utc)
    trail = replace(
        initial_tail_trail(sample_pair(), Decimal("100"), now),
        confirmed_stop_price=Decimal("99"),
        current_stop_price=Decimal("105"),
        previous_stop_price=Decimal("99"),
        last_amended_at=now,
        last_confirmed_at=now,
    )
    state = PairCycleState(
        pair=sample_pair(),
        head_state=HeadState.CLOSED,
        tail_state=TailState.LIVING,
        tail_mode=TailMode.FLYING,
        tail_identity=OrderIdentity(
            pair_name="pair-a",
            role="tail",
            client_order_id="CID-T",
            exchange_order_id="OID-T",
        ),
        tail_trail=trail,
        played_quantity=Decimal("2"),
    )

    next_state, intents = step_pair(
        state,
        EggMove(
            kind=EggMoveKind.NOT_PLAYED_NOR_CANCELED,
            occurred_at=now,
            symbol="PI_XBTUSD",
            pair_name="pair-a",
            role=OrderRole.TAIL,
            reply={
                "orderID": "OID-T",
                "clOrdID": "CID-T",
                "stopPx": 105.0,
            },
            is_private=True,
        ),
    )

    assert next_state.tail_trail is not None
    assert next_state.tail_trail.confirmed_stop_price == Decimal("105.0")
    assert next_state.tail_trail.current_stop_price == Decimal("105.0")
    assert intents == ()


def test_stale_buy_tail_open_confirmation_does_not_roll_back_desired_stop() -> None:
    pair = replace(
        sample_pair(),
        head=HeadSpec(side=Side.SELL, order_type="Market", delta=None),
        tail=TailSpec(side=Side.BUY, order_type="Stop", delta=None),
        tail_price_spec=101.0,
    )
    now = datetime.now(timezone.utc)
    trail = replace(
        initial_tail_trail(pair, Decimal("100"), now),
        confirmed_stop_price=Decimal("101"),
        current_stop_price=Decimal("95"),
        previous_stop_price=Decimal("101"),
        last_amended_at=now,
        last_confirmed_at=now,
    )
    state = PairCycleState(
        pair=pair,
        head_state=HeadState.CLOSED,
        tail_state=TailState.LIVING,
        tail_mode=TailMode.FLYING,
        tail_identity=OrderIdentity(
            pair_name="pair-a",
            role="tail",
            client_order_id="CID-T",
            exchange_order_id="OID-T",
        ),
        tail_trail=trail,
        played_quantity=Decimal("2"),
    )

    next_state, intents = step_pair(
        state,
        EggMove(
            kind=EggMoveKind.NOT_PLAYED_NOR_CANCELED,
            occurred_at=now,
            symbol="PI_XBTUSD",
            pair_name="pair-a",
            role=OrderRole.TAIL,
            reply={
                "orderID": "OID-T",
                "clOrdID": "CID-T",
                "stopPx": 101.0,
            },
            is_private=True,
        ),
    )

    assert next_state.tail_trail is not None
    assert next_state.tail_trail.confirmed_stop_price == Decimal("101.0")
    assert next_state.tail_trail.current_stop_price == Decimal("95")
    assert intents == ()


def test_closed_head_accepts_tail_terminal_private_close() -> None:
    closed = PairCycleState(
        pair=sample_pair(),
        head_state=HeadState.CLOSED,
        tail_state=TailState.SUBMITTED,
        tail_mode=TailMode.FLYING,
        tail_identity=OrderIdentity(
            pair_name="pair-a",
            role="tail",
            client_order_id="CID-T",
            exchange_order_id="OID-T",
        ),
        played_quantity=Decimal("3"),
    )

    next_state, intents = step_pair(
        closed,
        EggMove(
            kind=EggMoveKind.PLAYED_AND_CANCELED,
            occurred_at=datetime.now(timezone.utc),
            symbol="PI_XBTUSD",
            pair_name="pair-a",
            role=OrderRole.TAIL,
            reply={
                "orderID": "OID-T",
                "clOrdID": "CID-T",
                "cumQty": 3.0,
                "orderQty": 3.0,
                "execType": OrderReason.STOP_ORDER_TRIGGERED.value,
            },
            is_private=True,
        ),
    )

    assert next_state.head_state == HeadState.CLOSED
    assert next_state.tail_state == TailState.CLOSED
    assert intents == ()


def test_closed_head_accepts_tail_terminal_private_fail() -> None:
    closed = PairCycleState(
        pair=sample_pair(),
        head_state=HeadState.CLOSED,
        tail_state=TailState.SUBMITTED,
        tail_mode=TailMode.FLYING,
        tail_identity=OrderIdentity(
            pair_name="pair-a",
            role="tail",
            client_order_id="CID-T",
            exchange_order_id="OID-T",
        ),
        played_quantity=Decimal("3"),
    )

    next_state, intents = step_pair(
        closed,
        EggMove(
            kind=EggMoveKind.NOT_PLAYED_CANCELED,
            occurred_at=datetime.now(timezone.utc),
            symbol="PI_XBTUSD",
            pair_name="pair-a",
            role=OrderRole.TAIL,
            reply={
                "orderID": "OID-T",
                "clOrdID": "CID-T",
                "cumQty": 0.0,
                "orderQty": 3.0,
                "execType": OrderReason.CANCELLED_BY_USER.value,
            },
            is_private=True,
        ),
    )

    assert next_state.head_state == HeadState.CLOSED
    assert next_state.tail_state == TailState.FAILED
    assert intents == ()
