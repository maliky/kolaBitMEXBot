from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import pytest
from kolabi.bot.service import AdapterExchangePort
from kolabi.shared.config import ExchangeConfig
from kolabi.shared.core.models import OrderAck
from kolabi.shared.core.runtime_types import (
    AmendHeadCommand,
    AmendOrderCommandRequest,
    AmendTailCommand,
    PlaceHeadCommand,
    PlaceOrderCommandRequest,
    PlaceTailCommand,
    RuntimeCommandKind,
    Symbol,
    VerifyHeadVisibilityCommand,
)


class _FakeAdapter:
    last: tuple[str, dict[str, Any]] | None = None

    def __init__(self, **_kwargs: Any) -> None:
        type(self).last = None

    def place_order(self, *_args: Any, **_kwargs: Any) -> OrderAck:
        raise AssertionError("not used")

    def amend_order(self, order_id: str, **params: Any) -> OrderAck:
        type(self).last = (order_id, params)
        return OrderAck(order_id=order_id, status="Replaced")

    def cancel_order(self, order_id: str) -> OrderAck:
        raise AssertionError("not used")


class _TailAdapter:
    placed: tuple[tuple[Any, ...], dict[str, Any]] | None = None
    trigger_orders: list[dict[str, Any]] = []
    db_trigger_orders: list[dict[str, Any]] = []

    def __init__(self, **_kwargs: Any) -> None:
        type(self).placed = None

    def place_order(self, *args: Any, **kwargs: Any) -> OrderAck:
        type(self).placed = (args, kwargs)
        return OrderAck(order_id="OID-T", status="New", orig_qty=1.0, side="sell")

    def live_trigger_orders(self) -> list[dict[str, Any]]:
        return type(self).trigger_orders

    def live_trigger_orders_db(self) -> list[dict[str, Any]]:
        return type(self).db_trigger_orders

    def amend_order(self, order_id: str, **params: Any) -> OrderAck:
        raise AssertionError("not used")

    def cancel_order(self, order_id: str) -> OrderAck:
        raise AssertionError("not used")


class _HeadAdapter:
    placed: tuple[tuple[Any, ...], dict[str, Any]] | None = None
    open_orders: list[dict[str, Any]] = []

    def __init__(self, **_kwargs: Any) -> None:
        type(self).placed = None

    def place_order(self, *args: Any, **kwargs: Any) -> OrderAck:
        type(self).placed = (args, kwargs)
        return OrderAck(order_id="", status="New", orig_qty=1.0, side="buy")

    def live_open_orders(self) -> list[dict[str, Any]]:
        return type(self).open_orders

    def amend_order(self, order_id: str, **params: Any) -> OrderAck:
        raise AssertionError("not used")

    def cancel_order(self, order_id: str) -> OrderAck:
        raise AssertionError("not used")


def _config() -> ExchangeConfig:
    return ExchangeConfig(
        api_key="key",
        api_secret="secret",
        base_url="https://example.invalid",
        symbol="PI_XBTUSD",
    )


def test_amend_tail_maps_new_price_to_stop_px(monkeypatch) -> None:
    monkeypatch.setattr("kolabi.bot.service.get_adapter", lambda _exchange: _FakeAdapter)
    port = AdapterExchangePort(exchange="kraken", exchange_config=_config())

    asyncio.run(
        port.amend_tail(
            AmendTailCommand(
                kind=RuntimeCommandKind.AMEND,
                symbol=Symbol("PI_XBTUSD"),
                pair_name="pair-a",
                request=AmendOrderCommandRequest(
                    pair_name="pair-a",
                    side="buy",
                    ordType="Stop",
                    orderID="OID-T",
                    clOrdID="CID-T",
                    newPrice=101.5,
                ),
            )
        )
    )

    assert _FakeAdapter.last == ("OID-T", {"clOrdID": "CID-T", "stopPx": 101.5})


def test_amend_tail_stop_limit_derives_sell_limit_from_offset(monkeypatch) -> None:
    monkeypatch.setattr("kolabi.bot.service.get_adapter", lambda _exchange: _FakeAdapter)
    port = AdapterExchangePort(exchange="kraken", exchange_config=_config())

    asyncio.run(
        port.amend_tail(
            AmendTailCommand(
                kind=RuntimeCommandKind.AMEND,
                symbol=Symbol("PI_XBTUSD"),
                pair_name="pair-a",
                request=AmendOrderCommandRequest(
                    pair_name="pair-a",
                    side="sell",
                    ordType="SL",
                    orderID="OID-T",
                    clOrdID="CID-T",
                    newPrice=Decimal("100"),
                    oDelta=Decimal("0.5"),
                ),
            )
        )
    )

    assert _FakeAdapter.last == (
        "OID-T",
        {
            "clOrdID": "CID-T",
            "price": Decimal("99.5"),
            "stopPx": Decimal("100"),
        },
    )


