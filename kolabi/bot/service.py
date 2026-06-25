from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, Callable, Iterable, Optional, Protocol, TypeVar, cast

from sqlalchemy.exc import SQLAlchemyError

from kolabi.bot.domain import OrderIdentity, OrderPairSpec, StrategySpec
from kolabi.bot.exchange_routes import (
    DEFAULT_MARKET_TYPE,
    SUPPORTED_MARKET_TYPES,
    ExchangeRoute,
    exchange_supports_market_type,
    normalise_exchange_name,
    unsupported_market_message,
)
from kolabi.bot.exchange_routes import (
    pair_route as resolve_pair_route,
)
from kolabi.bot.indicators import (
    DummyIndicatorClient,
    IndicatorClient,
    KrakenDbIndicatorClient,
)
from kolabi.bot.ogun_executor import OgunExecutor, RestFlightPolicy
from kolabi.bot.order_codes import parse_order_code
from kolabi.bot.persistence import (
    OrderRecorder,
    PersistenceConfig,
    TailTelemetryRecorder,
)
from kolabi.bot.quantity import (
    QuantityResolutionError,
    available_usd_for_percent_quantity,
    mark_price_for_usd_quantity,
    pair_uses_percent_balance_quantity,
    pair_uses_usd_quantity,
    resolve_pair_percent_balance_quantity,
    resolve_pair_usd_quantity,
    validate_pair_absolute_quantity,
)
from kolabi.bot.strategy_runtime import (
    KrakenPrivateOrderPollingSource,
    KrakenPublicTriggerSource,
    PublicRuntimeStateReader,
    SimulatedExecutor,
    StaticHookSource,
    StrategyRunResult,
    StrategyRuntime,
    plan_strategy_once,
)
from kolabi.shared.binance_futures import (
    binance_futures_audit_db_url,
    binance_futures_critical_db_url,
    binance_futures_private_db_url,
    binance_futures_public_db_url,
    binance_futures_telemetry_db_url,
)
from kolabi.shared.bitmex_futures import (
    bitmex_futures_audit_db_url,
    bitmex_futures_critical_db_url,
    bitmex_futures_private_db_url,
    bitmex_futures_public_db_url,
    bitmex_futures_telemetry_db_url,
)
from kolabi.shared.config import (
    ExchangeConfig,
    exchange_base_url_env_names,
    exchange_credential_env_names,
    exchange_requires_explicit_base_url,
    load_exchange_config,
)
from kolabi.shared.core.models import OrderAck
from kolabi.shared.core.runtime_types import (
    AmendHeadCommand,
    AmendOrderCommandRequest,
    AmendTailCommand,
    CancelCommand,
    CancelOrderCommandRequest,
    DragonSong,
    ExchangePort,
    PlaceHeadCommand,
    PlaceOrderCommandRequest,
    PlaceTailCommand,
    PrivateOrderRecord,
    RuntimeCommandKind,
    Symbol,
)
from kolabi.shared.exchanges import get_adapter
from kolabi.shared.kraken_futures import (
    kraken_futures_audit_db_url,
    kraken_futures_environment,
    kraken_futures_public_db_url,
    kraken_futures_telemetry_db_url,
)
from kolabi.shared.logging import setup_logging
from kolabi.shared.pruning import DEFAULT_PRUNING, TimeCountPruning
from kolabi.shared.redaction import redact_url
from kolabi.shared.runtime_state import KrakenRuntimeStateClient, StrategyRuntimeState

_LOGGER = logging.getLogger("kola")
_T = TypeVar("_T")
_KOLABI_ORDER_CLIENT_ID_RE = re.compile(r"^[HT][1-9][0-9]*[A-Za-z]+-\d{12}$")


def _env_scope_key(account_scope: str) -> str:
    """Return the env-var-safe account scope key used for Kolabi DB lanes."""

    raw = account_scope.strip() or "default"
    return "".join(ch if ch.isalnum() else "_" for ch in raw).upper()


def _kolabi_scoped_db_url(lane: str, account_scope: str) -> str | None:
    """Resolve KOLABI_* DB lane URLs without mixing scoped accounts by accident."""

    lane_key = lane.upper()
    if (account_scope.strip() or "default") == "default":
        return os.environ.get(f"KOLABI_{lane_key}_DB_URL")
    return os.environ.get(f"KOLABI_{_env_scope_key(account_scope)}_{lane_key}_DB_URL")


def _pair_symbol(pair: OrderPairSpec, default_symbol: str) -> str:
    symbol = (pair.symbol or "").strip()
    return symbol or default_symbol


def _pair_route(
    pair: OrderPairSpec,
    *,
    default_exchange: str,
    default_symbol: str,
    default_market_type: str = DEFAULT_MARKET_TYPE,
) -> ExchangeRoute:
    return resolve_pair_route(
        pair,
        default_exchange=default_exchange,
        default_symbol=default_symbol,
        default_market_type=default_market_type,
    )


def _strategy_symbols(strategy: StrategySpec) -> tuple[str, ...]:
    symbols = tuple(sorted({str(pair.symbol) for pair in strategy.pairs if pair.symbol}))
    return symbols


def _strategy_routes(
    strategy: StrategySpec,
    *,
    default_exchange: str,
    default_symbol: str,
    default_market_type: str = DEFAULT_MARKET_TYPE,
) -> tuple[ExchangeRoute, ...]:
    routes = {
        _pair_route(
            pair,
            default_exchange=default_exchange,
            default_symbol=default_symbol,
            default_market_type=default_market_type,
        )
        for pair in strategy.pairs
    }
    return tuple(sorted(routes))


def _first_present_env_name(names: Iterable[str]) -> str | None:
    for name in names:
        if os.environ.get(name):
            return name
    return None


def _route_credential_status(
    route: ExchangeRoute,
    *,
    environment: str,
    api_key_env: str | None = None,
    api_secret_env: str | None = None,
    base_url: str | None = None,
) -> dict[str, object]:
    """Return route credential readiness without exposing secret values."""

    api_key_names = (
        [api_key_env]
        if api_key_env
        else list(
            exchange_credential_env_names(
                route.exchange,
                route.market_type,
                environment,
            )
        )
    )
    api_secret_names = (
        [api_secret_env]
        if api_secret_env
        else list(
            exchange_credential_env_names(
                route.exchange,
                route.market_type,
                environment,
                secret=True,
            )
        )
    )
    api_key_source = _first_present_env_name(api_key_names)
    api_secret_source = _first_present_env_name(api_secret_names)
    base_url_env = list(
        exchange_base_url_env_names(
            route.exchange,
            route.market_type,
            environment,
        )
    )
    base_url_source = "override" if base_url else _first_present_env_name(base_url_env)
    base_url_required = exchange_requires_explicit_base_url(
        route.exchange,
        route.market_type,
        environment,
    )
    base_url_ready = bool(base_url_source) or not base_url_required
    return {
        "route": route.label,
        "exchange": route.exchange,
        "market_type": route.market_type,
        "symbol": route.symbol,
        "api_key_env": api_key_names,
        "api_secret_env": api_secret_names,
        "api_key_present": api_key_source is not None,
        "api_secret_present": api_secret_source is not None,
        "credentials_present": (
            api_key_source is not None and api_secret_source is not None
        ),
        "api_key_source": api_key_source,
        "api_secret_source": api_secret_source,
        "base_url_env": base_url_env,
        "base_url_required": base_url_required,
        "base_url_present": bool(base_url_source),
        "base_url_ready": base_url_ready,
        "base_url_source": base_url_source,
    }


def _default_route_value(
    route: ExchangeRoute,
    *,
    default_exchange: str,
    default_market_type: str,
    value: _T | None,
) -> _T | None:
    """Return a config override only for the process default route."""

    if route.exchange == default_exchange and route.market_type == default_market_type:
        return value
    return None


def _missing_credential_fields(item: dict[str, object]) -> tuple[str, ...]:
    return tuple(
        label
        for label, key in (
            ("api_key", "api_key_present"),
            ("api_secret", "api_secret_present"),
        )
        if not bool(item.get(key))
    )


def _route_config_reasons(item: dict[str, object]) -> tuple[str, ...]:
    route = item.get("route") or "-"
    missing_credentials = _missing_credential_fields(item)
    credential_reasons = (
        (f"{route}:missing credentials " + ",".join(missing_credentials),)
        if missing_credentials
        else ()
    )
    base_url_reasons = (
        (f"{route}:missing required base_url override",)
        if not bool(item.get("base_url_ready", True))
        else ()
    )
    return credential_reasons + base_url_reasons


def _credential_reasons(
    credential_routes: Iterable[dict[str, object]],
) -> tuple[str, ...]:
    return tuple(
        reason
        for item in credential_routes
        for reason in _route_config_reasons(item)
    )


def _runtime_state_error_payload(
    route: ExchangeRoute,
    exc: BaseException,
) -> dict[str, object]:
    reason = f"{route.label}:runtime state unavailable {_compact_admin_error(exc)}"
    return {
        "exchange": route.exchange,
        "market_type": route.market_type,
        "symbol": route.symbol,
        "ready": False,
        "status": "error",
        "reasons": (reason,),
        "error": _compact_admin_error(exc),
    }


class InstrumentRulesExchange(Protocol):
    def instrument_rules(self, symbol: str | None = None) -> dict[str, object]: ...


class SymbolValidationExchange(InstrumentRulesExchange, Protocol):
    def validate_symbol(self, symbol: str | None = None) -> dict[str, object]: ...


def _adapter_class(exchange: str, market_type: str):
    """Load adapters while preserving older one-argument test doubles."""

    try:
        return get_adapter(exchange, market_type)
    except TypeError:
        return get_adapter(exchange)


def _validate_route_symbol(
    adapter: InstrumentRulesExchange,
    route: ExchangeRoute,
) -> dict[str, object]:
    """Validate one route symbol and return instrument rules."""

    validator = getattr(adapter, "validate_symbol", None)
    if callable(validator):
        validation = cast(SymbolValidationExchange, adapter).validate_symbol(route.symbol)
        rules = {
            **dict(validation),
            **adapter.instrument_rules(route.symbol),
        }
    else:
        rules = adapter.instrument_rules(route.symbol)
    tradeable = rules.get("tradeable")
    if tradeable is False:
        raise ValueError(f"Route {route.label} symbol is not tradeable")
    return rules


def _fetch_service_market_state(
    reader: PublicRuntimeStateReader,
    route: ExchangeRoute,
) -> object:
    try:
        return reader.fetch_market_state(
            symbol=route.symbol,
            exchange=route.exchange,
            market_type=route.market_type,
        )
    except TypeError as exc:
        message = str(exc)
        if "exchange" not in message and "market_type" not in message:
            raise
        return reader.fetch_market_state(route.symbol)


