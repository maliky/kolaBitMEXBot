from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import cast

import pytest
from kolabi.bot.domain import (
    HeadSpec,
    HeadState,
    OrderIdentity,
    OrderPairSpec,
    PairCycleState,
    PairIntent,
    PairIntentKind,
    Side,
    TailSpec,
    TailState,
    TailTrailSample,
    TailTrailState,
    TimeWindow,
)
from kolabi.bot.horus import plan_runtime_commands
from kolabi.shared.core.runtime_types import (
    AmendOrderCommandRequest,
    AmendTailCommand,
    PlaceHeadCommand,
    PlaceOrderCommandRequest,
    PlaceTailCommand,
    RuntimeCommandKind,
    Symbol,
)


def sample_pair(
    name: str,
    *,
    head_order_type: str = "Limit",
    tail_order_type: str = "Stop",
    tail_price_spec: float = 99.0,
    tail_price_spec_type: str = "tA",
    amount_type: str = "qApD",
) -> OrderPairSpec:
    return OrderPairSpec(
        name=name,
        window=TimeWindow(start_minutes=-1.0, end_minutes=10.0),
        try_num=1,
        dr_pause=None,
        timeout=60,
        head=HeadSpec(side=Side.BUY, order_type=head_order_type),
        head_price=(100.0, 101.0),
        head_price_type="pA",
        head_quantity=2,
        head_quantity_type="qA",
        tail=TailSpec(side=Side.SELL, order_type=tail_order_type, delta=0.5),
        tail_price_spec=tail_price_spec,
        tail_price_spec_type=tail_price_spec_type,
        amount_type=amount_type,
    )


def logbps_tail_pair(name: str) -> OrderPairSpec:
    pair = sample_pair(name)
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


def sample_state(
    *,
    name: str = "pair-a",
    head_state: HeadState = HeadState.LIVING,
    tail_state: TailState | None = TailState.LIVING,
    played_quantity: Decimal | None = Decimal("2"),
    tail_identity: OrderIdentity | None = None,
) -> PairCycleState:
    return PairCycleState(
        pair=sample_pair(name),
        head_state=head_state,
        tail_state=tail_state,
        played_quantity=played_quantity,
        tail_identity=tail_identity,
    )


def test_plan_runtime_commands_preserves_intent_order() -> None:
    state = sample_state()

    commands = plan_runtime_commands(
        state,
        (
            PairIntent(PairIntentKind.PLACE_HEAD),
            PairIntent(PairIntentKind.PLACE_TAIL),
        ),
        symbol=Symbol("PI_XBTUSD"),
    )

    assert [command.kind for command in commands] == [
        RuntimeCommandKind.PLACE,
        RuntimeCommandKind.PLACE,
    ]
    assert [command.reason for command in commands] == ["head", "tail"]


def test_plan_runtime_commands_is_deterministic() -> None:
    state = sample_state()
    intents = (
        PairIntent(PairIntentKind.PLACE_HEAD),
        PairIntent(PairIntentKind.PLACE_TAIL),
    )

    first = plan_runtime_commands(state, intents, symbol=Symbol("PI_XBTUSD"))
    second = plan_runtime_commands(state, intents, symbol=Symbol("PI_XBTUSD"))

    assert first == second


def test_place_head_translates_to_one_head_place_command() -> None:
    commands = plan_runtime_commands(
        sample_state(),
        (PairIntent(PairIntentKind.PLACE_HEAD),),
        symbol=Symbol("PI_XBTUSD"),
    )

    assert len(commands) == 1
    assert isinstance(commands[0], PlaceHeadCommand)
    assert commands[0].kind == RuntimeCommandKind.PLACE
    assert commands[0].reason == "head"
    assert commands[0].request == PlaceOrderCommandRequest(
        pair_name="pair-a",
        side="buy",
        ordType="Limit",
        orderQty=Decimal("2"),
    )


