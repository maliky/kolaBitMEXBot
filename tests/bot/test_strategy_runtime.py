from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, cast

from kolabi.bot.chronos import PendingRepeat
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
    StrategySpec,
    StrategyState,
    TailSpec,
    TailState,
    TimeWindow,
)
from kolabi.bot.exchange_routes import ExchangeRoute
from kolabi.bot.horus import plan_runtime_commands
from kolabi.bot.pair_cycle import step_pair
from kolabi.bot.repeat_adjustment import (
    RepeatAdjustmentContext,
    head_timed_out,
    register_repeat_adjustment,
)
from kolabi.bot.strategy_runtime import (
    KrakenPrivateOrderPollingSource,
    KrakenPublicTriggerSource,
    SimulatedExecutor,
    StrategyRuntime,
    _CommandSlot,
    _TailAmendPending,
    _TailVisibilityWindow,
    plan_strategy_once,
)
from kolabi.bot.tail_tracking import initial_tail_trail
from kolabi.shared.core.models import OrderAck
from kolabi.shared.core.runtime_types import (
    AmendOrderCommandRequest,
    AmendTailCommand,
    CancelCommand,
    CancelOrderCommandRequest,
    DragonSong,
    HeadVisibilityResult,
    PlaceHeadCommand,
    PlaceOrderCommandRequest,
    PlaceTailCommand,
    PrivateOrderRecord,
    RuntimeCommandKind,
    Symbol,
    VerifyHeadVisibilityCommand,
    to_decimal,
)
from kolabi.shared.runtime_state import KrakenRuntimeStateClient
from kolabi.tree.account import AccountStateStore, AccountStreamConfig


def _close_evidence(
    origin_pair_name: str,
    origin_attempt_index: int,
    satisfied_at: datetime,
) -> HookEvidence:
    return HookEvidence(
        target=HookTarget(origin_pair_name, HookTargetKind.PAIR_CLOSED),
        origin_attempt_index=origin_attempt_index,
        satisfied_at=satisfied_at,
    )


class _RecordingLiveExecutor:
    def __init__(self) -> None:
        self.commands: list[DragonSong] = []
        self.order_ids_by_client: dict[str, str] = {}
        self.started = asyncio.Event()

    async def execute(self, command: DragonSong) -> OrderAck:
        self.commands.append(command)
        client_order_id = getattr(command.request, "clOrdID", None) or f"NO-CID-{len(self.commands)}"
        order_id = self.order_ids_by_client.setdefault(
            str(client_order_id),
            f"OID-{client_order_id}",
        )
        self.started.set()
        await asyncio.sleep(0)
        price = getattr(command.request, "newPrice", None)
        if price is None:
            price = getattr(command.request, "price", None)
        if price is None:
            price = getattr(command.request, "stopPx", None)
        return OrderAck(
            order_id=order_id,
            status="New",
            price=None if price is None else float(to_decimal(price)),
            orig_qty=_command_quantity(command),
            executed_qty=0.0,
            side=getattr(command.request, "side", None),
        )


class _CancelAckLiveExecutor(_RecordingLiveExecutor):
    async def execute(self, command: DragonSong) -> OrderAck:
        if not isinstance(command, CancelCommand):
            return await super().execute(command)
        self.commands.append(command)
        self.started.set()
        await asyncio.sleep(0)
        return OrderAck(
            order_id=command.request.clOrdID,
            status="Canceled",
            orig_qty=0.0,
            executed_qty=0.0,
        )


class _PrivateDbHarness:
    def __init__(self, postgres_url_factory) -> None:
        self.critical_db_url = postgres_url_factory("critical_private")
        self.account_db_url = postgres_url_factory("account_private")
        self.market_db_url = postgres_url_factory("market")
        self.critical_store = AccountStateStore(
            AccountStreamConfig(db_url=self.critical_db_url)
        )
        self.account_store = AccountStateStore(AccountStreamConfig(db_url=self.account_db_url))
        self.reader = KrakenRuntimeStateClient(
            market_db_url=self.market_db_url,
            account_db_url=self.account_db_url,
            critical_account_db_url=self.critical_db_url,
            symbol="PI_XBTUSD",
        )

    def write_order(
        self,
        *,
        order_id: str,
        client_order_id: str,
        side: str,
        order_type: str,
        quantity: Decimal | int | float | str,
        price: Decimal | int | float | str | None,
        status: str = "open",
        filled: Decimal | int | float | str = "0",
        reason: str | None = None,
        is_cancel: bool = False,
    ) -> None:
        now = datetime.now(timezone.utc)
        order: dict[str, Any] = {
            "instrument": "PI_XBTUSD",
            "order_id": order_id,
            "cli_ord_id": client_order_id,
            "side": side,
            "type": order_type,
            "status": status,
            "qty": str(quantity),
            "filled": str(filled),
            "last_update_time": now.isoformat(),
        }
        if price is not None:
            order["price"] = str(price)
            if "stop" in order_type.lower():
                order["stop_price"] = str(price)
        if reason is not None:
            order["reason"] = reason
        if is_cancel:
            order["is_cancel"] = True
        self.critical_store.ingest_message(
            {"feed": "open_orders", "order": order},
            stream_kind="private_ws_critical",
            is_critical=True,
            received_at=now,
        )

    def write_fill(
        self,
        *,
        order_id: str,
        client_order_id: str,
        side: str,
        quantity: Decimal | int | float | str,
        price: Decimal | int | float | str,
        fill_id: str,
        order_type: str = "market",
    ) -> None:
        now = datetime.now(timezone.utc)
        self.critical_store.ingest_message(
            {
                "feed": "fills",
                "fill": {
                    "instrument": "PI_XBTUSD",
                    "order_id": order_id,
                    "cli_ord_id": client_order_id,
                    "side": side,
                    "type": order_type,
                    "qty": str(quantity),
                    "price": str(price),
                    "fill_id": fill_id,
                    "time": now.isoformat(),
                },
            },
            stream_kind="private_ws_critical",
            is_critical=True,
            received_at=now,
        )


class _DbBackedOneLifecycleSource:
    def __init__(self, db: _PrivateDbHarness, executor: _RecordingLiveExecutor) -> None:
        self.db = db
        self.executor = executor

    async def pump(self, runtime) -> None:
        pair_name = "pair-a"
        await runtime.enqueue(
            EggMove(
                kind=EggMoveKind.HEAD_HOOKED,
                occurred_at=datetime.now(timezone.utc),
                symbol=runtime.symbol,
                pair_name=pair_name,
                event_id="test:head-hooked",
            )
        )
        head_command = await _wait_for_command(runtime, PlaceHeadCommand, pair_name)
        head_client_id = _require_client_id(head_command)
        head_order_id = await _wait_for_executor_order_id(self.executor, head_client_id)
        self.db.write_fill(
            order_id=head_order_id,
            client_order_id=head_client_id,
            side="buy",
            quantity="1",
            price="100",
            fill_id="FID-HEAD-1",
        )

        tail_command = await _wait_for_command(runtime, PlaceTailCommand, pair_name)
        tail_client_id = _require_client_id(tail_command)
        tail_order_id = await _wait_for_executor_order_id(self.executor, tail_client_id)
        tail_stop = tail_command.request.stopPx or Decimal("99")
        self.db.write_order(
            order_id=tail_order_id,
            client_order_id=tail_client_id,
            side="sell",
            order_type="stop",
            quantity="1",
            price=tail_stop,
        )
        await _wait_for_pair(
            runtime,
            pair_name,
            lambda pair: pair.tail_state == TailState.LIVING,
        )

        first_unblocked_at = datetime.now(timezone.utc)
        await runtime.enqueue(
            EggMove(
                kind=EggMoveKind.MARKET_TICK,
                occurred_at=first_unblocked_at,
                symbol=runtime.symbol,
                pair_name=pair_name,
                event_id="test:market-tick-first-unblock",
                reply={"reference_price": 107.0, "tick_size": 0.5},
            )
        )
        await runtime.enqueue(
            EggMove(
                kind=EggMoveKind.MARKET_TICK,
                occurred_at=first_unblocked_at + timedelta(seconds=50),
                symbol=runtime.symbol,
                pair_name=pair_name,
                event_id="test:market-tick-amend",
                reply={"reference_price": 107.5, "tick_size": 0.5},
            )
        )
        amend_command = await _wait_for_command(runtime, AmendTailCommand, pair_name)
        amended_stop = amend_command.request.newPrice
        assert amended_stop is not None
        self.db.write_order(
            order_id=tail_order_id,
            client_order_id=tail_client_id,
            side="sell",
            order_type="stop",
            quantity="1",
            price=amended_stop,
        )
        await _wait_for_pair(
            runtime,
            pair_name,
            lambda pair: pair.tail_trail is not None
            and pair.tail_trail.confirmed_stop_price == amended_stop,
        )

        self.db.write_fill(
            order_id=tail_order_id,
            client_order_id=tail_client_id,
            side="sell",
            quantity="1",
            price=amended_stop,
            fill_id="FID-TAIL-1",
        )
        self.db.write_order(
            order_id=tail_order_id,
            client_order_id=tail_client_id,
            side="sell",
            order_type="market",
            quantity="1",
            price=amended_stop,
            status="canceled",
            filled="1",
            reason="stop_order_triggered",
            is_cancel=True,
        )
        await _wait_for_pair(
            runtime,
            pair_name,
            lambda pair: pair.tail_state == TailState.CLOSED,
        )


class _HeadOpenOnlySource:
    def __init__(
        self,
        db: _PrivateDbHarness,
        executor: _RecordingLiveExecutor,
        *,
        pair_name: str = "pair-a",
    ) -> None:
        self.db = db
        self.executor = executor
        self.pair_name = pair_name

    async def pump(self, runtime) -> None:
        await runtime.enqueue(
            EggMove(
                kind=EggMoveKind.HEAD_HOOKED,
                occurred_at=datetime.now(timezone.utc),
                symbol=runtime.symbol,
                pair_name=self.pair_name,
                event_id=f"test:{self.pair_name}:head-hooked",
            )
        )
        head_command = await _wait_for_command(runtime, PlaceHeadCommand, self.pair_name)
        head_client_id = _require_client_id(head_command)
        head_order_id = await _wait_for_executor_order_id(self.executor, head_client_id)
        self.db.write_order(
            order_id=head_order_id,
            client_order_id=head_client_id,
            side="buy",
            order_type="limit",
            quantity="1",
            price="100",
        )
        while runtime.running:
            await asyncio.sleep(0.01)


class _PersistentHeadHookSource:
    async def pump(self, runtime) -> None:
        await runtime.enqueue(
            EggMove(
                kind=EggMoveKind.HEAD_HOOKED,
                occurred_at=datetime.now(timezone.utc),
                symbol=runtime.symbol,
                pair_name="pair-a",
                event_id="test:pair-a:head-hooked",
            )
        )
        while runtime.running:
            await asyncio.sleep(0.01)


class _HeadOpenThenCancelSource(_HeadOpenOnlySource):
    async def pump(self, runtime) -> None:
        await runtime.enqueue(
            EggMove(
                kind=EggMoveKind.HEAD_HOOKED,
                occurred_at=datetime.now(timezone.utc),
                symbol=runtime.symbol,
                pair_name=self.pair_name,
                event_id=f"test:{self.pair_name}:head-hooked",
            )
        )
        head_command = await _wait_for_command(runtime, PlaceHeadCommand, self.pair_name)
        head_client_id = _require_client_id(head_command)
        head_order_id = await _wait_for_executor_order_id(self.executor, head_client_id)
        self.db.write_order(
            order_id=head_order_id,
            client_order_id=head_client_id,
            side="buy",
            order_type="limit",
            quantity="1",
            price="100",
        )
        await _wait_for_command(runtime, CancelCommand, self.pair_name)
        self.db.write_order(
            order_id=head_order_id,
            client_order_id=head_client_id,
            side="buy",
            order_type="limit",
            quantity="1",
            price="100",
            status="canceled",
            reason="cancelled_by_user",
            is_cancel=True,
        )
        while runtime.running:
            await asyncio.sleep(0.01)


def _command_quantity(command: DragonSong) -> float | None:
    quantity = getattr(command.request, "orderQty", None)
    if quantity is None:
        return None
    return float(to_decimal(quantity))


def _require_client_id(command: DragonSong) -> str:
    client_id = getattr(command.request, "clOrdID", None)
    assert isinstance(client_id, str) and client_id
    return client_id


async def _wait_for_executor_order_id(
    executor: _RecordingLiveExecutor,
    client_order_id: str,
    *,
    timeout: float = 1.0,
) -> str:
    deadline = datetime.now(timezone.utc) + timedelta(seconds=timeout)
    while datetime.now(timezone.utc) < deadline:
        order_id = executor.order_ids_by_client.get(client_order_id)
        if order_id is not None:
            return order_id
        await asyncio.sleep(0.01)
    raise AssertionError(f"executor did not see client order id {client_order_id}")


async def _wait_for_command(
    runtime: StrategyRuntime,
    command_type: type,
    pair_name: str,
    *,
    seen: int = 0,
    timeout: float = 1.0,
):
    deadline = datetime.now(timezone.utc) + timedelta(seconds=timeout)
    while datetime.now(timezone.utc) < deadline:
        matches = [
            command
            for command in runtime.commands
            if isinstance(command, command_type) and command.pair_name == pair_name
        ]
        if len(matches) > seen:
            return matches[-1]
        await asyncio.sleep(0.01)
    raise AssertionError(f"did not see {command_type.__name__} for {pair_name}")


async def _wait_for_pair(
    runtime: StrategyRuntime,
    pair_name: str,
    predicate,
    *,
    timeout: float = 1.5,
) -> None:
    deadline = datetime.now(timezone.utc) + timedelta(seconds=timeout)
    while datetime.now(timezone.utc) < deadline:
        pair_state = runtime.state.pairs[pair_name]
        if predicate(pair_state):
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"pair {pair_name} did not reach expected state")


async def _run_runtime_for(runtime: StrategyRuntime, *, seconds: float = 0.05):
    task = asyncio.create_task(runtime.run())
    await asyncio.sleep(seconds)
    await runtime.stop()
    return await task


def _record_ack_and_apply_followups(
    runtime: StrategyRuntime,
    command: DragonSong,
    ack: OrderAck,
) -> _CommandSlot:
    slot = runtime._command_slot(command)
    identity = runtime._command_identity_from_command(command)
    runtime._on_command_dispatched(slot, command, identity)
    runtime._record_live_ack(command, ack)
    for event in runtime._followup_events(command, ack, slot=slot):
        runtime.chronos.process_event(event)
    runtime.state = runtime.chronos.state
    runtime._prune_order_leases()
    return slot


def sample_strategy() -> tuple[OrderPairSpec, ...]:
    return (
        OrderPairSpec(
            name="pair-a",
            window=TimeWindow(start_minutes=0.0, end_minutes=60.0),
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
        ),
    )


def test_runtime_reuses_four_character_marker_for_head_and_tail_ids() -> None:
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="marked", pairs=sample_strategy()),
        symbol="PI_XBTUSD",
        simulate=False,
        run_marker="A1B2",
    )
    head = runtime._prepare_command(
        PlaceHeadCommand(
            kind=RuntimeCommandKind.PLACE,
            symbol=Symbol("PI_XBTUSD"),
            pair_name="pair-a",
            request=PlaceOrderCommandRequest(
                pair_name="pair-a",
                side="buy",
                ordType="Limit",
                orderQty=1,
                price=100,
            ),
        )
    )
    tail = runtime._prepare_command(
        PlaceTailCommand(
            kind=RuntimeCommandKind.PLACE,
            symbol=Symbol("PI_XBTUSD"),
            pair_name="pair-a",
            request=PlaceOrderCommandRequest(
                pair_name="pair-a",
                side="sell",
                ordType="Stop",
                orderQty=1,
                stopPx=99,
            ),
        )
    )

    assert head.request.clOrdID is not None
    assert tail.request.clOrdID is not None
    assert "-A1B2-" in head.request.clOrdID
    assert "-A1B2-" in tail.request.clOrdID


def test_head_visibility_fallback_enriches_lease_without_state_transition(caplog) -> None:
    class Executor:
        def __init__(self) -> None:
            self.visibility_calls = 0

        async def execute(self, command: DragonSong) -> OrderAck:
            raise AssertionError("ordinary execution is not used by this test")

        async def verify_head_visibility(
            self,
            command: VerifyHeadVisibilityCommand,
        ) -> HeadVisibilityResult:
            self.visibility_calls += 1
            return HeadVisibilityResult(
                pair_name=command.pair_name,
                attempt_index=command.attempt_index,
                checked_at=datetime.now(timezone.utc),
                visible=True,
                client_order_id=command.request.clOrdID,
                exchange_order_id="OID-REST",
                status="open",
                price=Decimal("100"),
                quantity=Decimal("1"),
            )

    async def exercise() -> tuple[StrategyRuntime, Executor]:
        executor = Executor()
        runtime = StrategyRuntime(
            strategy=StrategySpec(name="visibility", pairs=sample_strategy()),
            symbol="PI_XBTUSD",
            executor=executor,
            simulate=False,
            head_visibility_fallback_seconds=0,
            run_marker="A1B2",
        )
        pair_state = runtime.state.pairs["pair-a"]
        runtime.state = replace(
            runtime.state,
            pairs={"pair-a": replace(pair_state, head_state=HeadState.HOOKED)},
        )
        runtime.chronos.state = runtime.state
        command = runtime._prepare_command(
            PlaceHeadCommand(
                kind=RuntimeCommandKind.PLACE,
                symbol=Symbol("PI_XBTUSD"),
                pair_name="pair-a",
                request=PlaceOrderCommandRequest(
                    pair_name="pair-a",
                    side="buy",
                    ordType="Limit",
                    orderQty=1,
                    price=100,
                ),
            )
        )
        slot = runtime._command_slot(command)
        runtime._on_command_dispatched(
            slot,
            command,
            runtime._command_identity_from_command(command),
        )
        ack = OrderAck(
            order_id="OID-ACK",
            status="New",
            client_order_id=command.request.clOrdID,
        )
        runtime._record_live_ack(command, ack)
        runtime._schedule_head_visibility_fallback(command, ack, slot)
        await asyncio.gather(*tuple(runtime._head_visibility_tasks.values()))
        return runtime, executor

    with caplog.at_level("INFO", logger="kola"):
        runtime, executor = asyncio.run(exercise())

    assert executor.visibility_calls == 1
    assert runtime.state.pairs["pair-a"].head_state == HeadState.HOOKED
    assert runtime._order_leases[_CommandSlot("pair-a", 1, "head")].exchange_order_id == "OID-REST"
    assert "HEAD_REST_VISIBLE (pair-a#1): visible" in caplog.text