def _fetch_service_account_balance(
    reader: object,
    route: ExchangeRoute,
) -> object:
    fetch_balance = getattr(reader, "fetch_account_balance", None)
    if not callable(fetch_balance):
        raise QuantityResolutionError(
            f"QTY_PCT_TOO_SMALL pair=- route={route.label} missing account balance reader"
        )
    try:
        return fetch_balance(
            asset="USD",
            exchange=route.exchange,
            market_type=route.market_type,
        )
    except TypeError as exc:
        message = str(exc)
        if "exchange" not in message and "market_type" not in message:
            raise
        return fetch_balance("USD")


class ExchangeAdapterLike(Protocol):
    def place_order(
        self,
        side: str,
        orderQty: object,
        price: object | None = None,
        stopPx: object | None = None,
        type_: str = "LIMIT",
        **params: object,
    ) -> OrderAck: ...
    def amend_order(self, order_id: str, **params: object) -> OrderAck: ...
    def cancel_order(self, order_id: str) -> OrderAck: ...
    def get_position(self) -> Any: ...


class TriggerOrderReader(Protocol):
    def live_trigger_orders(self) -> list[dict[str, Any]]: ...
    def live_trigger_orders_db(self) -> list[dict[str, Any]]: ...


class OpenOrderReader(Protocol):
    def live_open_orders(self) -> list[dict[str, Any]]: ...
    def live_trigger_orders(self) -> list[dict[str, Any]]: ...
    def open_orders(self) -> list[dict[str, Any]]: ...
    def live_trigger_orders_db(self) -> list[dict[str, Any]]: ...


@dataclass
class BotConfig:
    """Runtime configuration for kolaBiBot."""

    exchange: str = "kraken"
    symbol: str = "PI_XBTUSD"
    market_type: str = DEFAULT_MARKET_TYPE
    environment: str = "demo"
    updatepause: int = 10
    logpause: int = 60
    dummy: bool = False
    log_level: str = "INFO"
    db_url: Optional[str] = None
    market_db_url: Optional[str] = None
    account_db_url: Optional[str] = None
    critical_account_db_url: Optional[str] = None
    audit_db_url: Optional[str] = None
    telemetry_db_url: Optional[str] = None
    account_scope: str = "default"
    api_key_env: Optional[str] = None
    api_secret_env: Optional[str] = None
    base_url: Optional[str] = None
    require_ready: bool = True
    ready_timeout_seconds: float = 45.0
    ready_poll_seconds: float = 1.0
    max_public_age_seconds: float = 15.0
    max_private_age_seconds: float = 30.0
    max_reconcile_age_seconds: float = 300.0
    tail_verify_timeout_seconds: float = 11.0
    tail_verify_poll_seconds: float = 0.5
    tail_visibility_timeout_seconds: float = 30.0
    max_active_pairs: int = 4
    rest_min_interval_seconds: float = 0.1
    rest_max_inflight: int = 2
    rest_audit_retention_minutes: int = DEFAULT_PRUNING.rest_audit.retention_minutes
    rest_audit_retention_limit: int = DEFAULT_PRUNING.rest_audit.retention_limit
    tail_telemetry_retention_minutes: int = (
        DEFAULT_PRUNING.tail_telemetry.retention_minutes
    )
    tail_telemetry_retention_limit: int = DEFAULT_PRUNING.tail_telemetry.retention_limit


