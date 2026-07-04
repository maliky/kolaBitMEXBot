from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from kolabi.bot.domain import (
    HeadSpec,
    HeadState,
    OrderIdentity,
    OrderPairSpec,
    PairCycleState,
    Side,
    StrategySpec,
    TailSpec,
    TailState,
    TimeWindow,
)
from kolabi.bot.exchange_routes import ExchangeRoute
from kolabi.bot.indicators import DummyIndicatorClient
from kolabi.bot.service import (
    AdapterExchangePort,
    BotConfig,
    BotService,
    SymbolRoutingExchangePort,
    _validate_route_symbol,
)
from kolabi.bot.strategy_runtime import (
    StrategyRunResult,
    StrategyRuntime,
    _CommandSlot,
    _OrderLease,
)
from kolabi.bot.tsv import read_strategy_file
from kolabi.shared.config import ExchangeConfig
from kolabi.shared.core.models import OrderAck, Position
from kolabi.shared.core.runtime_types import (
    AmendHeadCommand,
    AmendOrderCommandRequest,
    AmendTailCommand,
    CancelCommand,
    CancelOrderCommandRequest,
    PlaceHeadCommand,
    PlaceOrderCommandRequest,
    PlaceTailCommand,
    PrivateOrderRecord,
    RuntimeCommandKind,
    Symbol,
)
from kolabi.shared.runtime_state import (
    KrakenRuntimeStateClient,
    PrivateFeedState,
    PublicMarketState,
    StrategyRuntimeState,
)
from kolabi.tree.account import AccountStateStore, AccountStreamConfig
from kolabi.tree.kraken import KrakenConfig, KrakenTree
from sqlalchemy.exc import SQLAlchemyError


def _ready_runtime_state(
    *,
    symbol: str,
    exchange: str,
    market_type: str,
) -> StrategyRuntimeState:
    return StrategyRuntimeState(
        symbol=symbol,
        exchange=exchange,
        market_type=market_type,
        public=PublicMarketState(
            symbol=symbol,
            best_bid=100.0,
            best_ask=101.0,
            mid_price=100.5,
            last_price=100.5,
            mark_price=100.5,
            index_price=100.5,
            tick_size=0.01,
            spread=1.0,
            imbalance=None,
            avg_bid=100.0,
            avg_ask=101.0,
            recorded_at="2026-06-10T00:00:00+00:00",
            source_timestamp="2026-06-10T00:00:00+00:00",
            age_seconds=1.0,
            source_age_seconds=1.0,
            indicators={},
            ready=True,
            reason=None,
        ),
        private_ws=PrivateFeedState(
            stream_kind="private_ws",
            status="ok",
            updated_at="2026-06-10T00:00:00+00:00",
            last_heartbeat_at="2026-06-10T00:00:00+00:00",
            age_seconds=1.0,
            ready=True,
            last_error=None,
            reason=None,
        ),
        rest_reconciler=PrivateFeedState(
            stream_kind="rest_reconciler",
            status="ok",
            updated_at="2026-06-10T00:00:00+00:00",
            last_heartbeat_at="2026-06-10T00:00:00+00:00",
            age_seconds=1.0,
            ready=True,
            last_error=None,
            reason=None,
        ),
        open_order_count=0,
        fill_count=0,
        position_size=None,
        position_entry_price=None,
        ready=True,
        reasons=(),
    )


def _xbt_sell_tail_pair() -> OrderPairSpec:
    """Small local pair replacing the removed XBT sell-tail fixture file."""

    return OrderPairSpec(
        name="XSellTail",
        window=TimeWindow(start_minutes=0.0, end_minutes=1440.0),
        try_num=1,
        dr_pause=None,
        timeout=60,
        head=HeadSpec(side=Side.SELL, order_type="L"),
        head_price=(1.0, 2.0),
        head_price_type="pD",
        head_quantity=1,
        head_quantity_type="qA",
        tail=TailSpec(side=Side.BUY, order_type="S-"),
        tail_price_spec=49.88,
        tail_price_spec_type="tB",
        amount_type="qA",
        head_order_price_spec=1.0,
        head_order_price_spec_type="hD",
    )


def _xbt_sell_tail_strategy() -> StrategySpec:
    return StrategySpec(name="xbt-sell-tail", pairs=(_xbt_sell_tail_pair(),))


def test_demo_ada_strategy_parsed_and_planned_on_active_runtime() -> None:
    strategy = read_strategy_file(Path("orders/demo_ada.org"))
    assert len(strategy.pairs) >= 2

    service = BotService(
        BotConfig(symbol="XBTUSD", require_ready=False),
        indicators=DummyIndicatorClient({"ma": 42}),
    )
    result = service.run_strategy(strategy, dry_run=True)

    assert isinstance(result, StrategyRunResult)
    assert len(result.commands) == len(strategy.pairs)
    first = result.commands[0]
    first_pair = strategy.pairs[0]
    assert first.pair_name == first_pair.name
    assert first.role is not None and first.role.value == "head"
    assert first.request is not None


def test_bot_service_keeps_audit_and_telemetry_lanes_off_account_db(
    postgres_url_factory,
    monkeypatch,
) -> None:
    monkeypatch.setenv("KRKF_DEMO_API_KEY", "k")
    monkeypatch.setenv("KRKF_DEMO_API_SECRET", "s")
    account_db_url = postgres_url_factory("account")
    audit_db_url = postgres_url_factory("audit")
    telemetry_db_url = postgres_url_factory("telemetry")
    service = BotService(
        BotConfig(
            symbol="PI_XBTUSD",
            exchange="kraken",
            require_ready=False,
            account_db_url=account_db_url,
            audit_db_url=audit_db_url,
            telemetry_db_url=telemetry_db_url,
            account_scope="advers",
        ),
        indicators=DummyIndicatorClient({"ma": 42}),
    )

    assert service._account_db_url == account_db_url
    assert service._audit_db_url == audit_db_url
    assert service._telemetry_db_url == telemetry_db_url
    service._ensure_exchange_config()

    assert service.exchange_config is not None
    assert service.exchange_config.adapter_kwargs["account_db_url"] == account_db_url
    assert service.exchange_config.adapter_kwargs["audit_db_url"] == audit_db_url
    assert service.exchange_config.adapter_kwargs["account_scope"] == "advers"


def test_bot_service_builds_runtime_state_for_bitmex(monkeypatch) -> None:
    monkeypatch.setenv("BTX_DEMO_API_KEY", "k")
    monkeypatch.setenv("BTX_DEMO_API_SECRET", "s")

    service = BotService(
        BotConfig(
            symbol="XBTUSD",
            exchange="bitmex",
            require_ready=False,
            market_db_url="postgresql+psycopg://x/market",
            account_db_url="postgresql+psycopg://x/account",
            critical_account_db_url="postgresql+psycopg://x/critical",
        )
    )

    assert service.runtime_state is not None
    assert service.runtime_state.exchange == "bitmex"
    assert service._market_db_url == "postgresql+psycopg://x/market"
    assert service._account_db_url == "postgresql+psycopg://x/account"
    service._ensure_exchange_config()
    assert service.exchange_config is not None
    assert service.exchange_config.adapter_kwargs["market_type"] == "futures"
    assert service.exchange_config.adapter_kwargs["public_db_url"] == (
        "postgresql+psycopg://x/market"
    )
    assert service.exchange_config.adapter_kwargs["account_db_url"] == (
        "postgresql+psycopg://x/account"
    )


def test_bot_service_defaults_bitmex_account_scoped_lanes(monkeypatch) -> None:
    for name in (
        "KOLABI_MARKET_DB_URL",
        "KOLABI_ACCOUNT_DB_URL",
        "KOLABI_CRITICAL_DB_URL",
        "KOLABI_AUDIT_DB_URL",
        "KOLABI_TELEMETRY_DB_URL",
        "KOLABI_ADVERS_ACCOUNT_DB_URL",
        "KOLABI_ADVERS_CRITICAL_DB_URL",
        "KOLABI_ADVERS_AUDIT_DB_URL",
        "KOLABI_ADVERS_TELEMETRY_DB_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BTX_DEMO_API_KEY", "k")
    monkeypatch.setenv("BTX_DEMO_API_SECRET", "s")

    service = BotService(
        BotConfig(
            symbol="XBTUSD",
            exchange="bitmex",
            market_type="futures",
            environment="demo",
            require_ready=False,
            account_scope="advers",
        )
    )

    assert service._market_db_url == (
        "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_market"
    )
    assert service._account_db_url == (
        "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_account_advers"
    )
    assert service._critical_account_db_url == (
        "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_critical_advers"
    )
    assert service._audit_db_url == (
        "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_audit_advers"
    )
    assert service._telemetry_db_url == (
        "postgresql+psycopg://kolabi:kolabi@127.0.0.1:15433/kolabi_telemetry_advers"
    )
    service._ensure_exchange_config()
    assert service.exchange_config is not None
    assert service.exchange_config.adapter_kwargs["audit_db_url"] == service._audit_db_url
    assert service.exchange_config.adapter_kwargs["account_scope"] == "advers"


def test_runtime_state_readiness_is_scoped_by_exchange_market_and_symbol(
    postgres_url_factory,
) -> None:
    market_db_url = postgres_url_factory("route-scope-market")
    account_db_url = postgres_url_factory("route-scope-account")
    critical_db_url = postgres_url_factory("route-scope-critical")
    now = datetime.now(timezone.utc)
    target = ("binance", "spot", "BTCUSDT")

    wrong_public = KrakenTree(
        KrakenConfig(
            db_url=market_db_url,
            exchange="kraken",
            market_type="futures",
            pair=target[2],
        )
    )
    wrong_public.process_book(
        asks=[{"price": 101.0, "qty": 1.0}],
        bids=[{"price": 100.0, "qty": 1.0}],
    )
    wrong_private = AccountStateStore(
        AccountStreamConfig(
            db_url=critical_db_url,
            exchange="kraken",
            market_type="futures",
        )
    )
    wrong_private.record_connection_status("private_ws", "healthy", now)

    reader = KrakenRuntimeStateClient(
        market_db_url=market_db_url,
        account_db_url=account_db_url,
        critical_account_db_url=critical_db_url,
        exchange=target[0],
        market_type=target[1],
        symbol=target[2],
        max_public_age_seconds=60.0,
        max_private_age_seconds=60.0,
    )

    wrong_state = reader.fetch_runtime_state()

    assert wrong_state.ready is False
    assert wrong_state.exchange == "binance"
    assert wrong_state.market_type == "spot"
    assert "missing public market snapshot" in wrong_state.reasons
    assert "missing private_ws state" in wrong_state.reasons

    target_public = KrakenTree(
        KrakenConfig(
            db_url=market_db_url,
            exchange=target[0],
            market_type=target[1],
            pair=target[2],
        )
    )
    target_public.process_book(
        asks=[{"price": 101.0, "qty": 1.0}],
        bids=[{"price": 100.0, "qty": 1.0}],
    )
    target_private = AccountStateStore(
        AccountStreamConfig(
            db_url=critical_db_url,
            exchange=target[0],
            market_type=target[1],
        )
    )
    target_private.record_connection_status("private_ws", "healthy", now)

    ready_state = reader.fetch_runtime_state()

    assert ready_state.ready is True
    assert ready_state.reasons == ()
    assert ready_state.public.best_bid == 100.0
    assert ready_state.public.best_ask == 101.0
    assert ready_state.private_ws.status == "healthy"


