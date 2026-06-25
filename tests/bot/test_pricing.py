from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from kolabi.bot.domain import (
    HeadSpec,
    OrderPairSpec,
    PairCycleState,
    Side,
    TailSpec,
    TimeWindow,
)
from kolabi.bot.dragon import MarketSnapshotFact, head_hooked_from_market_snapshot
from kolabi.bot.order_building import tail_place_request
from kolabi.bot.pricing import (
    executable_head_reference_price,
    pair_window_has_ended,
    pair_window_is_open,
    resolve_head_order_prices,
)


def _pair(order_type: str) -> OrderPairSpec:
    return OrderPairSpec(
        name="pair-a",
        window=TimeWindow(start_minutes=0, end_minutes=60),
        try_num=1,
        dr_pause=None,
        timeout=4,
        head=HeadSpec(side=Side.BUY, order_type=order_type),
        head_price=(-5.0, -3.0),
        head_price_type="pD",
        head_quantity=3,
        head_quantity_type="qA",
        tail=TailSpec(side=Side.SELL, order_type="S-"),
        tail_price_spec=8,
        tail_price_spec_type="tD",
        amount_type="qAtDpD",
    )


@dataclass(frozen=True)
class _Market:
    best_bid: float | None
    best_ask: float | None
    mid_price: float | None
    last_price: float | None = None
    mark_price: float | None = None
    index_price: float | None = None
    tick_size: float | None = None


def test_head_limit_mark_suffix_uses_mark_reference() -> None:
    source, reference = executable_head_reference_price(
        _pair("Lm"),
        _Market(
            best_bid=99.0,
            best_ask=101.0,
            mid_price=100.0,
            mark_price=120.0,
        ),
    )

    assert source == "mark"
    assert reference == 120.0


def test_head_limit_without_suffix_uses_last_reference() -> None:
    source, reference = executable_head_reference_price(
        _pair("L"),
        _Market(
            best_bid=99.0,
            best_ask=101.0,
            mid_price=100.0,
            last_price=120.0,
        ),
    )

    assert source == "last"
    assert reference == 120.0


def test_head_limit_without_suffix_prefers_last_before_mark() -> None:
    source, reference = executable_head_reference_price(
        _pair("L"),
        _Market(
            best_bid=99.0,
            best_ask=101.0,
            mid_price=100.0,
            last_price=121.0,
            mark_price=120.0,
        ),
    )

    assert source == "last"
    assert reference == 121.0


def test_head_limit_without_suffix_falls_back_to_mark_reference() -> None:
    source, reference = executable_head_reference_price(
        _pair("L"),
        _Market(
            best_bid=99.0,
            best_ask=101.0,
            mid_price=100.0,
            mark_price=120.0,
        ),
    )

    assert source == "mark"
    assert reference == 120.0


def test_head_limit_without_suffix_falls_back_to_bid_for_sell() -> None:
    source, reference = executable_head_reference_price(
        replace(
            _pair("L"),
            head=HeadSpec(side=Side.SELL, order_type="L"),
        ),
        _Market(
            best_bid=99.0,
            best_ask=101.0,
            mid_price=100.0,
        ),
    )

    assert source == "bid"
    assert reference == 99.0


def test_head_limit_without_suffix_falls_back_to_ask_for_buy() -> None:
    source, reference = executable_head_reference_price(
        _pair("L"),
        _Market(
            best_bid=99.0,
            best_ask=101.0,
            mid_price=100.0,
        ),
    )

    assert source == "ask"
    assert reference == 101.0


def test_post_only_buy_limit_keeps_last_gate_reference_but_clamps_to_bid() -> None:
    pair = replace(
        _pair("L!"),
        head_order_price_spec=0.0001,
        head_order_price_spec_type="hD",
    )
    market = _Market(
        best_bid=0.1614,
        best_ask=0.1615,
        mid_price=0.16145,
        last_price=0.1616,
        tick_size=0.0001,
    )

    source, reference = executable_head_reference_price(pair, market)
    price, stop_price = resolve_head_order_prices(pair, market)

    assert source == "last"
    assert reference == 0.1616
    assert price == pytest.approx(0.1614)
    assert stop_price is None