class BotService:
    """High-level orchestrator for the single active `kolabi.bot` strategy path."""

    def __init__(
        self,
        config: BotConfig,
        indicators: IndicatorClient | None = None,
    ) -> None:
        self.config = config
        self.logger = setup_logging(config.log_level)
        self.exchange_config: ExchangeConfig | None = None
        self._exchange_config_cache: dict[tuple[str, str, str], ExchangeConfig] = {}
        self.default_exchange = normalise_exchange_name(config.exchange)
        self.default_market_type = (
            config.market_type or DEFAULT_MARKET_TYPE
        ).strip().lower()
        if not exchange_supports_market_type(
            self.default_exchange,
            self.default_market_type,
        ):
            reason = unsupported_market_message(
                self.default_exchange,
                self.default_market_type,
            )
            raise ValueError(
                f"Market type '{self.default_market_type}' {reason} for default "
                f"exchange '{self.default_exchange}'"
            )
        market_db_url = config.market_db_url or os.environ.get("KOLABI_MARKET_DB_URL")
        account_db_url = config.account_db_url or _kolabi_scoped_db_url(
            "ACCOUNT",
            config.account_scope,
        )
        critical_account_db_url = (
            config.critical_account_db_url
            or _kolabi_scoped_db_url("CRITICAL", config.account_scope)
        )
        audit_db_url = config.audit_db_url or _kolabi_scoped_db_url(
            "AUDIT",
            config.account_scope,
        )
        telemetry_db_url = config.telemetry_db_url or _kolabi_scoped_db_url(
            "TELEMETRY",
            config.account_scope,
        )
        if self.default_exchange == "kraken":
            env_cfg = kraken_futures_environment(config.environment)
            market_db_url = market_db_url or kraken_futures_public_db_url(
                config.environment,
                config.symbol,
            )
            account_db_url = account_db_url or env_cfg.private_db_url
            critical_account_db_url = (
                critical_account_db_url or env_cfg.critical_private_db_url
            )
            audit_db_url = audit_db_url or kraken_futures_audit_db_url(
                config.environment,
                config.account_scope,
            )
            telemetry_db_url = telemetry_db_url or kraken_futures_telemetry_db_url(
                config.environment,
                config.account_scope,
            )
        elif self.default_exchange == "binance":
            market_db_url = market_db_url or binance_futures_public_db_url(
                config.environment,
                config.symbol,
            )
            account_db_url = account_db_url or binance_futures_private_db_url(
                config.environment,
                config.account_scope,
            )
            critical_account_db_url = (
                critical_account_db_url
                or binance_futures_critical_db_url(
                    config.environment,
                    config.account_scope,
                )
            )
            audit_db_url = audit_db_url or binance_futures_audit_db_url(
                config.environment,
                config.account_scope,
            )
            telemetry_db_url = telemetry_db_url or binance_futures_telemetry_db_url(
                config.environment,
                config.account_scope,
            )
        elif self.default_exchange == "bitmex":
            market_db_url = market_db_url or bitmex_futures_public_db_url(
                config.environment,
                config.symbol,
            )
            account_db_url = account_db_url or bitmex_futures_private_db_url(
                config.environment,
                config.account_scope,
            )
            critical_account_db_url = (
                critical_account_db_url
                or bitmex_futures_critical_db_url(
                    config.environment,
                    config.account_scope,
                )
            )
            audit_db_url = audit_db_url or bitmex_futures_audit_db_url(
                config.environment,
                config.account_scope,
            )
            telemetry_db_url = telemetry_db_url or bitmex_futures_telemetry_db_url(
                config.environment,
                config.account_scope,
            )
        self.indicators: IndicatorClient = indicators or (
            KrakenDbIndicatorClient(
                db_url=market_db_url
                or kraken_futures_public_db_url(config.environment, config.symbol),
                exchange=self.default_exchange,
                environment=config.environment,
                market_type=self.default_market_type,
            )
            if self.default_exchange in {"kraken", "binance", "bitmex"}
            else DummyIndicatorClient()
        )
        self.recorder: OrderRecorder | None = (
            OrderRecorder(PersistenceConfig(config.db_url))
            if config.db_url
            else None
        )
        self._server_started = False
        self._account_db_url = account_db_url
        self._critical_account_db_url = critical_account_db_url
        self._market_db_url = market_db_url
        self._audit_db_url = audit_db_url
        self._telemetry_db_url = telemetry_db_url
        self._required_symbols: tuple[str, ...] = (config.symbol,)
        self._required_routes: tuple[ExchangeRoute, ...] = (
            ExchangeRoute(
                self.default_exchange,
                self.default_market_type,
                config.symbol,
            ),
        )
        self._instrument_rules_by_route: dict[ExchangeRoute, dict[str, object]] = {}
        self.runtime_state: KrakenRuntimeStateClient | None = None
        if (
            self.default_exchange in {"kraken", "binance", "bitmex"}
            and market_db_url is not None
            and account_db_url is not None
        ):
            self.runtime_state = KrakenRuntimeStateClient(
                market_db_url=market_db_url,
                account_db_url=account_db_url,
                critical_account_db_url=critical_account_db_url,
                symbol=config.symbol,
                exchange=self.default_exchange,
                environment=config.environment,
                market_type=self.default_market_type,
                account_scope=config.account_scope,
                max_public_age_seconds=config.max_public_age_seconds,
                max_private_age_seconds=config.max_private_age_seconds,
                max_reconcile_age_seconds=config.max_reconcile_age_seconds,
            )

    def start(self) -> None:
        if not self._server_started:
            self._wait_until_ready()
            self._server_started = True

    def preflight(self, strategy: StrategySpec | None = None) -> dict[str, object]:
        """Return the current runtime readiness payload."""
        if strategy is not None:
            strategy = self._materialize_strategy_symbols(strategy)
            self._required_symbols = _strategy_symbols(strategy) or (self.config.symbol,)
            self._required_routes = _strategy_routes(
                strategy,
                default_exchange=self.default_exchange,
                default_symbol=self.config.symbol,
                default_market_type=self.default_market_type,
            ) or (
                ExchangeRoute(
                    self.default_exchange,
                    self.default_market_type,
                    self.config.symbol,
                )
            )
        credential_routes = tuple(
            _route_credential_status(
                route,
                environment=self.config.environment,
                api_key_env=_default_route_value(
                    route,
                    default_exchange=self.default_exchange,
                    default_market_type=self.default_market_type,
                    value=self.config.api_key_env,
                ),
                api_secret_env=_default_route_value(
                    route,
                    default_exchange=self.default_exchange,
                    default_market_type=self.default_market_type,
                    value=self.config.api_secret_env,
                ),
                base_url=_default_route_value(
                    route,
                    default_exchange=self.default_exchange,
                    default_market_type=self.default_market_type,
                    value=self.config.base_url,
                ),
            )
            for route in self._required_routes
        )
        credentials_ready = all(
            bool(item.get("credentials_present")) for item in credential_routes
        )
        base_urls_ready = all(
            bool(item.get("base_url_ready", True)) for item in credential_routes
        )
        route_config_ready = credentials_ready and base_urls_ready
        credential_reasons = _credential_reasons(credential_routes)
        strategy_validation_ready = True
        strategy_validation_reasons: tuple[str, ...] = ()
        if strategy is not None and route_config_ready and self.runtime_state is not None:
            try:
                self._validate_pairs(strategy.pairs)
            except ValueError as exc:
                strategy_validation_ready = False
                strategy_validation_reasons = (_compact_admin_error(exc),)
        if self.runtime_state is None:
            return {
                "exchange": self.default_exchange,
                "symbol": self.config.symbol,
                "ready": route_config_ready and strategy_validation_ready,
                "reasons": credential_reasons + strategy_validation_reasons,
                "status": (
                    "not_applicable"
                    if route_config_ready and strategy_validation_ready
                    else "waiting"
                ),
                "credentials_ready": credentials_ready,
                "base_urls_ready": base_urls_ready,
                "route_config_ready": route_config_ready,
                "strategy_validation_ready": strategy_validation_ready,
                "credential_routes": credential_routes,
            }
        states: list[StrategyRuntimeState] = []
        state_errors: list[dict[str, object]] = []
        for route in self._required_routes:
            try:
                states.append(
                    self.runtime_state.fetch_runtime_state(
                        symbol=route.symbol,
                        exchange=route.exchange,
                        market_type=route.market_type,
                    )
                )
            except SQLAlchemyError as exc:
                state_errors.append(_runtime_state_error_payload(route, exc))
        runtime_error_reasons = tuple(
            reason
            for error in state_errors
            for reason in tuple(error.get("reasons") or ())
        )
        if len(self._required_routes) == 1 and states:
            payload = states[0].as_dict()
            payload["ready"] = (
                states[0].ready and route_config_ready and strategy_validation_ready
            )
            payload["status"] = "ok" if payload["ready"] else "waiting"
            payload["credentials_ready"] = credentials_ready
            payload["base_urls_ready"] = base_urls_ready
            payload["route_config_ready"] = route_config_ready
            payload["strategy_validation_ready"] = strategy_validation_ready
            payload["credential_routes"] = credential_routes
            payload["reasons"] = (
                tuple(payload.get("reasons") or ())
                + credential_reasons
                + strategy_validation_reasons
            )
            return payload
        if len(self._required_routes) == 1 and state_errors:
            payload = dict(state_errors[0])
            payload["credentials_ready"] = credentials_ready
            payload["base_urls_ready"] = base_urls_ready
            payload["route_config_ready"] = route_config_ready
            payload["strategy_validation_ready"] = strategy_validation_ready
            payload["credential_routes"] = credential_routes
            payload["reasons"] = (
                runtime_error_reasons
                + credential_reasons
                + strategy_validation_reasons
            )
            return payload
        ready = (
            len(states) == len(self._required_routes)
            and all(state.ready for state in states)
            and route_config_ready
            and strategy_validation_ready
        )
        reasons = tuple(
            f"{state.exchange or self.default_exchange}:{state.symbol}:{reason}"
            for state in states
            for reason in state.reasons
        ) + runtime_error_reasons + credential_reasons + strategy_validation_reasons
        route_payloads = tuple(state.as_dict() for state in states) + tuple(state_errors)
        return {
            "exchange": self.default_exchange,
            "ready": ready,
            "status": "ok" if ready else "waiting",
            "reasons": reasons,
            "routes": route_payloads,
            "credentials_ready": credentials_ready,
            "base_urls_ready": base_urls_ready,
            "route_config_ready": route_config_ready,
            "strategy_validation_ready": strategy_validation_ready,
            "credential_routes": credential_routes,
        }

    def _wait_until_ready(self) -> None:
        """Wait for fresh DB-grounded public/private state before starting runtime."""
        if (
            self.runtime_state is None
            or not self.config.require_ready
        ):
            return
        self.logger.info(
            "%s runtime preflight routes=%s env=%s market_db=%s account_db=%s critical_account_db=%s",
            self.default_exchange,
            ",".join(route.label for route in self._required_routes),
            self.config.environment,
            redact_url(self._market_db_url),
            redact_url(self._account_db_url),
            redact_url(self._critical_account_db_url or self._account_db_url),
        )
        states = tuple(
            self.runtime_state.wait_until_ready(
                symbol=route.symbol,
                exchange=route.exchange,
                market_type=route.market_type,
                timeout_seconds=self.config.ready_timeout_seconds,
                poll_seconds=self.config.ready_poll_seconds,
            )
            for route in self._required_routes
        )
        stale_states = tuple(state for state in states if not state.ready)
        if stale_states:
            raise TimeoutError(self._format_wait_timeouts(stale_states))
        self._cleanup_startup_orphans(self._required_routes)
        state = states[0]
        public_ages = ",".join(
            f"{item.exchange or self.default_exchange}:{item.symbol}:{item.public.age_seconds or 0.0:.2f}s"
            for item in states
        )
        self.logger.info(
            "%s runtime ready routes=%s public_ages=%s private_age=%.2fs",
            self.default_exchange,
            ",".join(route.label for route in self._required_routes),
            public_ages,
            state.private_ws.age_seconds or 0.0,
        )

    def _format_wait_timeouts(self, states: tuple[StrategyRuntimeState, ...]) -> str:
        return " | ".join(self._format_wait_timeout(state) for state in states)

    def _format_wait_timeout(self, state: StrategyRuntimeState) -> str:
        """Format a short readiness timeout message for CLI users."""
        reasons = ", ".join(state.reasons) if state.reasons else "unknown readiness failure"
        public_age = (
            f"{state.public.age_seconds:.2f}s"
            if state.public.age_seconds is not None
            else "unknown"
        )
        private_age = (
            f"{state.private_ws.age_seconds:.2f}s"
            if state.private_ws.age_seconds is not None
            else "unknown"
        )
        private_last_heartbeat = state.private_ws.last_heartbeat_at or "-"
        route = ExchangeRoute(
            exchange=state.exchange or self.default_exchange,
            market_type=state.market_type or self.default_market_type,
            symbol=state.symbol,
        )
        hint = ""
        if state.private_ws.status in {"missing", "missing_schema"}:
            hint = f" start_private='{_private_feeder_hint(route, self.config.account_scope)}'"
        public_hint = (
            f" start_public='{_public_feeder_hint(route)}'"
            if state.public.reason
            else ""
        )
        return (
            f"{(state.exchange or self.default_exchange).capitalize()} runtime did not become ready within "
            f"{self.config.ready_timeout_seconds:.0f}s for symbol={state.symbol}: {reasons} "
            f"(public_age={public_age} private_status={state.private_ws.status} "
            f"private_age={private_age} private_last_heartbeat={private_last_heartbeat} "
            f"account_scope={self.config.account_scope}{public_hint}{hint})"
        )

    def _cleanup_startup_orphans(self, routes: Iterable[ExchangeRoute | str]) -> None:
        if self.runtime_state is None:
            return
        routes = self._coerce_routes(routes)
        orphans = self._open_kolabi_orders(routes)
        if not orphans:
            return
        for record in orphans:
            self.logger.warning(
                "ORPHAN_FOUND (%s): %s",
                _record_route_label(record),
                _runtime_admin_fields(
                    record.client_order_id or "-",
                    record.exchange_order_id or "-",
                    record.side or "-",
                    record.quantity if record.quantity is not None else "-",
                    record.stop_price if record.stop_price is not None else record.price or "-",
                ),
            )
        port: ExchangePort
        default_route = ExchangeRoute(
            self.default_exchange,
            self.default_market_type,
            self.config.symbol,
        )
        if set(routes) == {default_route}:
            port = self._build_admin_port()
        else:
            port = self._build_routing_port(verify_tail_on_place=True)
        executor = OgunExecutor(port)
        for record in orphans:
            cancel_id = record.exchange_order_id or record.client_order_id
            if cancel_id is None:
                continue
            command = CancelCommand(
                kind=RuntimeCommandKind.CANCEL,
                symbol=Symbol(record.symbol),
                pair_name="__startup_orphan__",
                request=CancelOrderCommandRequest(
                    pair_name="__startup_orphan__",
                    clOrdID=cancel_id,
                ),
                reason="startup_orphan",
                exchange=record.exchange or self.default_exchange,
                market_type=record.market_type or self.default_market_type,
            )
            try:
                asyncio.run(executor.execute(command))
            except Exception as exc:
                raise RuntimeError(
                    "startup orphan cancel failed "
                    f"symbol={record.symbol} client_id={record.client_order_id or '-'} "
                    f"order_id={record.exchange_order_id or '-'} error={_compact_admin_error(exc)}"
                ) from exc
            self.logger.warning(
                "ORPHAN_CANCEL_SENT (%s): %s",
                _record_route_label(record),
                _runtime_admin_fields(
                    record.client_order_id or "-",
                    record.exchange_order_id or "-",
                    "startup_orphan",
                ),
            )
        survivors = self._wait_for_private_orders_closed(
            routes,
            orphans,
            timeout_seconds=self.config.ready_timeout_seconds,
            poll_seconds=self.config.ready_poll_seconds,
        )
        if survivors:
            details = ", ".join(_private_order_summary(record) for record in survivors)
            raise RuntimeError(f"startup orphan orders still open after cancel: {details}")

    def _open_kolabi_orders(self, routes: Iterable[ExchangeRoute | str]) -> tuple[PrivateOrderRecord, ...]:
        if self.runtime_state is None:
            return ()
        routes = self._coerce_routes(routes)
        rows: list[PrivateOrderRecord] = []
        for route in routes:
            fetch_latest = getattr(self.runtime_state, "fetch_latest_private_orders", None)
            if not callable(fetch_latest):
                continue
            try:
                records = fetch_latest(
                    symbol=route.symbol,
                    exchange=route.exchange,
                    market_type=route.market_type,
                    open_only=True,
                )
            except TypeError as exc:
                message = str(exc)
                if "exchange" not in message and "market_type" not in message:
                    raise
                records = fetch_latest(symbol=route.symbol, open_only=True)
            for record in records:
                if _is_kolabi_order_record(record):
                    rows.append(record)
        return tuple(rows)

    def _wait_for_private_orders_closed(
        self,
        routes: Iterable[ExchangeRoute | str],
        targets: Iterable[PrivateOrderRecord],
        *,
        timeout_seconds: float,
        poll_seconds: float,
    ) -> tuple[PrivateOrderRecord, ...]:
        routes = self._coerce_routes(routes)
        target_keys = {_private_order_key(record) for record in targets}
        target_keys.discard(None)
        if not target_keys:
            return ()
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        sleep_seconds = max(0.05, poll_seconds)
        while True:
            survivors = tuple(
                record
                for record in self._open_kolabi_orders(routes)
                if _private_order_key(record) in target_keys
            )
            if not survivors:
                for key in target_keys:
                    self.logger.info("ORPHAN_CLOSED: %s", key)
                return ()
            if time.monotonic() >= deadline:
                return survivors
            time.sleep(sleep_seconds)

    def _coerce_routes(self, routes: Iterable[ExchangeRoute | str]) -> tuple[ExchangeRoute, ...]:
        coerced: list[ExchangeRoute] = []
        for route in routes:
            if isinstance(route, ExchangeRoute):
                coerced.append(route)
                continue
            coerced.append(
                ExchangeRoute(
                    exchange=self.default_exchange,
                    market_type=self.default_market_type,
                    symbol=str(route),
                )
            )
        return tuple(coerced)

    def run_strategy(
        self,
        strategy: StrategySpec,
        *,
        dry_run: bool = False,
        simulate: bool = False,
    ) -> StrategyRunResult:
        """Execute the active typed runtime path in the foreground."""
        strategy = self._materialize_strategy_symbols(strategy)
        self._required_symbols = _strategy_symbols(strategy) or (self.config.symbol,)
        self._required_routes = _strategy_routes(
            strategy,
            default_exchange=self.default_exchange,
            default_symbol=self.config.symbol,
            default_market_type=self.default_market_type,
        ) or (
            ExchangeRoute(
                self.default_exchange,
                self.default_market_type,
                self.config.symbol,
            )
        )
        if not dry_run and not simulate:
            self._validate_multi_route_market_db(strategy)
        pair_list = list(strategy.pairs)
        defer_usd_validation = (
            not dry_run
            and not simulate
            and self.config.require_ready
        )
        if not dry_run or self.exchange_config is not None:
            self._validate_pairs(
                pair_list,
                validate_usd_notional=not defer_usd_validation,
            )
        if not dry_run and not simulate:
            self.start()
            if defer_usd_validation:
                self._validate_pairs(pair_list, validate_usd_notional=True)
        for pair in pair_list:
            run_id: Optional[int] = None
            if self.recorder:
                snapshot = self.indicators.fetch_snapshot(
                    _pair_symbol(pair, self.config.symbol)
                )
                run = self.recorder.start_run(pair, snapshot)
                run_id = run.id
                self.logger.info(f"[{pair.name}] submitted run #{run_id}")
            del run_id
        runtime = StrategyRuntime(
            strategy=strategy,
            symbol=self.config.symbol,
            executor=None if dry_run else self._build_executor(simulate=simulate),
            public_source=self._build_public_source(simulate=simulate),
            private_source=self._build_private_source(simulate=simulate),
            public_state_reader=(
                None
                if simulate
                else cast(PublicRuntimeStateReader | None, self.runtime_state)
            ),
            account_state_reader=None if simulate else self.runtime_state,
            tail_telemetry_writer=(
                None
                if dry_run or simulate or self._telemetry_db_url is None
                else TailTelemetryRecorder(
                    PersistenceConfig(
                        self._telemetry_db_url,
                        tail_telemetry_pruning=TimeCountPruning(
                            retention_minutes=(
                                self.config.tail_telemetry_retention_minutes
                            ),
                            retention_limit=self.config.tail_telemetry_retention_limit,
                            maintenance_seconds=(
                                DEFAULT_PRUNING.tail_telemetry.maintenance_seconds
                            ),
                        ),
                    )
                )
            ),
            exchange=self.default_exchange,
            environment=self.config.environment,
            market_type=self.default_market_type,
            account_scope=self.config.account_scope,
            tail_visibility_timeout_seconds=self.config.tail_visibility_timeout_seconds,
            max_active_pairs=self.config.max_active_pairs,
            simulate=simulate,
            instrument_rules_by_route=self._instrument_rules_by_route,
        )
        if dry_run:
            return plan_strategy_once(strategy=strategy, symbol=self.config.symbol)
        return self._run_runtime_with_cleanup(runtime, simulate=simulate)

    def _run_runtime_with_cleanup(
        self,
        runtime: StrategyRuntime,
        *,
        simulate: bool,
    ) -> StrategyRunResult:
        """Run the async runtime and unwind live exposure on operator/runtime aborts."""

        try:
            return asyncio.run(runtime.run())
        except KeyboardInterrupt:
            self._cleanup_runtime_after_abort(runtime, simulate=simulate, reason="interrupt")
            raise
        except Exception:
            self._cleanup_runtime_after_abort(
                runtime,
                simulate=simulate,
                reason="runtime_error",
            )
            raise

    def _cleanup_runtime_after_abort(
        self,
        runtime: StrategyRuntime,
        *,
        simulate: bool,
        reason: str,
    ) -> None:
        """Best-effort platform cleanup without hiding the original abort."""

        if simulate:
            return
        try:
            cleanup = self.cleanup_interrupted_pairs(runtime)
        except Exception as exc:
            self.logger.warning(
                "%s cleanup failed error=%s",
                reason,
                _compact_admin_error(exc),
            )
            return
        self.logger.info(
            "%s cleanup pairs=%s order_cancelled=%s close_orders=%s qty_before=%s qty_after=%s errors=%s survivors=%s",
            reason,
            cleanup["pairs"],
            cleanup.get("order_cancelled", cleanup["tail_cancelled"]),
            cleanup["close_orders"],
            cleanup["position_before_qty"],
            cleanup["position_after_qty"],
            cleanup["errors"],
            ",".join(cleanup.get("survivors", ())) or "-",
        )

    def run_orders(self, pairs: Iterable[OrderPairSpec], *, dry_run: bool = False, simulate: bool = False) -> StrategyRunResult:
        """Compatibilite: accepte directement une liste de paires canoniques."""
        return self.run_strategy(
            StrategySpec(name="compat", pairs=tuple(pairs)),
            dry_run=dry_run,
            simulate=simulate,
        )

    def _validate_pairs(
        self,
        pairs: Iterable[OrderPairSpec],
        *,
        validate_usd_notional: bool = True,
    ) -> None:
        """Validate exchange-specific instrument and grammar constraints."""
        self._instrument_rules_by_route = {}
        adapters: dict[tuple[str, str], InstrumentRulesExchange] = {}
        rules_by_route: dict[ExchangeRoute, dict[str, object]] = {}
        for pair in pairs:
            route = _pair_route(
                pair,
                default_exchange=self.default_exchange,
                default_symbol=self.config.symbol,
                default_market_type=self.default_market_type,
            )
            if route.exchange not in {"kraken", "binance", "bitmex"}:
                continue
            if route.exchange == "binance":
                _validate_binance_pair_grammar(pair, route.market_type)
            elif route.exchange == "kraken":
                _validate_kraken_pair_grammar(pair, route.market_type)
            elif route.exchange == "bitmex":
                _validate_bitmex_pair_grammar(pair, route.market_type)
            adapter_key = (route.exchange, route.market_type, route.symbol)
            adapter = adapters.get(adapter_key)
            if adapter is None:
                try:
                    exchange_config = self._exchange_config_for_route(route)
                except ValueError as exc:
                    raise ValueError(
                        f"Strategy pair '{pair.name}' cannot load exchange config "
                        f"for route {route.label}: {exc}"
                    ) from exc
                adapter_cls = _adapter_class(route.exchange, route.market_type)
                adapter = cast(
                    InstrumentRulesExchange,
                    adapter_cls(
                        api_key=exchange_config.api_key,
                        api_secret=exchange_config.api_secret,
                        base_url=exchange_config.base_url,
                        symbol=exchange_config.symbol,
                        **exchange_config.adapter_kwargs,
                    ),
                )
                adapters[adapter_key] = adapter
            if route not in rules_by_route:
                rules = _validate_route_symbol(adapter, route)
                rules_by_route[route] = rules
                self._instrument_rules_by_route[route] = rules
            rules = rules_by_route[route]
            if pair_uses_usd_quantity(pair) and validate_usd_notional:
                self._validate_startup_usd_quantity(pair, route, rules)
            elif pair_uses_percent_balance_quantity(pair) and validate_usd_notional:
                self._validate_startup_percent_quantity(pair, route, rules)
            elif (
                pair.head_quantity_type == "qA"
                and pair.head_quantity is not None
            ):
                self._validate_startup_absolute_quantity(pair, route, rules)

    def _validate_startup_absolute_quantity(
        self,
        pair: OrderPairSpec,
        route: ExchangeRoute,
        rules: dict[str, object],
    ) -> None:
        try:
            validation = validate_pair_absolute_quantity(pair, rules=rules)
        except QuantityResolutionError as exc:
            raise ValueError(
                f"Strategy '{pair.name}' quantity {pair.head_quantity} is not "
                f"valid for {route.label}: {_compact_admin_error(exc)}"
            ) from exc
        self.logger.info(
            "QTY_ABS_READY (%s): %s",
            pair.name,
            _runtime_admin_fields(
                route.label,
                f"qty={validation.quantity}",
                f"step={validation.quantity_step}",
                f"min={validation.min_quantity}",
            ),
        )

    def _validate_startup_usd_quantity(
        self,
        pair: OrderPairSpec,
        route: ExchangeRoute,
        rules: dict[str, object],
    ) -> None:
        if self.runtime_state is None:
            raise ValueError(
                f"Strategy '{pair.name}' uses qty U but no runtime market state is "
                f"available for startup validation on {route.label}."
            )
        market = _fetch_service_market_state(self.runtime_state, route)
        try:
            resolution = resolve_pair_usd_quantity(
                pair,
                mark_price=mark_price_for_usd_quantity(pair, market),
                rules=rules,
            )
        except QuantityResolutionError as exc:
            raise ValueError(
                f"Strategy '{pair.name}' qty U{pair.head_quantity} is not "
                f"placeable at startup for {route.label}: {_compact_admin_error(exc)}"
            ) from exc
        self.logger.info(
            "QTY_USD_READY (%s): %s",
            pair.name,
            _runtime_admin_fields(
                route.label,
                f"U{resolution.nominal_usd}",
                f"mark={resolution.mark_price}",
                f"contract={resolution.contract_size}",
                f"step={resolution.quantity_step}",
                f"min={resolution.min_quantity}",
                f"qty={resolution.quantity}",
                f"usd={resolution.approx_usd}",
            ),
        )

    def _validate_startup_percent_quantity(
        self,
        pair: OrderPairSpec,
        route: ExchangeRoute,
        rules: dict[str, object],
    ) -> None:
        if self.runtime_state is None:
            raise ValueError(
                f"Strategy '{pair.name}' uses qty % but no runtime account state is "
                f"available for startup validation on {route.label}."
            )
        market = _fetch_service_market_state(self.runtime_state, route)
        balance = _fetch_service_account_balance(self.runtime_state, route)
        try:
            resolution = resolve_pair_percent_balance_quantity(
                pair,
                available_usd=available_usd_for_percent_quantity(pair, balance),
                mark_price=mark_price_for_usd_quantity(
                    pair,
                    market,
                    error_code="QTY_PCT_TOO_SMALL",
                ),
                rules=rules,
            )
        except QuantityResolutionError as exc:
            raise ValueError(
                f"Strategy '{pair.name}' qty %{pair.head_quantity} is not "
                f"placeable at startup for {route.label}: {_compact_admin_error(exc)}"
            ) from exc
        self.logger.info(
            "QTY_PCT_READY (%s): %s",
            pair.name,
            _runtime_admin_fields(
                route.label,
                f"%{resolution.percent}",
                f"available={resolution.available_usd}",
                f"nominal_usd={resolution.nominal_usd}",
                f"mark={resolution.mark_price}",
                f"contract={resolution.contract_size}",
                f"step={resolution.quantity_step}",
                f"min={resolution.min_quantity}",
                f"qty={resolution.quantity}",
                f"usd={resolution.approx_usd}",
            ),
        )

    def _materialize_strategy_symbols(self, strategy: StrategySpec) -> StrategySpec:
        """Attach the CLI default symbol to strategy rows that did not specify one."""
        names: set[str] = set()
        duplicates: list[str] = []
        pairs: list[OrderPairSpec] = []
        for pair in strategy.pairs:
            if pair.name in names:
                duplicates.append(pair.name)
            names.add(pair.name)
            route = _pair_route(
                pair,
                default_exchange=self.default_exchange,
                default_symbol=self.config.symbol,
                default_market_type=self.default_market_type,
            )
            pairs.append(
                replace(
                    pair,
                    symbol=route.symbol,
                    exchange=route.exchange,
                    market_type=route.market_type,
                )
            )
        if duplicates:
            raise ValueError(
                "Duplicate pair name(s) in strategy: " + ", ".join(sorted(set(duplicates)))
            )
        return replace(strategy, pairs=tuple(pairs))

    def _validate_multi_route_market_db(self, strategy: StrategySpec) -> None:
        routes = _strategy_routes(
            strategy,
            default_exchange=self.default_exchange,
            default_symbol=self.config.symbol,
            default_market_type=self.default_market_type,
        )
        if len(routes) <= 1:
            return
        if self.config.market_db_url or os.environ.get("KOLABI_MARKET_DB_URL"):
            return
        raise ValueError(
            "Multi-instrument or multi-exchange strategies require a shared market DB URL. "
            "Start public feeders for every symbol and pass --market-db-url, "
            "or export KOLABI_MARKET_DB_URL. routes="
            + ",".join(route.label for route in routes)
        )

    def _validate_multi_symbol_market_db(self, strategy: StrategySpec) -> None:
        self._validate_multi_route_market_db(strategy)

    def _build_executor(self, *, simulate: bool):
        if simulate:
            return SimulatedExecutor()
        port = self._build_routing_port(verify_tail_on_place=True)
        return OgunExecutor(
            port,
            flight_policy=RestFlightPolicy(
                min_interval_seconds=self.config.rest_min_interval_seconds,
                max_inflight=self.config.rest_max_inflight,
            ),
        )

    def _build_routing_port(self, *, verify_tail_on_place: bool) -> SymbolRoutingExchangePort:
        return SymbolRoutingExchangePort(
            exchange=self.default_exchange,
            market_type=self.default_market_type,
            exchange_config_loader=self._exchange_config_for_route,
            verify_timeout_seconds=self.config.tail_verify_timeout_seconds,
            verify_poll_seconds=self.config.tail_verify_poll_seconds,
            run_blocking_calls_in_thread=True,
            verify_tail_on_place=verify_tail_on_place,
        )

    def _build_public_source(self, *, simulate: bool):
        if simulate:
            return StaticHookSource()
        if self.runtime_state is not None:
            return KrakenPublicTriggerSource(
                cast(PublicRuntimeStateReader, self.runtime_state)
            )
        return StaticHookSource()

    def _build_private_source(self, *, simulate: bool):
        if simulate or self.runtime_state is None:
            return None
        return KrakenPrivateOrderPollingSource(self.runtime_state)

    def _ensure_exchange_config(self) -> None:
        if self.exchange_config is not None:
            return
        self.exchange_config = load_exchange_config(
            self.default_exchange,
            symbol=self.config.symbol,
            market_type=self.default_market_type,
            environment=self.config.environment,
            api_key_env=self.config.api_key_env,
            api_secret_env=self.config.api_secret_env,
            base_url=self.config.base_url,
        )
        self._decorate_exchange_config(
            self.exchange_config,
            self.default_exchange,
            self.default_market_type,
        )
        self._exchange_config_cache[
            (self.default_exchange, self.default_market_type, self.config.symbol)
        ] = (
            self.exchange_config
        )

    def _exchange_config_for_route(self, route: ExchangeRoute) -> ExchangeConfig:
        key = (route.exchange, route.market_type, route.symbol)
        cached = self._exchange_config_cache.get(key)
        if cached is not None:
            return cached
        try:
            cfg = load_exchange_config(
                route.exchange,
                symbol=route.symbol,
                market_type=route.market_type,
                environment=self.config.environment,
                api_key_env=(
                    self.config.api_key_env
                    if route.exchange == self.default_exchange
                    and route.market_type == self.default_market_type
                    else None
                ),
                api_secret_env=(
                    self.config.api_secret_env
                    if route.exchange == self.default_exchange
                    and route.market_type == self.default_market_type
                    else None
                ),
                base_url=(
                    self.config.base_url
                    if route.exchange == self.default_exchange
                    and route.market_type == self.default_market_type
                    else None
                ),
            )
        except ValueError as exc:
            raise ValueError(
                f"Exchange route {route.label} configuration failed: {exc}"
            ) from exc
        self._decorate_exchange_config(cfg, route.exchange, route.market_type)
        self._exchange_config_cache[key] = cfg
        if route.exchange == self.default_exchange and route.symbol == self.config.symbol:
            self.exchange_config = cfg
        return cfg

    def _decorate_exchange_config(
        self,
        cfg: ExchangeConfig,
        exchange: str,
        market_type: str = DEFAULT_MARKET_TYPE,
    ) -> None:
        if exchange not in {"kraken", "binance", "bitmex"}:
            return
        if self._market_db_url is not None:
            cfg.adapter_kwargs["public_db_url"] = self._market_db_url
        if self._account_db_url is not None:
            cfg.adapter_kwargs["account_db_url"] = self._account_db_url
        if self._audit_db_url is not None:
            cfg.adapter_kwargs["audit_db_url"] = self._audit_db_url
        cfg.adapter_kwargs["rest_audit_retention_minutes"] = (
            self.config.rest_audit_retention_minutes
        )
        cfg.adapter_kwargs["rest_audit_retention_limit"] = (
            self.config.rest_audit_retention_limit
        )
        cfg.adapter_kwargs["account_scope"] = self.config.account_scope
        cfg.adapter_kwargs["market_type"] = market_type

    def _build_admin_port(self) -> AdapterExchangePort:
        self._ensure_exchange_config()
        if self.exchange_config is None:
            raise RuntimeError("Exchange configuration is required for admin execution")
        return AdapterExchangePort(
            exchange=self.default_exchange,
            market_type=self.default_market_type,
            exchange_config=self.exchange_config,
            verify_timeout_seconds=self.config.tail_verify_timeout_seconds,
            verify_poll_seconds=self.config.tail_verify_poll_seconds,
            run_blocking_calls_in_thread=True,
        )

    def cancel_all_orders(self) -> list[OrderAck]:
        """Cancel all currently visible open/trigger orders via bot execution path."""
        port = self._build_admin_port()
        cancelled, _cancel_errors = self._cancel_all_orders_with_port(port)
        return cancelled

    def _cancel_all_orders_with_port(
        self,
        port: AdapterExchangePort,
    ) -> tuple[list[OrderAck], list[dict[str, str]]]:
        executor = OgunExecutor(port)
        cancelled: list[OrderAck] = []
        cancel_errors: list[dict[str, str]] = []
        seen: set[str] = set()
        for order in _safe_cancel_order_candidates(cast(OpenOrderReader, port.adapter)):
            identity = _extract_cancelable_order_id(order)
            if identity is None:
                continue
            key = str(identity)
            if key in seen:
                continue
            seen.add(key)
            command = CancelCommand(
                kind=RuntimeCommandKind.CANCEL,
                symbol=Symbol(self.config.symbol),
                pair_name="__operator__",
                request=CancelOrderCommandRequest(
                    pair_name="__operator__",
                    clOrdID=key,
                ),
            )
            try:
                cancelled.append(asyncio.run(executor.execute(command)))
            except Exception as exc:
                cancel_errors.append({"order_id": key, "error": _compact_admin_error(exc)})
                continue
        return cancelled, cancel_errors

    def close_all_orders(self) -> dict[str, object]:
        """Cancel all orders then close residual position through the bot boundary."""
        port = self._build_admin_port()
        cancelled, cancel_errors = self._cancel_all_orders_with_port(port)
        position_before = port.adapter.get_position()
        qty_before = float(position_before.qty)
        close_ack: OrderAck | None = None
        close_action = "skipped_no_position"
        close_skipped_reason: str | None = "no_position"
        if qty_before != 0.0:
            close_side = "sell" if qty_before > 0 else "buy"
            close_params = _market_close_order_params(self.default_market_type)
            close_action = close_params["action"]
            close_skipped_reason = None
            close_ack = port.adapter.place_order(
                side=close_side,
                orderQty=abs(qty_before),
                type_="MARKET",
                **close_params["params"],
            )
        position_after = port.adapter.get_position()
        audit_errors = list(getattr(port.adapter, "rest_audit_errors", ()))
        return {
            "cancelled": cancelled,
            "cancel_errors": cancel_errors,
            "close_ack": close_ack,
            "close_action": close_action,
            "close_skipped_reason": close_skipped_reason,
            "position_before": position_before,
            "position_after": position_after,
            "closed": float(position_after.qty) == 0.0,
            "audit_persistence_ok": not audit_errors,
            "audit_persistence_errors": audit_errors,
        }

    def cancel_living_tails(self, runtime: StrategyRuntime) -> list[OrderAck]:
        """Best-effort cancellation of living/submitted tails on operator interrupt."""
        cancelled: list[OrderAck] = []
        adapters: dict[ExchangeRoute, ExchangeAdapterLike] = {}
        for target in _interrupt_cleanup_targets(runtime):
            adapter = self._adapter_for_route(target.route, adapters)
            cancel_id = _resolve_tail_cancel_id(
                cast(OpenOrderReader, adapter),
                target.tail_exchange_order_id,
                target.tail_client_order_id,
            )
            if cancel_id is None:
                continue
            try:
                cancelled.append(adapter.cancel_order(cancel_id))
            except Exception:
                continue
        return cancelled

    def cleanup_interrupted_pairs(self, runtime: StrategyRuntime) -> dict[str, object]:
        """Cancel active live orders and close associated head-opened exposure."""
        targets = _interrupt_cleanup_targets(runtime)
        cancelled: list[OrderAck] = []
        close_acks: list[OrderAck] = []
        errors = 0
        seen_cancel_ids: set[tuple[str, str]] = set()
        adapters: dict[ExchangeRoute, ExchangeAdapterLike] = {}
        active_identities = runtime.active_order_identities()
        for identity in active_identities:
            route = self._route_from_identity(identity)
            adapter = self._adapter_for_route(route, adapters)
            cancel_id = identity.exchange_order_id or identity.client_order_id
            cancel_key = (route.label, cancel_id or "")
            if not cancel_id or cancel_key in seen_cancel_ids:
                continue
            seen_cancel_ids.add(cancel_key)
            try:
                cancelled.append(adapter.cancel_order(cancel_id))
            except Exception:
                errors += 1
        position_before_by_route: dict[ExchangeRoute, float] = {}
        position_after_by_route: dict[ExchangeRoute, float] = {}
        for route in sorted({target.route for target in targets}):
            adapter = self._adapter_for_route(route, adapters)
            position = adapter.get_position()
            position_before_by_route[route] = float(position.qty)
        targets_by_route: dict[ExchangeRoute, list[_InterruptCleanupTarget]] = {}
        for target in targets:
            targets_by_route.setdefault(target.route, []).append(target)
        for route, route_targets in targets_by_route.items():
            adapter = self._adapter_for_route(route, adapters)
            position_before_qty = position_before_by_route.get(route)
            if position_before_qty is None:
                position_before_qty = float(adapter.get_position().qty)
                position_before_by_route[route] = position_before_qty
            remaining_long = max(0.0, position_before_qty)
            remaining_short = max(0.0, -position_before_qty)
            for target in route_targets:
                cancel_id = _resolve_tail_cancel_id(
                    cast(OpenOrderReader, adapter),
                    target.tail_exchange_order_id,
                    target.tail_client_order_id,
                )
                cancel_key = (route.label, cancel_id or "")
                if cancel_id is not None and cancel_key not in seen_cancel_ids:
                    seen_cancel_ids.add(cancel_key)
                    try:
                        cancelled.append(adapter.cancel_order(cancel_id))
                    except Exception:
                        errors += 1
                close_qty = target.played_quantity
                if target.close_side == "sell":
                    close_qty = min(close_qty, remaining_long)
                    remaining_long = max(0.0, remaining_long - close_qty)
                else:
                    close_qty = min(close_qty, remaining_short)
                    remaining_short = max(0.0, remaining_short - close_qty)
                if close_qty <= 0.0:
                    continue
                try:
                    close_params = _market_close_order_params(route.market_type)
                    close_acks.append(
                        adapter.place_order(
                            side=target.close_side,
                            orderQty=close_qty,
                            type_="MARKET",
                            **close_params["params"],
                        )
                    )
                except Exception:
                    errors += 1
            position_after_by_route[route] = float(adapter.get_position().qty)
        for route, adapter in adapters.items():
            if route not in position_after_by_route and route in position_before_by_route:
                position_after_by_route[route] = float(adapter.get_position().qty)
        position_before_qty = sum(position_before_by_route.values())
        position_after_qty = sum(position_after_by_route.values())
        survivors: tuple[PrivateOrderRecord, ...] = ()
        if active_identities and self.runtime_state is not None:
            survivor_targets = tuple(
                PrivateOrderRecord(
                    symbol=identity.symbol or self.config.symbol,
                    status="open",
                    exchange_order_id=identity.exchange_order_id,
                    client_order_id=identity.client_order_id,
                    exchange=identity.exchange or self.default_exchange,
                    market_type=identity.market_type or self.default_market_type,
                )
                for identity in active_identities
            )
            survivors = self._wait_for_private_orders_closed(
                tuple(
                    sorted(
                        {
                            ExchangeRoute(
                                record.exchange or self.default_exchange,
                                record.market_type or self.default_market_type,
                                record.symbol,
                            )
                            for record in survivor_targets
                        }
                    )
                ),
                survivor_targets,
                timeout_seconds=self.config.tail_verify_timeout_seconds,
                poll_seconds=self.config.tail_verify_poll_seconds,
            )
            for survivor in survivors:
                self.logger.warning(
                    "ORDER_SAFETY_BLOCKED (%s): %s",
                    _record_route_label(survivor),
                    _runtime_admin_fields(
                        survivor.client_order_id or "-",
                        survivor.exchange_order_id or "-",
                        "shutdown_survivor",
                    ),
                )
        return {
            "pairs": len(targets),
            "tail_cancelled": len(cancelled),
            "order_cancelled": len(cancelled),
            "close_orders": len(close_acks),
            "position_before_qty": position_before_qty,
            "position_after_qty": position_after_qty,
            "errors": errors,
            "survivors": tuple(_private_order_summary(record) for record in survivors),
        }

    def _route_from_identity(self, identity: OrderIdentity) -> ExchangeRoute:
        return ExchangeRoute(
            exchange=identity.exchange or self.default_exchange,
            market_type=identity.market_type or self.default_market_type,
            symbol=identity.symbol or self.config.symbol,
        )

    def _adapter_for_route(
        self,
        route: ExchangeRoute,
        adapters: dict[ExchangeRoute, ExchangeAdapterLike],
    ) -> ExchangeAdapterLike:
        existing = adapters.get(route)
        if existing is not None:
            return existing
        if (
            route.exchange == self.default_exchange
            and route.symbol == self.config.symbol
            and route.market_type == self.default_market_type
        ):
            adapter = self._build_admin_port().adapter
        else:
            adapter = AdapterExchangePort(
                exchange=route.exchange,
                market_type=route.market_type,
                exchange_config=self._exchange_config_for_route(route),
                verify_timeout_seconds=self.config.tail_verify_timeout_seconds,
                verify_poll_seconds=self.config.tail_verify_poll_seconds,
                run_blocking_calls_in_thread=False,
            ).adapter
        adapters[route] = adapter
        return adapter