def test_private_head_confirmation_suppresses_rest_visibility_fallback(caplog) -> None:
    class Executor:
        def __init__(self) -> None:
            self.visibility_calls = 0

        async def execute(self, command: DragonSong) -> OrderAck:
            raise AssertionError("ordinary execution is not used by this test")

        async def verify_head_visibility(
            self,
            command: VerifyHeadVisibilityCommand,
        ) -> HeadVisibilityResult:
            self.visibility_calls += 1
            raise AssertionError("private confirmation should suppress REST lookup")

    async def exercise() -> Executor:
        executor = Executor()
        runtime = StrategyRuntime(
            strategy=StrategySpec(name="visibility", pairs=sample_strategy()),
            symbol="PI_XBTUSD",
            executor=executor,
            simulate=False,
            head_visibility_fallback_seconds=0.02,
            run_marker="A1B2",
        )
        pair_state = runtime.state.pairs["pair-a"]
        runtime.state = replace(
            runtime.state,
            pairs={"pair-a": replace(pair_state, head_state=HeadState.HOOKED)},
        )
        runtime.chronos.state = runtime.state
        command = runtime._prepare_command(
            PlaceHeadCommand(
                kind=RuntimeCommandKind.PLACE,
                symbol=Symbol("PI_XBTUSD"),
                pair_name="pair-a",
                request=PlaceOrderCommandRequest(
                    pair_name="pair-a",
                    side="buy",
                    ordType="Limit",
                    orderQty=1,
                    price=100,
                ),
            )
        )
        slot = runtime._command_slot(command)
        runtime._on_command_dispatched(
            slot,
            command,
            runtime._command_identity_from_command(command),
        )
        ack = OrderAck(
            order_id="OID-ACK",
            status="New",
            client_order_id=command.request.clOrdID,
        )
        runtime._record_live_ack(command, ack)
        runtime._schedule_head_visibility_fallback(command, ack, slot)
        runtime._record_private_event_for_leases(
            EggMove(
                kind=EggMoveKind.NOT_PLAYED_NOR_CANCELED,
                occurred_at=datetime.now(timezone.utc),
                symbol="PI_XBTUSD",
                pair_name="pair-a",
                role=OrderRole.HEAD,
                reply={
                    "clOrdID": command.request.clOrdID,
                    "orderID": "OID-ACK",
                    "ordStatus": "open",
                    "side": "buy",
                    "orderQty": 1,
                    "price": 100,
                },
                is_private=True,
            )
        )
        await asyncio.gather(*tuple(runtime._head_visibility_tasks.values()))
        return executor

    with caplog.at_level("INFO", logger="kola"):
        executor = asyncio.run(exercise())

    assert executor.visibility_calls == 0
    assert "HEAD_CONFIRMED (pair-a#1):" in caplog.text
    assert "HEAD_REST_VISIBLE (pair-a#1):" not in caplog.text


def test_head_visibility_fallback_error_is_inert(caplog) -> None:
    class Executor:
        async def execute(self, command: DragonSong) -> OrderAck:
            raise AssertionError("ordinary execution is not used by this test")

        async def verify_head_visibility(
            self,
            command: VerifyHeadVisibilityCommand,
        ) -> HeadVisibilityResult:
            raise TimeoutError("openorders delayed")

    async def exercise() -> StrategyRuntime:
        runtime = StrategyRuntime(
            strategy=StrategySpec(name="visibility", pairs=sample_strategy()),
            symbol="PI_XBTUSD",
            executor=Executor(),
            simulate=False,
            head_visibility_fallback_seconds=0,
            run_marker="A1B2",
        )
        pair_state = runtime.state.pairs["pair-a"]
        runtime.state = replace(
            runtime.state,
            pairs={"pair-a": replace(pair_state, head_state=HeadState.HOOKED)},
        )
        runtime.chronos.state = runtime.state
        command = runtime._prepare_command(
            PlaceHeadCommand(
                kind=RuntimeCommandKind.PLACE,
                symbol=Symbol("PI_XBTUSD"),
                pair_name="pair-a",
                request=PlaceOrderCommandRequest(
                    pair_name="pair-a",
                    side="buy",
                    ordType="Limit",
                    orderQty=1,
                    price=100,
                ),
            )
        )
        slot = runtime._command_slot(command)
        runtime._on_command_dispatched(
            slot,
            command,
            runtime._command_identity_from_command(command),
        )
        ack = OrderAck(
            order_id="OID-ACK",
            status="New",
            client_order_id=command.request.clOrdID,
        )
        runtime._record_live_ack(command, ack)
        runtime._schedule_head_visibility_fallback(command, ack, slot)
        await asyncio.gather(*tuple(runtime._head_visibility_tasks.values()))
        return runtime

    with caplog.at_level("WARNING", logger="kola"):
        runtime = asyncio.run(exercise())

    assert runtime.state.pairs["pair-a"].head_state == HeadState.HOOKED
    assert runtime._order_leases[_CommandSlot("pair-a", 1, "head")].status == "ACKED"
    assert "HEAD_REST_VISIBLE (pair-a#1): error" in caplog.text
    assert "openorders delayed" in caplog.text


def test_plan_strategy_once_uses_the_chronos_path() -> None:
    from kolabi.bot.domain import StrategySpec

    result = plan_strategy_once(
        strategy=StrategySpec(name="demo", pairs=sample_strategy()),
        symbol="PI_XBTUSD",
    )

    assert result.commands
    assert result.commands[0].pair_name == "pair-a"
    assert result.commands[0].role is not None and result.commands[0].role.value == "head"


def test_plan_strategy_once_respects_chain_dependencies() -> None:
    parent = sample_strategy()[0]
    child = replace(parent, name="pair-b", hook_name="pair-a-tail-closed")

    result = plan_strategy_once(
        strategy=StrategySpec(name="demo-chain", pairs=(parent, child)),
        symbol="PI_XBTUSD",
    )

    assert [command.pair_name for command in result.commands] == ["pair-a"]


def test_strategy_runtime_simulation_advances_to_tail_state() -> None:
    from kolabi.bot.domain import StrategySpec

    runtime = StrategyRuntime(
        strategy=StrategySpec(name="demo", pairs=sample_strategy()),
        symbol="PI_XBTUSD",
        executor=SimulatedExecutor(),
        simulate=True,
    )
    result = asyncio.run(_run_runtime_for(runtime))

    assert result.commands


def test_runtime_materializes_usd_quantity_before_head_dispatch() -> None:
    class Market:
        best_bid = 99999.0
        best_ask = 100001.0
        mid_price = 100000.0
        last_price = None
        mark_price = 100000.0
        index_price = None
        tick_size = 0.5
        spread = 2.0
        recorded_at = "usd-valid"

    class Reader:
        def fetch_market_state(self, symbol=None, exchange=None, market_type=None):
            del symbol, exchange, market_type
            return Market()

    pair = replace(
        sample_strategy()[0],
        symbol="PF_XBTUSD",
        exchange="kraken",
        head_quantity=15.0,
        head_quantity_type="qU",
        amount_type="qUtApA",
    )
    executor = _RecordingLiveExecutor()
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="usd-valid", pairs=(pair,)),
        symbol="PF_XBTUSD",
        executor=executor,
        public_source=_PersistentHeadHookSource(),
        public_state_reader=Reader(),
        instrument_rules_by_route={
            ExchangeRoute("kraken", "futures", "PF_XBTUSD"): {
                "contractSize": 1,
                "quantityIncrement": "0.0001",
                "minimumQuantity": "0.0001",
            }
        },
        simulate=False,
    )

    result = asyncio.run(_run_runtime_for(runtime, seconds=0.1))

    assert result.commands
    assert executor.commands
    assert executor.commands[0].request.orderQty == Decimal("0.0001")


def test_runtime_materializes_percent_quantity_before_head_dispatch(caplog) -> None:
    class Market:
        best_bid = 99999.0
        best_ask = 100001.0
        mid_price = 100000.0
        last_price = None
        mark_price = 100000.0
        index_price = None
        tick_size = 0.5
        spread = 2.0
        recorded_at = "pct-valid"

    class Balance:
        ready = True
        reason = None
        available = 1000.0

    class Reader:
        def fetch_market_state(self, symbol=None, exchange=None, market_type=None):
            del symbol, exchange, market_type
            return Market()

        def fetch_account_balance(self, asset="USD", exchange=None, market_type=None):
            del asset, exchange, market_type
            return Balance()

    pair = replace(
        sample_strategy()[0],
        symbol="PF_XBTUSD",
        exchange="kraken",
        head_quantity=Decimal("2.5"),
        head_quantity_type="q%",
        amount_type="q%tApA",
    )
    executor = _RecordingLiveExecutor()
    reader = Reader()
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="pct-valid", pairs=(pair,)),
        symbol="PF_XBTUSD",
        executor=executor,
        public_source=_PersistentHeadHookSource(),
        public_state_reader=reader,
        account_state_reader=reader,
        instrument_rules_by_route={
            ExchangeRoute("kraken", "futures", "PF_XBTUSD"): {
                "contractSize": 1,
                "quantityIncrement": "0.0001",
                "minimumQuantity": "0.0001",
            }
        },
        simulate=False,
    )

    with caplog.at_level("INFO", logger="kola"):
        result = asyncio.run(_run_runtime_for(runtime, seconds=0.1))

    assert result.commands
    assert executor.commands
    assert executor.commands[0].request.orderQty == Decimal("0.0002")
    assert "QTY_PCT_RESOLVED (pair-a#1)" in caplog.text


def test_runtime_fails_only_pair_when_usd_quantity_later_resolves_to_zero(caplog) -> None:
    class Market:
        best_bid = 99999.0
        best_ask = 100001.0
        mid_price = 100000.0
        last_price = None
        mark_price = 100000.0
        index_price = None
        tick_size = 0.5
        spread = 2.0
        recorded_at = "usd-too-small"

    class Reader:
        def fetch_market_state(self, symbol=None, exchange=None, market_type=None):
            del symbol, exchange, market_type
            return Market()

    pair = replace(
        sample_strategy()[0],
        symbol="PF_XBTUSD",
        exchange="kraken",
        head_quantity=1.0,
        head_quantity_type="qU",
        amount_type="qUtApA",
    )
    executor = _RecordingLiveExecutor()
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="usd-too-small", pairs=(pair,)),
        symbol="PF_XBTUSD",
        executor=executor,
        public_source=_PersistentHeadHookSource(),
        public_state_reader=Reader(),
        instrument_rules_by_route={
            ExchangeRoute("kraken", "futures", "PF_XBTUSD"): {
                "contractSize": 1,
                "quantityIncrement": "0.0001",
                "minimumQuantity": "0.0001",
            }
        },
        simulate=False,
    )

    with caplog.at_level("WARNING", logger="kola"):
        result = asyncio.run(_run_runtime_for(runtime, seconds=0.1))

    assert result.state.pairs["pair-a"].head_state == HeadState.FAILED
    assert not executor.commands
    assert "COMMAND_FAILED (pair-a#1): QTY_USD_TOO_SMALL" in caplog.text


def test_db_backed_runtime_completes_one_lifecycle_with_amend_and_tail_fill(
    postgres_url_factory,
    caplog,
) -> None:
    db = _PrivateDbHarness(postgres_url_factory)
    executor = _RecordingLiveExecutor()
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="db-lifecycle", pairs=sample_strategy()),
        symbol="PI_XBTUSD",
        executor=executor,
        public_source=_DbBackedOneLifecycleSource(db, executor),
        private_source=KrakenPrivateOrderPollingSource(
            db.reader,
            poll_seconds=0.01,
            head_fill_reference_grace_seconds=0.5,
        ),
        simulate=False,
        tail_visibility_timeout_seconds=0.5,
    )

    with caplog.at_level("INFO", logger="kola"):
        result = asyncio.run(asyncio.wait_for(runtime.run(), timeout=4.0))

    pair_state = result.state.pairs["pair-a"]
    assert pair_state.head_state == HeadState.CLOSED
    assert pair_state.tail_state == TailState.CLOSED
    assert [type(command) for command in result.commands] == [
        PlaceHeadCommand,
        PlaceTailCommand,
        AmendTailCommand,
    ]
    assert "UPDATE (pair-a#1): closed--hooked" in caplog.text
    assert "buy 1.00 100.00" in caplog.text
    assert "UPDATE (pair-a#1): closed--living" in caplog.text
    assert "AMEND_SENT (pair-a#1):" in caplog.text
    assert "UPDATE (pair-a#1): closed--closed" in caplog.text
    assert "HFP=" not in caplog.text
    assert "TFP=" not in caplog.text
    assert not any(
        record.getMessage().startswith("TAIL_PENDING (") for record in caplog.records
    )


def test_head_timeout_cancels_unfilled_platform_ack(
    postgres_url_factory,
    caplog,
) -> None:
    db = _PrivateDbHarness(postgres_url_factory)
    executor = _RecordingLiveExecutor()
    pair = replace(sample_strategy()[0], timeout=0.001)
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="head-timeout", pairs=(pair,)),
        symbol="PI_XBTUSD",
        executor=executor,
        public_source=_HeadOpenOnlySource(db, executor),
        private_source=KrakenPrivateOrderPollingSource(
            db.reader,
            poll_seconds=0.01,
            head_fill_reference_grace_seconds=0.5,
        ),
        simulate=False,
        tail_visibility_timeout_seconds=0.1,
    )

    with caplog.at_level("INFO", logger="kola"):
        result = asyncio.run(_run_runtime_for(runtime, seconds=0.35))

    cancel_commands = [
        command for command in result.commands if isinstance(command, CancelCommand)
    ]
    head_commands = [
        command for command in result.commands if isinstance(command, PlaceHeadCommand)
    ]
    assert head_commands
    assert cancel_commands
    head_client_id = _require_client_id(head_commands[0])
    assert cancel_commands[0].request.clOrdID == executor.order_ids_by_client[head_client_id]
    assert cancel_commands[0].reason == "head_timeout"
    assert "HEAD_ACK (pair-a#1):" in caplog.text
    assert "HEAD_TIMEOUT (pair-a#1):" in caplog.text
    assert "HEAD_CANCEL_SENT (pair-a#1):" in caplog.text


def test_head_timeout_cancel_ack_does_not_terminate_unfilled_head_without_db(
    postgres_url_factory,
    caplog,
) -> None:
    db = _PrivateDbHarness(postgres_url_factory)
    executor = _CancelAckLiveExecutor()
    pair = replace(sample_strategy()[0], timeout=0.001)
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="head-timeout", pairs=(pair,)),
        symbol="PI_XBTUSD",
        executor=executor,
        public_source=_HeadOpenOnlySource(db, executor),
        private_source=KrakenPrivateOrderPollingSource(
            db.reader,
            poll_seconds=0.01,
            head_fill_reference_grace_seconds=0.5,
        ),
        simulate=False,
        tail_visibility_timeout_seconds=0.1,
    )

    with caplog.at_level("INFO", logger="kola"):
        result = asyncio.run(_run_runtime_for(runtime, seconds=0.45))

    cancel_commands = [
        command for command in result.commands if isinstance(command, CancelCommand)
    ]
    assert len(cancel_commands) == 1
    pair_state = result.state.pairs["pair-a"]
    assert pair_state.head_state == HeadState.NEW
    assert pair_state.tail_state is None
    assert pair_state.played_quantity == Decimal("0.0")
    assert "HEAD_CANCELLED (pair-a#1):" not in caplog.text
    assert "CANCEL_PENDING (pair-a#1):" in caplog.text


def test_head_timeout_private_db_cancel_terminates_unfilled_head(
    postgres_url_factory,
) -> None:
    db = _PrivateDbHarness(postgres_url_factory)
    executor = _CancelAckLiveExecutor()
    pair = replace(sample_strategy()[0], timeout=0.001)
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="head-timeout", pairs=(pair,)),
        symbol="PI_XBTUSD",
        executor=executor,
        public_source=_HeadOpenThenCancelSource(db, executor),
        private_source=KrakenPrivateOrderPollingSource(
            db.reader,
            poll_seconds=0.01,
            head_fill_reference_grace_seconds=0.5,
        ),
        simulate=False,
        tail_visibility_timeout_seconds=0.1,
    )

    result = asyncio.run(_run_runtime_for(runtime, seconds=0.45))

    cancel_commands = [
        command for command in result.commands if isinstance(command, CancelCommand)
    ]
    assert len(cancel_commands) == 1
    pair_state = result.state.pairs["pair-a"]
    assert pair_state.head_state == HeadState.FAILED
    assert pair_state.tail_state == TailState.LATENT
    assert pair_state.played_quantity == Decimal("0.0")


