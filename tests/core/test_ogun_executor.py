from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest
from kolabi.bot.ogun_executor import OgunExecutor, RestFlightPolicy, RetryPolicy
from kolabi.shared.core.models import OrderAck
from kolabi.shared.core.runtime_types import (
    AmendHeadCommand,
    AmendOrderCommandRequest,
    AmendTailCommand,
    CancelCommand,
    CancelOrderCommandRequest,
    ExchangePort,
    HeadVisibilityResult,
    PlaceHeadCommand,
    PlaceOrderCommandRequest,
    PlaceTailCommand,
    RuntimeCommandKind,
    Symbol,
    VerifyHeadVisibilityCommand,
)


@dataclass
class _Call:
    name: str
    pair_name: str


class _FakePort(ExchangePort):
    def __init__(self, *, fail_first: bool = False) -> None:
        self.calls: list[_Call] = []
        self.fail_first = fail_first
        self._count = 0

    async def place_head(self, command: PlaceHeadCommand) -> OrderAck:
        self._count += 1
        if self.fail_first and self._count == 1:
            raise RuntimeError("boom")
        self.calls.append(_Call("place_head", command.pair_name))
        return OrderAck(order_id="1", status="New")

    async def place_tail(self, command: PlaceTailCommand) -> OrderAck:
        self.calls.append(_Call("place_tail", command.pair_name))
        return OrderAck(order_id="2", status="New")

    async def amend_head(self, command: AmendHeadCommand) -> OrderAck:
        self.calls.append(_Call("amend_head", command.pair_name))
        return OrderAck(order_id="3", status="Replaced")

    async def amend_tail(self, command: AmendTailCommand) -> OrderAck:
        self.calls.append(_Call("amend_tail", command.pair_name))
        return OrderAck(order_id="4", status="Replaced")

    async def cancel(self, command: CancelCommand) -> OrderAck:
        self.calls.append(_Call("cancel", command.pair_name))
        return OrderAck(order_id="5", status="Canceled")

    async def verify_head_visibility(
        self,
        command: VerifyHeadVisibilityCommand,
    ) -> HeadVisibilityResult:
        self.calls.append(_Call("verify_head_visibility", command.pair_name))
        return HeadVisibilityResult(
            pair_name=command.pair_name,
            attempt_index=command.attempt_index,
            checked_at=datetime.now(timezone.utc),
            visible=False,
        )


def _place_head(pair_name: str = "pair-a") -> PlaceHeadCommand:
    return PlaceHeadCommand(
        kind=RuntimeCommandKind.PLACE,
        symbol=Symbol("PI_XBTUSD"),
        pair_name=pair_name,
        request=PlaceOrderCommandRequest(
            pair_name=pair_name,
            side="buy",
            ordType="Limit",
            orderQty=1,
            price=100.0,
        ),
    )


def _place_tail(pair_name: str = "pair-a") -> PlaceTailCommand:
    return PlaceTailCommand(
        kind=RuntimeCommandKind.PLACE,
        symbol=Symbol("PI_XBTUSD"),
        pair_name=pair_name,
        request=PlaceOrderCommandRequest(
            pair_name=pair_name,
            side="sell",
            ordType="Stop",
            orderQty=1,
            stopPx=90.0,
        ),
    )


def _amend_head() -> AmendHeadCommand:
    return AmendHeadCommand(
        kind=RuntimeCommandKind.AMEND,
        symbol=Symbol("PI_XBTUSD"),
        pair_name="pair-a",
        request=AmendOrderCommandRequest(
            pair_name="pair-a",
            side="buy",
            ordType="Limit",
            orderID="OID-H",
            newPrice=101.0,
        ),
    )


def _amend_tail() -> AmendTailCommand:
    return AmendTailCommand(
        kind=RuntimeCommandKind.AMEND,
        symbol=Symbol("PI_XBTUSD"),
        pair_name="pair-a",
        request=AmendOrderCommandRequest(
            pair_name="pair-a",
            side="sell",
            ordType="Stop",
            orderID="OID-T",
            newPrice=89.0,
        ),
    )