async def _run_private_stack(
    *_args: Any,
) -> None:
    raise RuntimeError("_run_private_stack is retired from the active bot path")


class AdapterExchangePort(ExchangePort):
    """ExchangePort adapter backed by shared exchange adapters."""

    def __init__(
        self,
        *,
        exchange: str,
        market_type: str = DEFAULT_MARKET_TYPE,
        exchange_config: ExchangeConfig,
        verify_timeout_seconds: float = 11.0,
        verify_poll_seconds: float = 0.5,
        run_blocking_calls_in_thread: bool = False,
        verify_tail_on_place: bool = True,
    ) -> None:
        adapter_cls = _adapter_class(exchange, market_type)
        adapter_kwargs = dict(exchange_config.adapter_kwargs)
        adapter_kwargs["market_type"] = market_type
        self.adapter = cast(
            ExchangeAdapterLike,
            adapter_cls(
                api_key=exchange_config.api_key,
                api_secret=exchange_config.api_secret,
                base_url=exchange_config.base_url,
                symbol=exchange_config.symbol,
                **adapter_kwargs,
            ),
        )
        self.verify_timeout_seconds = verify_timeout_seconds
        self.verify_poll_seconds = verify_poll_seconds
        self.run_blocking_calls_in_thread = run_blocking_calls_in_thread
        self.verify_tail_on_place = verify_tail_on_place

    async def place_head(self, command: PlaceHeadCommand) -> OrderAck:
        ack = await self._call_blocking(self._place, command.request)
        return await self._verify_head_open_order(command.request, ack)

    async def place_tail(self, command: PlaceTailCommand) -> OrderAck:
        ack = await self._call_blocking(self._place, command.request)
        if self.verify_tail_on_place:
            await self._verify_tail_trigger(command.request, ack)
        return ack

    async def amend_head(self, command: AmendHeadCommand) -> OrderAck:
        return await self._call_blocking(self._amend_head, command.request)

    async def amend_tail(self, command: AmendTailCommand) -> OrderAck:
        return await self._call_blocking(self._amend_tail, command.request)

    async def cancel(self, command: CancelCommand) -> OrderAck:
        return await self._call_blocking(self.adapter.cancel_order, command.request.clOrdID)

    async def _call_blocking(self, func: Callable[..., _T], *args: object) -> _T:
        if self.run_blocking_calls_in_thread:
            return await asyncio.to_thread(func, *args)
        return func(*args)

    def _place(self, request: PlaceOrderCommandRequest) -> OrderAck:
        if request.orderQty is None:
            raise ValueError(f"Missing orderQty for place request on pair '{request.pair_name}'")
        params: dict[str, Any] = {}
        if request.stopPx is not None:
            params["stopPx"] = request.stopPx
        price = request.price
        if (
            price is None
            and request.stopPx is not None
            and request.oDelta is not None
            and _order_type_uses_limit_offset(request.ordType)
        ):
            price = _limit_price_from_stop_offset(
                side=request.side,
                stop_px=request.stopPx,
                offset=request.oDelta,
            )
        _validate_order_prices(
            pair_name=request.pair_name,
            order_type=request.ordType,
            price=price,
            stop_px=request.stopPx,
        )
        if price is not None:
            params["price"] = price
        if request.clOrdID is not None:
            params["clOrdID"] = request.clOrdID
        if request.execInst is not None:
            params["execInst"] = request.execInst
        if request.text is not None:
            params["text"] = request.text
        if request.oDelta is not None:
            params["oDelta"] = request.oDelta
        return self.adapter.place_order(
            request.side,
            request.orderQty,
            type_=request.ordType,
            **params,
        )

    async def _verify_tail_trigger(
        self,
        request: PlaceOrderCommandRequest,
        ack: OrderAck,
    ) -> None:
        if request.stopPx is None or request.orderQty is None:
            return
        if not hasattr(self.adapter, "live_trigger_orders"):
            return
        reader = cast(TriggerOrderReader, self.adapter)
        deadline = asyncio.get_running_loop().time() + self.verify_timeout_seconds
        last_live_orders: list[dict[str, Any]] = []
        last_db_orders: list[dict[str, Any]] = []
        contradictory_ack = not _ack_can_rest_as_trigger(ack)
        while True:
            live_orders = await self._call_blocking(reader.live_trigger_orders)
            db_orders = await self._call_blocking(_trigger_orders_from_private_db, reader)
            last_live_orders = live_orders
            last_db_orders = db_orders
            match, source = _match_trigger_evidence(
                live_orders,
                db_orders,
                request,
                ack,
            )
            if match is not None and source is not None:
                if contradictory_ack:
                    _LOGGER.warning(
                        "tail trigger verified by %s despite contradictory ack: "
                        "pair=%s clOrdID=%s orderID=%s ack_status=%s "
                        "live_order_id=%s live_client_id=%s live_status=%s",
                        source,
                        request.pair_name,
                        request.clOrdID or "-",
                        ack.order_id,
                        ack.status,
                        match.get("order_id"),
                        match.get("client_order_id"),
                        match.get("status"),
                    )
                return
            if asyncio.get_running_loop().time() >= deadline:
                err_kind = (
                    "tail trigger order rejected by exchange"
                    if contradictory_ack
                    else "tail trigger order not visible after placement"
                )
                raise RuntimeError(
                    f"{err_kind}: "
                    f"pair={request.pair_name} clOrdID={request.clOrdID or '-'} "
                    f"orderID={ack.order_id} status={ack.status} "
                    f"stopPx={request.stopPx} qty={request.orderQty} "
                    f"live_seen={len(last_live_orders)} db_seen={len(last_db_orders)}"
                )
            await asyncio.sleep(self.verify_poll_seconds)

    async def _verify_head_open_order(
        self,
        request: PlaceOrderCommandRequest,
        ack: OrderAck,
    ) -> OrderAck:
        if request.stopPx is not None or request.price is None or request.orderQty is None:
            return ack
        if not hasattr(self.adapter, "live_open_orders"):
            return ack
        reader = cast(OpenOrderReader, self.adapter)
        try:
            live_orders = await self._call_blocking(reader.live_open_orders)
        except Exception as exc:
            _LOGGER.warning(
                "HEAD_OPEN_ENRICH_SKIPPED (%s): clOrdID=%s orderID=%s error=%s",
                request.pair_name,
                request.clOrdID or "-",
                ack.order_id,
                _compact_admin_error(exc),
            )
            return ack
        match = _matching_head_open_order(live_orders, request, ack)
        if match is None:
            return ack
        order_id = _order_value(match, "order_id", "orderID", "orderId", "id")
        client_id = _order_value(match, "client_order_id", "clOrdID", "cliOrdId", "cli_ord_id")
        return replace(
            ack,
            order_id=order_id or ack.order_id,
            client_order_id=client_id or ack.client_order_id or request.clOrdID,
            status=str(match.get("status") or ack.status),
            price=_decimal_or_none(match.get("price")) or ack.price,
            orig_qty=_decimal_or_none(match.get("qty")) or ack.orig_qty,
        )

    def _amend_head(self, request: AmendOrderCommandRequest) -> OrderAck:
        params: dict[str, Any] = {}
        if request.newPrice is not None:
            params["price"] = request.newPrice
        return self._amend_with_params(request, params)

    def _amend_tail(self, request: AmendOrderCommandRequest) -> OrderAck:
        params: dict[str, Any] = {}
        if request.newPrice is not None:
            params["stopPx"] = request.newPrice
            if request.oDelta is not None and _order_type_uses_limit_offset(request.ordType):
                params["price"] = _limit_price_from_stop_offset(
                    side=request.side,
                    stop_px=request.newPrice,
                    offset=request.oDelta,
                )
        return self._amend_with_params(request, params)

    def _amend_with_params(
        self,
        request: AmendOrderCommandRequest,
        params: dict[str, Any],
    ) -> OrderAck:
        if request.clOrdID is not None:
            params["clOrdID"] = request.clOrdID
        if request.newQty is not None:
            params["orderQty"] = request.newQty
        if request.text is not None:
            params["text"] = request.text
        return self.adapter.amend_order(request.orderID, **params)