def test_place_head_strips_local_limit_price_suffix() -> None:
    state = sample_state()
    state = PairCycleState(
        pair=sample_pair("pair-a", head_order_type="Lm"),
        head_state=state.head_state,
        tail_state=state.tail_state,
        played_quantity=state.played_quantity,
    )

    command = plan_runtime_commands(
        state,
        (PairIntent(PairIntentKind.PLACE_HEAD),),
        symbol=Symbol("PI_XBTUSD"),
    )[0]

    assert isinstance(command, PlaceHeadCommand)
    assert command.request.ordType == "L"
    assert command.request.execInst is None
    assert command.legacy_order is not None
    assert command.legacy_order["ordType"] == "L"


def test_place_head_uses_materialised_limit_price() -> None:
    state = sample_state()
    state = PairCycleState(
        pair=sample_pair("pair-a", head_order_type="L"),
        head_state=state.head_state,
        tail_state=state.tail_state,
        played_quantity=state.played_quantity,
        head_order_price=Decimal("73500.5"),
    )

    command = plan_runtime_commands(
        state,
        (PairIntent(PairIntentKind.PLACE_HEAD),),
        symbol=Symbol("PI_XBTUSD"),
    )[0]

    assert isinstance(command, PlaceHeadCommand)
    assert command.request.price == Decimal("73500.5")


def test_place_head_percent_offset_does_not_forward_raw_delta() -> None:
    state = sample_state()
    state = PairCycleState(
        pair=OrderPairSpec(
            name="pair-a",
            window=state.pair.window,
            try_num=state.pair.try_num,
            dr_pause=state.pair.dr_pause,
            timeout=state.pair.timeout,
            head=HeadSpec(
                side=Side.SELL,
                order_type="Lm!",
                delta=1.5,
                delta_type="oB",
            ),
            head_price=state.pair.head_price,
            head_price_type=state.pair.head_price_type,
            head_quantity=state.pair.head_quantity,
            head_quantity_type=state.pair.head_quantity_type,
            tail=state.pair.tail,
            tail_price_spec=state.pair.tail_price_spec,
            tail_price_spec_type=state.pair.tail_price_spec_type,
            amount_type="qAtDpDoB",
        ),
        head_state=state.head_state,
        tail_state=state.tail_state,
        played_quantity=state.played_quantity,
        head_order_price=Decimal("1015.0"),
    )

    command = plan_runtime_commands(
        state,
        (PairIntent(PairIntentKind.PLACE_HEAD),),
        symbol=Symbol("PI_XBTUSD"),
    )[0]

    assert isinstance(command, PlaceHeadCommand)
    assert command.request.price == Decimal("1015.0")
    assert command.request.oDelta is None


def test_place_head_uses_materialised_trigger_price() -> None:
    state = sample_state()
    state = PairCycleState(
        pair=sample_pair("pair-a", head_order_type="Sm"),
        head_state=state.head_state,
        tail_state=state.tail_state,
        played_quantity=state.played_quantity,
        head_order_stop_price=Decimal("73510.0"),
    )

    command = plan_runtime_commands(
        state,
        (PairIntent(PairIntentKind.PLACE_HEAD),),
        symbol=Symbol("PI_XBTUSD"),
    )[0]

    assert isinstance(command, PlaceHeadCommand)
    assert command.request.stopPx == Decimal("73510.0")


def test_place_head_maps_trigger_price_suffix() -> None:
    state = sample_state()
    state = PairCycleState(
        pair=sample_pair("pair-a", head_order_type="Sm"),
        head_state=state.head_state,
        tail_state=state.tail_state,
        played_quantity=state.played_quantity,
    )

    command = plan_runtime_commands(
        state,
        (PairIntent(PairIntentKind.PLACE_HEAD),),
        symbol=Symbol("PI_XBTUSD"),
    )[0]

    assert isinstance(command, PlaceHeadCommand)
    assert command.request.ordType == "S"
    assert command.request.execInst == "MarkPrice"