def test_head_timeout_private_db_cancel_drives_repeat_adjustment(
    postgres_url_factory,
) -> None:
    adjustment_name = "test_live_head_timeout_tightens_repeat"

    def tighten_after_head_timeout(
        pair: OrderPairSpec,
        context: RepeatAdjustmentContext,
    ) -> OrderPairSpec:
        if not head_timed_out(context):
            return pair
        return replace(pair, timeout=1.5, head_order_price_spec=45.0)

    register_repeat_adjustment(adjustment_name, tighten_after_head_timeout)
    db = _PrivateDbHarness(postgres_url_factory)
    executor = _CancelAckLiveExecutor()
    pair = replace(
        sample_strategy()[0],
        try_num=2,
        dr_pause=0.0,
        timeout=0.001,
        head_order_price_spec=50.0,
        head_order_price_spec_type="hB",
        repeat_adjustment=adjustment_name,
    )
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="head-timeout-repeat", pairs=(pair,)),
        symbol="PI_XBTUSD",
        executor=executor,
        public_source=_HeadOpenThenCancelSource(db, executor),
        private_source=KrakenPrivateOrderPollingSource(
            db.reader,
            poll_seconds=0.01,
            head_fill_reference_grace_seconds=0.5,
        ),
        simulate=False,
        tail_visibility_timeout_seconds=0.1,
    )

    result = asyncio.run(_run_runtime_for(runtime, seconds=0.45))

    cancel_commands = [
        command for command in result.commands if isinstance(command, CancelCommand)
    ]
    assert len(cancel_commands) == 1
    pair_state = result.state.pairs["pair-a"]
    assert pair_state.attempt_index == 2
    assert pair_state.head_state == HeadState.LATENT
    assert pair_state.pair.timeout == 1.5
    assert pair_state.pair.head_order_price_spec == 45.0


def test_unacked_head_visibility_timeout_warns_without_cancel(caplog) -> None:
    pair = sample_strategy()[0]
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="visibility", pairs=(pair,)),
        symbol="PI_XBTUSD",
        simulate=False,
        tail_visibility_timeout_seconds=0.1,
    )
    command = PlaceHeadCommand(
        kind=RuntimeCommandKind.PLACE,
        symbol=Symbol("PI_XBTUSD"),
        pair_name="pair-a",
        request=PlaceOrderCommandRequest(
            pair_name="pair-a",
            side="buy",
            ordType="Limit",
            orderQty=Decimal("1"),
            price=Decimal("100"),
            clOrdID="CID-H",
        ),
    )
    slot = _CommandSlot("pair-a", 1, "head")
    identity = runtime._command_identity_from_command(command)

    with caplog.at_level("WARNING", logger="kola"):
        runtime._on_command_dispatched(slot, command, identity)
        lease = runtime._order_leases[slot]
        runtime._check_order_lease_deadlines(
            lease.created_at + timedelta(seconds=0.2)
        )

    assert not any(isinstance(command, CancelCommand) for command in runtime.commands)
    assert "HEAD_VISIBILITY_PENDING (pair-a#1):" in caplog.text
    assert "head_visibility_timeout" not in caplog.text


def test_rejected_live_head_ack_fails_without_cancel(caplog) -> None:
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="rejected-head", pairs=sample_strategy()),
        symbol="PI_XBTUSD",
        simulate=False,
    )
    command = PlaceHeadCommand(
        kind=RuntimeCommandKind.PLACE,
        symbol=Symbol("PI_XBTUSD"),
        pair_name="pair-a",
        request=PlaceOrderCommandRequest(
            pair_name="pair-a",
            side="buy",
            ordType="Limit",
            orderQty=Decimal("1"),
            price=Decimal("100"),
            clOrdID="CID-H",
        ),
    )

    with caplog.at_level("WARNING", logger="kola"):
        slot = _record_ack_and_apply_followups(
            runtime,
            command,
            OrderAck(
                order_id="OID-H",
                status="Rejected",
                orig_qty=1.0,
                executed_qty=0.0,
                side="buy",
                client_order_id="CID-H",
                reason="insufficientAvailableFunds",
            ),
        )

    pair_state = runtime.state.pairs["pair-a"]
    assert pair_state.head_state == HeadState.FAILED
    assert pair_state.tail_state == TailState.LATENT
    assert runtime.active_order_leases() == ()
    assert slot not in runtime._head_fill_deadlines
    assert not any(isinstance(command, CancelCommand) for command in runtime.commands)
    assert "COMMAND_FAILED (pair-a#1): rejected insufficientAvailableFunds CID-H OID-H" in caplog.text


def test_rejected_live_tail_ack_fails_without_visibility_tracking(caplog) -> None:
    pair = sample_strategy()[0]
    trail = initial_tail_trail(pair, Decimal("100"), datetime.now(timezone.utc))
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="rejected-tail", pairs=(pair,)),
        symbol="PI_XBTUSD",
        simulate=False,
    )
    runtime.state = replace(
        runtime.state,
        pairs={
            "pair-a": replace(
                runtime.state.pairs["pair-a"],
                head_state=HeadState.CLOSED,
                tail_state=TailState.HOOKED,
                tail_trail=trail,
                played_quantity=Decimal("1"),
            )
        },
    )
    runtime.chronos.state = runtime.state
    command = PlaceTailCommand(
        kind=RuntimeCommandKind.PLACE,
        symbol=Symbol("PI_XBTUSD"),
        pair_name="pair-a",
        request=PlaceOrderCommandRequest(
            pair_name="pair-a",
            side="sell",
            ordType="Stop",
            orderQty=Decimal("1"),
            stopPx=Decimal("99"),
            clOrdID="CID-T",
        ),
    )

    with caplog.at_level("WARNING", logger="kola"):
        slot = _record_ack_and_apply_followups(
            runtime,
            command,
            OrderAck(
                order_id="OID-T",
                status="Rejected",
                orig_qty=1.0,
                executed_qty=0.0,
                side="sell",
                client_order_id="CID-T",
                reason="insufficientAvailableFunds",
            ),
        )

    pair_state = runtime.state.pairs["pair-a"]
    assert pair_state.head_state == HeadState.CLOSED
    assert pair_state.tail_state == TailState.FAILED
    assert runtime.active_order_leases() == ()
    assert slot not in runtime._pending_tail_visibility
    assert not any(isinstance(command, CancelCommand) for command in runtime.commands)
    assert "COMMAND_FAILED (pair-a#1): rejected insufficientAvailableFunds CID-T OID-T" in caplog.text


def test_unacked_head_visibility_timeout_arms_cancel_path(caplog) -> None:
    pair = replace(sample_strategy()[0], timeout=0.001)
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="visibility", pairs=(pair,)),
        symbol="PI_XBTUSD",
        simulate=False,
        tail_visibility_timeout_seconds=0.1,
    )
    runtime.state = replace(
        runtime.state,
        pairs={
            "pair-a": replace(
                runtime.state.pairs["pair-a"],
                head_state=HeadState.HOOKED,
            )
        },
    )
    command = PlaceHeadCommand(
        kind=RuntimeCommandKind.PLACE,
        symbol=Symbol("PI_XBTUSD"),
        pair_name="pair-a",
        request=PlaceOrderCommandRequest(
            pair_name="pair-a",
            side="buy",
            ordType="Limit",
            orderQty=Decimal("1"),
            price=Decimal("100"),
            clOrdID="CID-H",
        ),
    )
    slot = _CommandSlot("pair-a", 1, "head")
    identity = runtime._command_identity_from_command(command)

    with caplog.at_level("WARNING", logger="kola"):
        runtime._on_command_dispatched(slot, command, identity)
        lease = runtime._order_leases[slot]
        runtime._check_order_lease_deadlines(
            lease.created_at + timedelta(seconds=0.2)
        )
        runtime._check_head_fill_deadlines(
            lease.created_at + timedelta(seconds=0.21)
        )

    cancel_commands = [
        command for command in runtime.commands if isinstance(command, CancelCommand)
    ]
    assert len(cancel_commands) == 1
    assert cancel_commands[0].request.clOrdID == "CID-H"
    assert cancel_commands[0].reason == "head_timeout"
    assert runtime._head_fill_deadlines[slot].source == "visibility"
    assert "HEAD_VISIBILITY_TIMEOUT_ARMED (pair-a#1):" in caplog.text
    assert "HEAD_VISIBILITY_TIMEOUT (pair-a#1):" in caplog.text


def test_unacked_head_cancel_notfound_ack_terminates_hooked_head(caplog) -> None:
    pair = replace(sample_strategy()[0], timeout=0.001)
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="visibility", pairs=(pair,)),
        symbol="PI_XBTUSD",
        simulate=False,
        tail_visibility_timeout_seconds=0.1,
    )
    runtime.state = replace(
        runtime.state,
        pairs={
            "pair-a": replace(
                runtime.state.pairs["pair-a"],
                head_state=HeadState.HOOKED,
            )
        },
    )
    runtime.chronos.state = runtime.state
    command = PlaceHeadCommand(
        kind=RuntimeCommandKind.PLACE,
        symbol=Symbol("PI_XBTUSD"),
        pair_name="pair-a",
        request=PlaceOrderCommandRequest(
            pair_name="pair-a",
            side="buy",
            ordType="Limit",
            orderQty=Decimal("1"),
            price=Decimal("100"),
            clOrdID="CID-H",
        ),
    )
    slot = _CommandSlot("pair-a", 1, "head")
    identity = runtime._command_identity_from_command(command)
    runtime._on_command_dispatched(slot, command, identity)
    cancel = CancelCommand(
        kind=RuntimeCommandKind.CANCEL,
        symbol=Symbol("PI_XBTUSD"),
        pair_name="pair-a",
        request=CancelOrderCommandRequest(pair_name="pair-a", clOrdID="CID-H"),
        reason="head_timeout",
    )

    with caplog.at_level("INFO", logger="kola"):
        runtime._on_command_dispatched(
            _CommandSlot("pair-a", 1, "cancel"),
            cancel,
            None,
        )
        runtime._record_live_ack(
            cancel,
            OrderAck(
                order_id="CID-H",
                status="NotFound",
                executed_qty=0.0,
                reason="client_order_id_not_visible",
            ),
        )
        followups = runtime._followup_events(
            cancel,
            OrderAck(
                order_id="CID-H",
                status="NotFound",
                executed_qty=0.0,
                reason="client_order_id_not_visible",
            ),
            slot=_CommandSlot("pair-a", 1, "cancel"),
        )

    assert len(followups) == 1
    assert not runtime._should_ignore_stale_runtime_cancel(followups[0])
    runtime.chronos.process_event(followups[0])
    runtime.state = runtime.chronos.state
    pair_state = runtime.state.pairs["pair-a"]
    assert pair_state.head_state == HeadState.FAILED
    assert pair_state.tail_state == TailState.LATENT
    assert "HEAD_CANCEL_NOT_FOUND (pair-a#1):" in caplog.text


def test_unacked_head_cancel_retries_are_capped(caplog) -> None:
    pair = replace(sample_strategy()[0], timeout=0.001)
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="visibility", pairs=(pair,)),
        symbol="PI_XBTUSD",
        simulate=False,
        tail_visibility_timeout_seconds=0.1,
    )
    runtime.state = replace(
        runtime.state,
        pairs={
            "pair-a": replace(
                runtime.state.pairs["pair-a"],
                head_state=HeadState.HOOKED,
            )
        },
    )
    runtime.chronos.state = runtime.state
    command = PlaceHeadCommand(
        kind=RuntimeCommandKind.PLACE,
        symbol=Symbol("PI_XBTUSD"),
        pair_name="pair-a",
        request=PlaceOrderCommandRequest(
            pair_name="pair-a",
            side="buy",
            ordType="Limit",
            orderQty=Decimal("1"),
            price=Decimal("100"),
            clOrdID="CID-H",
        ),
    )
    slot = _CommandSlot("pair-a", 1, "head")
    identity = runtime._command_identity_from_command(command)
    runtime._on_command_dispatched(slot, command, identity)
    lease = runtime._order_leases[slot]
    runtime._check_order_lease_deadlines(lease.created_at + timedelta(seconds=0.2))
    runtime._check_head_fill_deadlines(lease.created_at + timedelta(seconds=0.21))
    cancel = cast(CancelCommand, runtime.commands[-1])
    runtime._on_command_dispatched(
        _CommandSlot("pair-a", 1, "cancel"),
        cancel,
        None,
    )

    now = lease.created_at + timedelta(seconds=10)
    with caplog.at_level("WARNING", logger="kola"):
        runtime._check_order_lease_deadlines(now)
        runtime._check_order_lease_deadlines(now + timedelta(seconds=10))
        runtime._check_order_lease_deadlines(now + timedelta(seconds=20))

    event = runtime.event_queue.get_nowait()
    assert not runtime._should_ignore_stale_runtime_cancel(event)
    runtime.chronos.process_event(event)
    runtime.state = runtime.chronos.state
    pair_state = runtime.state.pairs["pair-a"]
    assert pair_state.head_state == HeadState.FAILED
    assert pair_state.tail_state == TailState.LATENT
    assert len([item for item in runtime.commands if isinstance(item, CancelCommand)]) == 3
    assert "HEAD_CANCEL_GAVE_UP (pair-a#1): CID-H retry=3 head_unconfirmed_timeout" in caplog.text


def test_rest_acked_head_visibility_timeout_warns_without_cancel(caplog) -> None:
    pair = sample_strategy()[0]
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="visibility", pairs=(pair,)),
        symbol="PI_XBTUSD",
        simulate=False,
        tail_visibility_timeout_seconds=0.1,
    )
    command = PlaceHeadCommand(
        kind=RuntimeCommandKind.PLACE,
        symbol=Symbol("PI_XBTUSD"),
        pair_name="pair-a",
        request=PlaceOrderCommandRequest(
            pair_name="pair-a",
            side="buy",
            ordType="Limit",
            orderQty=Decimal("1"),
            price=Decimal("100"),
            clOrdID="CID-H",
        ),
    )
    slot = _CommandSlot("pair-a", 1, "head")
    identity = runtime._command_identity_from_command(command)

    with caplog.at_level("WARNING", logger="kola"):
        runtime._on_command_dispatched(slot, command, identity)
        runtime._enrich_order_lease_from_ack(
            command,
            OrderAck(order_id="OID-H", status="New"),
        )
        lease = runtime._order_leases[slot]
        runtime._check_order_lease_deadlines(
            lease.created_at + timedelta(seconds=0.2)
        )

    assert not any(isinstance(command, CancelCommand) for command in runtime.commands)
    assert runtime._head_fill_deadlines[slot].exchange_order_id == "OID-H"
    assert "HEAD_VISIBILITY_PENDING (pair-a#1):" in caplog.text
    assert "head_visibility_timeout" not in caplog.text


def test_rest_acked_head_still_times_out_without_private_visibility() -> None:
    pair = replace(sample_strategy()[0], timeout=0.001)
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="visibility", pairs=(pair,)),
        symbol="PI_XBTUSD",
        simulate=False,
        tail_visibility_timeout_seconds=0.1,
    )
    runtime.state = replace(
        runtime.state,
        pairs={
            "pair-a": replace(
                runtime.state.pairs["pair-a"],
                head_state=HeadState.HOOKED,
            )
        },
    )
    command = PlaceHeadCommand(
        kind=RuntimeCommandKind.PLACE,
        symbol=Symbol("PI_XBTUSD"),
        pair_name="pair-a",
        request=PlaceOrderCommandRequest(
            pair_name="pair-a",
            side="buy",
            ordType="Limit",
            orderQty=Decimal("1"),
            price=Decimal("100"),
            clOrdID="CID-H",
        ),
    )
    slot = _CommandSlot("pair-a", 1, "head")
    identity = runtime._command_identity_from_command(command)

    runtime._on_command_dispatched(slot, command, identity)
    runtime._enrich_order_lease_from_ack(
        command,
        OrderAck(order_id="OID-H", status="New"),
    )
    deadline = runtime._head_fill_deadlines[slot]
    runtime._check_head_fill_deadlines(deadline.deadline_at + timedelta(seconds=0.01))

    cancel_commands = [
        command for command in runtime.commands if isinstance(command, CancelCommand)
    ]
    assert len(cancel_commands) == 1
    assert cancel_commands[0].request.clOrdID == "OID-H"
    assert cancel_commands[0].reason == "head_timeout"


def test_head_timeout_notfound_without_cumqty_does_not_terminate_head(
    postgres_url_factory,
    caplog,
) -> None:
    class SparseNotFoundAckExecutor(_CancelAckLiveExecutor):
        async def execute(self, command: DragonSong) -> OrderAck:
            ack = await super().execute(command)
            if isinstance(command, CancelCommand):
                return OrderAck(
                    order_id=ack.order_id,
                    status="NotFound",
                    orig_qty=ack.orig_qty,
                    executed_qty=None,
                    side=ack.side,
                )
            return ack

    db = _PrivateDbHarness(postgres_url_factory)
    executor = SparseNotFoundAckExecutor()
    pair = replace(sample_strategy()[0], timeout=0.001)
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="head-timeout", pairs=(pair,)),
        symbol="PI_XBTUSD",
        executor=executor,
        public_source=_HeadOpenOnlySource(db, executor),
        private_source=KrakenPrivateOrderPollingSource(
            db.reader,
            poll_seconds=0.01,
            head_fill_reference_grace_seconds=0.5,
        ),
        simulate=False,
        tail_visibility_timeout_seconds=0.1,
    )

    with caplog.at_level("INFO", logger="kola"):
        result = asyncio.run(_run_runtime_for(runtime, seconds=0.45))

    cancel_commands = [
        command for command in result.commands if isinstance(command, CancelCommand)
    ]
    assert len(cancel_commands) == 1
    pair_state = result.state.pairs["pair-a"]
    assert pair_state.head_state == HeadState.NEW
    assert "HEAD_CANCELLED (pair-a#1):" not in caplog.text