def _cancel(pair_name: str = "pair-a") -> CancelCommand:
    return CancelCommand(
        kind=RuntimeCommandKind.CANCEL,
        symbol=Symbol("PI_XBTUSD"),
        pair_name=pair_name,
        request=CancelOrderCommandRequest(pair_name=pair_name, clOrdID="CID-1"),
    )


def _verify(pair_name: str = "pair-a") -> VerifyHeadVisibilityCommand:
    head = _place_head(pair_name)
    return VerifyHeadVisibilityCommand(
        kind=RuntimeCommandKind.VALIDATE,
        symbol=head.symbol,
        pair_name=pair_name,
        attempt_index=1,
        request=head.request,
        exchange_order_id=f"OID-{pair_name}",
    )


def test_dispatch_place_head() -> None:
    port = _FakePort()
    executor = OgunExecutor(port)
    ack = asyncio.run(executor.execute(_place_head()))
    assert ack.status == "New"
    assert port.calls[0].name == "place_head"


def test_dispatch_place_tail() -> None:
    port = _FakePort()
    executor = OgunExecutor(port)
    asyncio.run(executor.execute(_place_tail()))
    assert port.calls[0].name == "place_tail"


def test_dispatch_amend_head() -> None:
    port = _FakePort()
    executor = OgunExecutor(port)
    asyncio.run(executor.execute(_amend_head()))
    assert port.calls[0].name == "amend_head"


def test_dispatch_amend_tail() -> None:
    port = _FakePort()
    executor = OgunExecutor(port)
    asyncio.run(executor.execute(_amend_tail()))
    assert port.calls[0].name == "amend_tail"


def test_dispatch_cancel() -> None:
    port = _FakePort()
    executor = OgunExecutor(port)
    asyncio.run(executor.execute(_cancel()))
    assert port.calls[0].name == "cancel"


def test_dispatch_head_visibility_query() -> None:
    port = _FakePort()
    executor = OgunExecutor(port)
    result = asyncio.run(executor.verify_head_visibility(_verify()))
    assert result.visible is False
    assert port.calls[0].name == "verify_head_visibility"


def test_non_place_commands_still_retry_then_succeed() -> None:
    class _RetryAmendPort(_FakePort):
        def __init__(self) -> None:
            super().__init__()
            self.amend_calls = 0

        async def amend_head(self, command: AmendHeadCommand) -> OrderAck:
            self.amend_calls += 1
            if self.amend_calls == 1:
                raise RuntimeError("boom")
            self.calls.append(_Call("amend_head", command.pair_name))
            return OrderAck(order_id="3", status="Replaced")

    port = _RetryAmendPort()
    executor = OgunExecutor(port, retry_policy=RetryPolicy(attempts=2, base_delay_seconds=0.0))
    ack = asyncio.run(executor.execute(_amend_head()))
    assert ack.status == "Replaced"
    assert [call.name for call in port.calls] == ["amend_head"]


def test_exhausted_retries_raises() -> None:
    class _AlwaysFailPort(_FakePort):
        async def place_head(self, command: PlaceHeadCommand) -> OrderAck:
            raise RuntimeError("nope")

    executor = OgunExecutor(_AlwaysFailPort(), retry_policy=RetryPolicy(attempts=2, base_delay_seconds=0.0))
    with pytest.raises(RuntimeError, match="nope"):
        asyncio.run(executor.execute(_place_head()))


def test_place_commands_are_not_retried() -> None:
    class _AlwaysFailPlacePort(_FakePort):
        def __init__(self) -> None:
            super().__init__()
            self.place_head_calls = 0

        async def place_head(self, command: PlaceHeadCommand) -> OrderAck:
            self.place_head_calls += 1
            raise RuntimeError("nope")

    port = _AlwaysFailPlacePort()
    executor = OgunExecutor(port, retry_policy=RetryPolicy(attempts=3, base_delay_seconds=0.0))

    with pytest.raises(RuntimeError, match="nope"):
        asyncio.run(executor.execute(_place_head()))

    assert port.place_head_calls == 1