def test_post_only_sell_limit_keeps_last_gate_reference_but_clamps_to_ask() -> None:
    pair = replace(
        _pair("L!"),
        head=HeadSpec(side=Side.SELL, order_type="L!"),
        head_order_price_spec=0.0001,
        head_order_price_spec_type="hD",
    )
    market = _Market(
        best_bid=0.1617,
        best_ask=0.1618,
        mid_price=0.16175,
        last_price=0.1616,
        tick_size=0.0001,
    )

    source, reference = executable_head_reference_price(pair, market)
    price, stop_price = resolve_head_order_prices(pair, market)

    assert source == "last"
    assert reference == 0.1616
    assert price == pytest.approx(0.1618)
    assert stop_price is None


def test_sell_limit_logbps_hprice_materialises_above_mark_reference() -> None:
    pair = replace(
        _pair("Lm"),
        head=HeadSpec(
            side=Side.SELL,
            order_type="Lm",
        ),
        head_order_price_spec=148.89,
        head_order_price_spec_type="hB",
        head_price=(-1_000_000.0, 1_000_000.0),
        amount_type="qAtDpDhB",
    )

    price, stop_price = resolve_head_order_prices(
        pair,
        _Market(
            best_bid=99.0,
            best_ask=101.0,
            mid_price=100.0,
            mark_price=1000.0,
        ),
    )

    assert price == pytest.approx(1015.0, abs=0.001)
    assert stop_price is None


def test_buy_limit_logbps_hprice_materialises_below_mark_reference() -> None:
    pair = replace(
        _pair("Lm"),
        head=HeadSpec(
            side=Side.BUY,
            order_type="Lm",
        ),
        head_order_price_spec=148.89,
        head_order_price_spec_type="hB",
        head_price=(-1_000_000.0, 1_000_000.0),
        amount_type="qAtDpDhB",
    )

    price, stop_price = resolve_head_order_prices(
        pair,
        _Market(
            best_bid=99.0,
            best_ask=101.0,
            mid_price=100.0,
            mark_price=1000.0,
        ),
    )

    assert price == pytest.approx(985.0, abs=0.001)
    assert stop_price is None


def test_buy_limit_blank_hprice_materialises_one_tick_below_reference() -> None:
    pair = _pair("Lm")

    price, stop_price = resolve_head_order_prices(
        pair,
        _Market(
            best_bid=99.0,
            best_ask=101.0,
            mid_price=100.0,
            mark_price=1000.0,
            tick_size=0.00001,
        ),
    )

    assert price == 999.99999
    assert stop_price is None


def test_sell_limit_blank_hprice_materialises_one_tick_above_reference() -> None:
    pair = replace(_pair("Lm"), head=HeadSpec(side=Side.SELL, order_type="Lm"))

    price, stop_price = resolve_head_order_prices(
        pair,
        _Market(
            best_bid=99.0,
            best_ask=101.0,
            mid_price=100.0,
            mark_price=1000.0,
            tick_size=0.00001,
        ),
    )

    assert price == 1000.00001
    assert stop_price is None


def test_blank_head_hprice_keyword_requires_tick_size() -> None:
    with pytest.raises(ValueError, match="blank hPrice"):
        resolve_head_order_prices(
            _pair("Lm"),
            _Market(
                best_bid=99.0,
                best_ask=101.0,
                mid_price=100.0,
                mark_price=1000.0,
            ),
        )


def test_blank_stop_limit_tail_delta_materialises_one_tick_offset() -> None:
    pair = replace(
        _pair("M"),
        tail=TailSpec(side=Side.SELL, order_type="SLm", delta=None),
        tail_price_spec=95.0,
        tail_price_spec_type="tA",
        amount_type="qAtApD",
    )
    request = tail_place_request(
        PairCycleState(
            pair=pair,
            played_quantity=Decimal("3"),
            instrument_tick_size=Decimal("0.00001"),
        )
    )

    assert request.oDelta == Decimal("0.00001")


def test_head_limit_suffix_drives_price_condition() -> None:
    pair = _pair("Lm")
    now = datetime.now(timezone.utc)
    move = head_hooked_from_market_snapshot(
        pair_state=PairCycleState(
            pair=pair,
            head_trigger_reference_price=Decimal("120"),
        ),
        launched_at=now,
        snapshot=MarketSnapshotFact(
            symbol="PI_XBTUSD",
            best_bid=90.0,
            best_ask=91.0,
            mid_price=90.5,
            mark_price=116.0,
            tick_size=0.5,
            occurred_at=now,
        ),
    )

    assert move is not None
    assert move.reply is not None
    assert move.reply["reference_source"] == "mark"