def test_place_tail_translates_to_one_tail_place_command() -> None:
    commands = plan_runtime_commands(
        sample_state(),
        (PairIntent(PairIntentKind.PLACE_TAIL),),
        symbol=Symbol("PI_XBTUSD"),
    )

    assert len(commands) == 1
    assert isinstance(commands[0], PlaceTailCommand)
    assert commands[0].kind == RuntimeCommandKind.PLACE
    assert commands[0].reason == "tail"
    assert commands[0].request == PlaceOrderCommandRequest(
        pair_name="pair-a",
        side="sell",
        ordType="Stop",
        orderQty=Decimal("2"),
        stopPx=Decimal("99.0"),
        execInst="LastPrice",
        oDelta=Decimal("0.5"),
    )


def test_place_tail_translates_legacy_reduce_only_trigger_suffixes() -> None:
    state = sample_state()
    state = PairCycleState(
        pair=sample_pair("pair-a", tail_order_type="S-"),
        head_state=state.head_state,
        tail_state=state.tail_state,
        played_quantity=state.played_quantity,
    )

    command = plan_runtime_commands(
        state,
        (PairIntent(PairIntentKind.PLACE_TAIL),),
        symbol=Symbol("PI_XBTUSD"),
    )[0]

    assert isinstance(command, PlaceTailCommand)
    assert command.request.ordType == "S"
    assert command.request.execInst == "ReduceOnly,LastPrice"
    assert command.legacy_order is not None
    assert command.legacy_order["ordType"] == "S"
    assert command.legacy_order["execInst"] == "ReduceOnly,LastPrice"


def test_place_tail_translates_standard_stop_to_last_price_trigger() -> None:
    state = sample_state()
    state = PairCycleState(
        pair=sample_pair("pair-a", tail_order_type="S"),
        head_state=state.head_state,
        tail_state=state.tail_state,
        played_quantity=state.played_quantity,
    )

    command = plan_runtime_commands(
        state,
        (PairIntent(PairIntentKind.PLACE_TAIL),),
        symbol=Symbol("PI_XBTUSD"),
    )[0]

    assert isinstance(command, PlaceTailCommand)
    assert command.request.ordType == "S"
    assert command.request.execInst == "LastPrice"
    assert command.legacy_order is not None
    assert command.legacy_order["execInst"] == "LastPrice"


@pytest.mark.parametrize(
    ("tail_type", "expected_type", "expected_exec_inst"),
    [
        ("Sl-", "S", "ReduceOnly,LastPrice"),
        ("Sm-", "S", "ReduceOnly,MarkPrice"),
        ("Si-", "S", "ReduceOnly,IndexPrice"),
        ("SLm-", "SL", "ReduceOnly,MarkPrice"),
        ("LTi-", "LT", "ReduceOnly,IndexPrice"),
    ],
)
def test_place_tail_translates_price_suffix_matrix(
    tail_type: str,
    expected_type: str,
    expected_exec_inst: str,
) -> None:
    state = sample_state()
    state = PairCycleState(
        pair=sample_pair("pair-a", tail_order_type=tail_type),
        head_state=state.head_state,
        tail_state=state.tail_state,
        played_quantity=state.played_quantity,
    )

    command = plan_runtime_commands(
        state,
        (PairIntent(PairIntentKind.PLACE_TAIL),),
        symbol=Symbol("PI_XBTUSD"),
    )[0]

    assert isinstance(command, PlaceTailCommand)
    assert command.request.ordType == expected_type
    assert command.request.execInst == expected_exec_inst


def test_amend_tail_with_full_identity_translates_to_one_amend_command() -> None:
    state = sample_state(
        played_quantity=Decimal("1"),
        tail_identity=OrderIdentity(
            pair_name="pair-a",
            role="tail",
            client_order_id="CID-T",
            exchange_order_id="OID-T",
        )
    )

    commands = plan_runtime_commands(
        state,
        (PairIntent(PairIntentKind.AMEND_TAIL),),
        symbol=Symbol("PI_XBTUSD"),
    )

    assert len(commands) == 1
    assert isinstance(commands[0], AmendTailCommand)
    assert commands[0].kind == RuntimeCommandKind.AMEND
    assert commands[0].reason == "tail"
    assert commands[0].request == AmendOrderCommandRequest(
        pair_name="pair-a",
        side="sell",
        ordType="Stop",
        orderID="OID-T",
        clOrdID="CID-T",
        newPrice=Decimal("99.0"),
        newQty=Decimal("1"),
        oDelta=Decimal("0.5"),
    )


