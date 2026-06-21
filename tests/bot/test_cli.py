from __future__ import annotations

import argparse
from decimal import Decimal

import pytest
from kolabi.bot.__main__ import (
    build_service,
    build_parser,
    build_single_strategy,
    preflight_command,
    run_command,
    run_once_command,
)
from kolabi.bot.domain import StrategySpec


def test_run_once_parser_accepts_typed_fields() -> None:
    parser = build_parser()

    args = parser.parse_args(
        [
            "run-once",
            "--symbol",
            "PI_XBTUSD",
            "-m",
            "XSellTail",
            "-t",
            "0",
            "1440",
            "-O",
            "60",
            "-x",
            "D1 2",
            "--qty",
            "A1",
            "--tPrice",
            "%0.5",
            "-o",
            "L",
            "-y",
            "S-",
            "-c",
            "sell",
            "--dry-run",
        ]
    )

    strategy = build_single_strategy(args)
    assert isinstance(strategy, StrategySpec)
    assert len(strategy.pairs) == 1
    pair = strategy.pairs[0]

    assert pair.name == "XSellTail"
    assert pair.window.start_minutes == 0.0
    assert pair.window.end_minutes == 1440.0
    assert pair.timeout == 60
    assert pair.head_price == (1.0, 2.0)
    assert pair.head_quantity == 1
    assert pair.tail_price_spec == 0.5
    assert pair.head.order_type == "L"
    assert pair.tail.order_type == "S-"
    assert pair.head.side.value == "sell"
    assert pair.amount_type == "qAt%pDhDoD"


def test_run_once_parser_keeps_percent_tail_percent_head_grammar() -> None:
    parser = build_parser()

    args = parser.parse_args(
        [
            "run-once",
            "-m",
            "XSellPercentTail",
            "-x",
            "%-1 1",
            "--qty",
            "A1",
            "--tPrice",
            "%0.5",
            "-o",
            "M",
            "-y",
            "S-",
            "-c",
            "sell",
            "--dry-run",
        ]
    )

    pair = build_single_strategy(args).pairs[0]

    assert pair.head_quantity_type == "qA"
    assert pair.tail_price_spec_type == "t%"
    assert pair.head_price_type == "p%"
    assert pair.tail_price_spec == 0.5


def test_run_once_parser_accepts_usd_notional_quantity() -> None:
    parser = build_parser()

    args = parser.parse_args(
        [
            "run-once",
            "-m",
            "XUsdQty",
            "-x",
            "D1 2",
            "--qty",
            "U15.5",
            "--tPrice",
            "%0.5",
            "-o",
            "L",
            "-y",
            "S-",
            "-c",
            "sell",
            "--dry-run",
        ]
    )

    pair = build_single_strategy(args).pairs[0]

    assert pair.head_quantity == 15.5
    assert pair.head_quantity_type == "qU"


def test_run_once_parser_accepts_decimal_absolute_quantity() -> None:
    parser = build_parser()

    args = parser.parse_args(
        [
            "run-once",
            "-m",
            "XAbsQty",
            "-x",
            "D1 2",
            "--qty",
            "A0.0001",
            "--tPrice",
            "%0.5",
            "-o",
            "L",
            "-y",
            "S-",
            "-c",
            "sell",
            "--dry-run",
        ]
    )

    pair = build_single_strategy(args).pairs[0]

    assert pair.head_quantity == Decimal("0.0001")
    assert pair.head_quantity_type == "qA"


def test_run_once_parser_accepts_decimal_percent_quantity() -> None:
    parser = build_parser()

    args = parser.parse_args(
        [
            "run-once",
            "-m",
            "XPctQty",
            "-x",
            "D1 2",
            "--qty",
            "%0.5",
            "--tPrice",
            "%0.5",
            "-o",
            "L",
            "-y",
            "S-",
            "-c",
            "sell",
            "--dry-run",
        ]
    )

    pair = build_single_strategy(args).pairs[0]

    assert pair.head_quantity == Decimal("0.5")
    assert pair.head_quantity_type == "q%"


