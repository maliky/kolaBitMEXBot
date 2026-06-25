from __future__ import annotations

import re
from datetime import datetime, timezone

from kolabi.bot.domain import HeadSpec, OrderPairSpec, TailSpec, TimeWindow
from kolabi.bot.ids import head_client_order_id, tail_client_order_id
from kolabi.shared.core.runtime_types import Side


def _pair(name: str = "pair-a") -> OrderPairSpec:
    return OrderPairSpec(
        name=name,
        window=TimeWindow(start_minutes=0, end_minutes=1440),
        try_num=1,
        dr_pause=None,
        timeout=60,
        head=HeadSpec(side=Side.BUY, order_type="M"),
        head_price=(-0.25, 0.25),
        head_price_type="pD",
        head_quantity=1,
        head_quantity_type="q",
        tail=TailSpec(side=Side.SELL, order_type="S-"),
        tail_price_spec=1.5,
        tail_price_spec_type="tB",
        amount_type="q",
    )


def test_head_client_order_id_is_readable_and_safe() -> None:
    value = head_client_order_id(
        _pair(),
        attempt_index=2,
        at=datetime(2026, 5, 25, 18, 0, 0, tzinfo=timezone.utc),
    )

    assert value.startswith("H2")
    assert len(value) <= 64
    assert re.fullmatch(r"[A-Za-z0-9-]+", value) is not None
    assert len(value.split("-")) == 2
    assert value.split("-")[-1] == "260525180000"


def test_tail_client_order_id_is_readable_and_safe() -> None:
    value = tail_client_order_id(
        _pair(),
        attempt_index=3,
        at=datetime(2026, 5, 25, 18, 0, 0, tzinfo=timezone.utc),
    )

    assert value.startswith("T3")
    assert len(value) <= 64
    assert re.fullmatch(r"[A-Za-z0-9-]+", value) is not None
    assert len(value.split("-")) == 2
    assert value.split("-")[-1] == "260525180000"