def test_tail_commands_use_dynamic_trail_stop_when_present() -> None:
    now = datetime.now(timezone.utc)
    state = sample_state(
        played_quantity=Decimal("1"),
        tail_identity=OrderIdentity(
            pair_name="pair-a",
            role="tail",
            client_order_id="CID-T",
            exchange_order_id="OID-T",
        ),
    )
    state = PairCycleState(
        pair=state.pair,
        head_state=state.head_state,
        tail_state=state.tail_state,
        played_quantity=state.played_quantity,
        tail_identity=state.tail_identity,
        tail_trail=TailTrailState(
            entry_reference_price=Decimal("100"),
            baseline_width=Decimal("1"),
            current_stop_price=Decimal("101.2"),
            previous_stop_price=Decimal("99"),
            samples=(TailTrailSample(now, Decimal("102")),),
        ),
    )

    place, amend = plan_runtime_commands(
        state,
        (
            PairIntent(PairIntentKind.PLACE_TAIL),
            PairIntent(PairIntentKind.AMEND_TAIL),
        ),
        symbol=Symbol("PI_XBTUSD"),
    )

    assert isinstance(place, PlaceTailCommand)
    assert place.request.stopPx == Decimal("101.2")
    assert isinstance(amend, AmendTailCommand)
    assert amend.request.newPrice == Decimal("101.2")


def test_relative_tail_without_trail_fails_loudly() -> None:
    state = PairCycleState(
        pair=sample_pair(
            "pair-a",
            tail_price_spec=0.5,
            tail_price_spec_type="tB",
            amount_type="qAtBpB",
        ),
        head_state=HeadState.LIVING,
        tail_state=TailState.LIVING,
        played_quantity=Decimal("1"),
    )

    with pytest.raises(ValueError, match="initialised tail trail"):
        plan_runtime_commands(
            state,
            (PairIntent(PairIntentKind.PLACE_TAIL),),
            symbol=Symbol("PI_XBTUSD"),
        )


def test_amend_tail_without_identity_raises() -> None:
    state = sample_state(
        head_state=HeadState.CLOSED,
        tail_state=TailState.HOOKED,
        played_quantity=Decimal("1"),
    )

    with pytest.raises(ValueError, match="existing tail identity"):
        plan_runtime_commands(
            state,
            (PairIntent(PairIntentKind.AMEND_TAIL),),
            symbol=Symbol("PI_XBTUSD"),
        )


def test_amend_tail_with_partial_identity_raises() -> None:
    state = sample_state(
        tail_identity=OrderIdentity(
            pair_name="pair-a",
            role="tail",
            client_order_id="CID-T",
            exchange_order_id=None,
        )
    )

    with pytest.raises(ValueError, match="both client and exchange order IDs"):
        plan_runtime_commands(
            state,
            (PairIntent(PairIntentKind.AMEND_TAIL),),
            symbol=Symbol("PI_XBTUSD"),
        )


def test_unsupported_intent_kind_raises() -> None:
    bogus_intent = PairIntent(cast(PairIntentKind, "bogus_intent"))

    with pytest.raises(ValueError, match="unsupported pair intent kind"):
        plan_runtime_commands(
            sample_state(),
            (bogus_intent,),
            symbol=Symbol("PI_XBTUSD"),
        )


def test_planner_does_not_mutate_input_state_or_intents() -> None:
    state = sample_state(
        tail_identity=OrderIdentity(
            pair_name="pair-a",
            role="tail",
            client_order_id="CID-T",
            exchange_order_id="OID-T",
        )
    )
    intents = (
        PairIntent(PairIntentKind.PLACE_HEAD),
        PairIntent(PairIntentKind.AMEND_TAIL),
    )
    state_before = state
    intents_before = intents

    _ = plan_runtime_commands(
        state,
        intents,
        symbol=Symbol("PI_XBTUSD"),
    )

    assert state == state_before
    assert intents == intents_before
