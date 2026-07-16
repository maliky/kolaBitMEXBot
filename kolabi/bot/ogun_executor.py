"""Ogun command executor.

Purpose: execute DragonSong commands against an async exchange-agnostic port.
Inputs: DragonSong values and an ExchangePort implementation.
Outputs: typed order acknowledgements.
Side effects: exchange calls through the injected ExchangePort.
Important types: DragonSong, ExchangePort, OrderAck.
Role: async interpreter shell.
"""
from __future__ import annotations

import asyncio
import heapq
from dataclasses import dataclass
from typing import Awaitable, Callable, TypeVar, assert_never

from kolabi.shared.core.models import OrderAck
from kolabi.shared.core.runtime_types import (
    AmendHeadCommand,
    AmendTailCommand,
    CancelCommand,
    DragonSong,
    ExchangePort,
    HeadVisibilityResult,
    OgunCommand,
    PlaceHeadCommand,
    PlaceTailCommand,
    VerifyHeadVisibilityCommand,
)

_T = TypeVar("_T")


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 3
    base_delay_seconds: float = 0.25


@dataclass(frozen=True)
class RestFlightPolicy:
    min_interval_seconds: float = 0.0
    max_inflight: int = 0


@dataclass(frozen=True)
class _FlightTicket:
    priority: int
    sequence: int
    command: OgunCommand


class RestFlightGate:
    """Async platform-call gate for the effect boundary.

    The pure reducers produce immutable commands immediately. This gate only
    controls when Ogun lets those commands touch the exchange.
    """

    def __init__(self, policy: RestFlightPolicy | None = None) -> None:
        self.policy = policy or RestFlightPolicy()
        self._condition = asyncio.Condition()
        self._queue: list[tuple[int, int, _FlightTicket]] = []
        self._sequence = 0
        self._inflight = 0
        self._next_launch_at = 0.0

    async def fly(
        self,
        command: OgunCommand,
        dispatch: Callable[[], Awaitable[_T]],
    ) -> _T:
        if not self.enabled:
            return await dispatch()
        ticket = await self._enqueue(command)
        await self._await_turn(ticket)
        try:
            return await dispatch()
        finally:
            await self._release()

    @property
    def enabled(self) -> bool:
        return self.policy.min_interval_seconds > 0 or self.policy.max_inflight > 0

    async def _enqueue(self, command: OgunCommand) -> _FlightTicket:
        async with self._condition:
            self._sequence += 1
            ticket = _FlightTicket(
                priority=_flight_priority(command),
                sequence=self._sequence,
                command=command,
            )
            heapq.heappush(self._queue, (ticket.priority, ticket.sequence, ticket))
            self._condition.notify_all()
            return ticket

    async def _await_turn(self, ticket: _FlightTicket) -> None:
        claimed = False
        try:
            while True:
                async with self._condition:
                    delay = self._launch_delay_for(ticket)
                    if delay is None:
                        await self._condition.wait()
                        continue
                    if delay <= 0:
                        self._claim(ticket)
                        claimed = True
                        return
                    try:
                        await asyncio.wait_for(self._condition.wait(), timeout=delay)
                    except TimeoutError:
                        pass
        except asyncio.CancelledError:
            if claimed:
                await self._release()
            else:
                await self._remove_ticket(ticket)
            raise

    def _launch_delay_for(self, ticket: _FlightTicket) -> float | None:
        if not self._queue or self._queue[0][2] is not ticket:
            return None
        if self.policy.max_inflight > 0 and self._inflight >= self.policy.max_inflight:
            return None
        loop_time = asyncio.get_running_loop().time()
        return max(0.0, self._next_launch_at - loop_time)

    def _claim(self, ticket: _FlightTicket) -> None:
        popped = heapq.heappop(self._queue)[2]
        if popped is not ticket:
            raise RuntimeError("REST flight gate queue corruption")
        self._inflight += 1
        interval = max(0.0, self.policy.min_interval_seconds)
        self._next_launch_at = asyncio.get_running_loop().time() + interval

    async def _release(self) -> None:
        async with self._condition:
            self._inflight = max(0, self._inflight - 1)
            self._condition.notify_all()

    async def _remove_ticket(self, ticket: _FlightTicket) -> None:
        async with self._condition:
            self._queue = [entry for entry in self._queue if entry[2] is not ticket]
            heapq.heapify(self._queue)
            self._condition.notify_all()


class OgunExecutor:
    """Single active async executor for DragonSong commands."""

    def __init__(
        self,
        port: ExchangePort,
        *,
        retry_policy: RetryPolicy | None = None,
        flight_policy: RestFlightPolicy | None = None,
        flight_gate: RestFlightGate | None = None,
    ) -> None:
        self.port = port
        self.retry_policy = retry_policy or RetryPolicy()
        self.flight_gate = flight_gate or RestFlightGate(flight_policy)

    async def execute(self, command: DragonSong) -> OrderAck:
        return await self.flight_gate.fly(command, lambda: self._execute_with_retries(command))

    async def verify_head_visibility(
        self,
        command: VerifyHeadVisibilityCommand,
    ) -> HeadVisibilityResult:
        """Run one low-priority visibility query without retrying it."""

        return await self.flight_gate.fly(
            command,
            lambda: self.port.verify_head_visibility(command),
        )

    async def _execute_with_retries(self, command: DragonSong) -> OrderAck:
        attempts = self._attempts_for(command)
        delay = max(0.0, self.retry_policy.base_delay_seconds)
        for attempt in range(1, attempts + 1):
            try:
                return await self._dispatch(command)
            except Exception:
                if attempt >= attempts:
                    raise
                await asyncio.sleep(delay * attempt)
        raise RuntimeError("unreachable retry loop")

    def _attempts_for(self, command: DragonSong) -> int:
        # Place commands must be emitted once; adapter-local HTTP retries are the
        # only safe retry layer because a second send can duplicate live orders.
        if isinstance(command, (PlaceHeadCommand, PlaceTailCommand)):
            return 1
        return max(1, self.retry_policy.attempts)

    async def _dispatch(self, command: DragonSong) -> OrderAck:
        if isinstance(command, PlaceHeadCommand):
            return await self.port.place_head(command)
        if isinstance(command, PlaceTailCommand):
            return await self.port.place_tail(command)
        if isinstance(command, AmendHeadCommand):
            return await self.port.amend_head(command)
        if isinstance(command, AmendTailCommand):
            return await self.port.amend_tail(command)
        if isinstance(command, CancelCommand):
            return await self.port.cancel(command)
        raise TypeError(f"Unsupported DragonSong type: {type(command)!r}")


def _flight_priority(command: OgunCommand) -> int:
    if isinstance(command, CancelCommand):
        return 0
    if isinstance(command, (PlaceTailCommand, AmendTailCommand)):
        return 1
    if isinstance(command, AmendHeadCommand):
        return 2
    if isinstance(command, PlaceHeadCommand):
        return 3
    if isinstance(command, VerifyHeadVisibilityCommand):
        return 4
    assert_never(command)