class SymbolRoutingExchangePort(ExchangePort):
    """ExchangePort that dispatches commands by exchange, market type, and symbol."""

    def __init__(
        self,
        *,
        exchange: str,
        market_type: str = DEFAULT_MARKET_TYPE,
        exchange_config: ExchangeConfig | None = None,
        exchange_config_loader: Callable[[ExchangeRoute], ExchangeConfig] | None = None,
        verify_timeout_seconds: float = 11.0,
        verify_poll_seconds: float = 0.5,
        run_blocking_calls_in_thread: bool = False,
        verify_tail_on_place: bool = True,
    ) -> None:
        self.exchange = normalise_exchange_name(exchange)
        self.market_type = (market_type or DEFAULT_MARKET_TYPE).strip().lower()
        if not exchange_supports_market_type(self.exchange, self.market_type):
            reason = unsupported_market_message(self.exchange, self.market_type)
            raise ValueError(
                f"Market type '{self.market_type}' {reason} for exchange '{self.exchange}'"
            )
        self.exchange_config = exchange_config
        self.exchange_config_loader = exchange_config_loader
        self.verify_timeout_seconds = verify_timeout_seconds
        self.verify_poll_seconds = verify_poll_seconds
        self.run_blocking_calls_in_thread = run_blocking_calls_in_thread
        self.verify_tail_on_place = verify_tail_on_place
        self._ports: dict[ExchangeRoute, AdapterExchangePort] = {}

    def _route(self, command: DragonSong) -> ExchangeRoute:
        exchange = normalise_exchange_name(command.exchange or self.exchange)
        market_type = (command.market_type or self.market_type).strip().lower()
        if market_type not in SUPPORTED_MARKET_TYPES:
            raise ValueError(
                f"Unsupported market type '{market_type}' for command {command.pair_name}"
            )
        if not exchange_supports_market_type(exchange, market_type):
            reason = unsupported_market_message(exchange, market_type)
            raise ValueError(
                f"Market type '{market_type}' {reason} for command {command.pair_name}"
            )
        return ExchangeRoute(exchange=exchange, market_type=market_type, symbol=str(command.symbol))

    def _config_for_route(self, route: ExchangeRoute) -> ExchangeConfig:
        if self.exchange_config_loader is not None:
            return self.exchange_config_loader(route)
        if route.exchange != self.exchange or route.market_type != self.market_type:
            raise RuntimeError(
                f"No exchange config loader for route {route.label}; "
                f"default route is {self.exchange}:{self.market_type}"
            )
        if self.exchange_config is None:
            raise RuntimeError("Exchange configuration is required for active execution")
        return replace(
            self.exchange_config,
            symbol=route.symbol,
            adapter_kwargs={
                **self.exchange_config.adapter_kwargs,
                "market_type": route.market_type,
            },
        )

    def _port(self, command: DragonSong) -> AdapterExchangePort:
        route = self._route(command)
        existing = self._ports.get(route)
        if existing is not None:
            return existing
        exchange_config = self._config_for_route(route)
        port = AdapterExchangePort(
            exchange=route.exchange,
            market_type=route.market_type,
            exchange_config=exchange_config,
            verify_timeout_seconds=self.verify_timeout_seconds,
            verify_poll_seconds=self.verify_poll_seconds,
            run_blocking_calls_in_thread=self.run_blocking_calls_in_thread,
            verify_tail_on_place=self.verify_tail_on_place,
        )
        self._ports[route] = port
        return port

    async def place_head(self, command: PlaceHeadCommand) -> OrderAck:
        return await self._port(command).place_head(command)

    async def place_tail(self, command: PlaceTailCommand) -> OrderAck:
        return await self._port(command).place_tail(command)

    async def amend_head(self, command: AmendHeadCommand) -> OrderAck:
        return await self._port(command).amend_head(command)

    async def amend_tail(self, command: AmendTailCommand) -> OrderAck:
        return await self._port(command).amend_tail(command)

    async def cancel(self, command: CancelCommand) -> OrderAck:
        return await self._port(command).cancel(command)


