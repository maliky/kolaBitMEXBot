from __future__ import annotations

from decimal import Decimal

import pytest

from kolabi.bot.domain import HeadSpec, OrderPairSpec, Side, TailSpec, TimeWindow
from kolabi.bot.quantity import (
    QuantityResolutionError,
    resolve_pair_percent_balance_quantity,
    resolve_pair_usd_quantity,
    validate_pair_absolute_quantity,
)


def _usd_pair(quantity: float = 15.0) -> OrderPairSpec:
    return OrderPairSpec(
        name="usd-pair",
        window=TimeWindow(start_minutes=0.0, end_minutes=60.0),
        try_num=1,
        dr_pause=None,
        timeout=60,
        head=HeadSpec(side=Side.BUY, order_type="Limit"),
        head_price=(100.0, 101.0),
        head_price_type="pA",
        head_quantity=quantity,
        head_quantity_type="qU",
        tail=TailSpec(side=Side.SELL, order_type="Stop"),
        tail_price_spec=99.0,
        tail_price_spec_type="tA",
        amount_type="qUtApA",
    )


def _absolute_pair(quantity: Decimal) -> OrderPairSpec:
    pair = _usd_pair(float(quantity))
    return OrderPairSpec(
        name=pair.name,
        window=pair.window,
        try_num=pair.try_num,
        dr_pause=pair.dr_pause,
        timeout=pair.timeout,
        head=pair.head,
        head_price=pair.head_price,
        head_price_type=pair.head_price_type,
        head_quantity=quantity,
        head_quantity_type="qA",
        tail=pair.tail,
        tail_price_spec=pair.tail_price_spec,
        tail_price_spec_type=pair.tail_price_spec_type,
        amount_type="qAtApA",
    )


def _percent_pair(quantity: Decimal) -> OrderPairSpec:
    pair = _usd_pair(float(quantity))
    return OrderPairSpec(
        name=pair.name,
        window=pair.window,
        try_num=pair.try_num,
        dr_pause=pair.dr_pause,
        timeout=pair.timeout,
        head=pair.head,
        head_price=pair.head_price,
        head_price_type=pair.head_price_type,
        head_quantity=quantity,
        head_quantity_type="q%",
        tail=pair.tail,
        tail_price_spec=pair.tail_price_spec,
        tail_price_spec_type=pair.tail_price_spec_type,
        amount_type="q%tApA",
    )


def test_usd_quantity_floors_to_authorised_step() -> None:
    result = resolve_pair_usd_quantity(
        _usd_pair(),
        mark_price=100000,
        rules={
            "contractSize": 1,
            "quantityIncrement": "0.0001",
            "minimumQuantity": "0.0001",
        },
    )

    assert result.quantity == Decimal("0.0001")
    assert result.approx_usd == Decimal("10.0000")


def test_usd_quantity_rejects_when_flooring_reaches_zero() -> None:
    with pytest.raises(QuantityResolutionError, match="resolved=0"):
        resolve_pair_usd_quantity(
            _usd_pair(1),
            mark_price=100000,
            rules={
                "contractSize": 1,
                "quantityIncrement": "0.0001",
                "minimumQuantity": "0.0001",
            },
        )


def test_absolute_quantity_accepts_decimal_on_step() -> None:
    result = validate_pair_absolute_quantity(
        _absolute_pair(Decimal("0.0001")),
        rules={"quantityIncrement": "0.0001", "minQuantity": "0.0001"},
    )

    assert result.quantity == Decimal("0.0001")


def test_absolute_quantity_rejects_decimal_off_step() -> None:
    with pytest.raises(QuantityResolutionError, match="QTY_ABS_INVALID"):
        validate_pair_absolute_quantity(
            _absolute_pair(Decimal("0.00015")),
            rules={"quantityIncrement": "0.0001", "minQuantity": "0.0001"},
        )


def test_percent_balance_quantity_floors_to_authorised_step() -> None:
    result = resolve_pair_percent_balance_quantity(
        _percent_pair(Decimal("2.5")),
        available_usd=1000,
        mark_price=100000,
        rules={
            "contractSize": 1,
            "quantityIncrement": "0.0001",
            "minimumQuantity": "0.0001",
        },
    )

    assert result.percent == Decimal("2.5")
    assert result.available_usd == Decimal("1000")
    assert result.nominal_usd == Decimal("25.0")
    assert result.quantity == Decimal("0.0002")
    assert result.approx_usd == Decimal("20.0000")


def test_percent_balance_quantity_rejects_out_of_range_percent() -> None:
    with pytest.raises(QuantityResolutionError, match="must be below 100"):
        resolve_pair_percent_balance_quantity(
            _percent_pair(Decimal("100")),
            available_usd=1000,
            mark_price=100000,
            rules={"contractSize": 1, "quantityIncrement": "0.0001"},
        )