def test_bot_service_uses_scoped_kolabi_db_env_lanes(monkeypatch) -> None:
    monkeypatch.setenv("KOLABI_MARKET_DB_URL", "postgresql+psycopg://x/market")
    monkeypatch.setenv("KOLABI_ACCOUNT_DB_URL", "postgresql+psycopg://x/main_account")
    monkeypatch.setenv("KOLABI_CRITICAL_DB_URL", "postgresql+psycopg://x/main_critical")
    monkeypatch.setenv("KOLABI_AUDIT_DB_URL", "postgresql+psycopg://x/main_audit")
    monkeypatch.setenv("KOLABI_TELEMETRY_DB_URL", "postgresql+psycopg://x/main_telemetry")
    monkeypatch.setenv(
        "KOLABI_ADVERS_ACCOUNT_DB_URL",
        "postgresql+psycopg://x/advers_account",
    )
    monkeypatch.setenv(
        "KOLABI_ADVERS_CRITICAL_DB_URL",
        "postgresql+psycopg://x/advers_critical",
    )
    monkeypatch.setenv(
        "KOLABI_ADVERS_AUDIT_DB_URL",
        "postgresql+psycopg://x/advers_audit",
    )
    monkeypatch.setenv(
        "KOLABI_ADVERS_TELEMETRY_DB_URL",
        "postgresql+psycopg://x/advers_telemetry",
    )
    service = BotService(
        BotConfig(
            symbol="PI_XBTUSD",
            exchange="kraken",
            require_ready=False,
            account_scope="advers",
        ),
        indicators=DummyIndicatorClient({"ma": 42}),
    )

    assert service._market_db_url == "postgresql+psycopg://x/market"
    assert service._account_db_url == "postgresql+psycopg://x/advers_account"
    assert service._critical_account_db_url == "postgresql+psycopg://x/advers_critical"
    assert service._audit_db_url == "postgresql+psycopg://x/advers_audit"
    assert service._telemetry_db_url == "postgresql+psycopg://x/advers_telemetry"


def test_bot_service_dry_run_preserves_pair_symbols() -> None:
    base_pair = _xbt_sell_tail_pair()
    strategy = StrategySpec(
        name="multi-symbol",
        pairs=(
            replace(base_pair, name="xbt", symbol="PI_XBTUSD"),
            replace(base_pair, name="eth", symbol="PI_ETHUSD"),
        ),
    )
    service = BotService(
        BotConfig(symbol="PI_XBTUSD", exchange="kraken", require_ready=False),
        indicators=DummyIndicatorClient({"ma": 42}),
    )

    result = service.run_strategy(strategy, dry_run=True)

    assert {str(command.symbol) for command in result.commands} == {
        "PI_XBTUSD",
        "PI_ETHUSD",
    }
    assert service._required_symbols == ("PI_ETHUSD", "PI_XBTUSD")


def test_bot_service_dry_run_preserves_pair_exchange_routes() -> None:
    base_pair = _xbt_sell_tail_pair()
    strategy = StrategySpec(
        name="multi-exchange",
        pairs=(
            replace(base_pair, name="krk", symbol="PI_XBTUSD", exchange="kraken"),
            replace(base_pair, name="bin", symbol="BTCUSDT", exchange="binance"),
        ),
    )
    service = BotService(
        BotConfig(symbol="PI_XBTUSD", exchange="kraken", require_ready=False),
        indicators=DummyIndicatorClient({"ma": 42}),
    )

    result = service.run_strategy(strategy, dry_run=True)

    assert {
        (command.exchange, command.market_type, str(command.symbol))
        for command in result.commands
    } == {
        ("kraken", "futures", "PI_XBTUSD"),
        ("binance", "futures", "BTCUSDT"),
    }
    assert tuple(route.label for route in service._required_routes) == (
        "binance:futures:BTCUSDT",
        "kraken:futures:PI_XBTUSD",
    )


def test_bot_service_default_market_type_applies_to_rows_without_exchange_code() -> None:
    base_pair = _xbt_sell_tail_pair()
    strategy = StrategySpec(name="default-spot", pairs=(replace(base_pair, symbol=None),))
    service = BotService(
        BotConfig(
            symbol="BTCUSDT",
            exchange="binance",
            market_type="spot",
            require_ready=False,
        ),
        indicators=DummyIndicatorClient({"ma": 42}),
    )

    result = service.run_strategy(strategy, dry_run=True)

    assert {
        (command.exchange, command.market_type, str(command.symbol))
        for command in result.commands
    } == {("binance", "spot", "BTCUSDT")}
    assert tuple(route.label for route in service._required_routes) == (
        "binance:spot:BTCUSDT",
    )


def test_bot_service_default_market_type_reaches_exchange_config(monkeypatch) -> None:
    monkeypatch.setenv("BINS_DEMO_API_KEY", "spot-key")
    monkeypatch.setenv("BINS_DEMO_API_SECRET", "spot-secret")
    service = BotService(
        BotConfig(
            symbol="BTCUSDT",
            exchange="binance",
            market_type="spot",
            require_ready=False,
        ),
        indicators=DummyIndicatorClient({"ma": 42}),
    )

    service._ensure_exchange_config()

    assert service.exchange_config is not None
    assert service.exchange_config.base_url == "https://testnet.binance.vision"
    assert service.exchange_config.adapter_kwargs["market_type"] == "spot"
    assert (
        service._exchange_config_cache[("binance", "spot", "BTCUSDT")]
        is service.exchange_config
    )


def test_bot_service_default_route_uses_base_url_override(monkeypatch) -> None:
    monkeypatch.setenv("BINM_DEMO_API_KEY", "margin-key")
    monkeypatch.setenv("BINM_DEMO_API_SECRET", "margin-secret")
    service = BotService(
        BotConfig(
            symbol="BTCUSDT",
            exchange="binance",
            market_type="margin",
            base_url="https://margin-demo.example.test",
            require_ready=False,
        ),
        indicators=DummyIndicatorClient({"ma": 42}),
    )

    cfg = service._exchange_config_for_route(
        ExchangeRoute("binance", "margin", "BTCUSDT")
    )

    assert cfg.base_url == "https://margin-demo.example.test"
    assert cfg.adapter_kwargs["market_type"] == "margin"


def test_bot_service_base_url_override_is_default_route_only(monkeypatch) -> None:
    monkeypatch.setenv("BINS_DEMO_API_KEY", "spot-key")
    monkeypatch.setenv("BINS_DEMO_API_SECRET", "spot-secret")
    monkeypatch.setenv("BINM_DEMO_API_KEY", "margin-key")
    monkeypatch.setenv("BINM_DEMO_API_SECRET", "margin-secret")
    service = BotService(
        BotConfig(
            symbol="BTCUSDT",
            exchange="binance",
            market_type="spot",
            base_url="https://spot-demo.example.test",
            require_ready=False,
        ),
        indicators=DummyIndicatorClient({"ma": 42}),
    )

    with pytest.raises(ValueError, match="margin demo requires"):
        service._exchange_config_for_route(
            ExchangeRoute("binance", "margin", "BTCUSDT")
        )