def test_amend_tail_stop_limit_derives_buy_limit_from_offset(monkeypatch) -> None:
    monkeypatch.setattr("kolabi.bot.service.get_adapter", lambda _exchange: _FakeAdapter)
    port = AdapterExchangePort(exchange="kraken", exchange_config=_config())

    asyncio.run(
        port.amend_tail(
            AmendTailCommand(
                kind=RuntimeCommandKind.AMEND,
                symbol=Symbol("PI_XBTUSD"),
                pair_name="pair-a",
                request=AmendOrderCommandRequest(
                    pair_name="pair-a",
                    side="buy",
                    ordType="SL",
                    orderID="OID-T",
                    clOrdID="CID-T",
                    newPrice=Decimal("100"),
                    oDelta=Decimal("0.5"),
                ),
            )
        )
    )

    assert _FakeAdapter.last == (
        "OID-T",
        {
            "clOrdID": "CID-T",
            "price": Decimal("100.5"),
            "stopPx": Decimal("100"),
        },
    )


def test_amend_head_keeps_new_price_as_limit_price(monkeypatch) -> None:
    monkeypatch.setattr("kolabi.bot.service.get_adapter", lambda _exchange: _FakeAdapter)
    port = AdapterExchangePort(exchange="kraken", exchange_config=_config())

    asyncio.run(
        port.amend_head(
            AmendHeadCommand(
                kind=RuntimeCommandKind.AMEND,
                symbol=Symbol("PI_XBTUSD"),
                pair_name="pair-a",
                request=AmendOrderCommandRequest(
                    pair_name="pair-a",
                    side="sell",
                    ordType="Limit",
                    orderID="OID-H",
                    newPrice=100.5,
                ),
            )
        )
    )

    assert _FakeAdapter.last == ("OID-H", {"price": 100.5})


def test_head_visibility_query_enriches_sparse_placement_ack(monkeypatch) -> None:
    _HeadAdapter.open_orders = [
        {
            "order_id": "OID-H",
            "client_order_id": "CID-H",
            "symbol": "PI_XBTUSD",
            "side": "buy",
            "qty": 1.0,
            "price": 100.0,
            "status": "open",
        }
    ]
    monkeypatch.setattr("kolabi.bot.service.get_adapter", lambda _exchange: _HeadAdapter)
    port = AdapterExchangePort(exchange="kraken", exchange_config=_config())

    command = PlaceHeadCommand(
        kind=RuntimeCommandKind.PLACE,
        symbol=Symbol("PI_XBTUSD"),
        pair_name="pair-a",
        request=PlaceOrderCommandRequest(
            pair_name="pair-a",
            side="buy",
            ordType="Limit",
            orderQty=1.0,
            price=100.0,
            clOrdID="CID-H",
        ),
    )
    ack = asyncio.run(port.place_head(command))

    assert ack.order_id == ""
    assert ack.client_order_id is None
    assert ack.status == "New"

    visible = asyncio.run(
        port.verify_head_visibility(
            VerifyHeadVisibilityCommand(
                kind=RuntimeCommandKind.VALIDATE,
                symbol=command.symbol,
                pair_name=command.pair_name,
                attempt_index=1,
                request=command.request,
            )
        )
    )

    assert visible.visible is True
    assert visible.exchange_order_id == "OID-H"
    assert visible.client_order_id == "CID-H"
    assert visible.status == "open"


def test_place_head_keeps_rest_ack_when_open_order_is_not_visible(monkeypatch) -> None:
    _HeadAdapter.open_orders = []
    monkeypatch.setattr("kolabi.bot.service.get_adapter", lambda _exchange: _HeadAdapter)
    port = AdapterExchangePort(exchange="kraken", exchange_config=_config())

    ack = asyncio.run(
        port.place_head(
            PlaceHeadCommand(
                kind=RuntimeCommandKind.PLACE,
                symbol=Symbol("PI_XBTUSD"),
                pair_name="pair-a",
                request=PlaceOrderCommandRequest(
                    pair_name="pair-a",
                    side="buy",
                    ordType="Limit",
                    orderQty=1.0,
                    price=100.0,
                    clOrdID="CID-H",
                ),
            )
        )
    )

    assert ack.order_id == ""
    assert ack.client_order_id is None
    assert ack.status == "New"


