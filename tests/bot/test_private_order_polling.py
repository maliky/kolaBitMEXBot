from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Protocol, cast

from kolabi.bot.domain import (
    EggMove,
    HeadSpec,
    OrderIdentity,
    OrderPairSpec,
    OrderRole,
    PairCycleState,
    Side,
    StrategyState,
    TailSpec,
    TimeWindow,
)
from kolabi.bot.strategy_runtime import (
    KrakenPrivateOrderPollingSource,
    _CommandSlot,
    _PendingPrivateRecord,
    _TailAmendPending,
)
from kolabi.shared.core.runtime_types import PrivateOrderRecord
from kolabi.shared.persistence import Base, ExchangeOrder
from kolabi.shared.runtime_state import KrakenRuntimeStateClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session


def sample_pair(name: str) -> OrderPairSpec:
    return OrderPairSpec(
        name=name,
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
        head_quantity=3,
        head_quantity_type=pair.head_quantity_type,
        tail=pair.tail,
        tail_price_spec=148.89,
        tail_price_spec_type="tB",
        amount_type="qAtBpB",
    )


def test_private_order_poller_emits_head_confirmation_from_db(postgres_url_factory) -> None:
    market_db = postgres_url_factory("pub")
    account_db = postgres_url_factory("prv")
    market_engine = create_engine(market_db)
    account_engine = create_engine(account_db)
    Base.metadata.create_all(market_engine)
    Base.metadata.create_all(account_engine)
    now = datetime.now(timezone.utc)
    with Session(account_engine) as session:
        session.add(
            ExchangeOrder(
                local_uuid="ord-1",
                exchange="kraken",
                environment="demo",
                market_type="futures",
                account_scope="default",
                symbol="PI_XBTUSD",
                exchange_order_id="OID-H",
                client_order_id="CID-H",
                side="buy",
                order_type="limit",
                status="partially_filled",
                price=100.0,
                quantity=1.0,
                filled_quantity=0.5,
                reduce_only=False,
                source_timestamp=now,
                local_timestamp=now,
            )
        )
        session.commit()

    client = KrakenRuntimeStateClient(
        market_db_url=market_db,
        account_db_url=account_db,
        symbol="PI_XBTUSD",
    )
    source = KrakenPrivateOrderPollingSource(client, poll_seconds=0.0)
    emitted: list[EggMove] = []

    class _RuntimeLike(Protocol):
        strategy: object
        symbol: str
        running: bool
        state: StrategyState

        @property
        def all_pairs_terminal(self) -> bool: ...

        @property
        def should_keep_sources_alive(self) -> bool: ...

        async def enqueue(self, event: object) -> None: ...

        def pair_state_for_record(
            self, record: object
        ) -> tuple[PairCycleState, OrderRole] | None: ...

    class _Runtime:
        def __init__(self) -> None:
            self.strategy = object()
            self.symbol = "PI_XBTUSD"
            self.running = True
            self.state = StrategyState(
                launched_at=now,
                strategy_id="demo",
                pairs={
                    "pair-a": PairCycleState(
                        pair=sample_pair("pair-a"),
                        head_identity=OrderIdentity(
                            pair_name="pair-a",
                            role="head",
                            client_order_id="CID-H",
                            exchange_order_id="OID-H",
                        ),
                    )
                },
            )

        @property
        def all_pairs_terminal(self) -> bool:
            return False

        @property
        def should_keep_sources_alive(self) -> bool:
            return False

        async def enqueue(self, event) -> None:
            emitted.append(cast(EggMove, event))
            self.running = False

        def pair_state_for_record(self, record) -> tuple[PairCycleState, OrderRole] | None:
            return self.state.pairs["pair-a"], OrderRole.HEAD

    runtime: _RuntimeLike = _Runtime()
    asyncio.run(source.pump(runtime))

    assert len(emitted) == 1
    assert emitted[0].is_private is True
    assert emitted[0].kind.value == "played_not_canceled"