def test_mixed_strategy_missing_route_credentials_names_pair_and_route(
    monkeypatch,
) -> None:
    for name in (
        "BINS_DEMO_API_KEY",
        "BINS_DEMO_API_SECRET",
        "BINANCE_SPOT_DEMO_API_KEY",
        "BINANCE_SPOT_DEMO_API_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)
    base_pair = _xbt_sell_tail_pair()
    spot_pair = replace(
        base_pair,
        name="bin_spot",
        exchange="binance",
        market_type="spot",
        symbol="BTCUSDT",
        tail=replace(base_pair.tail, order_type="S"),
    )
    strategy = StrategySpec(name="mixed-missing-key", pairs=(spot_pair,))
    service = BotService(
        BotConfig(symbol="PI_XBTUSD", exchange="kraken", require_ready=False),
        indicators=DummyIndicatorClient({"ma": 42}),
    )

    with pytest.raises(ValueError) as exc_info:
        service.run_strategy(strategy, dry_run=False, simulate=True)

    message = str(exc_info.value)
    assert "Strategy pair 'bin_spot'" in message
    assert "route binance:spot:BTCUSDT" in message
    assert "BINS_DEMO_API_KEY" in message
    assert "BINS_DEMO_API_SECRET" in message


def test_preflight_reports_missing_strategy_route_credentials_without_adapter(
    monkeypatch,
) -> None:
    for name in (
        "BINS_DEMO_API_KEY",
        "BINS_DEMO_API_SECRET",
        "BINANCE_SPOT_DEMO_API_KEY",
        "BINANCE_SPOT_DEMO_API_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)
    base_pair = _xbt_sell_tail_pair()
    spot_pair = replace(
        base_pair,
        name="bin_spot",
        exchange="binance",
        market_type="spot",
        symbol="BTCUSDT",
        tail=replace(base_pair.tail, order_type="S"),
    )
    service = BotService(
        BotConfig(symbol="PI_XBTUSD", exchange="kraken", require_ready=False),
        indicators=DummyIndicatorClient({"ma": 42}),
    )
    service.runtime_state = None

    def _unexpected_config_load(_route):
        raise AssertionError("preflight must not build route adapter config")

    monkeypatch.setattr(service, "_exchange_config_for_route", _unexpected_config_load)

    payload = service.preflight(StrategySpec(name="missing-creds", pairs=(spot_pair,)))

    assert payload["ready"] is False
    assert payload["status"] == "waiting"
    assert payload["credentials_ready"] is False
    assert payload["reasons"] == (
        "binance:spot:BTCUSDT:missing credentials api_key,api_secret",
    )
    route = payload["credential_routes"][0]
    assert route["route"] == "binance:spot:BTCUSDT"
    assert route["api_key_env"] == [
        "BINS_DEMO_API_KEY",
        "BINANCE_SPOT_DEMO_API_KEY",
    ]
    assert route["api_secret_env"] == [
        "BINS_DEMO_API_SECRET",
        "BINANCE_SPOT_DEMO_API_SECRET",
    ]
    assert route["credentials_present"] is False
    assert route["api_key_source"] is None
    assert route["api_secret_source"] is None


def test_preflight_marks_present_strategy_route_credentials_without_values(
    monkeypatch,
) -> None:
    monkeypatch.setenv("BINS_DEMO_API_KEY", "spot-key")
    monkeypatch.setenv("BINS_DEMO_API_SECRET", "spot-secret")
    base_pair = _xbt_sell_tail_pair()
    spot_pair = replace(
        base_pair,
        name="bin_spot",
        exchange="binance",
        market_type="spot",
        symbol="BTCUSDT",
        tail=replace(base_pair.tail, order_type="S"),
    )
    service = BotService(
        BotConfig(symbol="PI_XBTUSD", exchange="kraken", require_ready=False),
        indicators=DummyIndicatorClient({"ma": 42}),
    )
    service.runtime_state = None

    payload = service.preflight(StrategySpec(name="present-creds", pairs=(spot_pair,)))

    assert payload["ready"] is True
    assert payload["status"] == "not_applicable"
    assert payload["credentials_ready"] is True
    route = payload["credential_routes"][0]
    assert route["credentials_present"] is True
    assert route["api_key_source"] == "BINS_DEMO_API_KEY"
    assert route["api_secret_source"] == "BINS_DEMO_API_SECRET"
    assert "spot-key" not in str(payload)
    assert "spot-secret" not in str(payload)


def test_preflight_honours_default_route_credential_env_overrides(
    monkeypatch,
) -> None:
    for name in (
        "BINS_DEMO_API_KEY",
        "BINS_DEMO_API_SECRET",
        "BINANCE_SPOT_DEMO_API_KEY",
        "BINANCE_SPOT_DEMO_API_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CUSTOM_BINS_KEY", "spot-key")
    monkeypatch.setenv("CUSTOM_BINS_SECRET", "spot-secret")
    base_pair = _xbt_sell_tail_pair()
    spot_pair = replace(
        base_pair,
        name="bin_spot",
        exchange="binance",
        market_type="spot",
        symbol="BTCUSDT",
        tail=replace(base_pair.tail, order_type="S"),
    )
    service = BotService(
        BotConfig(
            symbol="BTCUSDT",
            exchange="binance",
            market_type="spot",
            api_key_env="CUSTOM_BINS_KEY",
            api_secret_env="CUSTOM_BINS_SECRET",
            require_ready=False,
        ),
        indicators=DummyIndicatorClient({"ma": 42}),
    )
    service.runtime_state = None

    payload = service.preflight(StrategySpec(name="override-creds", pairs=(spot_pair,)))

    assert payload["ready"] is True
    assert payload["credentials_ready"] is True
    route = payload["credential_routes"][0]
    assert route["route"] == "binance:spot:BTCUSDT"
    assert route["api_key_env"] == ["CUSTOM_BINS_KEY"]
    assert route["api_secret_env"] == ["CUSTOM_BINS_SECRET"]
    assert route["api_key_source"] == "CUSTOM_BINS_KEY"
    assert route["api_secret_source"] == "CUSTOM_BINS_SECRET"
    assert "spot-key" not in str(payload)
    assert "spot-secret" not in str(payload)


def test_preflight_validates_strategy_route_symbol_when_db_context_present(
    monkeypatch,
) -> None:
    monkeypatch.setenv("BINS_DEMO_API_KEY", "spot-key")
    monkeypatch.setenv("BINS_DEMO_API_SECRET", "spot-secret")

    class FakeBinanceAdapter:
        def __init__(self, **_kwargs) -> None:
            pass

        def validate_symbol(self, symbol: str):
            if symbol == "BADADA":
                raise ValueError("Unknown Binance symbol 'BADADA'")
            return {"symbol": symbol, "tradeable": True, "minQuantity": 1.0}

        def instrument_rules(self, symbol: str):
            return self.validate_symbol(symbol)

    monkeypatch.setattr(
        "kolabi.bot.service.get_adapter",
        lambda _exchange, _market_type=None: FakeBinanceAdapter,
    )

    base_pair = _xbt_sell_tail_pair()
    spot_pair = replace(
        base_pair,
        name="bin_spot",
        exchange="binance",
        market_type="spot",
        symbol="BADADA",
        tail=replace(base_pair.tail, order_type="S"),
    )
    service = BotService(
        BotConfig(
            symbol="ADAUSDT",
            exchange="binance",
            market_type="spot",
            require_ready=False,
        ),
        indicators=DummyIndicatorClient({"ma": 42}),
    )

    class ReadyRuntimeState:
        def fetch_runtime_state(self, *, symbol, exchange, market_type):
            return _ready_runtime_state(
                symbol=symbol,
                exchange=exchange,
                market_type=market_type,
            )

    service.runtime_state = ReadyRuntimeState()

    payload = service.preflight(StrategySpec(name="bad-symbol", pairs=(spot_pair,)))

    assert payload["ready"] is False
    assert payload["route_config_ready"] is True
    assert payload["strategy_validation_ready"] is False
    assert payload["reasons"] == ("Unknown Binance symbol 'BADADA'",)
    assert "spot-key" not in str(payload)
    assert "spot-secret" not in str(payload)


def test_route_validation_falls_back_to_cached_tradeable_rules_on_live_outage() -> None:
    class FakeKrakenAdapter:
        def __init__(self) -> None:
            self.validate_calls = 0

        def instrument_rules(self, symbol: str):
            return {
                "symbol": symbol,
                "tradeable": True,
                "tickSize": 0.0001,
                "minQuantity": 1.0,
            }

        def validate_symbol(self, _symbol: str):
            self.validate_calls += 1
            raise RuntimeError("Kraken HTTP 503 on /instruments")

    adapter = FakeKrakenAdapter()

    rules = _validate_route_symbol(
        adapter,
        ExchangeRoute(exchange="kraken", market_type="futures", symbol="PF_ADAUSD"),
    )

    assert rules["tickSize"] == 0.0001
    assert adapter.validate_calls == 1


def test_route_validation_live_tradeable_status_overrides_cached_rules() -> None:
    class FakeKrakenAdapter:
        def instrument_rules(self, symbol: str):
            return {
                "symbol": symbol,
                "tradeable": True,
                "tickSize": 0.0001,
                "minQuantity": 1.0,
            }

        def validate_symbol(self, symbol: str):
            return {"symbol": symbol, "tradeable": False}

    with pytest.raises(ValueError, match="not tradeable"):
        _validate_route_symbol(
            FakeKrakenAdapter(),
            ExchangeRoute(exchange="kraken", market_type="futures", symbol="PF_ADAUSD"),
        )


def test_preflight_reports_required_base_url_for_demo_margin_route(
    monkeypatch,
) -> None:
    monkeypatch.setenv("BINM_DEMO_API_KEY", "margin-key")
    monkeypatch.setenv("BINM_DEMO_API_SECRET", "margin-secret")
    base_pair = _xbt_sell_tail_pair()
    margin_pair = replace(
        base_pair,
        name="bin_margin",
        exchange="binance",
        market_type="margin",
        symbol="BTCUSDT",
        tail=replace(base_pair.tail, order_type="S"),
    )
    service = BotService(
        BotConfig(
            symbol="BTCUSDT",
            exchange="binance",
            market_type="margin",
            require_ready=False,
        ),
        indicators=DummyIndicatorClient({"ma": 42}),
    )
    service.runtime_state = None

    def _unexpected_config_load(_route):
        raise AssertionError("preflight must not build route adapter config")

    monkeypatch.setattr(service, "_exchange_config_for_route", _unexpected_config_load)

    payload = service.preflight(StrategySpec(name="missing-base-url", pairs=(margin_pair,)))

    assert payload["ready"] is False
    assert payload["credentials_ready"] is True
    assert payload["base_urls_ready"] is False
    assert payload["route_config_ready"] is False
    assert payload["reasons"] == (
        "binance:margin:BTCUSDT:missing required base_url override",
    )
    route = payload["credential_routes"][0]
    assert route["credentials_present"] is True
    assert route["base_url_required"] is True
    assert route["base_url_present"] is False
    assert route["base_url_ready"] is False
    assert route["base_url_source"] is None
    assert route["base_url_env"] == [
        "BINANCE_MARGIN_TEST_BASE_URL",
        "BINANCE_TEST_BASE_URL",
    ]
    assert "margin-key" not in str(payload)
    assert "margin-secret" not in str(payload)


def test_preflight_accepts_default_route_base_url_override(
    monkeypatch,
) -> None:
    monkeypatch.setenv("BINM_DEMO_API_KEY", "margin-key")
    monkeypatch.setenv("BINM_DEMO_API_SECRET", "margin-secret")
    base_pair = _xbt_sell_tail_pair()
    margin_pair = replace(
        base_pair,
        name="bin_margin",
        exchange="binance",
        market_type="margin",
        symbol="BTCUSDT",
        tail=replace(base_pair.tail, order_type="S"),
    )
    service = BotService(
        BotConfig(
            symbol="BTCUSDT",
            exchange="binance",
            market_type="margin",
            base_url="https://margin-demo.example.test",
            require_ready=False,
        ),
        indicators=DummyIndicatorClient({"ma": 42}),
    )
    service.runtime_state = None

    payload = service.preflight(StrategySpec(name="base-url", pairs=(margin_pair,)))

    assert payload["ready"] is True
    assert payload["credentials_ready"] is True
    assert payload["base_urls_ready"] is True
    assert payload["route_config_ready"] is True
    route = payload["credential_routes"][0]
    assert route["base_url_required"] is True
    assert route["base_url_present"] is True
    assert route["base_url_ready"] is True
    assert route["base_url_source"] == "override"
    assert "margin-key" not in str(payload)
    assert "margin-secret" not in str(payload)
    assert "margin-demo.example.test" not in str(payload)


def test_preflight_reports_runtime_state_db_error_as_not_ready(monkeypatch) -> None:
    monkeypatch.setenv("KRKF_DEMO_API_KEY", "kraken-key")
    monkeypatch.setenv("KRKF_DEMO_API_SECRET", "kraken-secret")
    service = BotService(
        BotConfig(symbol="PI_XBTUSD", exchange="kraken", require_ready=False),
        indicators=DummyIndicatorClient({"ma": 42}),
    )

    class FailingRuntimeState:
        def fetch_runtime_state(self, **_kwargs):
            raise SQLAlchemyError("db down")

    service.runtime_state = FailingRuntimeState()

    payload = service.preflight()

    assert payload["ready"] is False
    assert payload["status"] == "error"
    assert payload["credentials_ready"] is True
    assert payload["credential_routes"][0]["credentials_present"] is True
    assert payload["reasons"] == (
        "kraken:futures:PI_XBTUSD:runtime state unavailable db down",
    )
    assert payload["error"] == "db down"
    assert "kraken-key" not in str(payload)
    assert "kraken-secret" not in str(payload)


def test_runtime_preflight_log_redacts_db_passwords(caplog) -> None:
    service = BotService(
        BotConfig(
            symbol="PI_XBTUSD",
            exchange="kraken",
            market_db_url=(
                "postgresql+psycopg://kolabi:marketpass@127.0.0.1:15433/kolabi_market"
            ),
            account_db_url=(
                "postgresql+psycopg://kolabi:accountpass@127.0.0.1:15433/kolabi_account"
            ),
            critical_account_db_url=(
                "postgresql+psycopg://kolabi:criticalpass@127.0.0.1:15433/kolabi_critical"
            ),
        ),
        indicators=DummyIndicatorClient({"ma": 42}),
    )

    class ReadyRuntimeState:
        def wait_until_ready(self, **kwargs):
            return _ready_runtime_state(
                symbol=kwargs["symbol"],
                exchange=kwargs["exchange"],
                market_type=kwargs["market_type"],
            )

    service.runtime_state = ReadyRuntimeState()
    service._cleanup_startup_orphans = lambda _routes: None
    caplog.set_level(logging.INFO, logger="kola")

    service._wait_until_ready()

    text = caplog.text
    assert "marketpass" not in text
    assert "accountpass" not in text
    assert "criticalpass" not in text
    assert (
        "market_db=postgresql+psycopg://kolabi:***@127.0.0.1:15433/kolabi_market"
        in text
    )
    assert (
        "account_db=postgresql+psycopg://kolabi:***@127.0.0.1:15433/kolabi_account"
        in text
    )
    assert (
        "critical_account_db=postgresql+psycopg://kolabi:***@127.0.0.1:15433/kolabi_critical"
        in text
    )


def test_bot_service_requires_shared_market_db_for_active_multi_symbol_strategy(
    monkeypatch,
) -> None:
    monkeypatch.delenv("KOLABI_MARKET_DB_URL", raising=False)
    base_pair = _xbt_sell_tail_pair()
    strategy = StrategySpec(
        name="multi-symbol",
        pairs=(
            replace(base_pair, name="xbt", symbol="PI_XBTUSD"),
            replace(base_pair, name="eth", symbol="PI_ETHUSD"),
        ),
    )
    service = BotService(
        BotConfig(symbol="PI_XBTUSD", exchange="kraken", require_ready=False),
        indicators=DummyIndicatorClient({"ma": 42}),
    )

    with pytest.raises(ValueError, match="shared market DB URL"):
        service.run_strategy(strategy, dry_run=False, simulate=False)


def test_kraken_run_strategy_rejects_too_small_absolute_quantity(monkeypatch) -> None:
    class FakeKrakenAdapter:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        def instrument_rules(self, symbol: str):
            return {"symbol": symbol, "minQuantity": 30.0}

    strategy = _xbt_sell_tail_strategy()
    service = BotService(
        BotConfig(symbol="PI_XBTUSD", exchange="kraken", require_ready=False),
        indicators=DummyIndicatorClient({"ma": 42}),
    )
    service.exchange_config = ExchangeConfig(
        api_key="k",
        api_secret="s",
        base_url="https://demo-futures.kraken.com",
        symbol="PI_XBTUSD",
        adapter_kwargs={},
    )
    monkeypatch.setattr("kolabi.bot.service.get_adapter", lambda _: FakeKrakenAdapter)

    with pytest.raises(ValueError, match="QTY_ABS_INVALID") as exc_info:
        service.run_strategy(strategy, dry_run=True)

    assert "min=30" in str(exc_info.value)


def test_kraken_run_strategy_accepts_decimal_absolute_quantity_on_step(monkeypatch) -> None:
    class FakeKrakenAdapter:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        def instrument_rules(self, symbol: str):
            return {
                "symbol": symbol,
                "minQuantity": "0.0001",
                "quantityIncrement": "0.0001",
            }

    base_pair = _xbt_sell_tail_pair()
    pair = replace(
        base_pair,
        symbol="PF_XBTUSD",
        head_quantity=Decimal("0.0001"),
        head_quantity_type="qA",
    )
    strategy = StrategySpec(name="absolute-decimal", pairs=(pair,))
    service = BotService(
        BotConfig(symbol="PF_XBTUSD", exchange="kraken", require_ready=False),
        indicators=DummyIndicatorClient({"ma": 42}),
    )
    service.exchange_config = ExchangeConfig(
        api_key="k",
        api_secret="s",
        base_url="https://demo-futures.kraken.com",
        symbol="PF_XBTUSD",
        adapter_kwargs={},
    )
    monkeypatch.setattr("kolabi.bot.service.get_adapter", lambda *args: FakeKrakenAdapter)

    result = service.run_strategy(strategy, dry_run=True)

    assert result.commands[0].request.orderQty == Decimal("0.0001")


def test_kraken_run_strategy_uses_enriched_rules_after_symbol_validation(monkeypatch) -> None:
    class FakeKrakenAdapter:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        def validate_symbol(self, symbol: str):
            return {
                "symbol": symbol,
                "type": "futures_flexible",
                "contractSize": 1,
                "tickSize": 0.5,
                "tradeable": True,
            }

        def instrument_rules(self, symbol: str):
            return {
                **self.validate_symbol(symbol),
                "minQuantity": "0.0001",
                "quantityIncrement": "0.0001",
            }

    base_pair = _xbt_sell_tail_pair()
    pair = replace(
        base_pair,
        symbol="PF_XBTUSD",
        head_quantity=Decimal("0.0001"),
        head_quantity_type="qA",
    )
    strategy = StrategySpec(name="absolute-enriched-rules", pairs=(pair,))
    service = BotService(
        BotConfig(symbol="PF_XBTUSD", exchange="kraken", require_ready=False),
        indicators=DummyIndicatorClient({"ma": 42}),
    )
    service.exchange_config = ExchangeConfig(
        api_key="k",
        api_secret="s",
        base_url="https://demo-futures.kraken.com",
        symbol="PF_XBTUSD",
        adapter_kwargs={},
    )
    monkeypatch.setattr("kolabi.bot.service.get_adapter", lambda *args: FakeKrakenAdapter)

    result = service.run_strategy(strategy, dry_run=True)

    assert result.commands[0].request.orderQty == Decimal("0.0001")


def test_kraken_run_strategy_rejects_decimal_absolute_quantity_off_step(monkeypatch) -> None:
    class FakeKrakenAdapter:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        def instrument_rules(self, symbol: str):
            return {
                "symbol": symbol,
                "minQuantity": "0.0001",
                "quantityIncrement": "0.0001",
            }

    base_pair = _xbt_sell_tail_pair()
    pair = replace(
        base_pair,
        symbol="PF_XBTUSD",
        head_quantity=Decimal("0.00015"),
        head_quantity_type="qA",
    )
    strategy = StrategySpec(name="absolute-off-step", pairs=(pair,))
    service = BotService(
        BotConfig(symbol="PF_XBTUSD", exchange="kraken", require_ready=False),
        indicators=DummyIndicatorClient({"ma": 42}),
    )
    service.exchange_config = ExchangeConfig(
        api_key="k",
        api_secret="s",
        base_url="https://demo-futures.kraken.com",
        symbol="PF_XBTUSD",
        adapter_kwargs={},
    )
    monkeypatch.setattr("kolabi.bot.service.get_adapter", lambda *args: FakeKrakenAdapter)

    with pytest.raises(ValueError, match="QTY_ABS_INVALID"):
        service.run_strategy(strategy, dry_run=True)


def test_kraken_run_strategy_rejects_too_small_usd_quantity_at_startup(monkeypatch) -> None:
    class FakeKrakenAdapter:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        def instrument_rules(self, symbol: str):
            return {
                "symbol": symbol,
                "contractSize": 1,
                "quantityIncrement": "0.0001",
                "minimumQuantity": "0.0001",
            }

    class Market:
        mark_price = 100000.0

    class RuntimeState:
        def fetch_market_state(self, symbol=None, exchange=None, market_type=None):
            del symbol, exchange, market_type
            return Market()

    base_pair = _xbt_sell_tail_pair()
    pair = replace(
        base_pair,
        symbol="PF_XBTUSD",
        head_quantity=1.0,
        head_quantity_type="qU",
        amount_type="qUtBpD",
    )
    strategy = StrategySpec(name="usd-too-small", pairs=(pair,))
    service = BotService(
        BotConfig(symbol="PF_XBTUSD", exchange="kraken", require_ready=False),
        indicators=DummyIndicatorClient({"ma": 42}),
    )
    service.exchange_config = ExchangeConfig(
        api_key="k",
        api_secret="s",
        base_url="https://demo-futures.kraken.com",
        symbol="PF_XBTUSD",
        adapter_kwargs={},
    )
    service.runtime_state = RuntimeState()
    monkeypatch.setattr("kolabi.bot.service.get_adapter", lambda *args: FakeKrakenAdapter)

    with pytest.raises(ValueError, match="not placeable at startup"):
        service.run_strategy(strategy, dry_run=True)


def test_kraken_run_strategy_rejects_too_small_percent_quantity_at_startup(monkeypatch) -> None:
    class FakeKrakenAdapter:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        def instrument_rules(self, symbol: str):
            return {
                "symbol": symbol,
                "contractSize": 1,
                "quantityIncrement": "0.0001",
                "minimumQuantity": "0.0001",
            }

    class Market:
        mark_price = 100000.0

    class Balance:
        ready = True
        reason = None
        available = 10.0

    class RuntimeState:
        def fetch_market_state(self, symbol=None, exchange=None, market_type=None):
            del symbol, exchange, market_type
            return Market()

        def fetch_account_balance(self, asset="USD", exchange=None, market_type=None):
            del asset, exchange, market_type
            return Balance()

    base_pair = _xbt_sell_tail_pair()
    pair = replace(
        base_pair,
        symbol="PF_XBTUSD",
        head_quantity=Decimal("0.5"),
        head_quantity_type="q%",
        amount_type="q%tBpD",
    )
    strategy = StrategySpec(name="pct-too-small", pairs=(pair,))
    service = BotService(
        BotConfig(symbol="PF_XBTUSD", exchange="kraken", require_ready=False),
        indicators=DummyIndicatorClient({"ma": 42}),
    )
    service.exchange_config = ExchangeConfig(
        api_key="k",
        api_secret="s",
        base_url="https://demo-futures.kraken.com",
        symbol="PF_XBTUSD",
        adapter_kwargs={},
    )
    service.runtime_state = RuntimeState()
    monkeypatch.setattr("kolabi.bot.service.get_adapter", lambda *args: FakeKrakenAdapter)

    with pytest.raises(ValueError, match="qty %0.5 is not placeable at startup"):
        service.run_strategy(strategy, dry_run=True)


@pytest.mark.parametrize(
    ("exchange", "market_type", "tail_order_type", "expected"),
    [
        ("kraken", "spot", "S-", "does not support reduce-only tail order"),
        ("kraken", "margin", "MT", "does not support MT tail orders"),
        ("bitmex", "spot", "Sm", "does not support mark/index-price triggers"),
        ("bitmex", "spot", "S-", "does not support reduce-only tail order"),
        ("bitmex", "spot", "S", "does not support S tail orders"),
    ],
)
def test_non_futures_routes_reject_unsupported_tail_grammar_at_preflight(
    exchange: str,
    market_type: str,
    tail_order_type: str,
    expected: str,
) -> None:
    base_pair = _xbt_sell_tail_pair()
    pair = replace(
        base_pair,
        exchange=exchange,
        market_type=market_type,
        symbol="XBT/USD" if exchange == "kraken" else "XBTUSD",
        tail=replace(base_pair.tail, order_type=tail_order_type),
    )
    strategy = StrategySpec(name="unsupported-route-grammar", pairs=(pair,))
    service = BotService(
        BotConfig(symbol="PI_XBTUSD", exchange="kraken", require_ready=False),
        indicators=DummyIndicatorClient({"ma": 42}),
    )
    service.exchange_config = ExchangeConfig(
        api_key="k",
        api_secret="s",
        base_url="https://example.test",
        symbol="PI_XBTUSD",
        adapter_kwargs={},
    )

    with pytest.raises(ValueError, match=expected):
        service.run_strategy(strategy, dry_run=True)


def test_adapter_exchange_port_forwards_execinst_once(monkeypatch) -> None:
    class FakeAdapter:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs
            self.calls: list[dict[str, object]] = []

        def place_order(self, side: str, orderQty: object, **params: object) -> OrderAck:
            self.calls.append({"side": side, "orderQty": orderQty, **params})
            return OrderAck(order_id="OID-1", status="New")

    adapter_holder: dict[str, FakeAdapter] = {}

    def build_adapter(**kwargs) -> FakeAdapter:
        adapter = FakeAdapter(**kwargs)
        adapter_holder["adapter"] = adapter
        return adapter

    monkeypatch.setattr("kolabi.bot.service.get_adapter", lambda _: build_adapter)
    port = AdapterExchangePort(
        exchange="kraken",
        exchange_config=ExchangeConfig(
            api_key="k",
            api_secret="s",
            base_url="https://demo-futures.kraken.com",
            symbol="PI_XBTUSD",
            adapter_kwargs={},
        ),
    )
    command = PlaceHeadCommand(
        kind=RuntimeCommandKind.PLACE,
        symbol=Symbol("PI_XBTUSD"),
        pair_name="pair-a",
        request=PlaceOrderCommandRequest(
            pair_name="pair-a",
            side="sell",
            ordType="Limit",
            orderQty=11,
            price=75000.0,
            execInst="ParticipateDoNotInitiate",
            clOrdID="CID-1",
        ),
    )

    ack = asyncio.run(port.place_head(command))

    assert ack.order_id == "OID-1"
    assert adapter_holder["adapter"].calls == [
        {
            "side": "sell",
            "orderQty": 11,
            "price": 75000.0,
            "type_": "Limit",
            "clOrdID": "CID-1",
            "execInst": "ParticipateDoNotInitiate",
        }
    ]


def test_adapter_exchange_port_keeps_head_ack_when_open_order_enrichment_fails(
    monkeypatch,
    caplog,
) -> None:
    class FakeAdapter:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs
            self.calls: list[dict[str, object]] = []

        def place_order(self, side: str, orderQty: object, **params: object) -> OrderAck:
            self.calls.append({"side": side, "orderQty": orderQty, **params})
            return OrderAck(order_id="OID-SPARSE", status="New")

        def live_open_orders(self) -> list[dict[str, object]]:
            raise TimeoutError("openorders lag")

    adapter_holder: dict[str, FakeAdapter] = {}

    def build_adapter(**kwargs) -> FakeAdapter:
        adapter = FakeAdapter(**kwargs)
        adapter_holder["adapter"] = adapter
        return adapter

    monkeypatch.setattr("kolabi.bot.service.get_adapter", lambda _: build_adapter)
    port = AdapterExchangePort(
        exchange="kraken",
        exchange_config=ExchangeConfig(
            api_key="k",
            api_secret="s",
            base_url="https://demo-futures.kraken.com",
            symbol="PI_XBTUSD",
            adapter_kwargs={},
        ),
    )
    command = PlaceHeadCommand(
        kind=RuntimeCommandKind.PLACE,
        symbol=Symbol("PI_XBTUSD"),
        pair_name="pair-a",
        request=PlaceOrderCommandRequest(
            pair_name="pair-a",
            side="sell",
            ordType="Limit",
            orderQty=11,
            price=75000.0,
            execInst="ParticipateDoNotInitiate",
            clOrdID="CID-1",
        ),
    )

    with caplog.at_level(logging.WARNING, logger="kola"):
        ack = asyncio.run(port.place_head(command))

    assert ack.order_id == "OID-SPARSE"
    assert ack.status == "New"
    assert "HEAD_OPEN_ENRICH_SKIPPED (pair-a)" in caplog.text
    assert adapter_holder["adapter"].calls == [
        {
            "side": "sell",
            "orderQty": 11,
            "price": 75000.0,
            "type_": "Limit",
            "clOrdID": "CID-1",
            "execInst": "ParticipateDoNotInitiate",
        }
    ]


def test_adapter_exchange_port_derives_stop_limit_price_from_tail_offset(monkeypatch) -> None:
    class FakeAdapter:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs
            self.calls: list[dict[str, object]] = []

        def place_order(self, side: str, orderQty: object, **params: object) -> OrderAck:
            self.calls.append({"side": side, "orderQty": orderQty, **params})
            return OrderAck(order_id="OID-1", status="New")

        def live_trigger_orders(self) -> list[dict[str, object]]:
            return []

    adapter_holder: dict[str, FakeAdapter] = {}

    def build_adapter(**kwargs) -> FakeAdapter:
        adapter = FakeAdapter(**kwargs)
        adapter_holder["adapter"] = adapter
        return adapter

    monkeypatch.setattr("kolabi.bot.service.get_adapter", lambda _: build_adapter)
    port = AdapterExchangePort(
        exchange="kraken",
        exchange_config=ExchangeConfig(
            api_key="k",
            api_secret="s",
            base_url="https://demo-futures.kraken.com",
            symbol="PI_XBTUSD",
            adapter_kwargs={},
        ),
        verify_tail_on_place=False,
    )
    command = PlaceTailCommand(
        kind=RuntimeCommandKind.PLACE,
        symbol=Symbol("PI_XBTUSD"),
        pair_name="pair-a",
        request=PlaceOrderCommandRequest(
            pair_name="pair-a",
            side="sell",
            ordType="SL",
            orderQty=11,
            stopPx=Decimal("100"),
            oDelta=Decimal("0.5"),
            clOrdID="CID-T",
        ),
    )

    ack = asyncio.run(port.place_tail(command))

    assert ack.order_id == "OID-1"
    assert adapter_holder["adapter"].calls == [
        {
            "side": "sell",
            "orderQty": 11,
            "price": Decimal("99.5"),
            "stopPx": Decimal("100"),
            "type_": "SL",
            "clOrdID": "CID-T",
            "oDelta": Decimal("0.5"),
        }
    ]


def test_symbol_routing_exchange_port_dispatches_by_exchange_and_symbol(monkeypatch) -> None:
    created: list[tuple[str, str]] = []

    class FakeAdapter:
        def __init__(self, *, exchange_name: str, symbol: str, **_kwargs) -> None:
            self.exchange_name = exchange_name
            self.symbol = symbol
            created.append((exchange_name, symbol))

        def place_order(self, side: str, orderQty: object, **_params: object) -> OrderAck:
            return OrderAck(
                order_id=f"{self.exchange_name}:{self.symbol}",
                status="New",
                side=side,
                orig_qty=orderQty,
            )

    def adapter_factory(exchange_name: str):
        def build_adapter(**kwargs) -> FakeAdapter:
            return FakeAdapter(exchange_name=exchange_name, **kwargs)

        return build_adapter

    monkeypatch.setattr("kolabi.bot.service.get_adapter", adapter_factory)

    def config_for_route(route) -> ExchangeConfig:
        return ExchangeConfig(
            api_key="k",
            api_secret="s",
            base_url=f"https://{route.exchange}.example.test",
            symbol=route.symbol,
            adapter_kwargs={},
        )

    port = SymbolRoutingExchangePort(
        exchange="kraken",
        exchange_config_loader=config_for_route,
        verify_tail_on_place=False,
    )

    kraken_ack = asyncio.run(
        port.place_head(
            PlaceHeadCommand(
                kind=RuntimeCommandKind.PLACE,
                exchange="kraken",
                market_type="futures",
                symbol=Symbol("PI_XBTUSD"),
                pair_name="krk",
                request=PlaceOrderCommandRequest(
                    pair_name="krk",
                    side="buy",
                    ordType="Limit",
                    orderQty=1,
                    price=100.0,
                ),
            )
        )
    )
    binance_ack = asyncio.run(
        port.place_head(
            PlaceHeadCommand(
                kind=RuntimeCommandKind.PLACE,
                exchange="binance",
                market_type="futures",
                symbol=Symbol("BTCUSDT"),
                pair_name="bin",
                request=PlaceOrderCommandRequest(
                    pair_name="bin",
                    side="buy",
                    ordType="Limit",
                    orderQty=1,
                    price=100.0,
                ),
            )
        )
    )

    assert kraken_ack.order_id == "kraken:PI_XBTUSD"
    assert binance_ack.order_id == "binance:BTCUSDT"
    assert created == [("kraken", "PI_XBTUSD"), ("binance", "BTCUSDT")]


def test_symbol_routing_exchange_port_rejects_unsupported_market_lane() -> None:
    port = SymbolRoutingExchangePort(
        exchange="kraken",
        exchange_config_loader=lambda route: ExchangeConfig(
            api_key="k",
            api_secret="s",
            base_url=f"https://{route.exchange}.example.test",
            symbol=route.symbol,
            adapter_kwargs={},
        ),
    )

    with pytest.raises(ValueError, match="only supported for Binance or Kraken"):
        asyncio.run(
            port.place_head(
                PlaceHeadCommand(
                    kind=RuntimeCommandKind.PLACE,
                    exchange="bitmex",
                    market_type="margin",
                    symbol=Symbol("XBTUSD"),
                    pair_name="bad",
                    request=PlaceOrderCommandRequest(
                        pair_name="bad",
                        side="buy",
                        ordType="Limit",
                        orderQty=1,
                        price=100.0,
                    ),
                )
            )
        )


def test_symbol_routing_exchange_port_uses_configured_default_market_type(monkeypatch) -> None:
    created: list[tuple[str, str, str]] = []

    class FakeAdapter:
        def __init__(
            self,
            *,
            exchange_name: str,
            market_type: str,
            symbol: str,
            **_kwargs,
        ) -> None:
            created.append((exchange_name, market_type, symbol))

        def place_order(
            self,
            side: str,
            orderQty: object,
            **_params: object,
        ) -> OrderAck:
            return OrderAck(
                order_id="OID-SPOT",
                status="New",
                side=side,
                orig_qty=orderQty,
            )

    def adapter_factory(exchange_name: str, market_type: str):
        def build_adapter(**kwargs) -> FakeAdapter:
            adapter_market_type = str(kwargs.pop("market_type"))
            assert adapter_market_type == market_type
            return FakeAdapter(
                exchange_name=exchange_name,
                market_type=adapter_market_type,
                **kwargs,
            )

        return build_adapter

    monkeypatch.setattr("kolabi.bot.service.get_adapter", adapter_factory)
    port = SymbolRoutingExchangePort(
        exchange="binance",
        market_type="spot",
        exchange_config_loader=lambda route: ExchangeConfig(
            api_key="k",
            api_secret="s",
            base_url="https://binance.example.test",
            symbol=route.symbol,
            adapter_kwargs={},
        ),
        verify_tail_on_place=False,
    )

    ack = asyncio.run(
        port.place_head(
            PlaceHeadCommand(
                kind=RuntimeCommandKind.PLACE,
                exchange=None,
                market_type=None,
                symbol=Symbol("BTCUSDT"),
                pair_name="spot",
                request=PlaceOrderCommandRequest(
                    pair_name="spot",
                    side="buy",
                    ordType="Limit",
                    orderQty=1,
                    price=100.0,
                ),
            )
        )
    )

    assert ack.order_id == "OID-SPOT"
    assert created == [("binance", "spot", "BTCUSDT")]


@pytest.mark.parametrize(
    ("exchange", "market_type", "symbol"),
    [
        ("kraken", "futures", "PI_XBTUSD"),
        ("kraken", "spot", "XBT/USD"),
        ("kraken", "margin", "XBT/USD"),
        ("binance", "futures", "BTCUSDT"),
        ("binance", "spot", "BTCUSDT"),
        ("binance", "margin", "BTCUSDT"),
        ("binance", "isolated_margin", "BTCUSDT"),
        ("bitmex", "futures", "XBTUSD"),
        ("bitmex", "spot", "BMEXUSDT"),
    ],
)
def test_symbol_routing_exchange_port_dispatches_every_supported_market_lane(
    monkeypatch,
    exchange: str,
    market_type: str,
    symbol: str,
) -> None:
    created: list[tuple[str, str, str, str]] = []

    class FakeAdapter:
        def __init__(
            self,
            *,
            exchange_name: str,
            adapter_market_type: str,
            base_url: str,
            symbol: str,
            market_type: str,
            **_kwargs,
        ) -> None:
            created.append((exchange_name, adapter_market_type, market_type, symbol))
            self.order_id = f"{exchange_name}:{adapter_market_type}:{symbol}"

        def place_order(
            self,
            side: str,
            orderQty: object,
            **_params: object,
        ) -> OrderAck:
            return OrderAck(
                order_id=self.order_id,
                status="New",
                side=side,
                orig_qty=orderQty,
            )

    def adapter_factory(exchange_name: str, adapter_market_type: str):
        def build_adapter(**kwargs) -> FakeAdapter:
            return FakeAdapter(
                exchange_name=exchange_name,
                adapter_market_type=adapter_market_type,
                **kwargs,
            )

        return build_adapter

    def config_for_route(route) -> ExchangeConfig:
        return ExchangeConfig(
            api_key="k",
            api_secret="s",
            base_url=f"https://{route.exchange}-{route.market_type}.example.test",
            symbol=route.symbol,
            adapter_kwargs={"market_type": "stale"},
        )

    monkeypatch.setattr("kolabi.bot.service.get_adapter", adapter_factory)
    port = SymbolRoutingExchangePort(
        exchange="kraken",
        market_type="futures",
        exchange_config_loader=config_for_route,
        verify_tail_on_place=False,
    )

    ack = asyncio.run(
        port.place_head(
            PlaceHeadCommand(
                kind=RuntimeCommandKind.PLACE,
                exchange=exchange,
                market_type=market_type,
                symbol=Symbol(symbol),
                pair_name=f"{exchange}-{market_type}",
                request=PlaceOrderCommandRequest(
                    pair_name=f"{exchange}-{market_type}",
                    side="buy",
                    ordType="Limit",
                    orderQty=1,
                    price=100.0,
                ),
            )
        )
    )

    assert ack.order_id == f"{exchange}:{market_type}:{symbol}"
    assert created == [(exchange, market_type, market_type, symbol)]


def test_symbol_routing_exchange_port_uses_one_route_for_all_command_kinds(
    monkeypatch,
) -> None:
    calls: list[tuple[str, str, str, str]] = []

    class FakeAdapter:
        def __init__(self, *, market_type: str, symbol: str, **_kwargs) -> None:
            self.market_type = market_type
            self.symbol = symbol

        def place_order(
            self,
            side: str,
            orderQty: object,
            **_params: object,
        ) -> OrderAck:
            calls.append((self.market_type, self.symbol, "place", side))
            return OrderAck(order_id="OID-PLACE", status="New", orig_qty=orderQty)

        def amend_order(self, order_id: str, **_params: object) -> OrderAck:
            calls.append((self.market_type, self.symbol, "amend", order_id))
            return OrderAck(order_id=order_id, status="Replaced")

        def cancel_order(self, order_id: str) -> OrderAck:
            calls.append((self.market_type, self.symbol, "cancel", order_id))
            return OrderAck(order_id=order_id, status="Canceled")

    monkeypatch.setattr(
        "kolabi.bot.service.get_adapter",
        lambda _exchange, _market_type: FakeAdapter,
    )
    port = SymbolRoutingExchangePort(
        exchange="kraken",
        market_type="futures",
        exchange_config_loader=lambda route: ExchangeConfig(
            api_key="k",
            api_secret="s",
            base_url="https://route.example.test",
            symbol=route.symbol,
            adapter_kwargs={},
        ),
        verify_tail_on_place=False,
    )
    route = {
        "exchange": "binance",
        "market_type": "isolated_margin",
        "symbol": Symbol("BTCUSDT"),
        "pair_name": "BIN-I",
    }

    asyncio.run(
        port.place_head(
            PlaceHeadCommand(
                kind=RuntimeCommandKind.PLACE,
                request=PlaceOrderCommandRequest(
                    pair_name="BIN-I",
                    side="buy",
                    ordType="Limit",
                    orderQty=1,
                    price=100.0,
                ),
                **route,
            )
        )
    )
    asyncio.run(
        port.place_tail(
            PlaceTailCommand(
                kind=RuntimeCommandKind.PLACE,
                request=PlaceOrderCommandRequest(
                    pair_name="BIN-I",
                    side="sell",
                    ordType="Stop",
                    orderQty=1,
                    stopPx=95.0,
                ),
                **route,
            )
        )
    )
    asyncio.run(
        port.amend_head(
            AmendHeadCommand(
                kind=RuntimeCommandKind.AMEND,
                request=AmendOrderCommandRequest(
                    pair_name="BIN-I",
                    side="buy",
                    ordType="Limit",
                    orderID="OID-H",
                    newPrice=101.0,
                ),
                **route,
            )
        )
    )
    asyncio.run(
        port.amend_tail(
            AmendTailCommand(
                kind=RuntimeCommandKind.AMEND,
                request=AmendOrderCommandRequest(
                    pair_name="BIN-I",
                    side="sell",
                    ordType="Stop",
                    orderID="OID-T",
                    newPrice=96.0,
                ),
                **route,
            )
        )
    )
    asyncio.run(
        port.cancel(
            CancelCommand(
                kind=RuntimeCommandKind.CANCEL,
                request=CancelOrderCommandRequest(
                    pair_name="BIN-I",
                    clOrdID="OID-C",
                ),
                **route,
            )
        )
    )

    assert calls == [
        ("isolated_margin", "BTCUSDT", "place", "buy"),
        ("isolated_margin", "BTCUSDT", "place", "sell"),
        ("isolated_margin", "BTCUSDT", "amend", "OID-H"),
        ("isolated_margin", "BTCUSDT", "amend", "OID-T"),
        ("isolated_margin", "BTCUSDT", "cancel", "OID-C"),
    ]


def test_symbol_routing_exchange_port_rejects_cross_lane_without_loader() -> None:
    port = SymbolRoutingExchangePort(
        exchange="binance",
        market_type="spot",
        exchange_config=ExchangeConfig(
            api_key="k",
            api_secret="s",
            base_url="https://binance.example.test",
            symbol="BTCUSDT",
            adapter_kwargs={"market_type": "spot"},
        ),
        verify_tail_on_place=False,
    )

    with pytest.raises(RuntimeError, match="No exchange config loader"):
        asyncio.run(
            port.place_head(
                PlaceHeadCommand(
                    kind=RuntimeCommandKind.PLACE,
                    exchange="binance",
                    market_type="margin",
                    symbol=Symbol("BTCUSDT"),
                    pair_name="margin",
                    request=PlaceOrderCommandRequest(
                        pair_name="margin",
                        side="buy",
                        ordType="Limit",
                        orderQty=1,
                        price=100.0,
                    ),
                )
            )
        )


def test_interrupt_cleanup_cancels_tail_by_exchange_id_and_reverses_played_qty(monkeypatch) -> None:
    strategy = _xbt_sell_tail_strategy()
    pair = strategy.pairs[0]
    service = BotService(BotConfig(symbol="PI_XBTUSD", exchange="kraken", require_ready=False))
    runtime = StrategyRuntime(strategy=strategy, symbol="PI_XBTUSD", simulate=True)
    runtime.state = replace(
        runtime.state,
        pairs={
            pair.name: PairCycleState(
                pair=pair,
                head_state=HeadState.CLOSED,
                tail_state=TailState.LIVING,
                played_quantity=Decimal("10"),
                tail_identity=OrderIdentity(
                    pair_name=pair.name,
                    role="tail",
                    client_order_id="CID-T",
                    exchange_order_id="OID-T",
                ),
            )
        },
    )
    expected_close_side = "sell" if pair.head.side.value == "buy" else "buy"
    initial_position = 12.0 if expected_close_side == "sell" else -12.0
    expected_after = initial_position - 10.0 if expected_close_side == "sell" else initial_position + 10.0

    class FakeAdapter:
        def __init__(self, position: float) -> None:
            self.position = position
            self.cancelled: list[str] = []
            self.closed: list[tuple[str, float, dict[str, object]]] = []

        def cancel_order(self, order_id: str) -> OrderAck:
            self.cancelled.append(order_id)
            return OrderAck(order_id=order_id, status="Canceled")

        def place_order(self, side: str, orderQty: object, **params: object) -> OrderAck:
            qty = float(orderQty) if isinstance(orderQty, (int, float)) else float(str(orderQty))
            self.closed.append((side, qty, dict(params)))
            if side == "sell":
                self.position -= qty
            else:
                self.position += qty
            return OrderAck(order_id=f"CLOSE-{len(self.closed)}", status="New", side=side)

        def get_position(self) -> Position:
            return Position(symbol="PI_XBTUSD", qty=self.position)

        def live_open_orders(self) -> list[dict[str, object]]:
            return []

        def live_trigger_orders(self) -> list[dict[str, object]]:
            return []

        def open_orders(self) -> list[dict[str, object]]:
            return []

        def live_trigger_orders_db(self) -> list[dict[str, object]]:
            return []

    adapter = FakeAdapter(initial_position)
    monkeypatch.setattr(
        service,
        "_build_admin_port",
        lambda: type("Port", (), {"adapter": adapter})(),
    )

    summary = service.cleanup_interrupted_pairs(runtime)

    assert adapter.cancelled == ["OID-T"]
    assert adapter.closed == [
        (expected_close_side, 10.0, {"type_": "MARKET", "reduceOnly": True})
    ]
    assert summary["tail_cancelled"] == 1
    assert summary["close_orders"] == 1
    assert summary["position_before_qty"] == initial_position
    assert summary["position_after_qty"] == expected_after


def test_close_all_orders_omits_reduce_only_for_margin(monkeypatch) -> None:
    service = BotService(
        BotConfig(
            symbol="XBT/USD",
            exchange="kraken",
            market_type="margin",
            require_ready=False,
        )
    )

    class FakeAdapter:
        def __init__(self) -> None:
            self.position = 2.0
            self.closed: list[dict[str, object]] = []

        def get_position(self) -> Position:
            return Position(symbol="XBT/USD", qty=self.position)

        def place_order(self, side: str, orderQty: object, **params: object) -> OrderAck:
            self.closed.append({"side": side, "orderQty": orderQty, **params})
            self.position = 0.0
            return OrderAck(order_id="CLOSE-M", status="New", side=side)

        def live_open_orders(self) -> list[dict[str, object]]:
            return []

        def live_trigger_orders(self) -> list[dict[str, object]]:
            return []

        def open_orders(self) -> list[dict[str, object]]:
            return []

        def live_trigger_orders_db(self) -> list[dict[str, object]]:
            return []

    adapter = FakeAdapter()
    monkeypatch.setattr(
        service,
        "_build_admin_port",
        lambda: type("Port", (), {"adapter": adapter})(),
    )

    result = service.close_all_orders()

    assert result["close_action"] == "submitted_market_close"
    assert adapter.closed == [
        {"side": "sell", "orderQty": 2.0, "type_": "MARKET"}
    ]
    assert result["closed"] is True


def test_interrupt_cleanup_omits_reduce_only_for_margin_route(monkeypatch) -> None:
    base_strategy = _xbt_sell_tail_strategy()
    base_pair = base_strategy.pairs[0]
    pair = replace(
        base_pair,
        exchange="kraken",
        market_type="margin",
        symbol="XBT/USD",
    )
    strategy = StrategySpec(name="margin-cleanup", pairs=(pair,))
    service = BotService(
        BotConfig(
            symbol="XBT/USD",
            exchange="kraken",
            market_type="margin",
            require_ready=False,
        )
    )
    runtime = StrategyRuntime(
        strategy=strategy,
        symbol="XBT/USD",
        simulate=True,
        exchange="kraken",
        market_type="margin",
    )
    runtime.state = replace(
        runtime.state,
        pairs={
            pair.name: PairCycleState(
                pair=pair,
                head_state=HeadState.CLOSED,
                tail_state=TailState.LIVING,
                played_quantity=Decimal("3"),
                tail_identity=OrderIdentity(
                    pair_name=pair.name,
                    role="tail",
                    client_order_id="CID-M",
                    exchange_order_id="OID-M",
                    symbol="XBT/USD",
                    exchange="kraken",
                    market_type="margin",
                ),
            )
        },
    )

    class FakeAdapter:
        def __init__(self) -> None:
            self.position = -4.0
            self.cancelled: list[str] = []
            self.closed: list[dict[str, object]] = []

        def cancel_order(self, order_id: str) -> OrderAck:
            self.cancelled.append(order_id)
            return OrderAck(order_id=order_id, status="Canceled")

        def place_order(self, side: str, orderQty: object, **params: object) -> OrderAck:
            self.closed.append({"side": side, "orderQty": orderQty, **params})
            if side == "sell":
                self.position -= float(orderQty)
            else:
                self.position += float(orderQty)
            return OrderAck(order_id="CLOSE-M", status="New", side=side)

        def get_position(self) -> Position:
            return Position(symbol="XBT/USD", qty=self.position)

        def live_open_orders(self) -> list[dict[str, object]]:
            return []

        def live_trigger_orders(self) -> list[dict[str, object]]:
            return []

        def open_orders(self) -> list[dict[str, object]]:
            return []

        def live_trigger_orders_db(self) -> list[dict[str, object]]:
            return []

    adapter = FakeAdapter()
    monkeypatch.setattr(
        service,
        "_adapter_for_route",
        lambda _route, _adapters: adapter,
    )

    summary = service.cleanup_interrupted_pairs(runtime)

    expected_close_side = "sell" if pair.head.side.value == "buy" else "buy"
    assert adapter.cancelled == ["OID-M"]
    assert len(adapter.closed) == 1
    assert adapter.closed[0]["side"] == expected_close_side
    assert adapter.closed[0]["orderQty"] == Decimal("3")
    assert adapter.closed[0]["type_"] == "MARKET"
    assert "reduceOnly" not in adapter.closed[0]
    assert summary["close_orders"] == 1


def test_cancel_living_tails_uses_route_adapter(monkeypatch) -> None:
    strategy = _xbt_sell_tail_strategy()
    pair = strategy.pairs[0]
    service = BotService(BotConfig(symbol="PI_XBTUSD", exchange="kraken", require_ready=False))
    runtime = StrategyRuntime(strategy=strategy, symbol="PI_XBTUSD", simulate=True)
    runtime.state = replace(
        runtime.state,
        pairs={
            pair.name: PairCycleState(
                pair=pair,
                head_state=HeadState.CLOSED,
                tail_state=TailState.LIVING,
                played_quantity=Decimal("10"),
                tail_identity=OrderIdentity(
                    pair_name=pair.name,
                    role="tail",
                    client_order_id="CID-T",
                    exchange_order_id="OID-T",
                ),
            )
        },
    )

    class FakeAdapter:
        def __init__(self) -> None:
            self.cancelled: list[str] = []

        def cancel_order(self, order_id: str) -> OrderAck:
            self.cancelled.append(order_id)
            return OrderAck(order_id=order_id, status="Canceled")

    adapter = FakeAdapter()
    monkeypatch.setattr(
        service,
        "_build_admin_port",
        lambda: type("Port", (), {"adapter": adapter})(),
    )

    cancelled = service.cancel_living_tails(runtime)

    assert [ack.order_id for ack in cancelled] == ["OID-T"]
    assert adapter.cancelled == ["OID-T"]


def test_interrupt_cleanup_cancels_active_head_lease(monkeypatch) -> None:
    strategy = _xbt_sell_tail_strategy()
    pair = strategy.pairs[0]
    service = BotService(BotConfig(symbol="PI_XBTUSD", exchange="kraken", require_ready=False))
    runtime = StrategyRuntime(strategy=strategy, symbol="PI_XBTUSD", simulate=False)
    runtime._order_leases[
        _CommandSlot(pair.name, 1, "head")
    ] = _OrderLease(
        pair_name=pair.name,
        attempt_index=1,
        role="head",
        symbol="PI_XBTUSD",
        client_order_id="H1test-260609220000",
        exchange_order_id="OID-H",
        side="buy",
        quantity=Decimal("1"),
        price=None,
        stop_price=Decimal("100"),
        status="LIVE",
        created_at=datetime.now(timezone.utc),
    )

    class FakeAdapter:
        def __init__(self) -> None:
            self.cancelled: list[str] = []

        def cancel_order(self, order_id: str) -> OrderAck:
            self.cancelled.append(order_id)
            return OrderAck(order_id=order_id, status="Canceled")

        def get_position(self) -> Position:
            return Position(symbol="PI_XBTUSD", qty=0.0)

    adapter = FakeAdapter()
    monkeypatch.setattr(
        service,
        "_build_admin_port",
        lambda: type("Port", (), {"adapter": adapter})(),
    )

    summary = service.cleanup_interrupted_pairs(runtime)

    assert adapter.cancelled == ["OID-H"]
    assert summary["order_cancelled"] == 1
    assert summary["close_orders"] == 0


def test_startup_quarantine_cancels_only_kolabi_orphans(monkeypatch) -> None:
    service = BotService(BotConfig(symbol="PI_XBTUSD", exchange="kraken", require_ready=False))
    kolabi_record = PrivateOrderRecord(
        symbol="PI_XBTUSD",
        status="untouched",
        exchange_order_id="OID-K",
        client_order_id="H1clean-260609220000",
        side="buy",
        order_type="stop",
        quantity=1.0,
        stop_price=100.0,
    )
    manual_record = PrivateOrderRecord(
        symbol="PI_XBTUSD",
        status="untouched",
        exchange_order_id="OID-M",
        client_order_id="manual-order",
        side="buy",
        order_type="stop",
        quantity=1.0,
        stop_price=101.0,
    )

    class FakeRuntimeState:
        def __init__(self) -> None:
            self.closed = False

        def fetch_latest_private_orders(self, *, symbol=None, open_only=False):
            del symbol, open_only
            return (manual_record,) if self.closed else (kolabi_record, manual_record)

    class FakeExecutorAdapter:
        def __init__(self, state: FakeRuntimeState) -> None:
            self.state = state
            self.cancelled: list[str] = []

        def cancel_order(self, order_id: str) -> OrderAck:
            self.cancelled.append(order_id)
            self.state.closed = True
            return OrderAck(order_id=order_id, status="Canceled")

    state = FakeRuntimeState()
    adapter = FakeExecutorAdapter(state)
    service.runtime_state = state  # type: ignore[assignment]
    class FakePort:
        def __init__(self, wrapped: FakeExecutorAdapter) -> None:
            self.adapter = wrapped

        async def cancel(self, command) -> OrderAck:
            return self.adapter.cancel_order(command.request.clOrdID)

    monkeypatch.setattr(
        service,
        "_build_admin_port",
        lambda: FakePort(adapter),
    )

    service._cleanup_startup_orphans(("PI_XBTUSD",))

    assert adapter.cancelled == ["OID-K"]


def test_interrupt_cleanup_resolves_tail_exchange_id_from_client_id(monkeypatch) -> None:
    strategy = _xbt_sell_tail_strategy()
    pair = strategy.pairs[0]
    service = BotService(BotConfig(symbol="PI_XBTUSD", exchange="kraken", require_ready=False))
    runtime = StrategyRuntime(strategy=strategy, symbol="PI_XBTUSD", simulate=True)
    runtime.state = replace(
        runtime.state,
        pairs={
            pair.name: PairCycleState(
                pair=pair,
                head_state=HeadState.CLOSED,
                tail_state=TailState.SUBMITTED,
                played_quantity=Decimal("7"),
                tail_identity=OrderIdentity(
                    pair_name=pair.name,
                    role="tail",
                    client_order_id="CID-T-ONLY",
                    exchange_order_id=None,
                ),
            )
        },
    )
    expected_close_side = "sell" if pair.head.side.value == "buy" else "buy"
    initial_position = 5.0 if expected_close_side == "sell" else -5.0

    class FakeAdapter:
        def __init__(self, position: float) -> None:
            self.position = position
            self.cancelled: list[str] = []
            self.closed: list[tuple[str, float]] = []

        def cancel_order(self, order_id: str) -> OrderAck:
            self.cancelled.append(order_id)
            return OrderAck(order_id=order_id, status="Canceled")

        def place_order(self, side: str, orderQty: object, **_params: object) -> OrderAck:
            qty = float(orderQty) if isinstance(orderQty, (int, float)) else float(str(orderQty))
            self.closed.append((side, qty))
            if side == "sell":
                self.position -= qty
            else:
                self.position += qty
            return OrderAck(order_id=f"CLOSE-{len(self.closed)}", status="New", side=side)

        def get_position(self) -> Position:
            return Position(symbol="PI_XBTUSD", qty=self.position)

        def live_open_orders(self) -> list[dict[str, object]]:
            return [
                {
                    "client_order_id": "CID-T-ONLY",
                    "order_id": "OID-FOUND",
                }
            ]

        def live_trigger_orders(self) -> list[dict[str, object]]:
            return []

        def open_orders(self) -> list[dict[str, object]]:
            return []

        def live_trigger_orders_db(self) -> list[dict[str, object]]:
            return []

    adapter = FakeAdapter(initial_position)
    monkeypatch.setattr(
        service,
        "_build_admin_port",
        lambda: type("Port", (), {"adapter": adapter})(),
    )

    summary = service.cleanup_interrupted_pairs(runtime)

    assert adapter.cancelled == ["OID-FOUND"]
    assert adapter.closed == [(expected_close_side, 5.0)]
    assert summary["tail_cancelled"] == 1
    assert summary["close_orders"] == 1
    assert summary["position_before_qty"] == initial_position
    assert summary["position_after_qty"] == 0.0


def test_runtime_error_triggers_live_cleanup_before_reraising(monkeypatch) -> None:
    service = BotService(BotConfig(symbol="PI_XBTUSD", exchange="kraken", require_ready=False))
    cleanup_calls: list[object] = []

    class FailingRuntime:
        async def run(self) -> StrategyRunResult:
            raise RuntimeError("head fill reference price missing")

    def cleanup(runtime: object) -> dict[str, object]:
        cleanup_calls.append(runtime)
        return {
            "pairs": 1,
            "tail_cancelled": 1,
            "close_orders": 1,
            "position_before_qty": 2.0,
            "position_after_qty": 0.0,
            "errors": 0,
        }

    monkeypatch.setattr(service, "cleanup_interrupted_pairs", cleanup)
    runtime = FailingRuntime()

    with pytest.raises(RuntimeError, match="head fill reference"):
        service._run_runtime_with_cleanup(runtime, simulate=False)  # type: ignore[arg-type]

    assert cleanup_calls == [runtime]


def test_wait_timeout_message_includes_runtime_diagnostics() -> None:
    service = BotService(BotConfig(exchange="kraken", ready_timeout_seconds=45.0))
    state = StrategyRuntimeState(
        symbol="PI_XBTUSD",
        public=PublicMarketState(
            symbol="PI_XBTUSD",
            best_bid=None,
            best_ask=None,
            mid_price=None,
            last_price=None,
            mark_price=None,
            index_price=None,
            tick_size=None,
            spread=None,
            imbalance=None,
            avg_bid=None,
            avg_ask=None,
            recorded_at=None,
            source_timestamp=None,
            age_seconds=9.5,
            source_age_seconds=None,
            indicators={},
            ready=False,
            reason="public market data is stale",
        ),
        private_ws=PrivateFeedState(
            stream_kind="private_ws",
            status="healthy",
            updated_at="2026-05-29T16:04:46.183674",
            last_heartbeat_at="2026-05-29T16:04:46.183674",
            age_seconds=266.1,
            ready=False,
            last_error=None,
            reason="private_ws state is stale",
        ),
        rest_reconciler=PrivateFeedState(
            stream_kind="rest_reconciler",
            status="healthy",
            updated_at=None,
            last_heartbeat_at=None,
            age_seconds=None,
            ready=True,
            last_error=None,
            reason=None,
        ),
        open_order_count=0,
        fill_count=0,
        position_size=None,
        position_entry_price=None,
        ready=False,
        reasons=("private_ws state is stale",),
    )

    message = service._format_wait_timeout(state)

    assert "private_ws state is stale" in message
    assert "public_age=9.50s" in message
    assert "private_status=healthy" in message
    assert "private_age=266.10s" in message
    assert "private_last_heartbeat=2026-05-29T16:04:46.183674" in message
    assert "account_scope=default" in message


def test_wait_timeout_message_hints_private_feeder_for_missing_schema() -> None:
    service = BotService(
        BotConfig(exchange="kraken", account_scope="advers", ready_timeout_seconds=5.0)
    )
    state = StrategyRuntimeState(
        symbol="PI_XBTUSD",
        public=PublicMarketState(
            symbol="PI_XBTUSD",
            best_bid=100.0,
            best_ask=101.0,
            mid_price=100.5,
            last_price=100.0,
            mark_price=100.0,
            index_price=100.0,
            tick_size=0.5,
            spread=1.0,
            imbalance=0.5,
            avg_bid=100.0,
            avg_ask=101.0,
            recorded_at="2026-06-01T00:00:00+00:00",
            source_timestamp="2026-06-01T00:00:00+00:00",
            age_seconds=1.0,
            source_age_seconds=1.0,
            indicators={},
            ready=True,
            reason=None,
        ),
        private_ws=PrivateFeedState(
            stream_kind="private_ws",
            status="missing_schema",
            updated_at=None,
            last_heartbeat_at=None,
            age_seconds=None,
            ready=False,
            last_error=None,
            reason="private_ws DB schema missing",
        ),
        rest_reconciler=PrivateFeedState(
            stream_kind="rest_reconciler",
            status="missing_schema",
            updated_at=None,
            last_heartbeat_at=None,
            age_seconds=None,
            ready=True,
            last_error=None,
            reason=None,
        ),
        open_order_count=0,
        fill_count=0,
        position_size=None,
        position_entry_price=None,
        ready=False,
        reasons=("private_ws DB schema missing",),
    )

    message = service._format_wait_timeout(state)

    assert "private_ws DB schema missing" in message
    assert "account_scope=advers" in message
    assert (
        "scripts/kolabidb private start --exchange kraken --market-type futures "
        "--pair PI_XBTUSD --account-scope advers"
    ) in message


def test_wait_timeout_message_hints_exact_exchange_market_feeders() -> None:
    service = BotService(
        BotConfig(
            exchange="binance",
            market_type="margin",
            symbol="BTCUSDT",
            account_scope="advers",
            ready_timeout_seconds=5.0,
        )
    )
    state = StrategyRuntimeState(
        symbol="BTCUSDT",
        exchange="binance",
        market_type="margin",
        public=PublicMarketState(
            symbol="BTCUSDT",
            best_bid=None,
            best_ask=None,
            mid_price=None,
            last_price=None,
            mark_price=None,
            index_price=None,
            tick_size=None,
            spread=None,
            imbalance=None,
            avg_bid=None,
            avg_ask=None,
            recorded_at=None,
            source_timestamp=None,
            age_seconds=None,
            source_age_seconds=None,
            indicators={},
            ready=False,
            reason="missing public market snapshot",
        ),
        private_ws=PrivateFeedState(
            stream_kind="private_ws",
            status="missing_schema",
            updated_at=None,
            last_heartbeat_at=None,
            age_seconds=None,
            ready=False,
            last_error=None,
            reason="private_ws DB schema missing",
        ),
        rest_reconciler=PrivateFeedState(
            stream_kind="rest_reconciler",
            status="missing_schema",
            updated_at=None,
            last_heartbeat_at=None,
            age_seconds=None,
            ready=True,
            last_error=None,
            reason=None,
        ),
        open_order_count=0,
        fill_count=0,
        position_size=None,
        position_entry_price=None,
        ready=False,
        reasons=("missing public market snapshot", "private_ws DB schema missing"),
    )

    message = service._format_wait_timeout(state)

    assert (
        "start_public='scripts/kolabidb public start --exchange binance "
        "--market-type margin --pair BTCUSDT'"
    ) in message
    assert (
        "start_private='scripts/kolabidb private start --exchange binance "
        "--market-type margin --pair BTCUSDT --account-scope advers'"
    ) in message


def test_wait_timeout_message_hints_private_feeder_with_route_symbol() -> None:
    service = BotService(
        BotConfig(
            exchange="bitmex",
            market_type="spot",
            symbol="XBT_USDT",
            account_scope="advers",
            ready_timeout_seconds=5.0,
        )
    )
    state = StrategyRuntimeState(
        symbol="XBT_USDT",
        exchange="bitmex",
        market_type="spot",
        public=PublicMarketState(
            symbol="XBT_USDT",
            best_bid=100.0,
            best_ask=101.0,
            mid_price=100.5,
            last_price=100.0,
            mark_price=100.0,
            index_price=100.0,
            tick_size=0.5,
            spread=1.0,
            imbalance=0.5,
            avg_bid=100.0,
            avg_ask=101.0,
            recorded_at="2026-06-01T00:00:00+00:00",
            source_timestamp="2026-06-01T00:00:00+00:00",
            age_seconds=1.0,
            source_age_seconds=1.0,
            indicators={},
            ready=True,
            reason=None,
        ),
        private_ws=PrivateFeedState(
            stream_kind="private_ws",
            status="missing_schema",
            updated_at=None,
            last_heartbeat_at=None,
            age_seconds=None,
            ready=False,
            last_error=None,
            reason="private DB schema missing",
        ),
        rest_reconciler=PrivateFeedState(
            stream_kind="rest_reconciler",
            status="missing_schema",
            updated_at=None,
            last_heartbeat_at=None,
            age_seconds=None,
            ready=True,
            last_error=None,
            reason=None,
        ),
        open_order_count=0,
        fill_count=0,
        position_size=None,
        position_entry_price=None,
        ready=False,
        reasons=("private DB schema missing",),
    )

    message = service._format_wait_timeout(state)

    assert (
        "start_private='scripts/kolabidb private start --exchange bitmex "
        "--market-type spot --pair XBT_USDT --account-scope advers'"
    ) in message