def test_place_tail_requires_matching_live_trigger_order(monkeypatch) -> None:
    _TailAdapter.trigger_orders = [
        {
            "order_id": "OID-T",
            "client_order_id": "CID-T",
            "symbol": "PI_XBTUSD",
            "side": "sell",
            "qty": 1.0,
            "stop_price": 99.0,
            "reduce_only": True,
        }
    ]
    monkeypatch.setattr("kolabi.bot.service.get_adapter", lambda _exchange: _TailAdapter)
    port = AdapterExchangePort(exchange="kraken", exchange_config=_config())

    ack = asyncio.run(
        port.place_tail(
            PlaceTailCommand(
                kind=RuntimeCommandKind.PLACE,
                symbol=Symbol("PI_XBTUSD"),
                pair_name="pair-a",
                request=PlaceOrderCommandRequest(
                    pair_name="pair-a",
                    side="sell",
                    ordType="S",
                    orderQty=1.0,
                    stopPx=99.0,
                    execInst="ReduceOnly,LastPrice",
                    clOrdID="CID-T",
                ),
            )
        )
    )

    assert ack.order_id == "OID-T"
    assert _TailAdapter.placed == (
        ("sell", 1.0),
        {
            "type_": "S",
            "stopPx": 99.0,
            "clOrdID": "CID-T",
            "execInst": "ReduceOnly,LastPrice",
        },
    )


def test_place_tail_fails_when_trigger_order_is_not_visible(monkeypatch) -> None:
    _TailAdapter.trigger_orders = []
    _TailAdapter.db_trigger_orders = []
    monkeypatch.setattr("kolabi.bot.service.get_adapter", lambda _exchange: _TailAdapter)
    monkeypatch.setattr("kolabi.bot.service.asyncio.sleep", _raise_timeout)
    port = AdapterExchangePort(exchange="kraken", exchange_config=_config())

    with pytest.raises(RuntimeError, match="tail trigger order not visible"):
        asyncio.run(
            port.place_tail(
                PlaceTailCommand(
                    kind=RuntimeCommandKind.PLACE,
                    symbol=Symbol("PI_XBTUSD"),
                    pair_name="pair-a",
                    request=PlaceOrderCommandRequest(
                        pair_name="pair-a",
                        side="sell",
                        ordType="S",
                        orderQty=1.0,
                        stopPx=99.0,
                        execInst="ReduceOnly,LastPrice",
                        clOrdID="CID-T",
                    ),
                )
            )
        )


def test_place_tail_rejected_ack_but_visible_trigger_is_accepted(monkeypatch) -> None:
    class _RejectedButVisibleAdapter(_TailAdapter):
        def place_order(self, *args: Any, **kwargs: Any) -> OrderAck:
            type(self).placed = (args, kwargs)
            return OrderAck(order_id="OID-R", status="Rejected", orig_qty=1.0, side="sell")

    _RejectedButVisibleAdapter.trigger_orders = [
        {
            "order_id": "OID-V",
            "client_order_id": "CID-R",
            "symbol": "PI_XBTUSD",
            "side": "sell",
            "qty": 1.0,
            "stop_price": 99.5,
            "reduce_only": True,
            "status": "New",
        }
    ]
    _RejectedButVisibleAdapter.db_trigger_orders = []
    monkeypatch.setattr("kolabi.bot.service.get_adapter", lambda _exchange: _RejectedButVisibleAdapter)
    port = AdapterExchangePort(exchange="kraken", exchange_config=_config())

    ack = asyncio.run(
        port.place_tail(
            PlaceTailCommand(
                kind=RuntimeCommandKind.PLACE,
                symbol=Symbol("PI_XBTUSD"),
                pair_name="pair-a",
                request=PlaceOrderCommandRequest(
                    pair_name="pair-a",
                    side="sell",
                    ordType="S",
                    orderQty=1.0,
                    stopPx=99.49,
                    execInst="ReduceOnly,LastPrice",
                    clOrdID="CID-R",
                ),
            )
        )
    )

    assert ack.status == "Rejected"