def test_head_limit_hook_carries_materialised_order_price() -> None:
    pair = _pair("L")
    now = datetime.now(timezone.utc)
    move = head_hooked_from_market_snapshot(
        pair_state=PairCycleState(
            pair=pair,
            head_trigger_reference_price=Decimal("100"),
        ),
        launched_at=now,
        snapshot=MarketSnapshotFact(
            symbol="PI_XBTUSD",
            best_bid=95.0,
            best_ask=96.0,
            mid_price=95.5,
            last_price=95.5,
            tick_size=0.5,
            occurred_at=now,
        ),
    )

    assert move is not None
    assert move.reply is not None
    assert move.reply["reference_source"] == "last"
    assert move.reply["head_order_price"] == 95.0


def test_sell_limit_hprice_is_lazy_relative_to_gate_open_reference() -> None:
    pair = replace(
        _pair("L"),
        head=HeadSpec(side=Side.SELL, order_type="L"),
        head_price=(-1000.0, -200.0),
        head_price_type="pB",
        head_order_price_spec=10.0,
        head_order_price_spec_type="hD",
    )
    now = datetime.now(timezone.utc)
    move = head_hooked_from_market_snapshot(
        pair_state=PairCycleState(
            pair=pair,
            head_trigger_reference_price=Decimal("100"),
        ),
        launched_at=now,
        snapshot=MarketSnapshotFact(
            symbol="PI_XBTUSD",
            best_bid=95.0,
            best_ask=98.1,
            mid_price=96.5,
            last_price=97.9,
            tick_size=0.1,
            occurred_at=now,
        ),
    )

    assert move is not None
    assert move.reply is not None
    assert move.reply["reference_price"] == 97.9
    assert move.reply["reference_source"] == "last"
    assert move.reply["head_order_price"] == 107.9


def test_head_stop_hook_carries_materialised_stop_price() -> None:
    pair = replace(
        _pair("Sm"),
        head=HeadSpec(side=Side.SELL, order_type="Sm"),
        head_price=(3.0, 5.0),
    )
    now = datetime.now(timezone.utc)
    move = head_hooked_from_market_snapshot(
        pair_state=PairCycleState(
            pair=pair,
            head_trigger_reference_price=Decimal("100"),
        ),
        launched_at=now,
        snapshot=MarketSnapshotFact(
            symbol="PI_XBTUSD",
            best_bid=103.0,
            best_ask=104.0,
            mid_price=103.5,
            mark_price=104.0,
            tick_size=0.5,
            occurred_at=now,
        ),
    )

    assert move is not None
    assert move.reply is not None
    assert move.reply["head_order_stop_price"] == 103.5


def test_buy_stop_hprice_places_trigger_above_reference() -> None:
    pair = replace(
        _pair("Sl"),
        head=HeadSpec(side=Side.BUY, order_type="Sl"),
        head_order_price_spec=10.0,
        head_order_price_spec_type="hD",
    )

    price, stop_price = resolve_head_order_prices(
        pair,
        _Market(
            best_bid=99.0,
            best_ask=101.0,
            mid_price=100.0,
            last_price=100.5,
        ),
    )

    assert price is None
    assert stop_price == 110.5


def test_sell_stop_hprice_places_trigger_below_reference() -> None:
    pair = replace(
        _pair("Sl"),
        head=HeadSpec(side=Side.SELL, order_type="Sl"),
        head_order_price_spec=10.0,
        head_order_price_spec_type="hD",
    )

    price, stop_price = resolve_head_order_prices(
        pair,
        _Market(
            best_bid=99.0,
            best_ask=101.0,
            mid_price=100.0,
            last_price=100.5,
        ),
    )

    assert price is None
    assert stop_price == 90.5


def test_pair_window_accepts_mixed_naive_and_aware_datetimes() -> None:
    launched_at = datetime(2026, 5, 30, 21, 0, tzinfo=timezone.utc)
    naive_now = datetime(2026, 5, 30, 21, 30)

    assert pair_window_is_open(
        _pair("M"),
        launched_at=launched_at,
        now=naive_now,
    )


def test_pair_window_end_accepts_mixed_naive_and_aware_datetimes() -> None:
    launched_at = datetime(2026, 5, 30, 21, 0, tzinfo=timezone.utc)
    naive_now = datetime(2026, 5, 30, 22, 1)

    assert pair_window_has_ended(
        _pair("M"),
        launched_at=launched_at,
        now=naive_now,
    )