def test_private_order_poller_waits_for_private_fill_reference_price() -> None:
    now = datetime.now(timezone.utc)
    order_record = PrivateOrderRecord(
        symbol="PI_XBTUSD",
        status="filled",
        exchange_order_id="OID-H",
        client_order_id="CID-H",
        quantity=3.0,
        filled_quantity=3.0,
        local_id=1,
        local_timestamp=now.isoformat(),
    )
    fill_record = PrivateOrderRecord(
        symbol="PI_XBTUSD",
        status="filled",
        exchange_order_id="OID-H",
        client_order_id="CID-H",
        price=75966.5,
        quantity=3.0,
        filled_quantity=3.0,
        local_id=2,
        local_timestamp=(now.replace(microsecond=0) + timedelta(seconds=1)).isoformat(),
    )

    class _Client:
        def __init__(self) -> None:
            self.order_calls = 0
            self.fill_calls = 0

        def fetch_private_orders_since(self, **_kwargs):
            self.order_calls += 1
            if self.order_calls > 1:
                return ()
            return (order_record,)

        def fetch_private_fills_since(self, **_kwargs):
            self.fill_calls += 1
            if self.fill_calls == 1:
                return ()
            return (fill_record,)

    class _Runtime:
        def __init__(self) -> None:
            self.symbol = "PI_XBTUSD"
            self.running = True
            self.state = StrategyState(
                launched_at=now,
                strategy_id="demo",
                pairs={
                    "pair-a": PairCycleState(
                        pair=logbps_tail_pair("pair-a"),
                        head_identity=OrderIdentity(
                            pair_name="pair-a",
                            role="head",
                            client_order_id="CID-H",
                            exchange_order_id="OID-H",
                        ),
                    )
                },
            )

        @property
        def all_pairs_terminal(self) -> bool:
            return False

        @property
        def should_keep_sources_alive(self) -> bool:
            return False

        async def enqueue(self, event) -> None:
            emitted.append(cast(EggMove, event))
            self.running = False

        def pair_state_for_record(self, record) -> tuple[PairCycleState, OrderRole] | None:
            return self.state.pairs["pair-a"], OrderRole.HEAD

    emitted: list[EggMove] = []
    client = _Client()
    source = KrakenPrivateOrderPollingSource(client, poll_seconds=0.0)
    runtime = _Runtime()

    asyncio.run(source.pump(runtime))

    assert emitted[0].reply is not None
    assert emitted[0].reply["reference_price"] == 75966.5
    assert client.fill_calls >= 2


def test_private_order_poller_prefers_fill_price_for_reference_price() -> None:
    now = datetime.now(timezone.utc)
    record = PrivateOrderRecord(
        symbol="PI_XBTUSD",
        status="filled",
        exchange_order_id="OID-H",
        client_order_id="CID-H",
        price=75966.5,
        quantity=7.0,
        filled_quantity=7.0,
        local_id=3,
        local_timestamp=now.isoformat(),
    )

    class _Client:
        def __init__(self) -> None:
            self.sent = False

        def fetch_private_orders_since(self, **_kwargs):
            if self.sent:
                return ()
            self.sent = True
            return (record,)

        def fetch_private_fills_since(self, **_kwargs):
            return ()

    class _Runtime:
        def __init__(self) -> None:
            self.symbol = "PI_XBTUSD"
            self.running = True
            self.state = StrategyState(
                launched_at=now,
                strategy_id="demo",
                pairs={
                    "pair-a": PairCycleState(
                        pair=sample_pair("pair-a"),
                        head_identity=OrderIdentity(
                            pair_name="pair-a",
                            role="head",
                            client_order_id="CID-H",
                            exchange_order_id="OID-H",
                        ),
                    )
                },
            )

        @property
        def all_pairs_terminal(self) -> bool:
            return False

        @property
        def should_keep_sources_alive(self) -> bool:
            return False

        async def enqueue(self, event) -> None:
            emitted.append(cast(EggMove, event))
            self.running = False

        def pair_state_for_record(self, record) -> tuple[PairCycleState, OrderRole] | None:
            return self.state.pairs["pair-a"], OrderRole.HEAD

    emitted: list[EggMove] = []
    source = KrakenPrivateOrderPollingSource(_Client(), poll_seconds=0.0)

    asyncio.run(source.pump(_Runtime()))

    assert emitted[0].reply is not None
    assert emitted[0].reply["reference_price"] == 75966.5