def test_head_timeout_zero_fill_notfound_terminates_unfilled_head() -> None:
    pair = replace(sample_strategy()[0], timeout=0.001)
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="head-timeout", pairs=(pair,)),
        symbol="PI_XBTUSD",
        simulate=False,
    )
    runtime.state = replace(
        runtime.state,
        pairs={
            "pair-a": PairCycleState(
                pair=pair,
                head_state=HeadState.NEW,
                head_identity=OrderIdentity(
                    pair_name="pair-a",
                    role="head",
                    client_order_id="CID-H",
                    exchange_order_id="OID-H",
                ),
                attempt_index=1,
            )
        },
    )
    runtime.chronos.state = runtime.state
    command = CancelCommand(
        kind=RuntimeCommandKind.CANCEL,
        symbol=Symbol("PI_XBTUSD"),
        pair_name="pair-a",
        request=CancelOrderCommandRequest(
            pair_name="pair-a",
            clOrdID="OID-H",
        ),
        reason="head_timeout",
    )

    followups = runtime._followup_events(
        command,
        OrderAck(
            order_id="OID-H",
            status="NotFound",
            executed_qty=0.0,
            reason="client_order_id_not_visible",
        ),
        slot=_CommandSlot(pair_name="pair-a", attempt_index=1, role="cancel"),
    )

    assert len(followups) == 1
    assert not runtime._should_ignore_stale_runtime_cancel(followups[0])
    runtime.chronos.process_event(followups[0])
    runtime.state = runtime.chronos.state
    pair_state = runtime.state.pairs["pair-a"]
    assert pair_state.head_state == HeadState.FAILED
    assert pair_state.tail_state == TailState.LATENT
    assert pair_state.played_quantity == Decimal("0.0")


def test_stale_head_timeout_cancel_ack_does_not_fail_new_attempt(caplog) -> None:
    pair = sample_strategy()[0]
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="head-timeout-stale", pairs=(pair,)),
        symbol="PI_XBTUSD",
        simulate=False,
    )
    runtime.state = replace(
        runtime.state,
        pairs={
            "pair-a": PairCycleState(
                pair=pair,
                head_state=HeadState.NEW,
                head_identity=OrderIdentity(
                    pair_name="pair-a",
                    role="head",
                    client_order_id="CID-H2",
                    exchange_order_id="OID-H2",
                ),
                attempt_index=2,
            )
        },
    )
    runtime.chronos.state = runtime.state
    command = CancelCommand(
        kind=RuntimeCommandKind.CANCEL,
        symbol=Symbol("PI_XBTUSD"),
        pair_name="pair-a",
        request=CancelOrderCommandRequest(
            pair_name="pair-a",
            clOrdID="OID-H1",
        ),
        reason="head_timeout",
    )

    with caplog.at_level("INFO", logger="kola"):
        followups = runtime._followup_events(
            command,
            OrderAck(order_id="OID-H1", status="Canceled", executed_qty=0.0),
            slot=_CommandSlot(pair_name="pair-a", attempt_index=1, role="cancel"),
        )

    assert followups == ()
    assert runtime.state.pairs["pair-a"].head_state == HeadState.NEW
    assert "HEAD_CANCEL_ACK_STALE" not in caplog.text


def test_old_live_head_identity_is_pruned_after_repeat_resets_to_latent() -> None:
    pair = sample_strategy()[0]
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="old-identity", pairs=(pair,)),
        symbol="PI_XBTUSD",
        simulate=False,
    )
    runtime._live_command_identities["old-head"] = OrderIdentity(
        pair_name="pair-a",
        role="head",
        client_order_id="CID-H1",
        exchange_order_id="OID-H1",
        symbol="PI_XBTUSD",
    )
    runtime.state = replace(
        runtime.state,
        pairs={
            "pair-a": PairCycleState(
                pair=pair,
                head_state=HeadState.LATENT,
                attempt_index=2,
            )
        },
    )
    runtime.chronos.state = runtime.state

    runtime._prune_live_command_identities()

    assert runtime._live_command_identities == {}
    assert (
        runtime.pair_state_for_record(
            PrivateOrderRecord(
                symbol="PI_XBTUSD",
                status="canceled",
                exchange_order_id="OID-H1",
                client_order_id="CID-H1",
            )
        )
        is None
    )


def test_entry_window_does_not_cancel_started_head_before_timeout(postgres_url_factory) -> None:
    db = _PrivateDbHarness(postgres_url_factory)
    executor = _RecordingLiveExecutor()
    pair = replace(
        sample_strategy()[0],
        window=TimeWindow(start_minutes=0.0, end_minutes=0.001),
        timeout=60,
    )
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="entry-window", pairs=(pair,)),
        symbol="PI_XBTUSD",
        executor=executor,
        public_source=_HeadOpenOnlySource(db, executor),
        private_source=KrakenPrivateOrderPollingSource(
            db.reader,
            poll_seconds=0.01,
            head_fill_reference_grace_seconds=0.5,
        ),
        simulate=False,
        tail_visibility_timeout_seconds=0.1,
    )

    result = asyncio.run(_run_runtime_for(runtime, seconds=0.25))

    assert any(isinstance(command, PlaceHeadCommand) for command in result.commands)
    assert not any(isinstance(command, CancelCommand) for command in result.commands)
    assert result.state.pairs["pair-a"].head_state == HeadState.NEW


def test_price_gated_latent_head_times_out_without_platform_cancel(caplog) -> None:
    class Market:
        best_bid = 99.5
        best_ask = 100.0
        mid_price = 99.75
        last_price = None
        mark_price = None
        index_price = None
        tick_size = 0.5
        recorded_at = "latent-low"

    class Reader:
        def fetch_market_state(self, symbol=None):
            return Market()

    pair = replace(
        sample_strategy()[0],
        timeout=0.001,
        head_price=(200.0, 201.0),
        head_price_type="pA",
    )
    reader = Reader()
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="latent-timeout", pairs=(pair,)),
        symbol="PI_XBTUSD",
        executor=_RecordingLiveExecutor(),
        public_source=KrakenPublicTriggerSource(reader, poll_seconds=0.01),
        public_state_reader=reader,
        simulate=False,
    )

    with caplog.at_level("INFO", logger="kola"):
        result = asyncio.run(_run_runtime_for(runtime, seconds=0.35))

    assert result.state.pairs["pair-a"].head_state == HeadState.FAILED
    assert not any(isinstance(command, PlaceHeadCommand) for command in result.commands)
    assert not any(isinstance(command, CancelCommand) for command in result.commands)
    assert "LATENT_TIMEOUT_ARMED (pair-a#1):" in caplog.text
    assert "LATENT_TIMEOUT (pair-a#1):" in caplog.text
    assert not any(
        record.getMessage().startswith("HEAD_CANCEL_SENT (") for record in caplog.records
    )


def test_latent_timeout_repeats_with_fresh_relative_price_baseline(caplog) -> None:
    pair = replace(
        sample_strategy()[0],
        timeout=0.003,
        try_num=2,
        dr_pause=0.0,
        head=HeadSpec(side=Side.SELL, order_type="M"),
        head_price=(5.0, 50.0),
        head_price_type="pD",
        amount_type="qAtDpD",
    )

    class Market:
        best_ask = 210.0
        mid_price = 200.0
        last_price = None
        mark_price = None
        index_price = None
        tick_size = 0.5

        def __init__(self, best_bid: float, recorded_at: str) -> None:
            self.best_bid = best_bid
            self.recorded_at = recorded_at

    class Reader:
        runtime: StrategyRuntime | None = None

        def __init__(self) -> None:
            self.calls = 0

        def fetch_market_state(self, symbol=None):
            self.calls += 1
            assert self.runtime is not None
            pair_state = self.runtime.state.pairs["pair-a"]
            if pair_state.attempt_index == 1:
                if pair_state.head_trigger_reference_price is None:
                    return Market(100.0, f"attempt-1-baseline-{self.calls}")
                return Market(104.0, f"attempt-1-wait-{self.calls}")
            if pair_state.head_trigger_reference_price is None:
                return Market(200.0, f"attempt-2-baseline-{self.calls}")
            return Market(205.0, f"attempt-2-ready-{self.calls}")

    reader = Reader()
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="latent-repeat", pairs=(pair,)),
        symbol="PI_XBTUSD",
        public_source=KrakenPublicTriggerSource(reader, poll_seconds=0.01),
        public_state_reader=reader,
        simulate=False,
    )
    reader.runtime = runtime

    with caplog.at_level("INFO", logger="kola"):
        result = asyncio.run(_run_runtime_for(runtime, seconds=0.8))

    pair_state = result.state.pairs["pair-a"]
    assert pair_state.attempt_index == 2
    assert pair_state.head_state == HeadState.HOOKED
    assert pair_state.head_trigger_reference_price == Decimal("200.0")
    assert any(isinstance(command, PlaceHeadCommand) for command in result.commands)
    assert not any(isinstance(command, CancelCommand) for command in result.commands)
    assert "LATENT_TIMEOUT (pair-a#1):" in caplog.text
    assert "LATENT_TIMEOUT_ARMED (pair-a#2):" in caplog.text


def test_chained_latent_head_timeout_waits_for_dependency_before_clock_starts() -> None:
    origin = replace(sample_strategy()[0], name="origin", timeout=None)
    chained = replace(
        sample_strategy()[0],
        name="chained",
        hook_name="origin",
        timeout=0.001,
    )
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="latent-chain", pairs=(origin, chained)),
        symbol="PI_XBTUSD",
        simulate=False,
    )
    first_check = runtime.state.launched_at + timedelta(seconds=1)

    runtime._check_latent_head_deadlines(first_check)

    assert ("chained", 1) not in runtime._latent_head_deadlines

    chained_ready = replace(
        runtime.state.pairs["chained"],
        dependency_evidence=(
            _close_evidence(
                "origin",
                1,
                first_check,
            ),
        ),
    )
    runtime.state = replace(
        runtime.state,
        pairs={**runtime.state.pairs, "chained": chained_ready},
    )
    runtime.chronos.state = runtime.state
    dependency_ready_at = first_check + timedelta(minutes=10)

    runtime._check_latent_head_deadlines(dependency_ready_at)

    deadline = runtime._latent_head_deadlines[("chained", 1)]
    assert deadline.started_at == dependency_ready_at
    assert deadline.deadline_at == dependency_ready_at + timedelta(minutes=0.001)


def test_chain_ready_log_waits_for_complete_all_expression(caplog) -> None:
    origin_a = replace(sample_strategy()[0], name="origin-a")
    origin_b = replace(sample_strategy()[0], name="origin-b")
    chained = replace(
        sample_strategy()[0],
        name="chained",
        hook_name="all(origin-a-tail-closed,origin-b-tail-closed)",
    )
    runtime = StrategyRuntime(
        strategy=StrategySpec(
            name="all-chain-log",
            pairs=(origin_a, origin_b, chained),
        ),
        symbol="PI_XBTUSD",
        simulate=False,
    )
    dependency_graph = runtime.chronos.dependency_graph
    assert dependency_graph is not None
    expression = dependency_graph.by_dependent["chained"]
    first_at = runtime.state.launched_at + timedelta(minutes=1)
    second_at = runtime.state.launched_at + timedelta(minutes=2)
    initial_pairs = runtime.state.pairs
    partial = replace(
        initial_pairs["chained"],
        dependency_evidence=(
            HookEvidence(
                target=expression.targets[0],
                origin_attempt_index=1,
                satisfied_at=first_at,
            ),
        ),
    )
    runtime.state = replace(
        runtime.state,
        pairs={**initial_pairs, "chained": partial},
    )

    runtime._log_chain_releases(initial_pairs)

    assert "CHAIN_READY (chained#1)" not in caplog.text

    partial_pairs = runtime.state.pairs
    ready = replace(
        partial,
        dependency_evidence=(
            *partial.dependency_evidence,
            HookEvidence(
                target=expression.targets[1],
                origin_attempt_index=1,
                satisfied_at=second_at,
            ),
        ),
    )
    runtime.state = replace(
        runtime.state,
        pairs={**partial_pairs, "chained": ready},
    )

    with caplog.at_level("INFO"):
        runtime._log_chain_releases(partial_pairs)

    assert "CHAIN_READY (chained#1): all:" in caplog.text
    assert runtime.event_queue.empty()


def test_gate_wait_logs_unchanged_status_every_five_minutes(caplog) -> None:
    class Market:
        best_bid = 99.5
        best_ask = 100.0
        mid_price = 99.75
        last_price = None
        mark_price = None
        index_price = None
        tick_size = 0.5
        recorded_at = "gate-low"

    class Reader:
        def fetch_market_state(self, symbol=None):
            return Market()

    pair = replace(
        sample_strategy()[0],
        head_price=(200.0, 201.0),
        head_price_type="pA",
    )
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="gate-log", pairs=(pair,)),
        symbol="PI_XBTUSD",
        public_state_reader=Reader(),
        simulate=False,
    )
    now = runtime.state.launched_at + timedelta(seconds=1)

    with caplog.at_level("INFO", logger="kola"):
        runtime._log_gate_waits(now)
        runtime._log_gate_waits(now + timedelta(seconds=299))
        runtime._log_gate_waits(now + timedelta(seconds=300))

    gate_waits = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("GATE_WAIT-2 (pair-a#1):")
    ]
    assert len(gate_waits) == 2
    assert "status=" not in gate_waits[0]
    assert "src=" not in gate_waits[0]


def test_gate_wait_reuses_one_market_read_for_pairs_on_same_route() -> None:
    class Market:
        best_bid = 99.5
        best_ask = 100.0
        mid_price = 99.75
        last_price = 100.0
        mark_price = None
        index_price = None
        tick_size = 0.5
        recorded_at = "shared-gate"

    class Reader:
        def __init__(self) -> None:
            self.calls = 0

        def fetch_market_state(self, symbol=None):
            self.calls += 1
            return Market()

    reader = Reader()
    first = replace(sample_strategy()[0], name="pair-a")
    second = replace(sample_strategy()[0], name="pair-b")
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="shared-gate", pairs=(first, second)),
        symbol="PI_XBTUSD",
        public_state_reader=reader,
        simulate=False,
    )

    runtime._log_gate_waits(runtime.state.launched_at + timedelta(seconds=1))

    assert reader.calls == 1


def test_ready_repeat_logs_deadline_and_gate_before_dispatch(caplog) -> None:
    class Market:
        best_bid = Decimal("99.5")
        best_ask = Decimal("100.0")
        mid_price = Decimal("99.75")
        last_price = None
        mark_price = None
        index_price = None
        tick_size = Decimal("0.5")
        recorded_at = "repeat-ready"

    class Reader:
        def fetch_market_state(self, symbol=None):
            return Market()

    pair = sample_strategy()[0]
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="repeat-observer", pairs=(pair,)),
        symbol="PI_XBTUSD",
        public_state_reader=Reader(),
        simulate=False,
    )
    now = runtime.state.launched_at + timedelta(seconds=1)
    runtime.chronos.pending_repeats["pair-a"] = PendingRepeat(
        pair_name="pair-a",
        ready_at=now,
        next_attempt=2,
    )

    with caplog.at_level("INFO", logger="kola"):
        repeat_commands = runtime.chronos.activate_ready_repeats(
            symbol=runtime.symbol,
            now=now,
        )
        previous_state = runtime.state
        runtime.state = runtime.chronos.state
        runtime._prune_latent_head_deadlines()
        runtime._log_repeat_attempts(previous_state.pairs)
        runtime._check_latent_head_deadlines(now)
        runtime._log_gate_waits(now)

    messages = [record.getMessage() for record in caplog.records]
    assert repeat_commands == ()
    repeat_index = next(
        index for index, message in enumerate(messages)
        if message.startswith("REPEAT_READY (pair-a#2):")
    )
    deadline_index = next(
        index for index, message in enumerate(messages)
        if message.startswith("LATENT_TIMEOUT_ARMED (pair-a#2):")
    )
    gate_index = next(
        index for index, message in enumerate(messages)
        if message.startswith("GATE_WAIT-2 (pair-a#2): ready")
    )
    assert repeat_index < deadline_index < gate_index


def test_gate_wait_ready_uses_side_fallback_when_last_mark_missing(caplog) -> None:
    class Market:
        best_bid = 205.0
        best_ask = 206.0
        mid_price = 200.0
        last_price = None
        mark_price = None
        index_price = None
        tick_size = 0.5
        recorded_at = "side-fallback"

    class Reader:
        def fetch_market_state(self, symbol=None):
            return Market()

    pair = replace(
        sample_strategy()[0],
        head=HeadSpec(
            side=Side.SELL,
            order_type="M",
        ),
        head_price=(5.0, 50.0),
        head_price_type="pD",
        amount_type="qAtDpD",
    )
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="gate-side-fallback", pairs=(pair,)),
        symbol="PI_XBTUSD",
        public_state_reader=Reader(),
        simulate=False,
    )
    now = runtime.state.launched_at + timedelta(seconds=1)
    initial_pair_state = runtime.state.pairs["pair-a"]
    runtime.state = replace(
        runtime.state,
        pairs={
            "pair-a": replace(
                initial_pair_state,
                head_trigger_reference_price=Decimal("200"),
                head_trigger_reference_source="bid",
                head_state=HeadState.LATENT,
            )
        },
    )
    runtime.chronos.state = runtime.state

    with caplog.at_level("INFO", logger="kola"):
        runtime._log_gate_waits(now)

    messages = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("GATE_WAIT-2 (pair-a#1):")
    ]
    assert any(
        message.startswith("GATE_WAIT-2 (pair-a#1): ready")
        for message in messages
    )
    assert any("bid" in message.split()[3:] for message in messages if message.startswith("GATE_WAIT-2"))