def _matching_tail_trigger_order(
    orders: list[dict[str, Any]],
    request: PlaceOrderCommandRequest,
    ack: OrderAck,
) -> dict[str, Any] | None:
    for order in orders:
        client_id = str(order.get("client_order_id") or "")
        order_id = str(order.get("order_id") or "")
        clordid_match = bool(request.clOrdID) and client_id == request.clOrdID
        orderid_match = bool(ack.order_id) and order_id == str(ack.order_id)
        if not (clordid_match or orderid_match):
            continue
        if request.side and not _matches_text(order.get("side"), request.side):
            continue
        if not _matches_quantity(order.get("qty"), request.orderQty):
            continue
        # clOrdID is the strongest identity anchor; exchange may quantize stop
        # prices to tick size, so strict stop matching can be too brittle.
        if not clordid_match and not _matches_price(order.get("stop_price"), request.stopPx):
            continue
        return order
    return None


def _matching_head_open_order(
    orders: list[dict[str, Any]],
    request: PlaceOrderCommandRequest,
    ack: OrderAck,
) -> dict[str, Any] | None:
    for order in orders:
        client_id = _order_value(order, "client_order_id", "clOrdID", "cliOrdId", "cli_ord_id")
        order_id = _order_value(order, "order_id", "orderID", "orderId", "id")
        clordid_match = bool(request.clOrdID) and client_id == request.clOrdID
        orderid_match = bool(ack.order_id) and order_id == str(ack.order_id)
        if not (clordid_match or orderid_match):
            continue
        if request.side and not _matches_text(order.get("side"), request.side):
            continue
        if not _matches_quantity(order.get("qty"), request.orderQty):
            continue
        if not clordid_match and not _matches_price(order.get("price"), request.price):
            continue
        return order
    return None