def test_place_tail_rejected_ack_without_visibility_still_fails(monkeypatch) -> None:
    class _RejectedAdapter(_TailAdapter):
        def place_order(self, *args: Any, **kwargs: Any) -> OrderAck:
            type(self).placed = (args, kwargs)
            return OrderAck(order_id="OID-R", status="Rejected", orig_qty=1.0, side="sell")

    _RejectedAdapter.trigger_orders = []
    _RejectedAdapter.db_trigger_orders = []
    monkeypatch.setattr("kolabi.bot.service.get_adapter", lambda _exchange: _RejectedAdapter)
    port = AdapterExchangePort(exchange="kraken", exchange_config=_config())

    with pytest.raises(RuntimeError, match="tail trigger order rejected by exchange"):
        asyncio.run(
            port.place_tail(
                PlaceTailCommand(
                    kind=RuntimeCommandKind.PLACE,
                    symbol=Symbol("PI_XBTUSD"),
                    pair_name="pair-a",
                    request=PlaceOrderCommandRequest(
                        pair_name="pair-a",
                        side="sell",
                        ordType="S",
                        orderQty=1.0,
                        stopPx=99.0,
                        execInst="ReduceOnly,LastPrice",
                        clOrdID="CID-R",
                    ),
                )
            )
        )


def test_place_tail_rejected_ack_is_accepted_when_db_evidence_appears(monkeypatch) -> None:
    class _RejectedDbEvidenceAdapter(_TailAdapter):
        calls = 0

        def place_order(self, *args: Any, **kwargs: Any) -> OrderAck:
            type(self).placed = (args, kwargs)
            return OrderAck(order_id="OID-R", status="Rejected", orig_qty=1.0, side="sell")

        def live_trigger_orders(self) -> list[dict[str, Any]]:
            type(self).calls += 1
            if type(self).calls >= 2:
                type(self).db_trigger_orders = [
                    {
                        "order_id": "OID-DB",
                        "client_order_id": "CID-DB",
                        "symbol": "PI_XBTUSD",
                        "side": "sell",
                        "qty": 1.0,
                        "stop_price": 99.0,
                        "reduce_only": True,
                        "status": "New",
                    }
                ]
            return []

    _RejectedDbEvidenceAdapter.calls = 0
    _RejectedDbEvidenceAdapter.db_trigger_orders = []
    monkeypatch.setattr("kolabi.bot.service.get_adapter", lambda _exchange: _RejectedDbEvidenceAdapter)
    port = AdapterExchangePort(
        exchange="kraken",
        exchange_config=_config(),
        verify_timeout_seconds=0.2,
        verify_poll_seconds=0.01,
    )

    ack = asyncio.run(
        port.place_tail(
            PlaceTailCommand(
                kind=RuntimeCommandKind.PLACE,
                symbol=Symbol("PI_XBTUSD"),
                pair_name="pair-a",
                request=PlaceOrderCommandRequest(
                    pair_name="pair-a",
                    side="sell",
                    ordType="S",
                    orderQty=1.0,
                    stopPx=99.0,
                    execInst="ReduceOnly,LastPrice",
                    clOrdID="CID-DB",
                ),
            )
        )
    )

    assert ack.status == "Rejected"


def test_place_tail_visible_trigger_is_accepted_even_when_reduce_only_flag_is_false(
    monkeypatch,
) -> None:
    _TailAdapter.trigger_orders = [
        {
            "order_id": "OID-T",
            "client_order_id": "CID-T",
            "symbol": "PI_XBTUSD",
            "side": "sell",
            "qty": 1.0,
            "stop_price": 99.0,
            "reduce_only": False,
            "status": "New",
        }
    ]
    _TailAdapter.db_trigger_orders = []
    monkeypatch.setattr("kolabi.bot.service.get_adapter", lambda _exchange: _TailAdapter)
    port = AdapterExchangePort(exchange="kraken", exchange_config=_config())

    ack = asyncio.run(
        port.place_tail(
            PlaceTailCommand(
                kind=RuntimeCommandKind.PLACE,
                symbol=Symbol("PI_XBTUSD"),
                pair_name="pair-a",
                request=PlaceOrderCommandRequest(
                    pair_name="pair-a",
                    side="sell",
                    ordType="S",
                    orderQty=1.0,
                    stopPx=99.0,
                    execInst="ReduceOnly,LastPrice",
                    clOrdID="CID-T",
                ),
            )
        )
    )

    assert ack.order_id == "OID-T"


async def _raise_timeout(_seconds: float) -> None:
    raise RuntimeError("tail trigger order not visible")