def test_runtime_legend_logs_once_with_compact_columns(caplog) -> None:
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="legend", pairs=sample_strategy()),
        symbol="PI_XBTUSD",
        simulate=False,
    )

    with caplog.at_level("INFO", logger="kola"):
        runtime._log_runtime_legend_once()
        runtime._log_runtime_legend_once()

    messages = [record.getMessage() for record in caplog.records]
    general = [message for message in messages if message.startswith("LEGEND--GENERAL:")]
    assert len(general) == 1
    assert "AI=attempt_index    PU=pair_update" in general[0]
    assert any(message.startswith("LEGEND--HEAD_SENT:") for message in messages)
    assert any(message.startswith("LEGEND--GATE_WAIT-2:") for message in messages)
    assert "RAPPEL:" not in caplog.text


def test_runtime_dispatches_different_pairs_concurrently() -> None:
    class BlockingExecutor:
        def __init__(self) -> None:
            self.started: list[str] = []
            self.release = asyncio.Event()

        async def execute(self, command):
            self.started.append(command.pair_name)
            await self.release.wait()
            return OrderAck(order_id=f"OID-{command.pair_name}", status="New")

    pair_a = sample_strategy()[0]
    pair_b = replace(pair_a, name="pair-b")
    executor = BlockingExecutor()
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="multi", pairs=(pair_a, pair_b)),
        symbol="PI_XBTUSD",
        executor=executor,
        simulate=False,
    )
    command_a = PlaceHeadCommand(
        kind=RuntimeCommandKind.PLACE,
        symbol=Symbol("PI_XBTUSD"),
        pair_name="pair-a",
        request=PlaceOrderCommandRequest(
            pair_name="pair-a",
            side="buy",
            ordType="Market",
            orderQty=Decimal("1"),
            clOrdID="CID-A",
        ),
    )
    command_b = PlaceHeadCommand(
        kind=RuntimeCommandKind.PLACE,
        symbol=Symbol("PI_XBTUSD"),
        pair_name="pair-b",
        request=PlaceOrderCommandRequest(
            pair_name="pair-b",
            side="buy",
            ordType="Market",
            orderQty=Decimal("1"),
            clOrdID="CID-B",
        ),
    )

    async def _run() -> tuple[int, tuple[str, ...]]:
        runtime.running = True
        runtime._dispatch_commands((command_a, command_b))
        await asyncio.sleep(0.01)
        inflight_count = len(runtime._inflight_commands)
        started = tuple(executor.started)
        executor.release.set()
        await asyncio.sleep(0.01)
        await runtime.stop()
        return inflight_count, started

    inflight_count, started = asyncio.run(_run())

    assert inflight_count == 2
    assert set(started) == {"pair-a", "pair-b"}


def test_runtime_defers_head_when_active_pair_capacity_is_full() -> None:
    class BlockingExecutor:
        def __init__(self) -> None:
            self.started: list[str] = []
            self.release = asyncio.Event()

        async def execute(self, command):
            self.started.append(command.pair_name)
            await self.release.wait()
            return OrderAck(order_id=f"OID-{command.pair_name}", status="New")

    pair_a = sample_strategy()[0]
    pair_b = replace(pair_a, name="pair-b")
    executor = BlockingExecutor()
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="multi", pairs=(pair_a, pair_b)),
        symbol="PI_XBTUSD",
        executor=executor,
        max_active_pairs=1,
        simulate=False,
    )
    command_a = PlaceHeadCommand(
        kind=RuntimeCommandKind.PLACE,
        symbol=Symbol("PI_XBTUSD"),
        pair_name="pair-a",
        request=PlaceOrderCommandRequest(
            pair_name="pair-a",
            side="buy",
            ordType="Market",
            orderQty=Decimal("1"),
            clOrdID="CID-A",
        ),
    )
    command_b = PlaceHeadCommand(
        kind=RuntimeCommandKind.PLACE,
        symbol=Symbol("PI_XBTUSD"),
        pair_name="pair-b",
        request=PlaceOrderCommandRequest(
            pair_name="pair-b",
            side="buy",
            ordType="Market",
            orderQty=Decimal("1"),
            clOrdID="CID-B",
        ),
    )

    async def _run() -> tuple[int, int, tuple[str, ...]]:
        runtime.running = True
        runtime._dispatch_commands((command_a, command_b))
        await asyncio.sleep(0.01)
        inflight_count = len(runtime._inflight_commands)
        pending_count = len(runtime._pending_head_commands)
        started = tuple(executor.started)
        executor.release.set()
        await asyncio.sleep(0.01)
        await runtime.stop()
        return inflight_count, pending_count, started

    inflight_count, pending_count, started = asyncio.run(_run())

    assert inflight_count == 1
    assert pending_count == 1
    assert started == ("pair-a",)


def test_tail_place_failure_marks_only_that_pair_failed() -> None:
    class FailingTailExecutor:
        async def execute(self, command):
            raise RuntimeError("Kraken HTTP 503 on /sendorder")

    pair = sample_strategy()[0]
    now = datetime.now(timezone.utc)
    trail = initial_tail_trail(pair, Decimal("100"), now)
    state = PairCycleState(
        pair=pair,
        head_state=HeadState.CLOSED,
        tail_state=TailState.HOOKED,
        tail_trail=trail,
        played_quantity=Decimal("1"),
    )
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="one", pairs=(pair,)),
        symbol="PI_XBTUSD",
        executor=FailingTailExecutor(),
        simulate=False,
    )
    runtime.state = replace(runtime.state, pairs={"pair-a": state})
    runtime.chronos.state = runtime.state
    command = PlaceTailCommand(
        kind=RuntimeCommandKind.PLACE,
        symbol=Symbol("PI_XBTUSD"),
        pair_name="pair-a",
        request=PlaceOrderCommandRequest(
            pair_name="pair-a",
            side="sell",
            ordType="Stop",
            orderQty=Decimal("1"),
            stopPx=Decimal("99"),
            clOrdID="TAIL-A",
        ),
    )

    async def _run() -> PairCycleState:
        runtime.running = True
        runtime._dispatch_commands((command,))
        await asyncio.sleep(0.01)
        runtime._reap_command_tasks()
        event = await asyncio.wait_for(runtime.event_queue.get(), timeout=0.5)
        runtime.chronos.process_event(event)
        runtime.state = runtime.chronos.state
        await runtime.stop()
        return runtime.state.pairs["pair-a"]

    final_state = asyncio.run(_run())

    assert not runtime._command_errors
    assert final_state.head_state == HeadState.CLOSED
    assert final_state.tail_state == TailState.FAILED
    assert final_state.completed_at is not None


def test_runtime_serialises_tail_amends_per_pair_but_not_across_pairs() -> None:
    class BlockingExecutor:
        def __init__(self) -> None:
            self.release = asyncio.Event()

        async def execute(self, command):
            await self.release.wait()
            return OrderAck(order_id=f"OID-{command.pair_name}", status="New")

    pair_a = sample_strategy()[0]
    pair_b = replace(pair_a, name="pair-b")
    now = datetime.now(timezone.utc)
    trail_a = replace(
        initial_tail_trail(pair_a, Decimal("100"), now),
        confirmed_stop_price=Decimal("99"),
        current_stop_price=Decimal("99"),
        last_confirmed_at=now,
    )
    trail_b = replace(
        initial_tail_trail(pair_b, Decimal("100"), now),
        confirmed_stop_price=Decimal("99"),
        current_stop_price=Decimal("99"),
        last_confirmed_at=now,
    )
    executor = BlockingExecutor()
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="multi-amend", pairs=(pair_a, pair_b)),
        symbol="PI_XBTUSD",
        executor=executor,
        simulate=False,
    )
    runtime.state = replace(
        runtime.state,
        pairs={
            "pair-a": PairCycleState(
                pair=pair_a,
                head_state=HeadState.CLOSED,
                tail_state=TailState.LIVING,
                tail_identity=OrderIdentity(
                    pair_name="pair-a",
                    role="tail",
                    client_order_id="CID-TA",
                    exchange_order_id="OID-TA",
                ),
                tail_trail=trail_a,
                played_quantity=Decimal("1"),
            ),
            "pair-b": PairCycleState(
                pair=pair_b,
                head_state=HeadState.CLOSED,
                tail_state=TailState.LIVING,
                tail_identity=OrderIdentity(
                    pair_name="pair-b",
                    role="tail",
                    client_order_id="CID-TB",
                    exchange_order_id="OID-TB",
                ),
                tail_trail=trail_b,
                played_quantity=Decimal("1"),
            ),
        },
    )

    def amend(pair_name: str, client_id: str, order_id: str, price: str) -> AmendTailCommand:
        return AmendTailCommand(
            kind=RuntimeCommandKind.AMEND,
            symbol=Symbol("PI_XBTUSD"),
            pair_name=pair_name,
            request=AmendOrderCommandRequest(
                pair_name=pair_name,
                side="sell",
                ordType="Stop",
                orderID=order_id,
                clOrdID=client_id,
                newPrice=Decimal(price),
            ),
        )

    async def _run() -> tuple[int, int, Decimal | None]:
        runtime.running = True
        runtime._dispatch_commands(
            (
                amend("pair-a", "CID-TA", "OID-TA", "99.5"),
                amend("pair-a", "CID-TA", "OID-TA", "100.0"),
                amend("pair-b", "CID-TB", "OID-TB", "99.5"),
            )
        )
        await asyncio.sleep(0.01)
        pending = runtime._pending_commands[
            _CommandSlot(pair_name="pair-a", attempt_index=1, role="tail")
        ]
        pending_request = cast(AmendOrderCommandRequest, pending[-1].request)
        pending_price = pending_request.newPrice
        inflight_count = len(runtime._inflight_commands)
        pending_count = len(pending)
        executor.release.set()
        await asyncio.sleep(0.01)
        await runtime.stop()
        return inflight_count, pending_count, None if pending_price is None else to_decimal(pending_price)

    inflight_count, pending_count, pending_price = asyncio.run(_run())

    assert inflight_count == 2
    assert pending_count == 1
    assert pending_price == Decimal("100.0")


def test_strategy_runtime_simulation_initialises_relative_tail_reference() -> None:
    pair = sample_strategy()[0]
    pair = replace(pair, tail_price_spec=148.89, tail_price_spec_type="tB", amount_type="qAtBpB")
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="demo", pairs=(pair,)),
        symbol="PI_XBTUSD",
        executor=SimulatedExecutor(),
        simulate=True,
    )

    result = asyncio.run(_run_runtime_for(runtime))

    assert result.state.pairs["pair-a"].tail_trail is not None
    assert result.state.pairs["pair-a"].tail_trail.entry_reference_price == Decimal("100.0")


def test_strategy_runtime_live_mode_does_not_emit_state_followups_from_ack() -> None:
    pair = sample_strategy()[0]
    pair = replace(pair, tail_price_spec=148.89, tail_price_spec_type="tB", amount_type="qAtBpB")

    runtime = StrategyRuntime(
        strategy=StrategySpec(name="demo", pairs=(pair,)),
        symbol="PI_XBTUSD",
        executor=SimulatedExecutor(),
        simulate=False,
    )
    command = plan_strategy_once(
        strategy=StrategySpec(name="demo", pairs=(pair,)),
        symbol="PI_XBTUSD",
    ).commands[0]
    prepared = runtime._prepare_command(command)
    ack = OrderAck(
        order_id="OID-H",
        status="Filled",
        price=100.0,
        orig_qty=1.0,
        executed_qty=1.0,
        side="buy",
    )

    followups = runtime._followup_events(prepared, ack)
    assert followups == ()


def test_strategy_runtime_live_mode_emits_rejected_tail_amend_followup() -> None:
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="demo", pairs=sample_strategy()),
        symbol="PI_XBTUSD",
        executor=SimulatedExecutor(),
        simulate=False,
    )
    command = AmendTailCommand(
        kind=RuntimeCommandKind.AMEND,
        symbol=Symbol("PI_XBTUSD"),
        pair_name="pair-a",
        request=AmendOrderCommandRequest(
            pair_name="pair-a",
            side="sell",
            ordType="Stop",
            orderID="OID-T",
            clOrdID="CID-T",
            newPrice=Decimal("98.5"),
        ),
    )

    followups = runtime._followup_events(
        command,
        OrderAck(order_id="OID-T", status="Rejected", price=98.5),
    )

    assert len(followups) == 1
    assert followups[0].kind == EggMoveKind.TAIL_AMEND_REJECTED
    assert followups[0].role == OrderRole.TAIL
    assert followups[0].reply is not None
    assert followups[0].reply["clOrdID"] == "CID-T"


def test_tail_submitted_followup_uses_exchange_rounded_ack_price_in_simulation() -> None:
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="demo", pairs=sample_strategy()),
        symbol="PI_XBTUSD",
        executor=SimulatedExecutor(),
        simulate=True,
    )
    command = PlaceTailCommand(
        kind=RuntimeCommandKind.PLACE,
        symbol=Symbol("PI_XBTUSD"),
        pair_name="pair-a",
        request=PlaceOrderCommandRequest(
            pair_name="pair-a",
            side="sell",
            ordType="Stop",
            orderQty=Decimal("1"),
            stopPx=Decimal("99.49"),
            clOrdID="CID-T",
        ),
    )

    followups = runtime._followup_events(
        command,
        OrderAck(order_id="OID-T", status="New", price=99.5),
    )

    assert followups[0].reply is not None
    assert followups[0].reply["stopPx"] == 99.5


def test_runtime_resolves_private_record_from_live_command_identity_without_ack_state() -> None:
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="demo", pairs=sample_strategy()),
        symbol="PI_XBTUSD",
        executor=SimulatedExecutor(),
        simulate=False,
    )
    command = PlaceHeadCommand(
        kind=RuntimeCommandKind.PLACE,
        symbol=Symbol("PI_XBTUSD"),
        pair_name="pair-a",
        request=PlaceOrderCommandRequest(
            pair_name="pair-a",
            side="buy",
            ordType="Market",
            orderQty=Decimal("1"),
            clOrdID="CID-HEAD-LIVE",
        ),
    )
    identity = runtime._command_identity_from_command(command)
    assert identity is not None
    runtime._live_command_identities["test"] = identity
    record = PrivateOrderRecord(
        symbol="PI_XBTUSD",
        status="filled",
        client_order_id="CID-HEAD-LIVE",
    )

    resolved = runtime.pair_state_for_record(record)

    assert resolved is not None
    pair_state, role = resolved
    assert pair_state.pair.name == "pair-a"
    assert role == OrderRole.HEAD


def test_pair_state_for_record_rejects_wrong_symbol_private_rows() -> None:
    xbt_pair = replace(sample_strategy()[0], name="xbt", symbol="PI_XBTUSD")
    eth_pair = replace(sample_strategy()[0], name="eth", symbol="PI_ETHUSD")
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="demo", pairs=(xbt_pair, eth_pair)),
        symbol="PI_XBTUSD",
        simulate=False,
    )
    runtime.state = replace(
        runtime.state,
        pairs={
            "xbt": replace(
                runtime.state.pairs["xbt"],
                head_identity=OrderIdentity(
                    pair_name="xbt",
                    role="head",
                    client_order_id="CID-SAME",
                ),
            ),
            "eth": replace(
                runtime.state.pairs["eth"],
                head_identity=OrderIdentity(
                    pair_name="eth",
                    role="head",
                    client_order_id="CID-SAME",
                ),
            ),
        },
    )
    record = PrivateOrderRecord(
        symbol="PI_ETHUSD",
        status="filled",
        client_order_id="CID-SAME",
    )

    resolved = runtime.pair_state_for_record(record)

    assert resolved is not None
    assert resolved[0].pair.name == "eth"
    assert resolved[1] == OrderRole.HEAD


def test_strategy_runtime_dispatches_exchange_commands_without_blocking_loop() -> None:
    class SlowExecutor:
        async def execute(self, command):
            await asyncio.sleep(0.2)
            return OrderAck(order_id="OID", status="New", price=100.0)

    runtime = StrategyRuntime(
        strategy=StrategySpec(name="demo", pairs=sample_strategy()),
        symbol="PI_XBTUSD",
        executor=SlowExecutor(),
        simulate=False,
    )
    event = EggMove(
        kind=EggMoveKind.HEAD_HOOKED,
        occurred_at=datetime.now(timezone.utc),
        symbol="PI_XBTUSD",
        pair_name="pair-a",
    )

    async def _run() -> bool:
        await runtime.start()
        await runtime.enqueue(event)
        task = asyncio.create_task(runtime.run())
        await asyncio.sleep(0.05)
        command_pending = bool(runtime._inflight_commands)
        await runtime.stop()
        await task
        return command_pending

    assert asyncio.run(_run()) is True