@pytest.mark.parametrize(
    "removed_args",
    [
        ["--aType", "qAt%pD"],
        ["--quantity", "1"],
        ["-q", "1"],
        ["--tailPrice", "0.5"],
        ["-T", "0.5"],
        ["--pgate", "D1 2"],
        ["--hprice", "D1"],
        ["--tp", "%0.5"],
        ["--oDelta", "D1"],
    ],
)
def test_run_once_parser_rejects_removed_legacy_args(removed_args: list[str]) -> None:
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "run-once",
                "-m",
                "XRemoved",
                "-x",
                "D1 2",
                "--qty",
                "A1",
                "--tPrice",
                "%0.5",
                "-o",
                "L",
                "-y",
                "S-",
                "-c",
                "sell",
                *removed_args,
                "--dry-run",
            ]
        )


def test_run_once_parser_accepts_tublk_keyword() -> None:
    parser = build_parser()

    args = parser.parse_args(
        [
            "run-once",
            "-m",
            "XTublk",
            "-x",
            "D1 2",
            "--qty",
            "A1",
            "--tPrice",
            "%0.5",
            "--tUblk",
            "D5",
            "-o",
            "L",
            "-y",
            "S-",
            "-c",
            "sell",
            "--dry-run",
        ]
    )

    pair = build_single_strategy(args).pairs[0]

    assert pair.tail_unblock_spec == 5.0
    assert pair.tail_unblock_spec_type == "uD"


def test_run_once_parser_accepts_wublk_keyword() -> None:
    parser = build_parser()

    args = parser.parse_args(
        [
            "run-once",
            "-m",
            "XWublk",
            "-x",
            "D1 2",
            "--qty",
            "A1",
            "--tPrice",
            "%0.5",
            "--wUblk",
            "6",
            "-o",
            "L",
            "-y",
            "S-",
            "-c",
            "sell",
            "--dry-run",
        ]
    )

    pair = build_single_strategy(args).pairs[0]

    assert pair.tail_second_update_wait_seconds == 360.0


def test_run_once_parser_accepts_cool_keyword() -> None:
    parser = build_parser()

    args = parser.parse_args(
        [
            "run-once",
            "-m",
            "XCool",
            "-x",
            "D1 2",
            "--qty",
            "A1",
            "--tPrice",
            "%0.5",
            "--cool",
            "2.5",
            "-o",
            "L",
            "-y",
            "S-",
            "-c",
            "sell",
            "--dry-run",
        ]
    )

    pair = build_single_strategy(args).pairs[0]

    assert pair.cooldown_minutes == 2.5


def test_bot_parser_uses_critical_db_url_only() -> None:
    parser = build_parser()

    args = parser.parse_args(
        [
            "run",
            "--strategy",
            "orders/advers.tsv",
            "--critical-db-url",
            "postgresql+psycopg://x/critical",
            "--audit-db-url",
            "postgresql+psycopg://x/audit",
            "--telemetry-db-url",
            "postgresql+psycopg://x/telemetry",
            "--rest-audit-retention-minutes",
            "60",
            "--rest-audit-retention-limit",
            "10",
            "--tail-telemetry-retention-minutes",
            "30",
            "--tail-telemetry-retention-limit",
            "5",
            "--account-scope",
            "advers",
            "--dry-run",
        ]
    )

    assert args.critical_account_db_url == "postgresql+psycopg://x/critical"
    assert args.audit_db_url == "postgresql+psycopg://x/audit"
    assert args.telemetry_db_url == "postgresql+psycopg://x/telemetry"
    assert args.rest_audit_retention_minutes == 60
    assert args.rest_audit_retention_limit == 10
    assert args.tail_telemetry_retention_minutes == 30
    assert args.tail_telemetry_retention_limit == 5
    assert args.account_scope == "advers"
    assert args.market_type == "futures"
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "run",
                "--strategy",
                "orders/advers.tsv",
                "--critical-account-db-url",
                "postgresql+psycopg://x/critical",
            ]
        )