def _order_value(order: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = order.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _match_trigger_evidence(
    live_orders: list[dict[str, Any]],
    db_orders: list[dict[str, Any]],
    request: PlaceOrderCommandRequest,
    ack: OrderAck,
) -> tuple[dict[str, Any] | None, str | None]:
    live_match = _matching_tail_trigger_order(live_orders, request, ack)
    if live_match is not None:
        return live_match, "rest_live"
    db_match = _matching_tail_trigger_order(db_orders, request, ack)
    if db_match is not None:
        return db_match, "private_db"
    return None, None


def _trigger_orders_from_private_db(reader: TriggerOrderReader) -> list[dict[str, Any]]:
    if not hasattr(reader, "live_trigger_orders_db"):
        return []
    return reader.live_trigger_orders_db()


def _ack_can_rest_as_trigger(ack: OrderAck) -> bool:
    status = ack.status.replace(" ", "_").replace("-", "_").lower()
    return status in {"new", "open", "placed", "submitted"}


def _matches_text(left: object, right: str) -> bool:
    return str(left or "").lower() == right.lower()


def _matches_quantity(left: object, right: object | None) -> bool:
    if right is None:
        return True
    left_decimal = _decimal_or_none(left)
    right_decimal = _decimal_or_none(right)
    if left_decimal is None or right_decimal is None:
        return False
    return abs(left_decimal - right_decimal) <= Decimal("0.00000001")


def _matches_price(left: object, right: object | None) -> bool:
    if right is None:
        return True
    left_decimal = _decimal_or_none(left)
    right_decimal = _decimal_or_none(right)
    if left_decimal is None or right_decimal is None:
        return False
    tolerance = max(Decimal("0.01"), abs(right_decimal) * Decimal("0.000001"))
    return abs(left_decimal - right_decimal) <= tolerance


def _decimal_or_none(value: object) -> Decimal | None:
    if value in (None, ""):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float, str)):
        return Decimal(str(value))
    return None