def test_private_record_event_id_changes_for_in_place_row_updates() -> None:
    open_record = PrivateOrderRecord(
        symbol="PI_XBTUSD",
        status="open",
        exchange_order_id="OID-T",
        client_order_id="CID-T",
        stop_price=73820.5,
        quantity=10.0,
        filled_quantity=0.0,
        local_timestamp="2026-05-29T18:08:24.085000+00:00",
        local_id=42,
    )
    amend_record = replace(
        open_record,
        stop_price=73978.0,
        local_timestamp="2026-05-29T18:10:03.971000+00:00",
    )
    closed_record = replace(
        amend_record,
        status="canceled",
        reason="stop_order_triggered",
        stop_price=None,
        local_timestamp="2026-05-29T18:11:24.655000+00:00",
    )

    open_id = KrakenPrivateOrderPollingSource._private_record_event_id(
        open_record,
        is_fill=False,
    )
    amend_id = KrakenPrivateOrderPollingSource._private_record_event_id(
        amend_record,
        is_fill=False,
    )
    closed_id = KrakenPrivateOrderPollingSource._private_record_event_id(
        closed_record,
        is_fill=False,
    )
    fill_id = KrakenPrivateOrderPollingSource._private_record_event_id(
        closed_record,
        is_fill=True,
    )

    assert open_id is not None
    assert amend_id is not None
    assert closed_id is not None
    assert len({open_id, amend_id, closed_id}) == 3
    assert fill_id is not None
    assert fill_id.startswith("private-fill:")


def test_private_tail_fill_record_closes_living_tail() -> None:
    class PrivateClient:
        def fetch_private_orders_since(self, **kwargs):
            return ()

        def fetch_private_fills_since(self, **kwargs):
            return ()

        def fetch_private_orders_for_identities(self, **kwargs):
            return ()

        def fetch_private_fills_for_identities(self, **kwargs):
            return (
                PrivateOrderRecord(
                    symbol="PI_XBTUSD",
                    status="filled",
                    exchange_order_id="OID-T",
                    client_order_id="CID-T",
                    reason="full_fill",
                    side="sell",
                    order_type="market",
                    price=99.0,
                    quantity=1.0,
                    filled_quantity=1.0,
                    source_timestamp="2026-05-29T20:43:43.720000+00:00",
                    local_timestamp="2026-05-29T20:44:14.904805+00:00",
                    local_id=106,
                ),
            )

    pair = sample_strategy()[0]
    now = datetime.now(timezone.utc)
    trail = replace(
        initial_tail_trail(pair, Decimal("100"), now),
        confirmed_stop_price=Decimal("99.5"),
        current_stop_price=Decimal("99.5"),
        last_confirmed_at=now,
    )
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="demo", pairs=(pair,)),
        symbol="PI_XBTUSD",
        private_source=KrakenPrivateOrderPollingSource(PrivateClient(), poll_seconds=0.0),
        simulate=False,
    )
    living = PairCycleState(
        pair=pair,
        head_state=HeadState.CLOSED,
        tail_state=TailState.LIVING,
        tail_mode=None,
        head_identity=OrderIdentity(
            pair_name="pair-a",
            role="head",
            client_order_id="CID-H",
            exchange_order_id="OID-H",
        ),
        tail_identity=OrderIdentity(
            pair_name="pair-a",
            role="tail",
            client_order_id="CID-T",
            exchange_order_id="OID-T",
        ),
        tail_trail=trail,
        played_quantity=Decimal("1"),
    )
    runtime.state = replace(runtime.state, pairs={"pair-a": living})
    runtime.chronos.state = runtime.state

    result = asyncio.run(asyncio.wait_for(runtime.run(), timeout=1.0))

    assert result.state.pairs["pair-a"].tail_state == TailState.CLOSED


def test_head_fill_reference_wait_is_not_required_after_tail_is_anchored() -> None:
    pair = replace(
        sample_strategy()[0],
        tail_price_spec=148.89,
        tail_price_spec_type="tB",
        amount_type="qAtBpB",
    )
    now = datetime.now(timezone.utc)
    anchored = PairCycleState(
        pair=pair,
        head_state=HeadState.CLOSED,
        tail_state=TailState.LIVING,
        tail_trail=initial_tail_trail(pair, Decimal("100"), now),
        played_quantity=Decimal("1"),
    )
    move = EggMove(
        kind=EggMoveKind.PLAYED_AND_CANCELED,
        occurred_at=now,
        symbol="PI_XBTUSD",
        pair_name="pair-a",
        role=OrderRole.HEAD,
        reply={"cumQty": 1.0, "orderQty": 1.0},
        is_private=True,
    )
    source = KrakenPrivateOrderPollingSource(cast(Any, object()))

    assert source._must_wait_for_private_fill_reference(move, anchored, OrderRole.HEAD) is False


def test_strategy_runtime_waits_for_tail_after_filled_head() -> None:
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="demo", pairs=sample_strategy()),
        symbol="PI_XBTUSD",
        executor=SimulatedExecutor(),
        simulate=True,
    )
    pair = sample_strategy()[0]

    runtime.state = replace(
        runtime.state,
        pairs={
            "pair-a": PairCycleState(
                pair=pair,
                head_state=HeadState.CLOSED,
                played_quantity=Decimal("1"),
                tail_state=TailState.HOOKED,
            )
        },
    )
    assert runtime.all_pairs_terminal is False

    runtime.state = replace(
        runtime.state,
        pairs={
            "pair-a": PairCycleState(
                pair=pair,
                head_state=HeadState.CLOSED,
                played_quantity=Decimal("1"),
                tail_state=TailState.SUBMITTED,
            )
        },
    )
    assert runtime.all_pairs_terminal is False

    runtime.state = replace(
        runtime.state,
        pairs={
            "pair-a": PairCycleState(
                pair=pair,
                head_state=HeadState.CLOSED,
                played_quantity=Decimal("1"),
                tail_state=TailState.CLOSED,
            )
        },
    )
    assert runtime.all_pairs_terminal is True


def test_strategy_runtime_does_not_exit_while_repeat_is_pending() -> None:
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="demo", pairs=sample_strategy()),
        symbol="PI_XBTUSD",
        executor=None,
        simulate=False,
    )
    pair = sample_strategy()[0]
    terminal = PairCycleState(
        pair=pair,
        head_state=HeadState.CLOSED,
        tail_state=TailState.CLOSED,
        played_quantity=Decimal("1"),
    )
    runtime.state = replace(runtime.state, pairs={"pair-a": terminal})
    runtime.chronos.state = runtime.state
    now = datetime.now(timezone.utc)
    runtime.chronos.pending_repeats["pair-a"] = PendingRepeat(
        pair_name="pair-a",
        ready_at=now + timedelta(seconds=5),
        next_attempt=2,
    )

    async def _run() -> bool:
        task = asyncio.create_task(runtime.run())
        await asyncio.sleep(0.15)
        still_running = not task.done()
        await runtime.stop()
        await task
        return still_running

    assert asyncio.run(_run()) is True


def test_tail_terminal_event_schedules_and_activates_next_attempt() -> None:
    pair = replace(sample_strategy()[0], try_num=2, dr_pause=1 / 60)
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="demo", pairs=(pair,)),
        symbol="PI_XBTUSD",
        executor=None,
        simulate=False,
    )
    now = datetime.now(timezone.utc)
    trail = replace(
        initial_tail_trail(pair, Decimal("100"), now),
        confirmed_stop_price=Decimal("99"),
    )
    living = PairCycleState(
        pair=pair,
        head_state=HeadState.CLOSED,
        tail_state=TailState.LIVING,
        tail_identity=OrderIdentity(
            pair_name="pair-a",
            role="tail",
            client_order_id="CID-T",
            exchange_order_id="OID-T",
        ),
        tail_trail=trail,
        played_quantity=Decimal("1"),
    )
    runtime.state = replace(runtime.state, pairs={"pair-a": living})
    runtime.chronos.state = runtime.state

    commands = runtime.chronos.process_event(
        EggMove(
            kind=EggMoveKind.PLAYED_AND_CANCELED,
            occurred_at=now,
            symbol="PI_XBTUSD",
            pair_name="pair-a",
            role=OrderRole.TAIL,
            reply={
                "orderID": "OID-T",
                "clOrdID": "CID-T",
                "cumQty": 1.0,
                "orderQty": 1.0,
            },
            is_private=True,
        ),
        now=now,
    )
    runtime.state = runtime.chronos.state

    assert commands == ()
    assert "pair-a" in runtime.chronos.pending_repeats

    repeat_commands = runtime.chronos.activate_ready_repeats(
        symbol="PI_XBTUSD",
        now=now + timedelta(seconds=1),
    )

    assert repeat_commands == ()
    assert runtime.chronos.state.pairs["pair-a"].attempt_index == 2
    assert runtime.chronos.state.pairs["pair-a"].head_state == HeadState.LATENT
    assert runtime.chronos.state.pairs["pair-a"].head_trigger_reference_price is None


def test_private_polling_source_keeps_running_while_repeat_is_pending() -> None:
    class PrivateClient:
        def fetch_private_orders_since(self, **kwargs):
            return ()

        def fetch_private_fills_since(self, **kwargs):
            return ()

    class Runtime:
        symbol = "PI_XBTUSD"
        running = True

        def __init__(self) -> None:
            self.state = StrategyRuntime(
                strategy=StrategySpec(name="demo", pairs=sample_strategy()),
                symbol="PI_XBTUSD",
                simulate=False,
            ).state

        @property
        def all_pairs_terminal(self) -> bool:
            return True

        @property
        def should_keep_sources_alive(self) -> bool:
            return True

        async def enqueue(self, event: EggMove) -> None:
            raise AssertionError(f"unexpected event: {event}")

        def pair_state_for_record(self, record: object):
            return None

    async def _run() -> bool:
        runtime = Runtime()
        source = KrakenPrivateOrderPollingSource(PrivateClient(), poll_seconds=0.01)
        task = asyncio.create_task(source.pump(runtime))
        await asyncio.sleep(0.03)
        still_running = not task.done()
        runtime.running = False
        await asyncio.wait_for(task, timeout=0.2)
        return still_running

    assert asyncio.run(_run()) is True


def test_public_polling_emits_market_ticks_for_living_tails() -> None:
    class Market:
        best_bid = 102.0
        best_ask = 102.5
        mid_price = 102.25
        last_price = 102.0
        mark_price = None
        index_price = None
        tick_size = 0.5
        recorded_at = "tick-1"

    class Client:
        def fetch_market_state(self, symbol=None):
            return Market()

    class Runtime:
        symbol = "PI_XBTUSD"
        running = True

        def __init__(self) -> None:
            pair = sample_strategy()[0]
            self.state = replace(
                plan_strategy_once(
                    strategy=StrategySpec(
                        name="demo",
                        pairs=(pair,),
                    ),
                    symbol="PI_XBTUSD",
                ).state,
                pairs={
                    "pair-a": PairCycleState(
                        pair=pair,
                        head_state=HeadState.LIVING,
                        tail_state=TailState.LIVING,
                        tail_trail=initial_tail_trail(
                            pair,
                            Decimal("100"),
                            datetime.now(timezone.utc),
                        ),
                    )
                },
            )
            self.events: list[EggMove] = []

        @property
        def all_pairs_terminal(self) -> bool:
            return bool(self.events)

        @property
        def should_keep_sources_alive(self) -> bool:
            return False

        async def enqueue(self, event: EggMove) -> None:
            self.events.append(event)

        def pair_state_for_record(
            self, record: object
        ) -> tuple[PairCycleState, OrderRole] | None:
            return None

    runtime = Runtime()
    source = KrakenPublicTriggerSource(Client(), poll_seconds=0.0)

    asyncio.run(source.pump(runtime))

    assert len(runtime.events) == 1
    assert runtime.events[0].kind == EggMoveKind.MARKET_TICK
    assert runtime.events[0].reply is not None
    assert runtime.events[0].reply["reference_price"] == 102.0


def test_public_polling_enqueues_ready_heads_before_tail_market_fanout() -> None:
    class Market:
        best_bid = 100.0
        best_ask = 100.5
        mid_price = 100.25
        last_price = 100.0
        mark_price = None
        index_price = None
        tick_size = 0.5
        spread = 0.5
        recorded_at = "shared-poll"

    class Client:
        def __init__(self) -> None:
            self.calls = 0

        def fetch_market_state(self, symbol=None):
            self.calls += 1
            return Market()

    class Runtime:
        symbol = "PI_XBTUSD"
        exchange = "kraken"
        market_type = "futures"
        running = True

        def __init__(self) -> None:
            tail_pair = replace(sample_strategy()[0], name="tail-first")
            ready_pair = replace(sample_strategy()[0], name="ready-second")
            launched_at = datetime.now(timezone.utc)
            self.state = StrategyState(
                launched_at=launched_at,
                strategy_id="priority",
                pairs={
                    "tail-first": PairCycleState(
                        pair=tail_pair,
                        head_state=HeadState.LIVING,
                        tail_state=TailState.LIVING,
                        tail_trail=initial_tail_trail(
                            tail_pair,
                            Decimal("100"),
                            launched_at,
                        ),
                    ),
                    "ready-second": PairCycleState(pair=ready_pair),
                },
            )
            self.events: list[EggMove] = []

        @property
        def all_pairs_terminal(self) -> bool:
            return len(self.events) >= 2

        @property
        def should_keep_sources_alive(self) -> bool:
            return False

        async def enqueue(self, event: EggMove) -> None:
            self.events.append(event)

        def pair_state_for_record(
            self,
            record: object,
        ) -> tuple[PairCycleState, OrderRole] | None:
            return None

    client = Client()
    runtime = Runtime()
    source = KrakenPublicTriggerSource(client, poll_seconds=0.0)

    asyncio.run(source.pump(runtime))

    assert [event.kind for event in runtime.events] == [
        EggMoveKind.HEAD_HOOKED,
        EggMoveKind.MARKET_TICK,
    ]
    assert client.calls == 1


def test_public_polling_does_not_deduplicate_changed_tail_reference() -> None:
    class Market:
        best_bid = 102.0
        best_ask = 102.5
        mid_price = 102.25
        mark_price = None
        index_price = None
        tick_size = 0.5
        recorded_at = "same-book-row"

        def __init__(self, last_price: float) -> None:
            self.last_price = last_price

    class Client:
        def __init__(self) -> None:
            self.prices = [102.0, 103.0]

        def fetch_market_state(self, symbol=None):
            return Market(self.prices.pop(0) if self.prices else 103.0)

    class Runtime:
        symbol = "PI_XBTUSD"
        running = True

        def __init__(self) -> None:
            pair = sample_strategy()[0]
            self.state = replace(
                plan_strategy_once(
                    strategy=StrategySpec(name="demo", pairs=(pair,)),
                    symbol="PI_XBTUSD",
                ).state,
                pairs={
                    "pair-a": PairCycleState(
                        pair=pair,
                        head_state=HeadState.LIVING,
                        tail_state=TailState.LIVING,
                        tail_trail=initial_tail_trail(
                            pair,
                            Decimal("100"),
                            datetime.now(timezone.utc),
                        ),
                    )
                },
            )
            self.events: list[EggMove] = []

        @property
        def all_pairs_terminal(self) -> bool:
            return len(self.events) >= 2

        @property
        def should_keep_sources_alive(self) -> bool:
            return False

        async def enqueue(self, event: EggMove) -> None:
            self.events.append(event)

        def pair_state_for_record(
            self, record: object
        ) -> tuple[PairCycleState, OrderRole] | None:
            return None

    runtime = Runtime()
    source = KrakenPublicTriggerSource(Client(), poll_seconds=0.0)

    asyncio.run(source.pump(runtime))

    assert [event.reply["reference_price"] for event in runtime.events if event.reply] == [
        102.0,
        103.0,
    ]


def test_public_polling_groups_active_pairs_by_symbol() -> None:
    xbt_pair = replace(sample_strategy()[0], name="xbt", symbol="PI_XBTUSD")
    eth_pair = replace(sample_strategy()[0], name="eth", symbol="PI_ETHUSD")

    class Market:
        best_bid = 102.0
        best_ask = 102.5
        mid_price = 102.25
        last_price = 102.0
        mark_price = None
        index_price = None
        tick_size = 0.5

        def __init__(self, symbol: str) -> None:
            self.recorded_at = f"tick-{symbol}"

    class Client:
        def __init__(self) -> None:
            self.symbols: list[str] = []

        def fetch_market_state(self, symbol=None):
            self.symbols.append(str(symbol))
            return Market(str(symbol))

    class Runtime:
        symbol = "PI_XBTUSD"
        running = True

        def __init__(self) -> None:
            now = datetime.now(timezone.utc)
            base_state = StrategyRuntime(
                strategy=StrategySpec(name="demo", pairs=(xbt_pair, eth_pair)),
                symbol="PI_XBTUSD",
                simulate=False,
            ).state
            self.state = replace(
                base_state,
                pairs={
                    "xbt": PairCycleState(
                        pair=xbt_pair,
                        head_state=HeadState.CLOSED,
                        tail_state=TailState.LIVING,
                        tail_trail=initial_tail_trail(xbt_pair, Decimal("100"), now),
                    ),
                    "eth": PairCycleState(
                        pair=eth_pair,
                        head_state=HeadState.CLOSED,
                        tail_state=TailState.LIVING,
                        tail_trail=initial_tail_trail(eth_pair, Decimal("100"), now),
                    ),
                },
            )
            self.events: list[EggMove] = []

        @property
        def all_pairs_terminal(self) -> bool:
            return len(self.events) >= 2

        @property
        def should_keep_sources_alive(self) -> bool:
            return False

        async def enqueue(self, event: EggMove) -> None:
            self.events.append(event)

        def pair_state_for_record(
            self, record: object
        ) -> tuple[PairCycleState, OrderRole] | None:
            return None

    client = Client()
    runtime = Runtime()
    source = KrakenPublicTriggerSource(client, poll_seconds=0.0)

    asyncio.run(source.pump(runtime))

    assert set(client.symbols) == {"PI_XBTUSD", "PI_ETHUSD"}
    assert {event.symbol for event in runtime.events} == {"PI_XBTUSD", "PI_ETHUSD"}