def test_run_parser_accepts_default_market_type() -> None:
    parser = build_parser()

    args = parser.parse_args(
        [
            "run",
            "--strategy",
            "orders/advers.tsv",
            "--exchange",
            "binance",
            "--market-type",
            "spot",
            "--symbol",
            "BTCUSDT",
            "--base-url",
            "https://spot-demo.example.test",
            "--dry-run",
        ]
    )

    assert args.exchange == "binance"
    assert args.market_type == "spot"
    assert args.symbol == "BTCUSDT"
    assert args.base_url == "https://spot-demo.example.test"


def test_run_service_defaults_symbol_from_exchange_market_route() -> None:
    parser = build_parser()

    args = parser.parse_args(
        [
            "run",
            "--strategy",
            "orders/advers.tsv",
            "--exchange",
            "binance",
            "--market-type",
            "spot",
            "--dry-run",
        ]
    )
    service = build_service(args)

    assert not hasattr(args, "symbol")
    assert service.config.exchange == "binance"
    assert service.config.market_type == "spot"
    assert service.config.symbol == "BTCUSDT"


def test_run_service_forwards_base_url_override() -> None:
    parser = build_parser()

    args = parser.parse_args(
        [
            "run",
            "--strategy",
            "orders/advers.tsv",
            "--exchange",
            "binance",
            "--market-type",
            "spot",
            "--base-url",
            "https://spot-demo.example.test",
            "--dry-run",
        ]
    )
    service = build_service(args)

    assert service.config.base_url == "https://spot-demo.example.test"


def test_preflight_parser_accepts_account_scope() -> None:
    parser = build_parser()

    args = parser.parse_args(
        [
            "preflight",
            "--market-type",
            "spot",
            "--account-scope",
            "advers",
            "--critical-db-url",
            "postgresql+psycopg://x/critical",
            "--base-url",
            "https://spot-demo.example.test",
            "--api-key-env",
            "CUSTOM_BINS_KEY",
            "--api-secret-env",
            "CUSTOM_BINS_SECRET",
        ]
    )

    assert args.account_scope == "advers"
    assert args.critical_account_db_url == "postgresql+psycopg://x/critical"
    assert args.market_type == "spot"
    assert args.base_url == "https://spot-demo.example.test"
    assert args.api_key_env == "CUSTOM_BINS_KEY"
    assert args.api_secret_env == "CUSTOM_BINS_SECRET"


def test_preflight_command_defaults_symbol_from_exchange_market_route(
    monkeypatch,
    capsys,
) -> None:
    parser = build_parser()
    observed: dict[str, object] = {}

    class FakeService:
        def __init__(self, config) -> None:
            observed["exchange"] = config.exchange
            observed["market_type"] = config.market_type
            observed["symbol"] = config.symbol
            observed["api_key_env"] = config.api_key_env
            observed["api_secret_env"] = config.api_secret_env
            observed["base_url"] = config.base_url

        def preflight(self, strategy):
            observed["strategy"] = strategy
            return {"ready": True, "status": "ok"}

    monkeypatch.setattr("kolabi.bot.__main__.BotService", FakeService)
    args = parser.parse_args(
        [
            "preflight",
            "--exchange",
            "bitmex",
            "--market-type",
            "spot",
            "--api-key-env",
            "CUSTOM_BINS_KEY",
            "--api-secret-env",
            "CUSTOM_BINS_SECRET",
            "--base-url",
            "https://spot-demo.example.test",
        ]
    )

    assert preflight_command(args) == 0
    assert observed == {
        "exchange": "bitmex",
        "market_type": "spot",
        "symbol": "XBT_USDT",
        "api_key_env": "CUSTOM_BINS_KEY",
        "api_secret_env": "CUSTOM_BINS_SECRET",
        "base_url": "https://spot-demo.example.test",
        "strategy": None,
    }
    assert '"ready": true' in capsys.readouterr().out


