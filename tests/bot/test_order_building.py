from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from kolabi.bot.domain import PairCycleState
from kolabi.bot.order_building import head_command, tail_command
from kolabi.bot.tail_tracking import initial_tail_trail
from kolabi.bot.tsv.parser import read_strategy_file
from kolabi.shared.core.runtime_types import RuntimeCommandKind, Symbol

DEFAULT_COLUMNS = (
    "name",
    "symbol",
    "tps_run",
    "essais",
    "pause",
    "tOut",
    "side",
    "oType",
    "hDelta",
    "tType",
    "tDelta",
    "qty",
    "tPrice",
    "tUblk",
    "wUblk",
    "pGate",
    "hPrice",
    "hook",
    "exchg",
)


def _org_row(values: list[str]) -> str:
    return "| " + " | ".join(values) + " |"


def _write_strategy(path: Path, row: dict[str, str]) -> Path:
    path.write_text(
        "\n".join(
            [
                _org_row(list(DEFAULT_COLUMNS)),
                "|" + "+".join("---" for _ in DEFAULT_COLUMNS) + "|",
                _org_row([row.get(column, "") for column in DEFAULT_COLUMNS]),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def test_org_strategy_route_reaches_head_and_tail_commands(tmp_path: Path) -> None:
    strategy = read_strategy_file(
        _write_strategy(
            tmp_path / "route.tsv",
            {
                "name": "BTX_SPOT",
                "symbol": "XBT_USDT",
                "tps_run": "0 60",
                "essais": "1",
                "tOut": "4",
                "side": "buy",
                "oType": "L",
                "tType": "S",
                "qty": "A3",
                "tPrice": "A8",
                "pGate": "D- -5",
                "exchg": "BTXS",
            },
        )
    )
    pair = strategy.pairs[0]
    state = PairCycleState(pair=pair)

    head = head_command(
        state,
        symbol=Symbol(pair.symbol or ""),
        kind=RuntimeCommandKind.PLACE,
    )
    tail = tail_command(
        state,
        symbol=Symbol(pair.symbol or ""),
        kind=RuntimeCommandKind.PLACE,
    )

    assert (head.exchange, head.market_type, head.symbol) == (
        "bitmex",
        "spot",
        "XBT_USDT",
    )
    assert (tail.exchange, tail.market_type, tail.symbol) == (
        "bitmex",
        "spot",
        "XBT_USDT",
    )


def test_trigger_limit_head_command_materialises_signed_delta_as_distance(tmp_path: Path) -> None:
    path = _write_strategy(
        tmp_path / "signed_delta.tsv",
        {
            "name": "KADA_BUY",
            "symbol": "PF_ADAUSD",
            "tps_run": "0 60",
            "essais": "1",
            "tOut": "4",
            "side": "buy",
            "oType": "LTm!",
            "hDelta": "D-.0002",
            "qty": "A14",
            "tType": "SLm",
            "pGate": "D- +",
            "hPrice": "D0",
            "tPrice": "B129.16",
            "exchg": "KRKF",
        },
    )
    strategy = read_strategy_file(path)
    pair = strategy.pairs[0]

    command = head_command(
        PairCycleState(pair=pair),
        symbol=Symbol(pair.symbol or ""),
        kind=RuntimeCommandKind.PLACE,
    )

    assert command.request.oDelta == Decimal("0.0002")


def test_dw_sel2_probe_head_materialises_post_only_touch_price(tmp_path: Path) -> None:
    strategy = read_strategy_file(
        _write_strategy(
            tmp_path / "dw_sel2.tsv",
            {
                "name": "DW_SEL2",
                "symbol": "PF_ADAUSD",
                "tps_run": "0 1440",
                "essais": "*",
                "tOut": "6",
                "pause": "1",
                "side": "sell",
                "oType": "LT!",
                "hPrice": "D.0",
                "tType": "S",
                "qty": "A15",
                "tPrice": "B246.93",
                "tUblk": "B89.60",
                "wUblk": ".5",
                "pGate": "B- -161.29",
                "exchg": "KRKF",
            },
        )
    )
    pair = strategy.pairs[0]
    state = PairCycleState(
        pair=pair,
        head_order_price=Decimal("0.0985"),
        head_order_stop_price=Decimal("0.0985"),
    )

    command = head_command(
        state,
        symbol=Symbol(pair.symbol or ""),
        kind=RuntimeCommandKind.PLACE,
    )

    assert pair.head_price == (-100000.0, -161.29)
    assert pair.head.delta is None
    assert pair.head_order_price_spec == 0.0
    assert pair.head_order_price_spec_type == "hD"
    assert pair.tail_unblock_spec == 89.6
    assert pair.tail_second_update_wait_seconds == 30.0
    assert command.request.ordType == "LT"
    assert command.request.side == "sell"
    assert command.request.price == Decimal("0.0985")
    assert command.request.stopPx == Decimal("0.0985")
    assert command.request.execInst == "ParticipateDoNotInitiate,LastPrice"
    assert command.request.oDelta is None


def test_post_only_zero_distance_limit_tail_materialises_marketable_price(
    tmp_path: Path,
) -> None:
    strategy = read_strategy_file(
        _write_strategy(
            tmp_path / "terminal_tail.tsv",
            {
                "name": "BIN_TERM",
                "symbol": "ADAUSDT",
                "tps_run": "0 60",
                "essais": "1",
                "tOut": "4",
                "side": "buy",
                "oType": "M",
                "tType": "L!",
                "qty": "A3",
                "tPrice": "D0",
                "pGate": "D- +",
                "hook": "PARENT-tail-closed",
                "exchg": "BINS",
            },
        )
    )
    pair = strategy.pairs[0]
    state = PairCycleState(
        pair=pair,
        played_quantity=Decimal("3"),
        tail_trail=initial_tail_trail(
            pair,
            Decimal("100.00"),
            datetime(2026, 6, 10, tzinfo=timezone.utc),
        ),
        instrument_tick_size=Decimal("0.01"),
    )

    command = tail_command(
        state,
        symbol=Symbol("ADAUSDT"),
        kind=RuntimeCommandKind.PLACE,
    )

    assert command.request.ordType == "L"
    assert command.request.execInst == "ParticipateDoNotInitiate"
    assert command.request.price == Decimal("99.99")
    assert command.request.stopPx is None
    assert command.legacy_order is not None
    assert command.legacy_order["price"] == Decimal("99.99")