def test_public_polling_records_head_baseline_before_market_head_hook() -> None:
    pair = replace(
        sample_strategy()[0],
        head=HeadSpec(side=Side.SELL, order_type="M"),
        head_price=(5.0, 50.0),
        head_price_type="pD",
        amount_type="qAtDpD",
    )

    class Market:
        best_ask = 110.0
        mid_price = 100.0
        last_price = None
        mark_price = None
        index_price = None
        tick_size = 0.5

        def __init__(self, best_bid: float, recorded_at: str) -> None:
            self.best_bid = best_bid
            self.recorded_at = recorded_at

    class Client:
        def __init__(self) -> None:
            self.markets = [
                Market(100.0, "baseline"),
                Market(104.5, "not-yet"),
                Market(105.0, "ready"),
            ]

        def fetch_market_state(self, symbol=None):
            return self.markets.pop(0) if self.markets else Market(105.0, "ready")

    class Runtime:
        symbol = "PI_XBTUSD"
        running = True

        def __init__(self) -> None:
            self.state = StrategyRuntime(
                strategy=StrategySpec(name="demo", pairs=(pair,)),
                symbol="PI_XBTUSD",
                simulate=False,
            ).state
            self.events: list[EggMove] = []

        @property
        def all_pairs_terminal(self) -> bool:
            return bool(self.events) and self.events[-1].kind == EggMoveKind.HEAD_HOOKED

        @property
        def should_keep_sources_alive(self) -> bool:
            return False

        async def enqueue(self, event: EggMove) -> None:
            self.events.append(event)
            if event.kind == EggMoveKind.HEAD_TRIGGER_BASELINED:
                pair_state = self.state.pairs["pair-a"]
                self.state = replace(
                    self.state,
                    pairs={
                        "pair-a": replace(
                            pair_state,
                            head_trigger_reference_price=Decimal("100.0"),
                            head_trigger_reference_source="bid",
                            head_trigger_reference_at=event.occurred_at,
                        )
                    },
                )

        def pair_state_for_record(
            self, record: object
        ) -> tuple[PairCycleState, OrderRole] | None:
            return None

    runtime = Runtime()
    source = KrakenPublicTriggerSource(Client(), poll_seconds=0.0)

    asyncio.run(source.pump(runtime))

    assert [event.kind for event in runtime.events] == [
        EggMoveKind.HEAD_TRIGGER_BASELINED,
        EggMoveKind.HEAD_HOOKED,
    ]
    assert runtime.events[0].reply is not None
    assert runtime.events[0].reply["reference_source"] == "bid"
    assert runtime.events[1].reply is not None
    assert runtime.events[1].reply["reference_price"] == 105.0


def test_repeat_activation_waits_for_fresh_head_price_baseline() -> None:
    pair = replace(
        sample_strategy()[0],
        head=HeadSpec(side=Side.SELL, order_type="M"),
        head_price=(5.0, 50.0),
        head_price_type="pD",
        amount_type="qAtDpD",
        try_num=2,
    )
    strategy_state = StrategyRuntime(
        strategy=StrategySpec(name="demo", pairs=(pair,)),
        symbol="PI_XBTUSD",
        simulate=False,
    ).state
    now = strategy_state.launched_at + timedelta(seconds=1)
    terminal = PairCycleState(
        pair=pair,
        head_state=HeadState.CLOSED,
        tail_state=TailState.CLOSED,
        played_quantity=Decimal("1"),
        attempt_index=1,
        head_trigger_reference_price=Decimal("100"),
        head_trigger_reference_source="bid",
        head_trigger_reference_at=now - timedelta(seconds=30),
    )
    chronos = StrategyRuntime(
        strategy=StrategySpec(name="demo", pairs=(pair,)),
        symbol="PI_XBTUSD",
        simulate=False,
    ).chronos
    chronos.state = replace(strategy_state, pairs={"pair-a": terminal})

    repeat_commands = chronos.process_event(
        EggMove(
            kind=EggMoveKind.PLAYED_AND_CANCELED,
            occurred_at=now,
            symbol="PI_XBTUSD",
            pair_name="pair-a",
            role=OrderRole.TAIL,
            reply={"cumQty": 1.0, "orderQty": 1.0},
            is_private=True,
        ),
        now=now,
    )

    assert repeat_commands == ()
    repeated = chronos.state.pairs["pair-a"]
    assert repeated.attempt_index == 2
    assert repeated.head_state == HeadState.LATENT
    assert repeated.head_trigger_reference_price is None

    class Market:
        best_ask = 210.0
        mid_price = 200.0
        last_price = None
        mark_price = None
        index_price = None
        tick_size = 0.5

        def __init__(self, best_bid: float, recorded_at: str) -> None:
            self.best_bid = best_bid
            self.recorded_at = recorded_at

    class Client:
        def __init__(self) -> None:
            self.markets = [
                Market(200.0, "repeat-baseline"),
                Market(204.5, "repeat-not-yet"),
                Market(205.0, "repeat-ready"),
            ]

        def fetch_market_state(self, symbol=None):
            return self.markets.pop(0) if self.markets else Market(205.0, "repeat-ready")

    class Runtime:
        symbol = "PI_XBTUSD"
        running = True

        def __init__(self, state) -> None:
            self.state = state
            self.events: list[EggMove] = []

        @property
        def all_pairs_terminal(self) -> bool:
            return bool(self.events) and self.events[-1].kind == EggMoveKind.HEAD_HOOKED

        @property
        def should_keep_sources_alive(self) -> bool:
            return False

        async def enqueue(self, event: EggMove) -> None:
            self.events.append(event)
            if event.kind == EggMoveKind.HEAD_TRIGGER_BASELINED:
                pair_state = self.state.pairs["pair-a"]
                reply = event.reply or {}
                self.state = replace(
                    self.state,
                    pairs={
                        "pair-a": replace(
                            pair_state,
                            head_trigger_reference_price=to_decimal(
                                cast(Any, reply["reference_price"])
                            ),
                            head_trigger_reference_source=str(
                                reply["reference_source"]
                            ),
                            head_trigger_reference_at=event.occurred_at,
                        )
                    },
                )

        def pair_state_for_record(
            self, record: object
        ) -> tuple[PairCycleState, OrderRole] | None:
            return None

    runtime = Runtime(chronos.state)
    source = KrakenPublicTriggerSource(Client(), poll_seconds=0.0)

    asyncio.run(source.pump(runtime))

    assert [event.kind for event in runtime.events] == [
        EggMoveKind.HEAD_TRIGGER_BASELINED,
        EggMoveKind.HEAD_HOOKED,
    ]
    assert runtime.events[0].reply is not None
    assert runtime.events[0].reply["reference_price"] == 200.0
    assert runtime.events[1].reply is not None
    assert runtime.events[1].reply["reference_price"] == 205.0


def test_public_polling_waits_for_bare_hook_dependency_before_baseline() -> None:
    origin = replace(
        sample_strategy()[0],
        name="repS",
        head=HeadSpec(side=Side.SELL, order_type="M"),
        head_price=(5.0, 50.0),
        head_price_type="pD",
        amount_type="qAtDpD",
    )
    dependent = replace(
        sample_strategy()[0],
        name="repS-lk",
        head=HeadSpec(side=Side.BUY, order_type="M"),
        head_price=(-50.0, -5.0),
        head_price_type="pD",
        amount_type="qAtDpD",
        hook_name="repS",
    )

    class Market:
        best_bid = 99.0
        best_ask = 100.0
        mid_price = 99.5
        last_price = None
        mark_price = None
        index_price = None
        tick_size = 0.5

        def __init__(self, recorded_at: str) -> None:
            self.recorded_at = recorded_at

    class Client:
        def __init__(self, runtime=None) -> None:
            self.count = 0
            self.runtime = runtime

        def fetch_market_state(self, symbol=None):
            self.count += 1
            if self.runtime is not None:
                self.runtime.running = False
            return Market(f"tick-{self.count}")

    class Runtime:
        symbol = "PI_XBTUSD"
        running = True

        def __init__(self) -> None:
            self.state = StrategyRuntime(
                strategy=StrategySpec(name="demo", pairs=(origin, dependent)),
                symbol="PI_XBTUSD",
                simulate=False,
            ).state
            self.events: list[EggMove] = []

        @property
        def all_pairs_terminal(self) -> bool:
            return len(self.events) >= 1

        @property
        def should_keep_sources_alive(self) -> bool:
            return False

        async def enqueue(self, event: EggMove) -> None:
            self.events.append(event)

        def pair_state_for_record(
            self, record: object
        ) -> tuple[PairCycleState, OrderRole] | None:
            return None

    runtime = Runtime()
    source = KrakenPublicTriggerSource(Client(runtime), poll_seconds=0.0)
    asyncio.run(source.pump(runtime))

    assert {event.pair_name for event in runtime.events} == {"repS"}

    runtime = Runtime()
    runtime.state = replace(
        runtime.state,
        pairs={
            "repS": replace(
                runtime.state.pairs["repS"],
                head_state=HeadState.CLOSED,
                tail_state=TailState.CLOSED,
                played_quantity=Decimal("1"),
            ),
            "repS-lk": runtime.state.pairs["repS-lk"],
        },
    )
    source = KrakenPublicTriggerSource(Client(runtime), poll_seconds=0.0)
    asyncio.run(source.pump(runtime))

    assert runtime.events == []

    runtime = Runtime()
    runtime.state = replace(
        runtime.state,
        pairs={
            "repS": replace(
                runtime.state.pairs["repS"],
                head_state=HeadState.CLOSED,
                tail_state=TailState.CLOSED,
                played_quantity=Decimal("1"),
            ),
            "repS-lk": replace(
                runtime.state.pairs["repS-lk"],
                dependency_evidence=(
                    _close_evidence(
                        "repS",
                        1,
                        datetime.now(timezone.utc),
                    ),
                ),
            ),
        },
    )
    source = KrakenPublicTriggerSource(Client(runtime), poll_seconds=0.0)
    asyncio.run(source.pump(runtime))

    assert any(event.pair_name == "repS-lk" for event in runtime.events)


def test_public_polling_hooks_cross_exchange_chained_child_on_child_route() -> None:
    origin = replace(
        sample_strategy()[0],
        name="kraken-origin",
        symbol="PI_XBTUSD",
        exchange="kraken",
        market_type="futures",
    )
    child = replace(
        sample_strategy()[0],
        name="binance-child",
        hook_name="kraken-origin-tail-closed",
        symbol="BTCUSDT",
        exchange="binance",
        market_type="spot",
    )
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="cross-exchange-chain", pairs=(origin, child)),
        symbol="PI_XBTUSD",
        exchange="kraken",
        market_type="futures",
        simulate=False,
    )
    closed_at = runtime.state.launched_at + timedelta(minutes=1)
    released_state = replace(
        runtime.state,
        pairs={
            "kraken-origin": PairCycleState(
                pair=origin,
                head_state=HeadState.CLOSED,
                tail_state=TailState.CLOSED,
                played_quantity=Decimal("1"),
            ),
            "binance-child": runtime.state.pairs["binance-child"],
        },
    )
    runtime.chronos.state = released_state
    runtime.chronos.process_event(
        EggMove(
            kind=EggMoveKind.PLAYED_AND_CANCELED,
            occurred_at=closed_at,
            symbol="PI_XBTUSD",
            pair_name="kraken-origin",
            role=OrderRole.TAIL,
            reply={"orderID": "OID-ORIGIN-TAIL", "ordStatus": "Filled", "cumQty": "1"},
            event_id="origin-tail-closed",
            is_private=True,
        )
    )
    assert runtime.chronos.state.pairs["binance-child"].dependency_evidence

    class Market:
        best_bid = 100.0
        best_ask = 100.5
        mid_price = 100.25
        last_price = 100.4
        mark_price = None
        index_price = None
        tick_size = 0.01

        def __init__(self, recorded_at: str) -> None:
            self.recorded_at = recorded_at

    class Client:
        def __init__(self, runtime_obj) -> None:
            self.runtime = runtime_obj
            self.calls: list[tuple[str | None, str | None, str | None]] = []

        def fetch_market_state(
            self,
            symbol=None,
            exchange=None,
            market_type=None,
        ):
            self.calls.append((exchange, market_type, symbol))
            self.runtime.running = False
            return Market(f"{exchange}:{market_type}:{symbol}")

    class PublicRuntime:
        symbol = "PI_XBTUSD"
        exchange = "kraken"
        market_type = "futures"
        running = True
        all_pairs_terminal = False
        should_keep_sources_alive = False

        def __init__(self, state) -> None:
            self.strategy = StrategySpec(
                name="cross-exchange-chain",
                pairs=(origin, child),
            )
            self.state = state
            self.events: list[EggMove] = []

        async def enqueue(self, event: EggMove) -> None:
            self.events.append(event)

        def pair_state_for_record(
            self,
            _record: object,
        ) -> tuple[PairCycleState, OrderRole] | None:
            return None

    public_runtime = PublicRuntime(runtime.chronos.state)
    client = Client(public_runtime)
    source = KrakenPublicTriggerSource(client, poll_seconds=0.0)

    asyncio.run(source.pump(public_runtime))

    assert ("binance", "spot", "BTCUSDT") in client.calls
    assert [event.pair_name for event in public_runtime.events] == ["binance-child"]
    child_commands = runtime.chronos.process_event(public_runtime.events[0])
    assert len(child_commands) == 1
    command = child_commands[0]
    assert isinstance(command, PlaceHeadCommand)
    assert command.pair_name == "binance-child"
    assert command.exchange == "binance"
    assert command.market_type == "spot"
    assert str(command.symbol) == "BTCUSDT"


def test_market_tick_reaches_horus_as_tail_amend_command() -> None:
    pair = sample_strategy()[0]
    confirmed_at = datetime.now(timezone.utc)
    trail = replace(
        initial_tail_trail(pair, Decimal("100"), confirmed_at),
        confirmed_stop_price=Decimal("99.0"),
        first_unblocked_at=confirmed_at - timedelta(seconds=50),
        last_confirmed_at=confirmed_at,
    )
    state = PairCycleState(
        pair=pair,
        head_state=HeadState.LIVING,
        tail_state=TailState.LIVING,
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
        state,
        EggMove(
            kind=EggMoveKind.MARKET_TICK,
            occurred_at=datetime.now(timezone.utc),
            symbol="PI_XBTUSD",
            pair_name="pair-a",
            reply={"reference_price": 102.0},
        ),
    )
    commands = plan_runtime_commands(next_state, intents, symbol=Symbol("PI_XBTUSD"))

    assert len(commands) == 1
    assert isinstance(commands[0], AmendTailCommand)
    assert commands[0].kind == RuntimeCommandKind.AMEND
    assert commands[0].request.newPrice is not None
    assert commands[0].request.newPrice > Decimal("100")


def test_tail_telemetry_rows_include_distance_and_last_update() -> None:
    class Market:
        best_bid = 102.0
        best_ask = 102.5
        mid_price = 102.25
        last_price = 102.0
        mark_price = None
        index_price = None
        tick_size = 0.5
        recorded_at = "tick-1"

    class Reader:
        def fetch_market_state(self, symbol=None):
            return Market()

    pair = sample_strategy()[0]
    confirmed_at = datetime.now(timezone.utc)
    trail = replace(
        initial_tail_trail(pair, Decimal("100"), confirmed_at),
        confirmed_stop_price=Decimal("99.0"),
        last_confirmed_at=confirmed_at,
    )
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="demo", pairs=(pair,)),
        symbol="PI_XBTUSD",
        simulate=False,
        public_state_reader=Reader(),
    )
    runtime.state = replace(
        runtime.state,
        pairs={
            "pair-a": PairCycleState(
                pair=pair,
                head_state=HeadState.CLOSED,
                tail_state=TailState.LIVING,
                tail_mode=None,
                tail_trail=trail,
                played_quantity=Decimal("1"),
            )
        },
    )

    rows = runtime._collect_tail_telemetry_rows(datetime.now(timezone.utc))

    assert len(rows) == 1
    row = rows[0]
    assert row.initial_distance == float(trail.baseline_width)
    assert row.current_distance == float(Decimal("102.0") - Decimal("99.0"))