def test_run_once_command_dry_run_prints_canonical_structure(capsys) -> None:
    args = argparse.Namespace(
        name="XSellTail",
        tps_run=[0.0, 1440.0],
        essais="1",
        pause=None,
        tOut=60,
        side="sell",
        pGate="D1 2",
        qty="A1",
        tPrice="%0.5",
        oType="L",
        hDelta=None,
        tDelta=None,
        tType="S-",
        hook="",
        dry_run=True,
        sync=False,
        simulate=False,
        exchange="kraken",
        symbol="PI_XBTUSD",
        environment="demo",
        db_url=None,
        market_db_url=None,
        account_db_url=None,
        skip_ready_check=True,
        ready_timeout_seconds=45.0,
        ready_poll_seconds=1.0,
        max_public_age_seconds=15.0,
        max_private_age_seconds=30.0,
        max_reconcile_age_seconds=300.0,
        log_level="INFO",
        update_pause=10,
        log_pause=60,
    )

    exit_code = run_once_command(args)

    assert exit_code == 0
    output = capsys.readouterr().out
    assert '"name": "XSellTail"' in output
    assert '"pairs"' in output
    assert '"commands"' in output
    assert '"kind": "place"' in output


def test_run_and_run_once_share_bot_service_path(monkeypatch) -> None:
    class RecordingService:
        def __init__(self) -> None:
            self.calls: list[tuple[StrategySpec, bool, bool]] = []

        def run_strategy(self, strategy: StrategySpec, *, dry_run: bool, simulate: bool):
            from datetime import datetime, timezone

            from kolabi.bot.domain import PairCycleState, StrategyState
            from kolabi.bot.strategy_runtime import StrategyRunResult

            self.calls.append((strategy, dry_run, simulate))
            state = StrategyState(
                launched_at=datetime.now(timezone.utc),
                pairs={pair.name: PairCycleState(pair=pair) for pair in strategy.pairs},
            )
            return StrategyRunResult(state=state, commands=(), notices=())

    service = RecordingService()

    monkeypatch.setattr("kolabi.bot.__main__.build_service", lambda _args: service)

    run_args = argparse.Namespace(
        strategy="orders/demo_ada.org",
        dry_run=False,
        sync=True,
        simulate=False,
    )
    run_once_args = argparse.Namespace(
        name="XSellTail",
        tps_run=[0.0, 1440.0],
        essais="1",
        pause=None,
        tOut=60,
        side="sell",
        pGate="D1 2",
        qty="A1",
        tPrice="%0.5",
        oType="L",
        hDelta=None,
        tDelta=None,
        tType="S-",
        hook="",
        dry_run=False,
        sync=True,
        simulate=False,
    )
    strategy = build_single_strategy(run_once_args)
    monkeypatch.setattr("kolabi.bot.__main__.read_strategy_file", lambda _path: strategy)

    assert run_command(run_args) == 0
    assert run_once_command(run_once_args) == 0
    assert len(service.calls) == 2
    assert len(service.calls[0][0].pairs) == 1
    assert len(service.calls[1][0].pairs) == 1
    assert service.calls[0][1] is False
    assert service.calls[1][1] is False
    assert service.calls[0][2] is False
    assert service.calls[1][2] is False


def test_run_once_returns_130_on_keyboard_interrupt(monkeypatch, capsys) -> None:
    class InterruptingService:
        def run_strategy(self, strategy: StrategySpec, *, dry_run: bool, simulate: bool):
            del strategy, dry_run, simulate
            raise KeyboardInterrupt

    monkeypatch.setattr("kolabi.bot.__main__.build_service", lambda _args: InterruptingService())
    args = argparse.Namespace(
        name="XSellTail",
        tps_run=[0.0, 1440.0],
        essais="1",
        pause=None,
        tOut=60,
        side="sell",
        pGate="D1 2",
        qty="A1",
        tPrice="%0.5",
        oType="L",
        hDelta=None,
        tDelta=None,
        tType="S-",
        hook="",
        dry_run=False,
        sync=True,
        simulate=False,
    )
    exit_code = run_once_command(args)
    assert exit_code == 130
    assert "Interrupted by operator." in capsys.readouterr().out