def test_private_order_poller_retries_fresh_unmatched_head_fill() -> None:
    now = datetime.now(timezone.utc)
    record = PrivateOrderRecord(
        symbol="PI_XBTUSD",
        status="filled",
        exchange_order_id="OID-H",
        client_order_id="CID-H",
        quantity=3.0,
        filled_quantity=3.0,
        local_id=7,
        local_timestamp=now.isoformat(),
    )

    class _Client:
        def __init__(self) -> None:
            self.calls = 0

        def fetch_private_orders_since(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return (record,)
            return ()

        def fetch_private_fills_since(self, **_kwargs):
            return ()

    class _Runtime:
        def __init__(self) -> None:
            self.symbol = "PI_XBTUSD"
            self.running = True
            self.match_attempts = 0
            self.state = StrategyState(
                launched_at=now,
                strategy_id="demo",
                pairs={
                    "pair-a": PairCycleState(
                        pair=sample_pair("pair-a"),
                        head_identity=OrderIdentity(
                            pair_name="pair-a",
                            role="head",
                            client_order_id="CID-H",
                            exchange_order_id="OID-H",
                        ),
                    )
                },
            )

        @property
        def all_pairs_terminal(self) -> bool:
            return False

        @property
        def should_keep_sources_alive(self) -> bool:
            return False

        async def enqueue(self, event) -> None:
            emitted.append(cast(EggMove, event))
            self.running = False

        def pair_state_for_record(self, record) -> tuple[PairCycleState, OrderRole] | None:
            self.match_attempts += 1
            if self.match_attempts == 1:
                return None
            return self.state.pairs["pair-a"], OrderRole.HEAD

    emitted: list[EggMove] = []
    client = _Client()
    runtime = _Runtime()
    source = KrakenPrivateOrderPollingSource(client, poll_seconds=0.0)

    asyncio.run(source.pump(runtime))

    assert client.calls == 2
    assert len(emitted) == 1
    assert emitted[0].kind.value == "played_and_canceled"


def test_private_order_poller_prunes_stale_unmatched_records() -> None:
    now = datetime.now(timezone.utc)
    stale_record = PrivateOrderRecord(
        symbol="PI_XBTUSD",
        status="open",
        exchange_order_id="OID-STALE",
        client_order_id="CID-STALE",
        local_id=8,
        local_timestamp=(now - timedelta(minutes=5)).isoformat(),
    )

    class _Client:
        def fetch_private_orders_since(self, **_kwargs):
            return ()

        def fetch_private_fills_since(self, **_kwargs):
            return ()

    class _Runtime:
        def __init__(self) -> None:
            self.symbol = "PI_XBTUSD"
            self.running = True
            self.state = StrategyState(
                launched_at=now,
                strategy_id="demo",
                pairs={
                    "pair-a": PairCycleState(
                        pair=sample_pair("pair-a"),
                    )
                },
            )

        @property
        def all_pairs_terminal(self) -> bool:
            return True

        @property
        def should_keep_sources_alive(self) -> bool:
            return False

        async def enqueue(self, event) -> None:
            raise AssertionError(f"unexpected event {event!r}")

        def pair_state_for_record(self, record) -> tuple[PairCycleState, OrderRole] | None:
            return None

    source = KrakenPrivateOrderPollingSource(
        _Client(),
        poll_seconds=0.0,
        pending_record_ttl_seconds=60.0,
    )
    source._pending_records.append(
        _PendingPrivateRecord(
            record=stale_record,
            first_seen_at=now - timedelta(minutes=5),
        )
    )

    asyncio.run(source.pump(_Runtime()))

    assert source._pending_records == []


def test_private_order_poller_matches_tail_identity_as_tail_role() -> None:
    now = datetime.now(timezone.utc)
    record = PrivateOrderRecord(
        symbol="PI_XBTUSD",
        status="filled",
        exchange_order_id="OID-T",
        client_order_id="CID-T",
        quantity=3.0,
        filled_quantity=3.0,
        local_id=9,
        local_timestamp=now.isoformat(),
    )

    class _Client:
        def fetch_private_orders_since(self, **_kwargs):
            return (record,)

        def fetch_private_fills_since(self, **_kwargs):
            return ()

    class _Runtime:
        def __init__(self) -> None:
            self.symbol = "PI_XBTUSD"
            self.running = True
            self.state = StrategyState(
                launched_at=now,
                strategy_id="demo",
                pairs={
                    "pair-a": PairCycleState(
                        pair=sample_pair("pair-a"),
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
                    )
                },
            )

        @property
        def all_pairs_terminal(self) -> bool:
            return False

        @property
        def should_keep_sources_alive(self) -> bool:
            return False

        async def enqueue(self, event) -> None:
            emitted.append(cast(EggMove, event))
            self.running = False

        def pair_state_for_record(self, rec) -> tuple[PairCycleState, OrderRole] | None:
            if rec.client_order_id == "CID-T":
                return self.state.pairs["pair-a"], OrderRole.TAIL
            return None

    emitted: list[EggMove] = []
    source = KrakenPrivateOrderPollingSource(_Client(), poll_seconds=0.0)

    asyncio.run(source.pump(_Runtime()))

    assert len(emitted) == 1
    assert emitted[0].role == OrderRole.TAIL


def test_private_order_poller_emits_snapshot_tombstone_for_tail() -> None:
    now = datetime.now(timezone.utc)
    record = PrivateOrderRecord(
        symbol="PI_XBTUSD",
        status="canceled",
        reason="absent_from_open_orders_snapshot",
        exchange_order_id="OID-T",
        client_order_id="CID-T",
        quantity=3.0,
        filled_quantity=0.0,
        local_id=10,
        local_timestamp=now.isoformat(),
    )

    class _Client:
        def fetch_private_orders_since(self, **_kwargs):
            return (record,)

        def fetch_private_fills_since(self, **_kwargs):
            return ()

    class _Runtime:
        def __init__(self) -> None:
            self.symbol = "PI_XBTUSD"
            self.running = True
            self.state = StrategyState(
                launched_at=now,
                strategy_id="demo",
                pairs={
                    "pair-a": PairCycleState(
                        pair=sample_pair("pair-a"),
                        tail_identity=OrderIdentity(
                            pair_name="pair-a",
                            role="tail",
                            client_order_id="CID-T",
                            exchange_order_id="OID-T",
                        ),
                    )
                },
            )

        @property
        def all_pairs_terminal(self) -> bool:
            return False

        @property
        def should_keep_sources_alive(self) -> bool:
            return False

        async def enqueue(self, event) -> None:
            emitted.append(cast(EggMove, event))
            self.running = False

        def pair_state_for_record(self, rec) -> tuple[PairCycleState, OrderRole] | None:
            if rec.client_order_id == "CID-T":
                return self.state.pairs["pair-a"], OrderRole.TAIL
            return None

    emitted: list[EggMove] = []
    source = KrakenPrivateOrderPollingSource(_Client(), poll_seconds=0.0)

    asyncio.run(source.pump(_Runtime()))

    assert len(emitted) == 1
    assert emitted[0].role == OrderRole.TAIL
    assert emitted[0].kind.value == "not_played_canceled"


def test_private_order_poller_suppresses_tail_cancel_and_emits_replacement() -> None:
    now = datetime.now(timezone.utc)
    cancel_record = PrivateOrderRecord(
        symbol="PI_XBTUSD",
        status="canceled",
        reason="replaced_by_amend",
        exchange_order_id="OID-T",
        client_order_id="CID-T",
        quantity=3.0,
        filled_quantity=0.0,
        local_id=12,
        local_timestamp=now.isoformat(),
    )
    replacement_record = PrivateOrderRecord(
        symbol="PI_XBTUSD",
        status="open",
        exchange_order_id="OID-R",
        client_order_id="CID-R",
        quantity=3.0,
        filled_quantity=0.0,
        stop_price=99.5,
        local_id=13,
        local_timestamp=(now + timedelta(milliseconds=1)).isoformat(),
    )

    class _Client:
        def fetch_private_orders_since(self, **_kwargs):
            return (cancel_record, replacement_record)

        def fetch_private_fills_since(self, **_kwargs):
            return ()

    class _Runtime:
        def __init__(self) -> None:
            self.symbol = "PI_XBTUSD"
            self.exchange = "kraken"
            self.market_type = "futures"
            self.running = True
            self.state = StrategyState(
                launched_at=now,
                strategy_id="demo",
                pairs={
                    "pair-a": PairCycleState(
                        pair=sample_pair("pair-a"),
                        tail_identity=OrderIdentity(
                            pair_name="pair-a",
                            role="tail",
                            client_order_id="CID-T",
                            exchange_order_id="OID-T",
                        ),
                    )
                },
            )
            self._pending_tail_amends = {
                _CommandSlot("pair-a", 1, "tail"): _TailAmendPending(
                    pair_name="pair-a",
                    attempt_index=1,
                    desired_stop_price=Decimal("99.5"),
                    client_order_id="CID-T",
                    exchange_order_id="OID-T",
                    started_at=now,
                    deadline_at=now + timedelta(seconds=5),
                )
            }

        @property
        def all_pairs_terminal(self) -> bool:
            return True

        @property
        def should_keep_sources_alive(self) -> bool:
            return False

        async def enqueue(self, event) -> None:
            emitted.append(cast(EggMove, event))
            self.running = False

        def pair_state_for_record(self, rec) -> tuple[PairCycleState, OrderRole] | None:
            if rec.client_order_id in {"CID-T", "CID-R"}:
                return self.state.pairs["pair-a"], OrderRole.TAIL
            return None

    emitted: list[EggMove] = []
    source = KrakenPrivateOrderPollingSource(_Client(), poll_seconds=0.0)

    asyncio.run(source.pump(_Runtime()))

    assert len(emitted) == 1
    assert emitted[0].role == OrderRole.TAIL
    assert emitted[0].kind.value == "not_played_nor_canceled"
    assert emitted[0].reply is not None
    assert emitted[0].reply["clOrdID"] == "CID-R"


def test_private_order_poller_emits_from_fill_stream_when_order_stream_empty() -> None:
    now = datetime.now(timezone.utc)
    fill_record = PrivateOrderRecord(
        symbol="PI_XBTUSD",
        status="filled",
        exchange_order_id="OID-H",
        client_order_id="CID-H",
        quantity=2.0,
        filled_quantity=2.0,
        local_id=11,
        local_timestamp=now.isoformat(),
    )

    class _Client:
        def fetch_private_orders_since(self, **_kwargs):
            return ()

        def fetch_private_fills_since(self, **_kwargs):
            return (fill_record,)

    class _Runtime:
        def __init__(self) -> None:
            self.symbol = "PI_XBTUSD"
            self.running = True
            self.state = StrategyState(
                launched_at=now,
                strategy_id="demo",
                pairs={
                    "pair-a": PairCycleState(
                        pair=sample_pair("pair-a"),
                        head_identity=OrderIdentity(
                            pair_name="pair-a",
                            role="head",
                            client_order_id="CID-H",
                            exchange_order_id="OID-H",
                        ),
                    )
                },
            )

        @property
        def all_pairs_terminal(self) -> bool:
            return False

        @property
        def should_keep_sources_alive(self) -> bool:
            return False

        async def enqueue(self, event) -> None:
            emitted.append(cast(EggMove, event))
            self.running = False

        def pair_state_for_record(self, rec) -> tuple[PairCycleState, OrderRole] | None:
            if rec.client_order_id == "CID-H":
                return self.state.pairs["pair-a"], OrderRole.HEAD
            return None

    emitted: list[EggMove] = []
    source = KrakenPrivateOrderPollingSource(_Client(), poll_seconds=0.0)
    asyncio.run(source.pump(_Runtime()))

    assert len(emitted) == 1
    assert emitted[0].kind.value == "played_and_canceled"