def _safe_cancel_order_candidates(adapter: OpenOrderReader) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for source_name in (
        "live_open_orders",
        "live_trigger_orders",
        "open_orders",
        "live_trigger_orders_db",
    ):
        source = getattr(adapter, source_name, None)
        if not callable(source):
            continue
        try:
            rows = source()
        except Exception:
            continue
        if not isinstance(rows, list):
            continue
        candidates.extend([row for row in rows if isinstance(row, dict)])
    return candidates


def _extract_cancelable_order_id(order: dict[str, object]) -> str | None:
    for key in (
        "orderID",
        "orderId",
        "order_id",
        "id",
        "clOrdID",
        "cliOrdId",
        "cli_ord_id",
        "client_order_id",
    ):
        value = order.get(key)
        if value:
            return str(value)
    return None


def _market_close_order_params(market_type: str) -> dict[str, Any]:
    if market_type == DEFAULT_MARKET_TYPE:
        return {
            "action": "submitted_reduce_only_market",
            "params": {"reduceOnly": True},
        }
    return {
        "action": "submitted_market_close",
        "params": {},
    }


def _public_feeder_hint(route: ExchangeRoute) -> str:
    return (
        "scripts/kolabidb public start"
        f" --exchange {route.exchange}"
        f" --market-type {route.market_type}"
        f" --pair {route.symbol}"
    )


def _private_feeder_hint(route: ExchangeRoute, account_scope: str) -> str:
    return (
        "scripts/kolabidb private start"
        f" --exchange {route.exchange}"
        f" --market-type {route.market_type}"
        f" --pair {route.symbol}"
        f" --account-scope {account_scope}"
    )


def _is_kolabi_order_record(record: PrivateOrderRecord) -> bool:
    client_id = record.client_order_id or ""
    return bool(_KOLABI_ORDER_CLIENT_ID_RE.match(client_id))


def _private_order_key(record: PrivateOrderRecord) -> str | None:
    route = _record_route_label(record)
    if record.exchange_order_id:
        return f"{route}:exchange:{record.exchange_order_id}"
    if record.client_order_id:
        return f"{route}:client:{record.client_order_id}"
    return None


def _record_route_label(record: PrivateOrderRecord) -> str:
    return (
        f"{record.exchange or '-'}:"
        f"{record.market_type or '-'}:"
        f"{record.symbol}"
    )


def _private_order_summary(record: PrivateOrderRecord) -> str:
    return (
        f"route={_record_route_label(record)} client_id={record.client_order_id or '-'} "
        f"order_id={record.exchange_order_id or '-'} side={record.side or '-'} "
        f"qty={record.quantity if record.quantity is not None else '-'} "
        f"price={record.stop_price if record.stop_price is not None else record.price or '-'} "
        f"status={record.status}"
    )


def _runtime_admin_fields(*values: object) -> str:
    return " ".join(str(value) for value in values)


def _compact_admin_error(exc: BaseException) -> str:
    return " ".join(str(exc).split())


def _limit_price_from_stop_offset(
    *,
    side: str,
    stop_px: object,
    offset: object,
) -> Decimal:
    stop = Decimal(str(stop_px))
    distance = abs(Decimal(str(offset)))
    if side.lower() == "buy":
        return stop + distance
    return stop - distance


def _order_type_uses_limit_offset(order_type: str) -> bool:
    return parse_order_code(order_type).base_key in {"SL", "LT"}


def _validate_order_prices(
    *,
    pair_name: str,
    order_type: str,
    price: object | None,
    stop_px: object | None,
) -> None:
    base = parse_order_code(order_type).base_key
    if base == "L" and price is None:
        raise ValueError(f"Order pair '{pair_name}' limit order needs a price")
    if base in {"S", "SL", "MT", "LT"} and stop_px is None:
        raise ValueError(f"Order pair '{pair_name}' trigger order needs a stopPx")
    if base in {"SL", "LT"} and price is None:
        raise ValueError(f"Order pair '{pair_name}' trigger-limit order needs a limit price")


def _validate_binance_pair_grammar(
    pair: OrderPairSpec,
    market_type: str,
) -> None:
    """Fail unsupported Binance grammar at preflight, before REST submission."""

    for role, raw in (("head", pair.head.order_type), ("tail", pair.tail.order_type)):
        code = parse_order_code(raw)
        supported = {"M", "L", "S"} if market_type == DEFAULT_MARKET_TYPE else {"M", "L", "S", "SL"}
        if code.base_key not in supported:
            raise ValueError(
                f"Binance {market_type} does not support {code.base} {role} orders in v1; "
                f"use {', '.join(sorted(supported))}."
            )
        if market_type == DEFAULT_MARKET_TYPE and code.price_suffix == "i":
            raise ValueError(
                f"Binance Futures does not support index-price triggers for {role} "
                f"order type '{raw}'. Use last/contract or mark price."
            )
        if market_type != DEFAULT_MARKET_TYPE and code.price_suffix in {"i", "m"}:
            raise ValueError(
                f"Binance {market_type} does not support mark/index-price triggers "
                f"for {role} order type '{raw}'. Use last price."
            )
        if market_type != DEFAULT_MARKET_TYPE and code.reduce_only:
            raise ValueError(
                f"Binance {market_type} does not support reduce-only {role} order "
                f"type '{raw}'."
            )
        if code.post_only and code.base_key != "L":
            raise ValueError(
                f"Binance {market_type} post-only is only supported on limit orders; got {raw}."
            )


def _validate_kraken_pair_grammar(
    pair: OrderPairSpec,
    market_type: str,
) -> None:
    """Fail unsupported Kraken spot/margin grammar before REST submission."""

    if market_type == DEFAULT_MARKET_TYPE:
        return
    supported = {"M", "L", "S", "SL"}
    for role, raw in (("head", pair.head.order_type), ("tail", pair.tail.order_type)):
        code = parse_order_code(raw)
        if code.base_key not in supported:
            raise ValueError(
                f"Kraken {market_type} does not support {code.base} {role} orders in v1; "
                f"use {', '.join(sorted(supported))}."
            )
        if code.price_suffix in {"i", "m"}:
            raise ValueError(
                f"Kraken {market_type} does not support mark/index-price triggers "
                f"for {role} order type '{raw}'. Use last price."
            )
        if code.reduce_only:
            raise ValueError(
                f"Kraken {market_type} does not support reduce-only {role} order "
                f"type '{raw}'."
            )


def _validate_bitmex_pair_grammar(
    pair: OrderPairSpec,
    market_type: str,
) -> None:
    """Fail unsupported BitMEX spot grammar before REST submission."""

    if market_type != "spot":
        return
    supported = {"M", "L"}
    for role, raw in (("head", pair.head.order_type), ("tail", pair.tail.order_type)):
        code = parse_order_code(raw)
        if code.price_suffix in {"i", "m"}:
            raise ValueError(
                f"BitMEX spot does not support mark/index-price triggers "
                f"for {role} order type '{raw}'. Use last price."
            )
        if code.reduce_only:
            raise ValueError(
                f"BitMEX spot does not support reduce-only {role} order "
                f"type '{raw}'."
            )
        if code.base_key not in supported:
            raise ValueError(
                f"BitMEX spot does not support {code.base} {role} orders in v1; "
                f"use {', '.join(sorted(supported))}."
            )


@dataclass(frozen=True)
class _InterruptCleanupTarget:
    pair_name: str
    route: ExchangeRoute
    close_side: str
    played_quantity: float
    tail_client_order_id: str | None
    tail_exchange_order_id: str | None


def _interrupt_cleanup_targets(runtime: StrategyRuntime) -> tuple[_InterruptCleanupTarget, ...]:
    targets: list[_InterruptCleanupTarget] = []
    for pair_name, pair_state in runtime.state.pairs.items():
        if pair_state.head_state.value != "closed":
            continue
        if pair_state.tail_state is None:
            continue
        if pair_state.tail_state.value not in {"hooked", "submitted", "living"}:
            continue
        played_quantity = float(pair_state.played_quantity or 0.0)
        if played_quantity <= 0.0:
            continue
        close_side = "sell" if pair_state.pair.head.side.value == "buy" else "buy"
        identity = pair_state.tail_identity
        targets.append(
            _InterruptCleanupTarget(
                pair_name=pair_name,
                route=resolve_pair_route(
                    pair_state.pair,
                    default_exchange=runtime.exchange,
                    default_symbol=runtime.symbol,
                    default_market_type=runtime.market_type,
                ),
                close_side=close_side,
                played_quantity=played_quantity,
                tail_client_order_id=None if identity is None else identity.client_order_id,
                tail_exchange_order_id=None if identity is None else identity.exchange_order_id,
            )
        )
    return tuple(targets)


def _resolve_tail_cancel_id(
    adapter: OpenOrderReader,
    tail_exchange_order_id: str | None,
    tail_client_order_id: str | None,
) -> str | None:
    if tail_exchange_order_id:
        return tail_exchange_order_id
    if not tail_client_order_id:
        return None
    for order in _safe_cancel_order_candidates(adapter):
        client_id = str(
            order.get("client_order_id")
            or order.get("cli_ord_id")
            or order.get("clOrdID")
            or order.get("cliOrdId")
            or ""
        )
        if client_id != tail_client_order_id:
            continue
        order_id = _extract_cancelable_order_id(order)
        if order_id:
            return order_id
    return tail_client_order_id