def test_tail_telemetry_write_failure_does_not_stop_runtime_source(caplog) -> None:
    class Market:
        best_bid = 102.0
        best_ask = 102.5
        mid_price = 102.25
        last_price = 102.0
        mark_price = None
        index_price = None
        tick_size = 0.5
        recorded_at = "tick-1"

    class Reader:
        def fetch_market_state(self, symbol=None):
            return Market()

    class Writer:
        def record_rows(self, rows):
            raise RuntimeError("telemetry write failed")

    pair = sample_strategy()[0]
    confirmed_at = datetime.now(timezone.utc)
    trail = replace(
        initial_tail_trail(pair, Decimal("100"), confirmed_at),
        confirmed_stop_price=Decimal("99.0"),
        last_confirmed_at=confirmed_at,
    )
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="demo", pairs=(pair,)),
        symbol="PI_XBTUSD",
        simulate=False,
        public_state_reader=Reader(),
        tail_telemetry_writer=Writer(),
        tail_telemetry_interval_seconds=1.0,
    )
    runtime.state = replace(
        runtime.state,
        pairs={
            "pair-a": PairCycleState(
                pair=pair,
                head_state=HeadState.CLOSED,
                tail_state=TailState.LIVING,
                tail_trail=trail,
                played_quantity=Decimal("1"),
            )
        },
    )

    async def _run() -> bool:
        runtime.running = True
        with caplog.at_level("WARNING", logger="kola"):
            task = asyncio.create_task(runtime._pump_tail_telemetry())
            await asyncio.sleep(0.05)
            still_running = not task.done()
            runtime.running = False
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return still_running

    assert asyncio.run(_run()) is True
    assert "tail telemetry persistence failed" in caplog.text


def test_update_log_closed_hooked_includes_head_fill_fields(caplog) -> None:
    pair = sample_strategy()[0]
    now = datetime.now(timezone.utc)
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="demo", pairs=(pair,)),
        symbol="PI_XBTUSD",
        simulate=True,
    )
    runtime._record_head_lifecycle(
        EggMove(
            kind=EggMoveKind.PLAYED_NOT_CANCELED,
            occurred_at=now,
            symbol="PI_XBTUSD",
            pair_name="pair-a",
            role=OrderRole.HEAD,
            reply={"side": "buy", "cumQty": 1.0, "price": 100.0},
        )
    )
    previous = PairCycleState(
        pair=pair,
        head_state=HeadState.CLOSED,
        tail_state=None,
        played_quantity=Decimal("1"),
    )
    current = PairCycleState(
        pair=pair,
        head_state=HeadState.CLOSED,
        tail_state=TailState.HOOKED,
        tail_trail=initial_tail_trail(pair, Decimal("100"), now),
        played_quantity=Decimal("1"),
    )
    runtime.state = replace(runtime.state, pairs={"pair-a": current})

    with caplog.at_level("INFO", logger="kola"):
        runtime._log_living_updates({"pair-a": previous})

    assert "UPDATE (pair-a#1): closed--hooked" in caplog.text
    assert "buy 1.00 100.00" in caplog.text
    assert "HFS=" not in caplog.text
    assert "HFQ=" not in caplog.text
    assert "HFP=" not in caplog.text


def test_update_log_closed_submitted_omits_head_fill_fields(caplog) -> None:
    pair = sample_strategy()[0]
    now = datetime.now(timezone.utc)
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="demo", pairs=(pair,)),
        symbol="PI_XBTUSD",
        simulate=True,
    )
    trail = replace(
        initial_tail_trail(pair, Decimal("100"), now),
        confirmed_stop_price=Decimal("99.0"),
        last_confirmed_at=now,
    )
    previous = PairCycleState(
        pair=pair,
        head_state=HeadState.CLOSED,
        tail_state=TailState.HOOKED,
        tail_trail=trail,
        played_quantity=Decimal("1"),
    )
    current = PairCycleState(
        pair=pair,
        head_state=HeadState.CLOSED,
        tail_state=TailState.SUBMITTED,
        tail_identity=OrderIdentity(
            pair_name="pair-a",
            role="tail",
            client_order_id="CID-T",
            exchange_order_id="OID-T",
        ),
        tail_trail=trail,
        played_quantity=Decimal("1"),
    )
    runtime.state = replace(runtime.state, pairs={"pair-a": current})

    with caplog.at_level("INFO", logger="kola"):
        runtime._log_living_updates({"pair-a": previous})

    assert "UPDATE (pair-a#1): closed--submitted" in caplog.text
    assert "1 99.00 99.00 CID-T OID-T" in caplog.text
    assert "TCID=" not in caplog.text
    assert "TOID=" not in caplog.text
    assert "HFS=" not in caplog.text


def test_update_log_closed_tail_includes_tail_fill_fields(caplog) -> None:
    pair = sample_strategy()[0]
    now = datetime.now(timezone.utc)
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="demo", pairs=(pair,)),
        symbol="PI_XBTUSD",
        simulate=True,
    )
    trail = replace(
        initial_tail_trail(pair, Decimal("100"), now),
        confirmed_stop_price=Decimal("99.0"),
        current_stop_price=Decimal("99.0"),
        last_confirmed_at=now,
    )
    runtime._record_head_lifecycle(
        EggMove(
            kind=EggMoveKind.PLAYED_AND_CANCELED,
            occurred_at=now,
            symbol="PI_XBTUSD",
            pair_name="pair-a",
            role=OrderRole.TAIL,
            reply={"cumQty": 1.0, "price": 98.5},
        )
    )
    previous = PairCycleState(
        pair=pair,
        head_state=HeadState.CLOSED,
        tail_state=TailState.LIVING,
        tail_trail=trail,
        played_quantity=Decimal("1"),
    )
    current = replace(previous, tail_state=TailState.CLOSED)
    runtime.state = replace(runtime.state, pairs={"pair-a": current})

    with caplog.at_level("INFO", logger="kola"):
        runtime._log_living_updates({"pair-a": previous})

    assert "UPDATE (pair-a#1): closed--closed" in caplog.text
    assert "sell 1.00 98.50" in caplog.text
    assert "TFS=" not in caplog.text
    assert "TFQ=" not in caplog.text
    assert "TFP=" not in caplog.text


def test_update_log_emits_when_desired_stop_changes_only(caplog) -> None:
    pair = sample_strategy()[0]
    now = datetime.now(timezone.utc)
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="demo", pairs=(pair,)),
        symbol="PI_XBTUSD",
        simulate=True,
    )
    previous_trail = replace(
        initial_tail_trail(pair, Decimal("100"), now),
        confirmed_stop_price=Decimal("99.0"),
        current_stop_price=Decimal("99.0"),
        last_confirmed_at=now,
    )
    current_trail = replace(previous_trail, current_stop_price=Decimal("98.0"))
    previous = PairCycleState(
        pair=pair,
        head_state=HeadState.CLOSED,
        tail_state=TailState.SUBMITTED,
        tail_identity=OrderIdentity(
            pair_name="pair-a",
            role="tail",
            client_order_id="CID-T",
            exchange_order_id="OID-T",
        ),
        tail_trail=previous_trail,
        played_quantity=Decimal("1"),
    )
    current = replace(previous, tail_trail=current_trail)
    runtime.state = replace(runtime.state, pairs={"pair-a": current})

    with caplog.at_level("INFO", logger="kola"):
        runtime._log_living_updates({"pair-a": previous})

    assert "UPDATE (pair-a#1): closed--submitted" in caplog.text
    assert "1 99.00 98.00 CID-T OID-T" in caplog.text
    assert "CS=" not in caplog.text
    assert "DS=" not in caplog.text


def test_amend_dispatch_logs_and_tracks_pending_confirmation(caplog) -> None:
    pair = sample_strategy()[0]
    now = datetime.now(timezone.utc)
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="demo", pairs=(pair,)),
        symbol="PI_XBTUSD",
        simulate=False,
    )
    trail = replace(
        initial_tail_trail(pair, Decimal("100"), now),
        confirmed_stop_price=Decimal("99.0"),
        current_stop_price=Decimal("99.0"),
        last_confirmed_at=now,
    )
    runtime.state = replace(
        runtime.state,
        pairs={
            "pair-a": PairCycleState(
                pair=pair,
                head_state=HeadState.CLOSED,
                tail_state=TailState.LIVING,
                tail_identity=OrderIdentity(
                    pair_name="pair-a",
                    role="tail",
                    client_order_id="CID-T",
                    exchange_order_id="OID-T",
                ),
                tail_trail=trail,
                played_quantity=Decimal("1"),
            )
        },
    )
    command = AmendTailCommand(
        kind=RuntimeCommandKind.AMEND,
        symbol=Symbol("PI_XBTUSD"),
        pair_name="pair-a",
        request=AmendOrderCommandRequest(
            pair_name="pair-a",
            side="sell",
            ordType="Stop",
            orderID="OID-T",
            clOrdID="CID-T",
            newPrice=Decimal("98.5"),
        ),
    )
    slot = _CommandSlot(pair_name="pair-a", attempt_index=1, role="tail")
    identity = runtime._command_identity_from_command(command)

    with caplog.at_level("INFO", logger="kola"):
        runtime._on_command_dispatched(slot, command, identity)

    assert "AMEND_SENT (pair-a#1):" in caplog.text
    assert "AMEND_SENT (pair-a#1): 99.00 98.50" in caplog.text
    assert "CS=" not in caplog.text
    assert "DS=" not in caplog.text
    assert slot in runtime._pending_tail_amends


def test_rejected_live_tail_amend_clears_pending_confirmation(caplog) -> None:
    pair = sample_strategy()[0]
    now = datetime.now(timezone.utc)
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="demo", pairs=(pair,)),
        symbol="PI_XBTUSD",
        simulate=False,
    )
    trail = replace(
        initial_tail_trail(pair, Decimal("100"), now),
        confirmed_stop_price=Decimal("99.0"),
        current_stop_price=Decimal("99.0"),
        last_confirmed_at=now,
    )
    runtime.state = replace(
        runtime.state,
        pairs={
            "pair-a": PairCycleState(
                pair=pair,
                head_state=HeadState.CLOSED,
                tail_state=TailState.LIVING,
                tail_identity=OrderIdentity(
                    pair_name="pair-a",
                    role="tail",
                    client_order_id="CID-T",
                    exchange_order_id="OID-T",
                ),
                tail_trail=trail,
                played_quantity=Decimal("1"),
            )
        },
    )
    command = AmendTailCommand(
        kind=RuntimeCommandKind.AMEND,
        symbol=Symbol("PI_XBTUSD"),
        pair_name="pair-a",
        request=AmendOrderCommandRequest(
            pair_name="pair-a",
            side="sell",
            ordType="Stop",
            orderID="OID-T",
            clOrdID="CID-T",
            newPrice=Decimal("98.5"),
        ),
    )
    slot = _CommandSlot(pair_name="pair-a", attempt_index=1, role="tail")
    runtime._pending_tail_amends[slot] = _TailAmendPending(
        pair_name="pair-a",
        attempt_index=1,
        desired_stop_price=Decimal("98.5"),
        client_order_id="CID-T",
        exchange_order_id="OID-T",
        started_at=now,
        deadline_at=now + timedelta(seconds=20),
    )

    with caplog.at_level("WARNING", logger="kola"):
        runtime._record_live_ack(command, OrderAck(order_id="OID-T", status="Rejected"))

    assert slot not in runtime._pending_tail_amends
    assert "AMEND_REJECTED (pair-a#1):" in caplog.text


def test_pending_amend_warns_when_db_confirmation_is_late(caplog) -> None:
    pair = sample_strategy()[0]
    now = datetime.now(timezone.utc)
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="demo", pairs=(pair,)),
        symbol="PI_XBTUSD",
        simulate=False,
    )
    trail = replace(
        initial_tail_trail(pair, Decimal("100"), now),
        confirmed_stop_price=Decimal("99.0"),
        current_stop_price=Decimal("98.0"),
        last_confirmed_at=now,
    )
    runtime.state = replace(
        runtime.state,
        pairs={
            "pair-a": PairCycleState(
                pair=pair,
                head_state=HeadState.CLOSED,
                tail_state=TailState.LIVING,
                tail_identity=OrderIdentity(
                    pair_name="pair-a",
                    role="tail",
                    client_order_id="CID-T",
                    exchange_order_id="OID-T",
                ),
                tail_trail=trail,
                played_quantity=Decimal("1"),
            )
        },
    )
    slot = _CommandSlot(pair_name="pair-a", attempt_index=1, role="tail")
    runtime._pending_tail_amends[slot] = _TailAmendPending(
        pair_name="pair-a",
        attempt_index=1,
        desired_stop_price=Decimal("98.5"),
        client_order_id="CID-T",
        exchange_order_id="OID-T",
        started_at=now - timedelta(seconds=25),
        deadline_at=now - timedelta(seconds=1),
    )

    with caplog.at_level("WARNING", logger="kola"):
        runtime._check_tail_amend_deadlines(now)

    assert "AMEND_PENDING (pair-a#1):" in caplog.text
    assert slot not in runtime._pending_tail_amends


def test_tail_visibility_deadline_warns_without_stopping_runtime(caplog) -> None:
    pair = sample_strategy()[0]
    now = datetime.now(timezone.utc)
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="demo", pairs=(pair,)),
        symbol="PI_XBTUSD",
        simulate=False,
    )
    runtime.state = replace(
        runtime.state,
        pairs={
            "pair-a": PairCycleState(
                pair=pair,
                head_state=HeadState.CLOSED,
                tail_state=TailState.HOOKED,
                played_quantity=Decimal("1"),
            )
        },
    )
    slot = _CommandSlot(pair_name="pair-a", attempt_index=1, role="tail")
    runtime._pending_tail_visibility[slot] = _TailVisibilityWindow(
        pair_name="pair-a",
        attempt_index=1,
        client_order_id="CID-T",
        exchange_order_id="OID-T",
        started_at=now - timedelta(seconds=65),
        deadline_at=now - timedelta(seconds=45),
    )

    with caplog.at_level("WARNING", logger="kola"):
        runtime._check_tail_visibility_deadlines(now)

    assert "TAIL_PENDING (pair-a#1):" in caplog.text
    assert slot in runtime._pending_tail_visibility


def test_late_tail_visibility_window_clears_after_private_identity(caplog) -> None:
    pair = sample_strategy()[0]
    now = datetime.now(timezone.utc)
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="demo", pairs=(pair,)),
        symbol="PI_XBTUSD",
        simulate=False,
    )
    runtime.state = replace(
        runtime.state,
        pairs={
            "pair-a": PairCycleState(
                pair=pair,
                head_state=HeadState.CLOSED,
                tail_state=TailState.LIVING,
                tail_identity=OrderIdentity(
                    pair_name="pair-a",
                    role="tail",
                    client_order_id="CID-T",
                    exchange_order_id="OID-T",
                ),
                played_quantity=Decimal("1"),
            )
        },
    )
    slot = _CommandSlot(pair_name="pair-a", attempt_index=1, role="tail")
    runtime._pending_tail_visibility[slot] = _TailVisibilityWindow(
        pair_name="pair-a",
        attempt_index=1,
        client_order_id="CID-T",
        exchange_order_id="OID-T",
        started_at=now - timedelta(seconds=65),
        deadline_at=now - timedelta(seconds=45),
        last_warned_at=now - timedelta(seconds=20),
    )

    with caplog.at_level("INFO", logger="kola"):
        runtime._check_tail_visibility_deadlines(now)

    assert "TAIL_VISIBLE (pair-a#1):" in caplog.text
    assert slot not in runtime._pending_tail_visibility


def test_private_lease_open_replay_is_debug_but_close_stays_info(caplog) -> None:
    pair = sample_strategy()[0]
    now = datetime.now(timezone.utc)

    def runtime_with_submitted_tail() -> StrategyRuntime:
        runtime = StrategyRuntime(
            strategy=StrategySpec(name="demo", pairs=(pair,)),
            symbol="PI_XBTUSD",
            simulate=False,
        )
        runtime.state = replace(
            runtime.state,
            pairs={
                "pair-a": PairCycleState(
                    pair=pair,
                    head_state=HeadState.CLOSED,
                    tail_state=TailState.SUBMITTED,
                    played_quantity=Decimal("1"),
                )
            },
        )
        return runtime

    def private_tail_status(status: str) -> EggMove:
        return EggMove(
            kind=EggMoveKind.NOT_PLAYED_NOR_CANCELED,
            occurred_at=now,
            symbol="PI_XBTUSD",
            pair_name="pair-a",
            role=OrderRole.TAIL,
            reply={
                "clOrdID": "CID-T",
                "orderID": "OID-T",
                "ordStatus": status,
                "side": "sell",
                "orderQty": "1",
                "price": "99",
            },
            is_private=True,
        )

    runtime = runtime_with_submitted_tail()
    with caplog.at_level("INFO", logger="kola"):
        runtime._record_private_event_for_leases(private_tail_status("open"))

    assert "LEASE_OPEN (pair-a#1):" not in caplog.text

    caplog.clear()
    with caplog.at_level("INFO", logger="kola"):
        runtime._record_private_event_for_leases(private_tail_status("filled"))

    assert "LEASE_CLOSED (pair-a#1):" in caplog.text

    debug_runtime = runtime_with_submitted_tail()
    caplog.clear()
    with caplog.at_level("DEBUG", logger="kola"):
        debug_runtime._record_private_event_for_leases(private_tail_status("open"))

    assert "LEASE_OPEN (pair-a#1):" in caplog.text


def test_runtime_matches_private_record_to_tail_identity() -> None:
    pair = sample_strategy()[0]
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="demo", pairs=(pair,)),
        symbol="PI_XBTUSD",
        simulate=False,
    )
    runtime.state = replace(
        runtime.state,
        pairs={
            "pair-a": PairCycleState(
                pair=pair,
                head_state=HeadState.CLOSED,
                tail_state=TailState.SUBMITTED,
                tail_identity=OrderIdentity(
                    pair_name="pair-a",
                    role="tail",
                    client_order_id="CID-T",
                    exchange_order_id="OID-T",
                ),
                played_quantity=Decimal("1"),
            )
        },
    )

    class _Record:
        client_order_id = "CID-T"
        exchange_order_id = "OID-T"

    matched = runtime.pair_state_for_record(_Record())

    assert matched is not None
    assert matched[1] == OrderRole.TAIL