def test_rest_flight_gate_limits_concurrent_platform_calls() -> None:
    class _BlockingPort(_FakePort):
        def __init__(self) -> None:
            super().__init__()
            self.active = 0
            self.max_seen = 0
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def place_head(self, command: PlaceHeadCommand) -> OrderAck:
            self.active += 1
            self.max_seen = max(self.max_seen, self.active)
            self.started.set()
            await self.release.wait()
            self.calls.append(_Call("place_head", command.pair_name))
            self.active -= 1
            return OrderAck(order_id=command.pair_name, status="New")

    async def _run() -> tuple[int, int]:
        port = _BlockingPort()
        executor = OgunExecutor(port, flight_policy=RestFlightPolicy(max_inflight=1))
        tasks = [
            asyncio.create_task(executor.execute(_place_head(f"pair-{index}")))
            for index in range(3)
        ]
        await asyncio.wait_for(port.started.wait(), timeout=0.5)
        await asyncio.sleep(0.01)
        started_before_release = len(port.calls) + port.active
        port.release.set()
        await asyncio.gather(*tasks)
        return port.max_seen, started_before_release

    max_seen, started_before_release = asyncio.run(_run())

    assert max_seen == 1
    assert started_before_release == 1


def test_rest_flight_gate_prioritises_safety_commands_after_active_call() -> None:
    class _PriorityPort(_FakePort):
        def __init__(self) -> None:
            super().__init__()
            self.started_names: list[str] = []
            self.first_started = asyncio.Event()
            self.release_first = asyncio.Event()

        async def _record(self, name: str, pair_name: str) -> OrderAck:
            self.started_names.append(f"{name}:{pair_name}")
            if len(self.started_names) == 1:
                self.first_started.set()
                await self.release_first.wait()
            return OrderAck(order_id=f"OID-{pair_name}", status="New")

        async def place_head(self, command: PlaceHeadCommand) -> OrderAck:
            return await self._record("place_head", command.pair_name)

        async def place_tail(self, command: PlaceTailCommand) -> OrderAck:
            return await self._record("place_tail", command.pair_name)

        async def cancel(self, command: CancelCommand) -> OrderAck:
            return await self._record("cancel", command.pair_name)

        async def verify_head_visibility(
            self,
            command: VerifyHeadVisibilityCommand,
        ) -> HeadVisibilityResult:
            await self._record("verify", command.pair_name)
            return HeadVisibilityResult(
                pair_name=command.pair_name,
                attempt_index=command.attempt_index,
                checked_at=datetime.now(timezone.utc),
                visible=False,
            )

    async def _run() -> list[str]:
        port = _PriorityPort()
        executor = OgunExecutor(port, flight_policy=RestFlightPolicy(max_inflight=1))
        first = asyncio.create_task(executor.execute(_place_head("first")))
        await asyncio.wait_for(port.first_started.wait(), timeout=0.5)
        queued = [
            asyncio.create_task(executor.verify_head_visibility(_verify("verify"))),
            asyncio.create_task(executor.execute(_place_head("second"))),
            asyncio.create_task(executor.execute(_place_tail("tail"))),
            asyncio.create_task(executor.execute(_cancel("cancel"))),
        ]
        await asyncio.sleep(0.01)
        port.release_first.set()
        await asyncio.gather(first, *queued)
        return port.started_names

    assert asyncio.run(_run()) == [
        "place_head:first",
        "cancel:cancel",
        "place_tail:tail",
        "place_head:second",
        "verify:verify",
    ]


def test_rest_flight_gate_spaces_platform_launches() -> None:
    class _TimestampPort(_FakePort):
        def __init__(self) -> None:
            super().__init__()
            self.launches: list[float] = []

        async def place_head(self, command: PlaceHeadCommand) -> OrderAck:
            self.launches.append(asyncio.get_running_loop().time())
            return OrderAck(order_id=command.pair_name, status="New")

    async def _run() -> list[float]:
        port = _TimestampPort()
        executor = OgunExecutor(
            port,
            flight_policy=RestFlightPolicy(min_interval_seconds=0.02, max_inflight=3),
        )
        await asyncio.gather(
            executor.execute(_place_head("first")),
            executor.execute(_place_head("second")),
            executor.execute(_place_head("third")),
        )
        return port.launches

    launches = asyncio.run(_run())

    assert launches[1] - launches[0] >= 0.015
    assert launches[2] - launches[1] >= 0.015
