"""Kraken Futures exchange adapter backed by REST and optional private DB state."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, Iterable, Sequence, TypeAlias, cast
from urllib.parse import urlencode
from uuid import uuid4

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from kolabi.kraken_contract import build_send_order_contract
from kolabi.shared.core.models import OrderAck, Position
from kolabi.shared.core.runtime_types import OrderQty, Price, StopPrice
from kolabi.shared.core.types import ExchangeABC
from kolabi.shared.kraken_futures import (
    kraken_futures_audit_db_url,
    kraken_futures_environment,
)
from kolabi.shared.persistence import (
    Base,
    ExchangeFill,
    ExchangeInstrument,
    ExchangeOrder,
    ExchangeRestCall,
    create_persistence_engine,
    prune_exchange_rest_calls,
)
from kolabi.shared.pruning import DEFAULT_PRUNING
from kolabi.tree.account import sign_rest_auth

_LOGGER = logging.getLogger("kola")

JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


@dataclass(frozen=True)
class _Ticker:
    bid: float
    ask: float
    mark_price: float
    index_price: float
    last: float


@dataclass(frozen=True)
class _KrakenSpotOrderRequest:
    side: str
    quantity: float
    kolabi_type: str
    kraken_type: str
    price: float | None
    stop_price: float | None
    client_order_id: str | None
    leverage: str | None


def _default_audit_db_url(
    *,
    environment: str,
    account_scope: str,
    account_db_url: str | None,
) -> str:
    """Return the default PostgreSQL audit lane."""

    del account_db_url
    return kraken_futures_audit_db_url(environment, account_scope)


class KrakenFuturesAdapter(ExchangeABC):
    """Adapter exposing a BitMEX-like surface to the legacy runtime."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        base_url: str,
        symbol: str,
        *,
        environment: str = "demo",
        account_db_url: str | None = None,
        public_db_url: str | None = None,
        audit_db_url: str | None = None,
        account_scope: str = "default",
        rest_audit_retention_minutes: int = DEFAULT_PRUNING.rest_audit.retention_minutes,
        rest_audit_retention_limit: int = DEFAULT_PRUNING.rest_audit.retention_limit,
        rest_audit_maintenance_seconds: float = (
            DEFAULT_PRUNING.rest_audit.maintenance_seconds
        ),
        timeout: float = 10.0,
        postOnly: bool = False,
        session: requests.Session | None = None,
        **_ignored: Any,
    ) -> None:
        super().__init__(api_key, api_secret, base_url, symbol)
        self.environment = environment
        self.timeout = timeout
        self.post_only = postOnly
        self.session = session or requests.Session()
        self.rest_url = base_url.rstrip("/") + "/derivatives/api/v3"
        env_cfg = kraken_futures_environment(environment)
        self.account_db_url = account_db_url or env_cfg.private_db_url
        self.public_db_url = public_db_url or env_cfg.public_db_url
        self.account_scope = account_scope or "default"
        self.rest_audit_retention_minutes = max(0, int(rest_audit_retention_minutes))
        self.rest_audit_retention_limit = max(0, int(rest_audit_retention_limit))
        self.rest_audit_maintenance_seconds = max(
            1.0,
            float(rest_audit_maintenance_seconds),
        )
        self.audit_db_url = audit_db_url or _default_audit_db_url(
            environment=environment,
            account_scope=self.account_scope,
            account_db_url=account_db_url,
        )
        self.dummy = False
        self.dummyID = ""
        self._last_nonce = 0
        self._engine = create_persistence_engine(self.account_db_url)
        self._audit_engine = create_persistence_engine(self.audit_db_url)
        self._public_engine = create_persistence_engine(self.public_db_url)
        self._sessionmaker = sessionmaker(
            bind=self._engine,
            expire_on_commit=False,
            class_=Session,
        )
        self._audit_sessionmaker = sessionmaker(
            bind=self._audit_engine,
            expire_on_commit=False,
            class_=Session,
        )
        self._public_sessionmaker = sessionmaker(
            bind=self._public_engine,
            expire_on_commit=False,
            class_=Session,
        )
        self.rest_audit_errors: list[str] = []
        self._last_rest_audit_prune_monotonic = 0.0
        Base.metadata.create_all(self._engine)
        Base.metadata.create_all(self._audit_engine)
        Base.metadata.create_all(self._public_engine)

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _first(payload: Any) -> Dict[str, Any]:
        if isinstance(payload, list):
            return payload[0] if payload else {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _clean_params(params: Sequence[tuple[str, Any]]) -> list[tuple[str, Any]]:
        """Garde l'ordre des champs tout en supprimant les valeurs vides."""
        return [
            (key, value) for key, value in params if value is not None and value != ""
        ]

    def _next_nonce(self) -> str:
        now = int(time.time() * 1000)
        self._last_nonce = max(now, self._last_nonce + 1)
        return str(self._last_nonce)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Sequence[tuple[str, Any]] | Dict[str, Any] | None = None,
        auth: bool = False,
        retry_attempts: int = 3,
    ) -> Dict[str, Any]:
        url = f"{self.rest_url}{path}"
        raw_params = (
            list(params.items()) if isinstance(params, dict) else list(params or [])
        )
        payload = self._clean_params(raw_params)
        max_attempts = _effective_retry_attempts(
            method=method,
            path=path,
            payload=payload,
            configured=retry_attempts,
        )
        last_error: RuntimeError | None = None
        for attempt in range(1, max_attempts + 1):
            headers: Dict[str, str] = {}
            if auth:
                nonce = self._next_nonce()
                post_data = urlencode(payload)
                headers = {
                    "APIKey": self.api_key,
                    "Authent": sign_rest_auth(
                        post_data=post_data,
                        nonce=nonce,
                        endpoint_path=f"/api/v3{path}",
                        api_secret=self.api_secret,
                    ),
                    "Nonce": nonce,
                }
            try:
                response = self.session.request(
                    method=method.upper(),
                    url=url,
                    params=payload if method.upper() == "GET" else None,
                    data=payload if method.upper() != "GET" else None,
                    headers=headers,
                    timeout=self.timeout,
                )
            except requests.exceptions.RequestException as exc:
                error = RuntimeError(
                    f"Kraken transport error on {path}: {exc.__class__.__name__}: {exc}"
                )
                if attempt < max_attempts:
                    time.sleep(0.5 * attempt)
                    last_error = error
                    continue
                self._record_rest_call(
                    method=method,
                    path=path,
                    request_params=payload,
                    attempt_count=attempt,
                    http_status=None,
                    response_payload={},
                    result_kind="transport_error",
                    error_text=str(error),
                )
                raise error from exc
            try:
                data = response.json()
            except ValueError:
                data = {"raw_text": response.text}
            status_code = getattr(response, "status_code", 200)
            if status_code >= 400:
                error = RuntimeError(f"Kraken HTTP {status_code} on {path}: {data}")
                if status_code in {502, 503, 504} and attempt < max_attempts:
                    time.sleep(0.5 * attempt)
                    last_error = error
                    continue
                self._record_rest_call(
                    method=method,
                    path=path,
                    request_params=payload,
                    attempt_count=attempt,
                    http_status=status_code,
                    response_payload=data if isinstance(data, dict) else {"payload": data},
                    result_kind="http_error",
                    error_text=str(error),
                )
                raise error
            if not isinstance(data, dict):
                raise RuntimeError(f"Unexpected Kraken payload type: {type(data)!r}")
            if data.get("result") == "error":
                error = RuntimeError(str(data))
                if "nonceBelowThreshold" in str(data) and attempt < max_attempts:
                    time.sleep(0.25 * attempt)
                    last_error = error
                    continue
                self._record_rest_call(
                    method=method,
                    path=path,
                    request_params=payload,
                    attempt_count=attempt,
                    http_status=status_code,
                    response_payload=data,
                    result_kind="exchange_error",
                    error_text=str(error),
                )
                raise error
            self._record_rest_call(
                method=method,
                path=path,
                request_params=payload,
                attempt_count=attempt,
                http_status=status_code,
                response_payload=data,
                result_kind="ok",
                error_text=None,
            )
            return data
        assert last_error is not None
        raise last_error

    def _record_rest_call(
        self,
        *,
        method: str,
        path: str,
        request_params: Sequence[tuple[str, Any]],
        attempt_count: int,
        http_status: int | None,
        response_payload: Dict[str, Any],
        result_kind: str,
        error_text: str | None,
    ) -> None:
        if not _should_persist_rest_call(method=method, path=path):
            return
        payload = (
            response_payload
            if isinstance(response_payload, dict)
            else {"payload": response_payload}
        )
        request_payload = request_params_dict(request_params)
        payload = cast(Dict[str, Any], _json_safe_value(payload))
        request_payload = cast(Dict[str, Any], _json_safe_value(request_payload))
        order_like = _extract_order_like(payload) if payload else {}
        response_order_id = optional_str(
            order_like.get("order_id") or order_like.get("orderId")
        )
        request_order_id = optional_str(
            request_payload.get("order_id") or request_payload.get("orderId")
        )
        endpoint_order_id = response_order_id or request_order_id
        client_order_id = optional_str(
            order_like.get("cli_ord_id")
            or order_like.get("cliOrdId")
            or request_payload.get("cliOrdId")
        )
        exchange_order_id = endpoint_order_id
        ack_status = _map_order_status_from_payload(order_like) if order_like else None
        ack_order_id = endpoint_order_id
        ack_client_order_id = client_order_id
        with self._audit_sessionmaker() as session:
            try:
                session.add(
                    ExchangeRestCall(
                        local_uuid=str(uuid4()),
                        exchange="kraken",
                        environment=self.environment,
                        market_type="futures",
                        account_scope=self.account_scope,
                        symbol=self.symbol,
                        method=method.upper(),
                        path=path,
                        request_params=request_payload,
                        attempt_count=attempt_count,
                        http_status=http_status,
                        result_kind=result_kind,
                        response_payload=payload,
                        error_text=error_text,
                        client_order_id=client_order_id,
                        exchange_order_id=exchange_order_id,
                        endpoint_order_id=endpoint_order_id,
                        correlation_id=client_order_id or endpoint_order_id or request_order_id,
                        ack_status=ack_status,
                        ack_order_id=ack_order_id,
                        ack_client_order_id=ack_client_order_id,
                    )
                )
                session.commit()
                self._prune_rest_audit_if_due(session)
                return
            except Exception as exc:
                session.rollback()
                self._record_rest_audit_failure(
                    method=method,
                    path=path,
                    client_order_id=client_order_id,
                    endpoint_order_id=endpoint_order_id,
                    retry=0,
                    exc=exc,
                )
                return

    def _record_rest_audit_failure(
        self,
        *,
        method: str,
        path: str,
        client_order_id: str | None,
        endpoint_order_id: str | None,
        retry: int,
        exc: BaseException,
    ) -> None:
        error = _compact_error(exc)
        self.rest_audit_errors.append(
            (
                f"method={method.upper()} path={path} "
                f"clOrdID={client_order_id or '-'} orderID={endpoint_order_id or '-'} "
                f"retry={retry} error={error}"
            )
        )
        _LOGGER.warning(
            "rest call audit persistence failed method=%s path=%s clOrdID=%s orderID=%s retry=%s error=%s",
            method.upper(),
            path,
            client_order_id or "-",
            endpoint_order_id or "-",
            retry,
            error,
        )

    def _prune_rest_audit_if_due(self, session: Session) -> None:
        now_monotonic = time.monotonic()
        if (
            now_monotonic - self._last_rest_audit_prune_monotonic
            < self.rest_audit_maintenance_seconds
        ):
            return
        self._last_rest_audit_prune_monotonic = now_monotonic
        try:
            prune_exchange_rest_calls(
                session,
                exchange="kraken",
                environment=self.environment,
                market_type="futures",
                account_scope=self.account_scope,
                retention_minutes=self.rest_audit_retention_minutes,
                retention_limit=self.rest_audit_retention_limit,
                now=datetime.now(timezone.utc),
            )
            session.commit()
        except Exception as exc:
            session.rollback()
            _LOGGER.warning("rest call audit pruning skipped error=%s", _compact_error(exc))

    def _ticker(self) -> _Ticker:
        payload = self._request("GET", f"/tickers/{self.symbol}")
        ticker = self._first(payload.get("ticker") or payload.get("result") or payload)
        bid = float(ticker.get("bid", ticker.get("bidPrice", 0.0)) or 0.0)
        ask = float(ticker.get("ask", ticker.get("askPrice", 0.0)) or 0.0)
        mark = float(ticker.get("markPrice", ticker.get("mark_price", bid)) or bid)
        index_price = float(
            ticker.get("indexPrice", ticker.get("index_price", mark)) or mark
        )
        last = float(ticker.get("last", ticker.get("lastPrice", mark)) or mark)
        return _Ticker(
            bid=bid, ask=ask, mark_price=mark, index_price=index_price, last=last
        )

    def list_instruments(self) -> list[Dict[str, Any]]:
        """Retourne les instruments Futures exposes par Kraken."""
        payload = self._request("GET", "/instruments")
        instruments = payload.get("instruments")
        if isinstance(instruments, list):
            rows = [item for item in instruments if isinstance(item, dict)]
            self._sync_instrument_cache(rows)
            return rows
        return []

    def _sync_instrument_cache(self, instruments: Sequence[Dict[str, Any]]) -> None:
        """Persist instrument metadata to the public DB for local consultation."""
        now = datetime.now(timezone.utc)
        with self._public_sessionmaker() as session:
            for instrument in instruments:
                symbol = str(
                    instrument.get("symbol") or instrument.get("product_id") or ""
                )
                if not symbol:
                    continue
                row = (
                    session.execute(
                        select(ExchangeInstrument).where(
                            ExchangeInstrument.exchange == "kraken",
                            ExchangeInstrument.environment == self.environment,
                            ExchangeInstrument.market_type == "futures",
                            ExchangeInstrument.symbol == symbol,
                        )
                    )
                    .scalars()
                    .first()
                )
                payload = dict(instrument)
                min_quantity = _extract_min_quantity_from_instrument(payload)
                tick_size = _optional_float(payload.get("tickSize"))
                contract_size = _optional_float(payload.get("contractSize"))
                if row is None:
                    row = ExchangeInstrument(
                        exchange="kraken",
                        environment=self.environment,
                        market_type="futures",
                        symbol=symbol,
                        instrument_type=str(
                            payload.get("type") or payload.get("tag") or ""
                        ),
                        tradeable=bool(payload.get("tradeable", True)),
                        tick_size=tick_size,
                        contract_size=contract_size,
                        min_quantity=min_quantity,
                        raw_payload=payload,
                        updated_at=now,
                    )
                    session.add(row)
                else:
                    row.instrument_type = str(
                        payload.get("type") or payload.get("tag") or ""
                    )
                    row.tradeable = bool(payload.get("tradeable", True))
                    row.tick_size = tick_size
                    row.contract_size = contract_size
                    row.min_quantity = min_quantity
                    row.raw_payload = payload
                    row.updated_at = now
            session.commit()

    def instrument_rules(self, symbol: str | None = None) -> Dict[str, Any]:
        """Return cached local instrument rules, syncing from Kraken if needed."""
        target_symbol = symbol or self.symbol
        with self._public_sessionmaker() as session:
            row = (
                session.execute(
                    select(ExchangeInstrument).where(
                        ExchangeInstrument.exchange == "kraken",
                        ExchangeInstrument.environment == self.environment,
                        ExchangeInstrument.market_type == "futures",
                        ExchangeInstrument.symbol == target_symbol,
                    )
                )
                .scalars()
                .first()
            )
            if row is not None:
                if row.tick_size is not None:
                    return {
                        "symbol": row.symbol,
                        "tradeable": row.tradeable,
                        "tickSize": row.tick_size,
                        "contractSize": row.contract_size,
                        "minQuantity": row.min_quantity,
                        "type": row.instrument_type,
                        **dict(row.raw_payload or {}),
                    }
                refreshed = self.validate_symbol(target_symbol)
                return {
                    "symbol": row.symbol,
                    "tradeable": row.tradeable,
                    "tickSize": _optional_float(refreshed.get("tickSize")),
                    "contractSize": row.contract_size,
                    "minQuantity": row.min_quantity,
                    "type": row.instrument_type,
                    **dict(refreshed),
                }
        instrument = self.validate_symbol(target_symbol)
        return {
            "symbol": str(instrument.get("symbol") or target_symbol),
            "tradeable": bool(instrument.get("tradeable", True)),
            "tickSize": _optional_float(instrument.get("tickSize")),
            "contractSize": _optional_float(instrument.get("contractSize")),
            "minQuantity": _extract_min_quantity_from_instrument(instrument),
            "type": instrument.get("type") or instrument.get("tag"),
            **dict(instrument),
        }

    def minimum_order_quantity(self, symbol: str | None = None) -> float:
        """Return the locally cached minimum order quantity for the instrument."""
        rules = self.instrument_rules(symbol)
        return float(rules.get("minQuantity") or 1.0)

    def validate_symbol(self, symbol: str | None = None) -> Dict[str, Any]:
        """Valide un product id localement contre la liste publique Kraken."""
        target_symbol = symbol or self.symbol
        candidates = self.list_instruments()
        for instrument in candidates:
            if (
                str(instrument.get("symbol") or instrument.get("product_id") or "")
                == target_symbol
            ):
                return instrument
        hint = ""
        if target_symbol.startswith("PF_"):
            hint = f" Did you mean '{target_symbol.replace('PF_', 'PI_', 1)}'?"
        raise ValueError(f"Unknown Kraken Futures symbol '{target_symbol}'.{hint}")

    def _market_like_limit_price(self, side: str) -> float:
        ticker = self._ticker()
        if side.lower() == "buy":
            return ticker.ask * 1.01
        return ticker.bid * 0.99

    def _cached_tick_size(self) -> float | None:
        with self._public_sessionmaker() as session:
            row = (
                session.execute(
                    select(ExchangeInstrument.tick_size).where(
                        ExchangeInstrument.exchange == "kraken",
                        ExchangeInstrument.environment == self.environment,
                        ExchangeInstrument.market_type == "futures",
                        ExchangeInstrument.symbol == self.symbol,
                    )
                )
                .scalars()
                .first()
            )
        return float(row) if row else None

    @staticmethod
    def _legacy_ack_from_order(
        order: Dict[str, Any], *, exec_type: str = "New"
    ) -> Dict[str, Any]:
        quantity = float(
            order.get(
                "qty",
                order.get("size", order.get("quantity", order.get("orderQty", 0.0))),
            )
            or 0.0
        )
        event_filled, event_price = _execution_summary_from_order_events(order)
        filled = float(
            order.get("filled", order.get("filled_quantity", event_filled)) or 0.0
        )
        price = order.get(
            "limit_price",
            order.get(
                "limitPrice",
                order.get("price", order.get("stop_price", order.get("stopPrice"))),
            ),
        )
        if price in (None, ""):
            price = event_price
        side = (
            "buy"
            if str(order.get("direction", order.get("side", "buy"))) in {"0", "buy"}
            else "sell"
        )
        status = _map_order_status_from_payload(order)
        if exec_type == "Canceled" and filled == 0:
            status = "Canceled"
        if filled > 0 and quantity > 0:
            status = "Filled" if filled >= quantity else "PartiallyFilled"
        reason = optional_str(order.get("reason"))
        if reason is None and status == "Rejected":
            reason = optional_str(order.get("status"))
        return {
            "orderID": str(order.get("order_id", order.get("orderId", ""))),
            "clOrdID": str(order.get("cli_ord_id", order.get("cliOrdId", ""))),
            "ordStatus": status,
            "execType": exec_type,
            "reason": reason,
            "price": _optional_float(price),
            "orderQty": quantity,
            "cumQty": filled,
            "side": side.capitalize(),
            "transactTime": _parse_ms(order.get("last_update_time") or order.get("time")),
        }

    def _read_orders_from_db(self) -> list[ExchangeOrder]:
        with self._sessionmaker() as session:
            stmt = (
                select(ExchangeOrder)
                .where(
                    ExchangeOrder.exchange == "kraken",
                    ExchangeOrder.environment == self.environment,
                    ExchangeOrder.market_type == "futures",
                    ExchangeOrder.symbol == self.symbol,
                )
                .order_by(ExchangeOrder.local_timestamp.desc(), ExchangeOrder.id.desc())
            )
            return list(session.execute(stmt).scalars())

    def _read_fills_from_db(self) -> list[ExchangeFill]:
        with self._sessionmaker() as session:
            stmt = (
                select(ExchangeFill)
                .join(ExchangeOrder)
                .where(
                    ExchangeOrder.exchange == "kraken",
                    ExchangeOrder.environment == self.environment,
                    ExchangeOrder.market_type == "futures",
                    ExchangeOrder.symbol == self.symbol,
                )
                .order_by(ExchangeFill.local_timestamp.desc(), ExchangeFill.id.desc())
            )
            return list(session.execute(stmt).scalars())

    def place(
        self,
        orderQty: float,
        *,
        side: str,
        asBulk: bool = False,
        ordType: str = "Limit",
        price: float | None = None,
        stopPx: float | None = None,
        execInst: str = "",
        clOrdID: str | None = None,
        text: str | None = None,
        **_opts: Any,
    ) -> Dict[str, Any]:
        del asBulk
        client_order_id = clOrdID or _generated_client_order_id()
        fallback_market_price = None
        contract = build_send_order_contract(
            ord_type=ordType,
            symbol=self.symbol,
            side=side,
            size=orderQty,
            price=price,
            stop_price=stopPx,
            fallback_market_price=fallback_market_price,
            cli_ord_id=client_order_id,
            reduce_only=_has_exec_flag(execInst, "ReduceOnly"),
            post_only=self.post_only
            or _has_exec_flag(execInst, "ParticipateDoNotInitiate"),
            trigger_signal=_map_trigger_signal(execInst),
            trailing_stop_max_deviation=_opts.get("trailingStopMaxDeviation"),
            trailing_stop_deviation_unit=_opts.get("trailingStopDeviationUnit"),
            tag=text,
        )
        payload = self._request(
            "POST",
            "/sendorder",
            params=contract.as_params(),
            auth=True,
        )
        order = _merge_order_payload_defaults(
            _extract_order_like(payload),
            side=side,
            size=orderQty,
            price=price if price is not None else fallback_market_price,
            stop_price=stopPx,
            reduce_only=_has_exec_flag(execInst, "ReduceOnly"),
            cli_ord_id=client_order_id,
        )
        return self._legacy_ack_from_order(order)

    def amend(self, order: Dict[str, Any] | str, **params: Any) -> Dict[str, Any]:
        order_id = _extract_order_id(order)
        payload = [
            ("order_id", order_id),
            ("orderId", order_id),
            ("limitPrice", params.get("price")),
            ("stopPrice", params.get("stopPx")),
            ("size", params.get("orderQty")),
            ("cliOrdId", params.get("clOrdID")),
        ]
        response = self._request("POST", "/editorder", params=payload, auth=True)
        order_payload = _extract_order_like(response)
        if params.get("price") is not None and not any(
            key in order_payload for key in ("limit_price", "limitPrice", "price")
        ):
            order_payload["limit_price"] = params.get("price")
        if params.get("stopPx") is not None and not any(
            key in order_payload for key in ("stop_price", "stopPrice")
        ):
            order_payload["stop_price"] = params.get("stopPx")
        if params.get("orderQty") is not None and not any(
            key in order_payload for key in ("qty", "size", "quantity", "orderQty")
        ):
            order_payload["qty"] = params.get("orderQty")
        if params.get("clOrdID") is not None and not any(
            key in order_payload for key in ("cli_ord_id", "cliOrdId")
        ):
            order_payload["cli_ord_id"] = params.get("clOrdID")
        return self._legacy_ack_from_order(order_payload, exec_type="Replaced")

    def cancel(self, order: str | Sequence[str]) -> Dict[str, Any] | list[Dict[str, Any]]:
        if isinstance(order, (list, tuple)):
            replies: list[Dict[str, Any]] = []
            for item in order:
                if not isinstance(item, str):
                    continue
                reply = self.cancel(item)
                if isinstance(reply, dict):
                    replies.append(reply)
            return replies
        payload = self._request(
            "POST",
            "/cancelorder",
            params=[("order_id", order)],
            auth=True,
        )
        ack = _extract_order_like(payload)
        if not ack:
            ack = {"order_id": order, "cli_ord_id": order, "reason": "cancelled_by_user"}
        return self._legacy_ack_from_order(ack, exec_type="Canceled")

    def http_open_orders(self) -> list[Dict[str, Any]]:
        payload = self._request("GET", "/openorders", auth=True)
        return list(payload.get("openOrders", []))

    def live_open_orders(self) -> list[Dict[str, Any]]:
        """Retourne les ordres limites/resting depuis la vue REST fraiche."""
        return [
            _normalize_live_order(order)
            for order in self.http_open_orders()
            if _matches_symbol(order, self.symbol) and not _is_trigger_order(order)
        ]

    def live_trigger_orders(self) -> list[Dict[str, Any]]:
        """Retourne les ordres conditionnels/trigger depuis la vue REST fraiche."""
        return [
            _normalize_live_order(order)
            for order in self.http_open_orders()
            if _matches_symbol(order, self.symbol) and _is_trigger_order(order)
        ]

    def live_trigger_orders_db(self) -> list[Dict[str, Any]]:
        """Retourne les trigger orders ouverts depuis la DB privee locale."""
        rows = self._read_orders_from_db()
        open_statuses = {"new", "open", "partiallyfilled", "partial_fill"}
        return [
            _normalize_db_trigger_order(row)
            for row in rows
            if _db_order_is_open_trigger(row, open_statuses)
        ]

    def open_orders(self) -> list[Dict[str, Any]]:
        rows = self._read_orders_from_db()
        if rows:
            open_statuses = {"new", "open", "partiallyfilled", "partial_fill"}
            return [
                _db_order_to_legacy(row)
                for row in rows
                if row.status.replace(" ", "").replace("_", "").lower() in open_statuses
            ]
        return [
            self._legacy_ack_from_order(order)
            for order in self.http_open_orders()
            if _matches_symbol(order, self.symbol) and not _is_trigger_order(order)
        ]

    def exec_orders(self) -> list[Dict[str, Any]]:
        rows = self._read_orders_from_db()
        fills = self._read_fills_from_db()
        if rows:
            return build_exec_orders(rows, fills)
        return []

    def position(self, symbol: str | None = None) -> Dict[str, Any]:
        target_symbol = symbol or self.symbol
        payload = self._request("GET", "/openpositions", auth=True)
        positions = payload.get("openPositions", [])
        for position in positions:
            if str(position.get("symbol") or position.get("instrument")) == target_symbol:
                current_qty = float(
                    position.get(
                        "size", position.get("balance", position.get("qty", 0.0))
                    )
                )
                side_hint = str(
                    position.get("side") or position.get("direction") or ""
                ).lower()
                if current_qty > 0 and side_hint in {"short", "sell", "-1"}:
                    current_qty = -current_qty
                return {
                    "symbol": target_symbol,
                    "currentQty": current_qty,
                    "avgEntryPrice": _optional_float(
                        position.get("entry_price") or position.get("price")
                    ),
                    "leverage": _optional_float(position.get("leverage")),
                    "liquidationPrice": _optional_float(
                        position.get("liquidation_price")
                    ),
                }
        return {
            "symbol": target_symbol,
            "currentQty": 0.0,
            "avgEntryPrice": None,
            "leverage": 1.0,
            "liquidationPrice": None,
        }

    def margin(self) -> Dict[str, Any]:
        payload = self._request("GET", "/accounts", auth=True)
        available = _extract_available_margin(payload)
        return {"availableMargin": available}

    def instrument(self, symbol: str) -> Dict[str, Any]:
        metadata = self.instrument_rules(symbol)
        ticker = (
            self._ticker()
            if symbol == self.symbol
            else _ticker_from_payload(self._request("GET", f"/tickers/{symbol}"))
        )
        return {
            "symbol": symbol,
            "tickSize": metadata.get("tickSize"),
            "contractSize": metadata.get("contractSize"),
            "minQuantity": metadata.get("minQuantity"),
            "bidPrice": ticker.bid,
            "askPrice": ticker.ask,
            "markPrice": ticker.mark_price,
            "indicativeSettlePrice": ticker.index_price,
            "lastPrice": ticker.last,
        }

    def recent_trades(self) -> list[Dict[str, Any]]:
        payload = self._request("GET", "/history", params={"symbol": self.symbol})
        return list(payload.get("history", []))

    def place_order(
        self,
        side: str,
        orderQty: OrderQty | float,
        price: Price | float | None = None,
        stopPx: StopPrice | float | None = None,
        type_: str = "LIMIT",
        **params: Any,
    ) -> OrderAck:
        requested_exec_inst = str(params.pop("execInst", "") or "")
        reduce_only = bool(params.pop("reduceOnly", False))
        exec_flags = [flag for flag in requested_exec_inst.split(",") if flag]
        if reduce_only and "ReduceOnly" not in exec_flags:
            exec_flags.append("ReduceOnly")
        exec_inst = ",".join(exec_flags)
        tick_size = self._cached_tick_size()
        rounded_price = _round_price_to_tick(price, tick_size)
        rounded_stop = _round_price_to_tick(stopPx, tick_size)
        response = self.place(
            orderQty=float(orderQty),
            side=side,
            ordType=type_,
            price=rounded_price,
            stopPx=rounded_stop,
            execInst=exec_inst,
            **params,
        )
        return _ack_from_legacy(response)

    def amend_order(self, order_id: str, **params: float) -> OrderAck:
        tick_size = self._cached_tick_size()
        rounded_params: Dict[str, Any] = dict(params)
        if "price" in rounded_params:
            rounded_params["price"] = _round_price_to_tick(rounded_params.get("price"), tick_size)
        if "stopPx" in rounded_params:
            rounded_params["stopPx"] = _round_price_to_tick(rounded_params.get("stopPx"), tick_size)
        response = self.amend({"orderID": order_id}, **rounded_params)
        return _ack_from_legacy(response)

    def cancel_order(self, order_id: str) -> OrderAck:
        resolved_order_id = self._resolve_futures_cancel_order_id(order_id)
        if resolved_order_id is None:
            return OrderAck(
                order_id=order_id,
                status="NotFound",
                executed_qty=None,
                client_order_id=order_id,
                reason="client_order_id_not_visible",
            )
        response = self.cancel(resolved_order_id)
        if isinstance(response, list):
            response = response[0]
        return _ack_from_legacy(response)

    def _resolve_futures_cancel_order_id(self, order_id: str) -> str | None:
        if not _looks_like_runtime_client_order_id(order_id):
            return order_id
        for order in self.http_open_orders():
            if not _matches_symbol(order, self.symbol):
                continue
            normalized = _normalize_live_order(order)
            if normalized.get("client_order_id") != order_id:
                continue
            resolved_order_id = str(normalized.get("order_id") or "")
            return resolved_order_id or None
        return None

    def get_position(self) -> Position:
        position = self.position(self.symbol)
        return Position(
            symbol=self.symbol,
            qty=float(position.get("currentQty", 0.0)),
            entry_price=_optional_float(position.get("avgEntryPrice")),
        )

    def get_balance(self) -> float:
        margin = self.margin()
        return float(margin.get("availableMargin", 0.0))


def _ack_from_legacy(payload: Dict[str, Any]) -> OrderAck:
    return OrderAck(
        order_id=str(payload.get("orderID", "")),
        status=str(payload.get("ordStatus", "")),
        price=_optional_float(payload.get("price")),
        orig_qty=_optional_float(payload.get("orderQty")),
        executed_qty=_optional_float(payload.get("cumQty")),
        side=payload.get("side"),
        client_order_id=optional_str(payload.get("clOrdID")),
        reason=optional_str(payload.get("reason")),
    )


def _extract_min_quantity_from_instrument(instrument: Dict[str, Any]) -> float:
    for key in (
        "minimumQuantity",
        "minOrderSize",
        "minimumOrderSize",
        "lotSize",
        "minQuantity",
    ):
        value = instrument.get(key)
        if value not in (None, ""):
            return float(cast(float | str, value))
    for key in (
        "quantityStep",
        "quantity_step",
        "quantityIncrement",
        "qtyIncrement",
        "orderQtyStep",
        "stepSize",
    ):
        value = instrument.get(key)
        if value not in (None, ""):
            return float(cast(float | str, value))
    if _is_flexible_futures_instrument(instrument):
        return 0.0001
    if _is_inverse_futures_instrument(instrument):
        return 1.0
    contract_size = instrument.get("contractSize")
    if contract_size not in (None, ""):
        return max(float(cast(float | str, contract_size)), 1.0)
    return 1.0


def _is_flexible_futures_instrument(instrument: Dict[str, Any]) -> bool:
    symbol = str(instrument.get("symbol") or instrument.get("product_id") or "").upper()
    instrument_type = str(instrument.get("type") or instrument.get("tag") or "").lower()
    return symbol.startswith("PF_") or "flex" in instrument_type


def _is_inverse_futures_instrument(instrument: Dict[str, Any]) -> bool:
    symbol = str(instrument.get("symbol") or instrument.get("product_id") or "").upper()
    instrument_type = str(instrument.get("type") or instrument.get("tag") or "").lower()
    return symbol.startswith("PI_") or "inverse" in instrument_type


def _extract_order_like(payload: Dict[str, Any]) -> Dict[str, Any]:
    for key in (
        "sendStatus",
        "editStatus",
        "cancelStatus",
        "order",
        "orderTrigger",
        "trigger",
        "result",
    ):
        value = payload.get(key)
        if isinstance(value, dict):
            if "order" in value and isinstance(value["order"], dict):
                return value["order"]
            if "orderTrigger" in value and isinstance(value["orderTrigger"], dict):
                merged = dict(value)
                merged.update(
                    {
                        nested_key: nested_value
                        for nested_key, nested_value in value["orderTrigger"].items()
                        if nested_key not in merged
                    }
                )
                return merged
            return value
    return payload


def _execution_summary_from_order_events(
    payload: Dict[str, Any],
) -> tuple[float, float | None]:
    events = payload.get("orderEvents")
    if not isinstance(events, list):
        return 0.0, None
    filled = 0.0
    price: float | None = None
    for event in events:
        if not isinstance(event, dict):
            continue
        if str(event.get("type") or "").upper() != "EXECUTION":
            continue
        amount = event.get("amount")
        if isinstance(amount, (int, float, str)):
            filled += abs(float(amount))
        event_price = event.get("price")
        if isinstance(event_price, (int, float, str)):
            price = float(event_price)
    return filled, price


def _merge_order_payload_defaults(
    payload: Dict[str, Any],
    *,
    side: str,
    size: float,
    price: float | None,
    stop_price: float | None,
    reduce_only: bool,
    cli_ord_id: str | None,
) -> Dict[str, Any]:
    merged = dict(payload)
    if not any(key in merged for key in ("qty", "size", "quantity", "orderQty")):
        merged["qty"] = size
    if not any(key in merged for key in ("direction", "side")):
        merged["side"] = side
    if cli_ord_id and not any(key in merged for key in ("cli_ord_id", "cliOrdId")):
        merged["cli_ord_id"] = cli_ord_id
    if price is not None and not any(key in merged for key in ("limit_price", "price")):
        merged["price"] = price
    if stop_price is not None and not any(
        key in merged for key in ("stop_price", "stopPrice")
    ):
        merged["stop_price"] = stop_price
    if reduce_only and "reduce_only" not in merged and "reduceOnly" not in merged:
        merged["reduce_only"] = True
    return merged


def _matches_symbol(order: Dict[str, Any], symbol: str) -> bool:
    merged = _merge_trigger_order_payload(order)
    return str(merged.get("instrument") or merged.get("symbol") or "") == symbol


def _is_trigger_order(order: Dict[str, Any]) -> bool:
    merged = _merge_trigger_order_payload(order)
    if isinstance(order.get("orderTrigger"), dict):
        return True
    order_type = str(merged.get("type") or merged.get("orderType") or "").lower()
    if order_type in {
        "stp",
        "stop",
        "stop_loss",
        "stoploss",
        "stop_limit",
        "stoplosslimit",
        "take_profit",
        "takeprofit",
        "take_profit_limit",
        "takeprofitlimit",
        "trailing_stop",
        "trailingstop",
        "trailing_stop_limit",
        "trailingstoplimit",
        "market_if_touched",
        "limit_if_touched",
    }:
        return True
    for key in (
        "stop_price",
        "stopPrice",
        "triggerPrice",
        "trigger_price",
        "trailingStopMaxDeviation",
        "trailing_stop_max_deviation",
    ):
        value = merged.get(key)
        if value not in (None, "", 0, 0.0):
            return True
    return False


def _normalize_live_order(order: Dict[str, Any]) -> Dict[str, Any]:
    merged = _merge_trigger_order_payload(order)
    filled = _optional_float(
        _first_present_value(merged, "filled", "filled_quantity", "filledSize")
    )
    return {
        "order_id": str(
            merged.get("order_id")
            or merged.get("orderId")
            or merged.get("uid")
            or merged.get("id")
            or ""
        ),
        "client_order_id": str(
            merged.get("cli_ord_id")
            or merged.get("cliOrdId")
            or merged.get("clientId")
            or merged.get("client_order_id")
            or ""
        ),
        "symbol": str(merged.get("instrument") or merged.get("symbol") or ""),
        "side": _normalise_live_side(merged),
        "order_type": str(merged.get("type") or merged.get("orderType") or ""),
        "qty": _normalise_live_quantity(merged, filled),
        "filled": filled,
        "price": _optional_float(
            _first_present_value(merged, "limit_price", "limitPrice", "price")
        ),
        "stop_price": _optional_float(
            _first_present_value(
                merged,
                "stop_price",
                "stopPrice",
                "triggerPrice",
                "trigger_price",
            )
        ),
        "trigger_signal": str(
            merged.get("triggerSignal") or merged.get("trigger_signal") or ""
        ),
        "reduce_only": _truthy(
            _first_present_value(merged, "reduce_only", "reduceOnly")
        ),
        "status": _map_order_status_from_payload(merged),
    }


def _merge_trigger_order_payload(order: Dict[str, Any]) -> Dict[str, Any]:
    trigger = order.get("orderTrigger")
    if not isinstance(trigger, dict):
        return order
    merged = dict(order)
    for key, value in trigger.items():
        if key not in merged or merged[key] in (None, ""):
            merged[key] = value
    return merged


def _normalise_live_side(order: Dict[str, Any]) -> str:
    value = order.get("direction") or order.get("side") or ""
    if str(value) == "0":
        return "buy"
    if str(value) == "1":
        return "sell"
    return str(value).lower()


def _normalise_live_quantity(order: Dict[str, Any], filled: float | None) -> float | None:
    quantity = _optional_float(_first_present_value(order, "qty", "quantity", "size"))
    if quantity is not None:
        return quantity
    unfilled = _optional_float(
        _first_present_value(order, "unfilledSize", "unfilled_size")
    )
    if unfilled is None:
        return None
    return unfilled + (filled or 0.0)


def _extract_order_id(order: Dict[str, Any] | str) -> str:
    if isinstance(order, str):
        return order
    return str(
        order.get("orderID") or order.get("order_id") or order.get("clOrdID") or ""
    )


def _map_trigger_signal(exec_inst: str) -> str | None:
    normalized = exec_inst.lower()
    if "markprice" in normalized:
        return "mark"
    if "lastprice" in normalized:
        return "last"
    if "indexprice" in normalized:
        return "index"
    return None


def _has_exec_flag(exec_inst: str, flag: str) -> bool:
    return flag.lower() in (exec_inst or "").lower()


def _optional_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float, str)):
        return float(value)
    raise TypeError(f"cannot convert {type(value)!r} to float")


def _round_price_to_tick(value: object | None, tick_size: float | None) -> float | None:
    if value is None:
        return None
    price = Decimal(str(value))
    if tick_size is None or tick_size <= 0:
        return float(price)
    tick = Decimal(str(tick_size))
    rounded = (price / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick
    return float(rounded)


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "t", "yes", "y"}
    return False


def _first_present_value(payload: Dict[str, Any], *keys: str) -> object | None:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return None


def request_params_dict(params: Sequence[tuple[str, Any]]) -> Dict[str, Any]:
    """Convert ordered request params to a JSON-safe dict without auth headers."""
    return cast(Dict[str, Any], _json_safe_value({str(key): value for key, value in params}))


def _json_safe_value(value: object) -> JsonValue:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe_value(item) for item in value]
    return str(value)


def _should_persist_rest_call(*, method: str, path: str) -> bool:
    """Persist only trading REST calls needed for order-ack forensics."""
    if method.upper() != "POST":
        return False
    return path in {"/sendorder", "/editorder", "/cancelorder"}


def _effective_retry_attempts(
    *,
    method: str,
    path: str,
    payload: Sequence[tuple[str, Any]],
    configured: int,
) -> int:
    if method.upper() != "POST":
        return max(1, configured)
    if path != "/sendorder":
        return max(1, configured)
    if _payload_has_client_order_id(payload):
        return max(1, configured)
    return 1


def _payload_has_client_order_id(payload: Sequence[tuple[str, Any]]) -> bool:
    for key, value in payload:
        if str(key) != "cliOrdId":
            continue
        if isinstance(value, str) and value.strip():
            return True
    return False


def _generated_client_order_id() -> str:
    return f"k-{uuid4().hex}"


def _compact_error(exc: BaseException) -> str:
    message = str(exc).strip().replace("\n", " ")
    if len(message) <= 280:
        return message
    return f"{message[:277]}..."


def optional_str(value: object) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _first_float(payload: Dict[str, Any], *keys: str, default: float) -> float:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, dict):
            continue
        if value not in (None, ""):
            if isinstance(value, (int, float, str)):
                return float(value)
            continue
    return default


def _extract_available_margin(payload: Dict[str, Any]) -> float:
    accounts = payload.get("accounts")
    candidates: list[Dict[str, Any]] = []
    if isinstance(accounts, list):
        candidates.extend(item for item in accounts if isinstance(item, dict))
    elif isinstance(accounts, dict):
        candidates.extend(value for value in accounts.values() if isinstance(value, dict))
    if not candidates:
        candidates = [payload]
    for account in candidates:
        available = _first_float(
            account,
            "available",
            "availableMargin",
            "available_margin",
            "availableFunds",
            default=-1.0,
        )
        if available >= 0:
            return available
        auxiliary = account.get("auxiliary")
        if isinstance(auxiliary, dict):
            available = _first_float(
                auxiliary,
                "available",
                "availableMargin",
                "available_margin",
                "availableFunds",
                default=-1.0,
            )
            if available >= 0:
                return available
    return 0.0


def _map_order_status_from_payload(payload: Dict[str, Any]) -> str:
    status = str(payload.get("status", "") or "").strip()
    normalized_status = status.replace(" ", "").replace("_", "").replace("-", "").lower()
    if normalized_status in {
        "clientorderidalreadyexist",
        "clientordidalreadyexist",
        "alreadyexists",
    }:
        # Kraken duplicate client-id responses are idempotency signals:
        # the original order may already be live.
        return "New"
    if normalized_status in {"placed", "new", "open", "edited", "untouched"}:
        return "New"
    if normalized_status in {"partiallyfilled", "partialfill"}:
        return "PartiallyFilled"
    if normalized_status in {"filled", "fullfill"}:
        return "Filled"
    if normalized_status in {"cancelled", "canceled"}:
        return "Canceled"
    if normalized_status:
        return "Rejected"
    reason = str(payload.get("reason", "") or "").lower()
    if payload.get("is_cancel") is True:
        if "fill" in reason:
            return "Filled"
        return "Canceled"
    if reason == "partial_fill":
        return "PartiallyFilled"
    if reason in {"new_placed_order_by_user", "", "new"}:
        return "New"
    if reason == "cancelled_by_user":
        return "Canceled"
    if "edit" in reason or "replace" in reason:
        return "New"
    return "New"


def _parse_ms(value: object) -> str:
    if value in (None, ""):
        return KrakenFuturesAdapter._now_iso()
    if isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc)
        return dt.isoformat()
    return str(value)


def _db_order_to_legacy(row: ExchangeOrder) -> Dict[str, Any]:
    return {
        "orderID": row.exchange_order_id or "",
        "clOrdID": row.client_order_id or "",
        "ordStatus": _db_status_to_legacy(row.status),
        "execType": _db_exec_type(row.status),
        "price": row.price,
        "orderQty": row.quantity,
        "cumQty": row.filled_quantity,
        "side": row.side.capitalize(),
        "transactTime": (
            row.source_timestamp or row.local_timestamp or datetime.now(timezone.utc)
        ).isoformat(),
    }


def _db_order_is_open_trigger(
    row: ExchangeOrder,
    open_statuses: set[str],
) -> bool:
    status = row.status.replace(" ", "").replace("_", "").lower()
    if status not in open_statuses:
        return False
    order_type = str(row.order_type or "").lower()
    return "stop" in order_type or order_type in {"s", "stp"}


def _normalize_db_trigger_order(row: ExchangeOrder) -> Dict[str, Any]:
    return {
        "order_id": str(row.exchange_order_id or ""),
        "client_order_id": str(row.client_order_id or ""),
        "symbol": str(row.symbol or ""),
        "side": str(row.side or "").lower(),
        "order_type": str(row.order_type or ""),
        "qty": float(row.quantity) if row.quantity is not None else None,
        "filled": float(row.filled_quantity) if row.filled_quantity is not None else None,
        "price": float(row.price) if row.price is not None else None,
        "stop_price": float(row.price) if row.price is not None else None,
        "trigger_signal": "",
        "reduce_only": bool(row.reduce_only),
        "status": _db_status_to_legacy(row.status),
    }


def _db_status_to_legacy(status: str) -> str:
    normalized = status.replace(" ", "").replace("_", "").lower()
    if normalized in {"filled", "fullfill"}:
        return "Filled"
    if normalized in {"canceled", "cancelled"}:
        return "Canceled"
    if normalized in {"partialfill", "partiallyfilled"}:
        return "PartiallyFilled"
    return "New"


def _db_exec_type(status: str) -> str:
    normalized = status.replace(" ", "").replace("_", "").lower()
    if normalized in {"filled", "fullfill", "partialfill", "partiallyfilled"}:
        return "Trade"
    if normalized in {"canceled", "cancelled"}:
        return "Canceled"
    if normalized in {"replaced", "amended", "edited"}:
        return "Replaced"
    return "New"


def build_exec_orders(
    orders: Iterable[ExchangeOrder],
    fills: Iterable[ExchangeFill],
) -> list[Dict[str, Any]]:
    by_order_id = {row.id: row for row in orders}
    rows = [_db_order_to_legacy(order) for order in orders]
    for fill in fills:
        order = by_order_id.get(fill.order_id)
        if order is None:
            continue
        rows.append(
            {
                "orderID": order.exchange_order_id or "",
                "clOrdID": order.client_order_id or "",
                "ordStatus": (
                    "Filled"
                    if order.filled_quantity >= order.quantity
                    else "PartiallyFilled"
                ),
                "execType": "Trade",
                "price": fill.price,
                "orderQty": order.quantity,
                "cumQty": order.filled_quantity,
                "side": order.side.capitalize(),
                "transactTime": (
                    fill.source_timestamp
                    or fill.local_timestamp
                    or datetime.now(timezone.utc)
                ).isoformat(),
            }
        )
    return rows


def _ticker_from_payload(payload: Dict[str, Any]) -> _Ticker:
    ticker = KrakenFuturesAdapter._first(
        payload.get("ticker") or payload.get("result") or payload
    )
    bid = float(ticker.get("bid", ticker.get("bidPrice", 0.0)) or 0.0)
    ask = float(ticker.get("ask", ticker.get("askPrice", 0.0)) or 0.0)
    mark = float(ticker.get("markPrice", ticker.get("mark_price", bid)) or bid)
    index_price = float(ticker.get("indexPrice", ticker.get("index_price", mark)) or mark)
    last = float(ticker.get("last", ticker.get("lastPrice", mark)) or mark)
    return _Ticker(bid=bid, ask=ask, mark_price=mark, index_price=index_price, last=last)


class KrakenSpotAdapter(ExchangeABC):
    """REST adapter for Kraken Spot."""

    market_type = "spot"

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        base_url: str,
        symbol: str,
        *,
        environment: str = "live",
        timeout: float = 10.0,
        market_type: str | None = None,
        leverage: str | int | float | None = None,
        audit_db_url: str | None = None,
        account_scope: str = "default",
        rest_audit_retention_minutes: int = DEFAULT_PRUNING.rest_audit.retention_minutes,
        rest_audit_retention_limit: int = DEFAULT_PRUNING.rest_audit.retention_limit,
        rest_audit_maintenance_seconds: float = (
            DEFAULT_PRUNING.rest_audit.maintenance_seconds
        ),
        session: requests.Session | None = None,
        **_ignored: Any,
    ) -> None:
        super().__init__(api_key, api_secret, base_url.rstrip("/"), symbol)
        self.environment = environment
        self.timeout = float(timeout)
        self.market_type = market_type or self.market_type
        self.leverage = None if leverage in (None, "") else str(leverage)
        self.session = session or requests.Session()
        self.account_scope = account_scope or "default"
        self.audit_db_url = audit_db_url
        self.rest_audit_retention_minutes = max(0, int(rest_audit_retention_minutes))
        self.rest_audit_retention_limit = max(0, int(rest_audit_retention_limit))
        self.rest_audit_maintenance_seconds = max(
            1.0,
            float(rest_audit_maintenance_seconds),
        )
        self.rest_audit_errors: list[str] = []
        self._last_rest_audit_prune_monotonic = 0.0
        self._audit_engine = (
            create_persistence_engine(audit_db_url) if audit_db_url else None
        )
        self._audit_sessionmaker = (
            sessionmaker(
                bind=self._audit_engine,
                expire_on_commit=False,
                class_=Session,
            )
            if self._audit_engine is not None
            else None
        )
        if self._audit_engine is not None:
            Base.metadata.create_all(self._audit_engine)
        self._orders_by_order_id: dict[str, _KrakenSpotOrderRequest] = {}
        self._orders_by_client_id: dict[str, _KrakenSpotOrderRequest] = {}

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Dict[str, Any] | None = None,
        auth: bool = False,
    ) -> Dict[str, Any]:
        payload = {str(key): value for key, value in dict(params or {}).items() if value not in (None, "")}
        headers: dict[str, str] = {}
        if auth:
            nonce = str(int(time.time() * 1000))
            payload["nonce"] = nonce
            encoded = urlencode(payload)
            headers = {
                "API-Key": self.api_key,
                "API-Sign": _sign_spot_rest(path, encoded, nonce, self.api_secret),
            }
        method_name = method.upper()
        try:
            response = self.session.request(
                method=method_name,
                url=f"{self.base_url}{path}",
                params=payload if method_name == "GET" else None,
                data=payload if method_name != "GET" else None,
                headers=headers,
                timeout=self.timeout,
            )
        except requests.exceptions.RequestException as exc:
            error = RuntimeError(
                f"Kraken Spot transport error on {path}: {exc.__class__.__name__}: {exc}"
            )
            self._record_rest_call(
                method=method_name,
                path=path,
                request_params=payload,
                attempt_count=1,
                http_status=None,
                response_payload={},
                result_kind="transport_error",
                error_text=str(error),
            )
            raise error from exc
        try:
            data = response.json()
        except ValueError:
            data = {"raw_text": getattr(response, "text", "")}
        status_code = getattr(response, "status_code", 200)
        if status_code >= 400:
            error = RuntimeError(f"Kraken Spot HTTP {status_code} on {path}: {data}")
            self._record_rest_call(
                method=method_name,
                path=path,
                request_params=payload,
                attempt_count=1,
                http_status=status_code,
                response_payload=data if isinstance(data, dict) else {"payload": data},
                result_kind="http_error",
                error_text=str(error),
            )
            raise error
        if not isinstance(data, dict):
            raise RuntimeError(f"Kraken Spot non-object response on {path}: {data}")
        errors = data.get("error")
        if isinstance(errors, list) and errors:
            error = RuntimeError(f"Kraken Spot error on {path}: {errors}")
            self._record_rest_call(
                method=method_name,
                path=path,
                request_params=payload,
                attempt_count=1,
                http_status=status_code,
                response_payload=data,
                result_kind="exchange_error",
                error_text=str(error),
            )
            raise error
        self._record_rest_call(
            method=method_name,
            path=path,
            request_params=payload,
            attempt_count=1,
            http_status=status_code,
            response_payload=data,
            result_kind="ok",
            error_text=None,
        )
        return cast(Dict[str, Any], data)

    def _record_rest_call(
        self,
        *,
        method: str,
        path: str,
        request_params: Dict[str, Any],
        attempt_count: int,
        http_status: int | None,
        response_payload: Dict[str, Any],
        result_kind: str,
        error_text: str | None,
    ) -> None:
        if self._audit_sessionmaker is None:
            return
        if not _should_persist_spot_rest_call(method=method, path=path):
            return
        request_payload = cast(Dict[str, Any], _json_safe_value(request_params))
        payload = cast(Dict[str, Any], _json_safe_value(response_payload))
        exchange_order_id, client_order_id = _spot_rest_identity(
            path=path,
            request_payload=request_payload,
            response_payload=payload,
        )
        endpoint_order_id = exchange_order_id or client_order_id
        with self._audit_sessionmaker() as session:
            try:
                session.add(
                    ExchangeRestCall(
                        local_uuid=str(uuid4()),
                        exchange="kraken",
                        environment=self.environment,
                        market_type=self.market_type,
                        account_scope=self.account_scope,
                        symbol=self.symbol,
                        method=method.upper(),
                        path=path,
                        request_params=request_payload,
                        attempt_count=attempt_count,
                        http_status=http_status,
                        result_kind=result_kind,
                        response_payload=payload,
                        error_text=error_text,
                        client_order_id=client_order_id,
                        exchange_order_id=exchange_order_id,
                        endpoint_order_id=endpoint_order_id,
                        correlation_id=client_order_id or exchange_order_id,
                        ack_status=_spot_rest_ack_status(path, result_kind),
                        ack_order_id=exchange_order_id,
                        ack_client_order_id=client_order_id,
                    )
                )
                session.commit()
                self._prune_rest_audit_if_due(session)
            except Exception as exc:
                session.rollback()
                self._record_rest_audit_failure(
                    method=method,
                    path=path,
                    client_order_id=client_order_id,
                    endpoint_order_id=endpoint_order_id,
                    exc=exc,
                )

    def _record_rest_audit_failure(
        self,
        *,
        method: str,
        path: str,
        client_order_id: str | None,
        endpoint_order_id: str | None,
        exc: BaseException,
    ) -> None:
        error = _compact_error(exc)
        self.rest_audit_errors.append(
            (
                f"method={method.upper()} path={path} "
                f"clOrdID={client_order_id or '-'} orderID={endpoint_order_id or '-'} "
                f"error={error}"
            )
        )
        _LOGGER.warning(
            "rest call audit persistence failed method=%s path=%s clOrdID=%s orderID=%s error=%s",
            method.upper(),
            path,
            client_order_id or "-",
            endpoint_order_id or "-",
            error,
        )

    def _prune_rest_audit_if_due(self, session: Session) -> None:
        now_monotonic = time.monotonic()
        if (
            now_monotonic - self._last_rest_audit_prune_monotonic
            < self.rest_audit_maintenance_seconds
        ):
            return
        self._last_rest_audit_prune_monotonic = now_monotonic
        try:
            prune_exchange_rest_calls(
                session,
                exchange="kraken",
                environment=self.environment,
                market_type=self.market_type,
                account_scope=self.account_scope,
                retention_minutes=self.rest_audit_retention_minutes,
                retention_limit=self.rest_audit_retention_limit,
                now=datetime.now(timezone.utc),
            )
            session.commit()
        except Exception as exc:
            session.rollback()
            _LOGGER.warning("rest call audit pruning skipped error=%s", _compact_error(exc))

    def instrument_rules(self, symbol: str | None = None) -> dict[str, object]:
        target_symbol = symbol or self.symbol
        payload = self._request(
            "GET",
            "/0/public/AssetPairs",
            params={"pair": target_symbol},
        )
        pair = _first_spot_result(payload)
        pair_decimals = int(float(pair.get("pair_decimals", 0) or 0))
        lot_decimals = int(float(pair.get("lot_decimals", 0) or 0))
        return {
            "symbol": target_symbol,
            "status": pair.get("status") or pair.get("wsname"),
            "tickSize": float(Decimal(1).scaleb(-pair_decimals)) if pair_decimals >= 0 else None,
            "stepSize": float(Decimal(1).scaleb(-lot_decimals)) if lot_decimals >= 0 else None,
            "minQuantity": _optional_float(pair.get("ordermin")) or 0.0,
            "leverageBuy": pair.get("leverage_buy"),
            "leverageSell": pair.get("leverage_sell"),
        }

    def list_instruments(self) -> list[Dict[str, Any]]:
        """Return Kraken spot asset pairs from the selected REST endpoint."""

        payload = self._request("GET", "/0/public/AssetPairs")
        result = payload.get("result") if isinstance(payload, dict) else {}
        if not isinstance(result, dict):
            return []
        rows: list[Dict[str, Any]] = []
        for pair_id, item in result.items():
            if not isinstance(item, dict):
                continue
            symbol = str(pair_id)
            wsname = optional_str(item.get("wsname"))
            status = optional_str(item.get("status"))
            rows.append(
                {
                    "symbol": symbol,
                    "product_id": symbol,
                    "wsname": wsname,
                    "type": self.market_type,
                    "status": status,
                    "tradeable": status not in {"cancel_only", "post_only"},
                    **item,
                }
            )
        return rows

    def validate_symbol(self, symbol: str | None = None) -> Dict[str, Any]:
        """Validate a Kraken spot/margin pair and return compact metadata."""

        target_symbol = symbol or self.symbol
        rules = self.instrument_rules(target_symbol)
        status = optional_str(rules.get("status"))
        return {
            "symbol": target_symbol,
            "product_id": target_symbol,
            "type": self.market_type,
            "tradeable": status not in {"cancel_only", "post_only"},
            **rules,
        }

    def instrument(self, symbol: str) -> Dict[str, Any]:
        rules = self.instrument_rules(symbol)
        payload = self._request("GET", "/0/public/Ticker", params={"pair": symbol})
        ticker = _first_spot_result(payload)
        bid = _spot_array_float(ticker.get("b"))
        ask = _spot_array_float(ticker.get("a"))
        last = _spot_array_float(ticker.get("c"))
        return {
            "symbol": symbol,
            "tickSize": rules.get("tickSize"),
            "minQuantity": rules.get("minQuantity"),
            "bidPrice": bid,
            "askPrice": ask,
            "markPrice": last,
            "indicativeSettlePrice": last,
            "lastPrice": last,
        }

    def place_order(
        self,
        side: str,
        orderQty: OrderQty | float,
        price: Price | float | None = None,
        stopPx: StopPrice | float | None = None,
        type_: str = "LIMIT",
        **params: Any,
    ) -> OrderAck:
        exec_inst = str(params.pop("execInst", "") or "")
        if _has_exec_flag(exec_inst, "ReduceOnly") or _truthy(params.pop("reduceOnly", False)):
            raise ValueError(f"Kraken {self.market_type} does not support ReduceOnly")
        kraken_type = _map_spot_order_type(type_)
        leverage = self._order_leverage(params.pop("leverage", None))
        request: dict[str, Any] = {
            "pair": self.symbol,
            "type": side.lower(),
            "ordertype": kraken_type,
            "volume": float(orderQty),
        }
        client_order_id = optional_str(params.pop("clOrdID", None))
        if client_order_id:
            request["cl_ord_id"] = client_order_id
        if _has_exec_flag(exec_inst, "ParticipateDoNotInitiate"):
            request["oflags"] = "post"
        if leverage:
            request["leverage"] = leverage
        if kraken_type == "limit":
            if price is None:
                raise ValueError("Kraken Spot limit order requires price")
            request["price"] = float(price)
        elif kraken_type == "market":
            pass
        elif kraken_type == "stop-loss":
            if stopPx is None:
                raise ValueError("Kraken Spot stop-loss order requires stopPx")
            request["price"] = float(stopPx)
        elif kraken_type == "stop-loss-limit":
            if stopPx is None or price is None:
                raise ValueError("Kraken Spot stop-loss-limit requires stopPx and price")
            request["price"] = float(stopPx)
            request["price2"] = float(price)
        else:
            raise ValueError(f"Unsupported Kraken Spot order type: {kraken_type}")
        payload = self._request("POST", "/0/private/AddOrder", params=request, auth=True)
        ack = _spot_ack_from_payload(payload, client_order_id=client_order_id)
        self._cache_order(
            ack,
            _KrakenSpotOrderRequest(
                side=side.lower(),
                quantity=float(orderQty),
                kolabi_type=str(type_),
                kraken_type=kraken_type,
                price=float(price) if price is not None else None,
                stop_price=float(stopPx) if stopPx is not None else None,
                client_order_id=client_order_id,
                leverage=leverage,
            ),
        )
        return ack

    def amend_order(self, order_id: str, **params: Any) -> OrderAck:
        cached = self._lookup_cached_order(order_id, params.get("clOrdID"))
        if cached is None:
            raise ValueError(
                "Kraken Spot amend requires a cached original order from this process; "
                f"order_id={order_id}"
            )
        self.cancel_order(order_id)
        return self.place_order(
            side=cached.side,
            orderQty=float(params.get("orderQty") or cached.quantity),
            price=params.get("price", cached.price),
            stopPx=params.get("stopPx", cached.stop_price),
            type_=cached.kolabi_type,
            clOrdID=params.get("newClOrdID")
            or params.get("replaceClOrdID")
            or _replacement_spot_client_id(cached.client_order_id),
            leverage=params.get("leverage", cached.leverage),
        )

    def cancel_order(self, order_id: str) -> OrderAck:
        key = "txid"
        if _looks_like_client_order_id(order_id):
            key = "cl_ord_id"
        payload = self._request(
            "POST",
            "/0/private/CancelOrder",
            params={key: order_id},
            auth=True,
        )
        self._forget_order(order_id)
        return OrderAck(order_id=order_id, status="Canceled")

    def live_open_orders(self) -> list[Dict[str, Any]]:
        payload = self._request("POST", "/0/private/OpenOrders", auth=True)
        open_orders = payload.get("result", {}).get("open") if isinstance(payload.get("result"), dict) else {}
        if not isinstance(open_orders, dict):
            return []
        return [
            _normalise_spot_open_order(order_id, cast(Dict[str, Any], row))
            for order_id, row in open_orders.items()
            if isinstance(row, dict) and not _is_spot_trigger_order(row)
        ]

    def live_trigger_orders(self) -> list[Dict[str, Any]]:
        payload = self._request("POST", "/0/private/OpenOrders", auth=True)
        open_orders = payload.get("result", {}).get("open") if isinstance(payload.get("result"), dict) else {}
        if not isinstance(open_orders, dict):
            return []
        return [
            _normalise_spot_open_order(order_id, cast(Dict[str, Any], row))
            for order_id, row in open_orders.items()
            if isinstance(row, dict) and _is_spot_trigger_order(row)
        ]

    def live_trigger_orders_db(self) -> list[Dict[str, Any]]:
        return []

    def open_orders(self) -> list[Dict[str, Any]]:
        return [
            *_spot_legacy_orders(self.live_open_orders()),
            *_spot_legacy_orders(self.live_trigger_orders()),
        ]

    def get_position(self) -> Position:
        if self.market_type != "margin":
            payload = self._request("POST", "/0/private/Balance", auth=True)
            result = payload.get("result") if isinstance(payload, dict) else {}
            return Position(
                symbol=self.symbol,
                qty=_spot_base_balance_quantity(result, self.symbol),
                entry_price=None,
            )
        payload = self._request("POST", "/0/private/OpenPositions", auth=True)
        result = payload.get("result") if isinstance(payload, dict) else {}
        qty = 0.0
        entry_price: float | None = None
        if isinstance(result, dict):
            for row in result.values():
                if not isinstance(row, dict):
                    continue
                if str(row.get("pair") or row.get("symbol") or "") not in {
                    "",
                    self.symbol,
                }:
                    continue
                volume = float(row.get("vol") or row.get("volume") or 0.0)
                qty += _spot_position_side(row) * volume
                if entry_price is None:
                    entry_price = _spot_entry_price(row, volume)
        return Position(symbol=self.symbol, qty=qty, entry_price=entry_price)

    def get_balance(self) -> float:
        payload = self._request("POST", "/0/private/Balance", auth=True)
        result = payload.get("result") if isinstance(payload, dict) else {}
        if not isinstance(result, dict):
            return 0.0
        quote = _spot_quote_asset(self.symbol)
        for key, value in result.items():
            if str(key).upper().replace("Z", "", 1) == quote:
                return float(value or 0.0)
        return 0.0

    def _order_leverage(self, override: object | None) -> str | None:
        leverage = None if override in (None, "") else str(override)
        leverage = leverage or self.leverage
        if self.market_type == "margin" and not leverage:
            raise ValueError(
                "Kraken margin orders require explicit leverage via adapter_kwargs "
                "or KRAKEN_SPOT_MARGIN_LEVERAGE"
            )
        return leverage

    def _lookup_cached_order(
        self,
        order_id: str,
        client_order_id: object | None = None,
    ) -> _KrakenSpotOrderRequest | None:
        if order_id in self._orders_by_order_id:
            return self._orders_by_order_id[order_id]
        if order_id in self._orders_by_client_id:
            return self._orders_by_client_id[order_id]
        if client_order_id is not None and str(client_order_id) in self._orders_by_client_id:
            return self._orders_by_client_id[str(client_order_id)]
        return None

    def _cache_order(self, ack: OrderAck, request: _KrakenSpotOrderRequest) -> None:
        if ack.order_id:
            self._orders_by_order_id[str(ack.order_id)] = request
        if request.client_order_id:
            self._orders_by_client_id[request.client_order_id] = request

    def _forget_order(self, identity: str) -> None:
        cached = self._orders_by_order_id.pop(identity, None)
        if cached is not None and cached.client_order_id:
            self._orders_by_client_id.pop(cached.client_order_id, None)
            return
        self._orders_by_client_id.pop(identity, None)


class KrakenMarginAdapter(KrakenSpotAdapter):
    """Kraken Spot margin adapter using AddOrder leverage."""

    market_type = "margin"


def _should_persist_spot_rest_call(*, method: str, path: str) -> bool:
    """Persist only spot/margin trading mutations needed for order forensics."""

    if method.upper() != "POST":
        return False
    return path in {"/0/private/AddOrder", "/0/private/CancelOrder"}


def _spot_rest_identity(
    *,
    path: str,
    request_payload: Dict[str, Any],
    response_payload: Dict[str, Any],
) -> tuple[str | None, str | None]:
    """Extract exchange/client order identifiers from Kraken Spot REST evidence."""

    client_order_id = optional_str(request_payload.get("cl_ord_id"))
    exchange_order_id = optional_str(request_payload.get("txid"))
    if path == "/0/private/AddOrder":
        result = response_payload.get("result")
        if isinstance(result, dict):
            txids = result.get("txid")
            if isinstance(txids, list) and txids:
                exchange_order_id = optional_str(txids[0])
            elif txids not in (None, ""):
                exchange_order_id = optional_str(txids)
    if path == "/0/private/CancelOrder":
        if request_payload.get("cl_ord_id") not in (None, ""):
            client_order_id = optional_str(request_payload.get("cl_ord_id"))
            exchange_order_id = None
        elif request_payload.get("txid") not in (None, ""):
            exchange_order_id = optional_str(request_payload.get("txid"))
    return exchange_order_id, client_order_id


def _spot_rest_ack_status(path: str, result_kind: str) -> str | None:
    if result_kind != "ok":
        return "Rejected"
    if path == "/0/private/AddOrder":
        return "New"
    if path == "/0/private/CancelOrder":
        return "Canceled"
    return None


def _sign_spot_rest(path: str, post_data: str, nonce: str, api_secret: str) -> str:
    sha = hashlib.sha256((nonce + post_data).encode("utf-8")).digest()
    mac = hmac.new(
        base64.b64decode(api_secret),
        path.encode("utf-8") + sha,
        hashlib.sha512,
    )
    return base64.b64encode(mac.digest()).decode("ascii")


def _first_spot_result(payload: Dict[str, Any]) -> Dict[str, Any]:
    result = payload.get("result")
    if isinstance(result, dict):
        for value in result.values():
            if isinstance(value, dict):
                return cast(Dict[str, Any], value)
    return {}


def _spot_array_float(value: object) -> float | None:
    if isinstance(value, list) and value:
        return _optional_float(value[0])
    return _optional_float(value)


def _map_spot_order_type(value: object) -> str:
    normalized = str(value or "").replace("_", "").replace("-", "").lower()
    if normalized in {"m", "market"}:
        return "market"
    if normalized in {"l", "limit"}:
        return "limit"
    if normalized in {"s", "stp", "stop", "stoploss", "stopmarket"}:
        return "stop-loss"
    if normalized in {"sl", "stoplimit", "stoplosslimit"}:
        return "stop-loss-limit"
    raise ValueError(f"Unsupported Kraken Spot order type '{value}'")


def _spot_ack_from_payload(
    payload: Dict[str, Any],
    *,
    client_order_id: str | None,
) -> OrderAck:
    result = payload.get("result") if isinstance(payload, dict) else {}
    txids: object = None
    if isinstance(result, dict):
        txids = result.get("txid")
    order_id = ""
    if isinstance(txids, list) and txids:
        order_id = str(txids[0])
    elif txids not in (None, ""):
        order_id = str(txids)
    return OrderAck(
        order_id=order_id,
        status="New",
        client_order_id=client_order_id,
    )


def _normalise_spot_open_order(order_id: object, row: Dict[str, Any]) -> Dict[str, Any]:
    descr = row.get("descr") if isinstance(row.get("descr"), dict) else {}
    return {
        "order_id": str(order_id),
        "client_order_id": optional_str(row.get("cl_ord_id")),
        "symbol": str(descr.get("pair") or row.get("pair") or ""),
        "side": str(descr.get("type") or row.get("type") or "").lower(),
        "order_type": str(descr.get("ordertype") or row.get("ordertype") or ""),
        "qty": _optional_float(row.get("vol")),
        "filled": _optional_float(row.get("vol_exec")),
        "price": _optional_float(descr.get("price") or row.get("price")),
        "stop_price": _optional_float(descr.get("price") or row.get("stopprice")),
        "trigger_signal": "",
        "reduce_only": False,
        "status": _spot_status_to_legacy(row.get("status")),
    }


def _spot_legacy_orders(orders: Iterable[Dict[str, Any]]) -> list[Dict[str, Any]]:
    return [
        {
            "orderID": order.get("order_id", ""),
            "clOrdID": order.get("client_order_id", ""),
            "ordStatus": order.get("status", ""),
            "price": order.get("price") or order.get("stop_price"),
            "orderQty": order.get("qty"),
            "cumQty": order.get("filled"),
            "side": str(order.get("side") or "").capitalize(),
        }
        for order in orders
    ]


def _is_spot_trigger_order(row: Dict[str, Any]) -> bool:
    descr = row.get("descr") if isinstance(row.get("descr"), dict) else {}
    order_type = str(descr.get("ordertype") or row.get("ordertype") or "").lower()
    return "stop" in order_type or "take-profit" in order_type


def _spot_status_to_legacy(value: object) -> str:
    normalized = str(value or "").lower()
    if normalized == "open":
        return "New"
    if normalized == "closed":
        return "Filled"
    if normalized == "canceled":
        return "Canceled"
    return normalized.capitalize() or "New"


def _looks_like_client_order_id(value: object) -> bool:
    text = str(value or "")
    return text.startswith(("H", "T")) or "-" in text and not text.startswith("O")


def _looks_like_runtime_client_order_id(value: object) -> bool:
    text = str(value or "")
    return text.startswith(("H", "T"))


def _replacement_spot_client_id(previous: str | None) -> str:
    prefix = (previous or "krk")[:24].rstrip("-")
    return f"{prefix}-r{uuid4().hex[:9]}"[:32]


def _spot_quote_asset(symbol: str) -> str:
    compact = _normalise_spot_symbol_text(symbol)
    for quote in ("USDT", "USDC", "USD", "EUR", "BTC", "ETH"):
        if compact.endswith(quote):
            return quote
    return "USD"


def _spot_base_asset(symbol: str) -> str:
    compact = _normalise_spot_symbol_text(symbol)
    quote = _spot_quote_asset(compact)
    if quote and compact.endswith(quote):
        return compact[: -len(quote)]
    return compact


def _normalise_spot_symbol_text(symbol: str) -> str:
    return symbol.replace("/", "").replace("_", "").replace("-", "").upper()


def _spot_asset_key(value: object) -> str:
    text = str(value or "").upper()
    if "." in text:
        text = text.split(".", 1)[0]
    if len(text) > 3 and text[0] in {"X", "Z"}:
        text = text[1:]
    return text


def _spot_base_balance_quantity(result: object, symbol: str) -> float:
    if not isinstance(result, dict):
        return 0.0
    base = _spot_base_asset(symbol)
    for key, value in result.items():
        if _spot_asset_key(key) == base:
            return float(value or 0.0)
    return 0.0


def _spot_position_side(row: Dict[str, Any]) -> float:
    side = str(row.get("type") or row.get("side") or "").lower()
    return -1.0 if side in {"sell", "short"} else 1.0


def _spot_entry_price(row: Dict[str, Any], volume: float) -> float | None:
    price = _optional_float(row.get("price") or row.get("entryPrice"))
    if price is not None:
        return price
    cost = _optional_float(row.get("cost"))
    if cost is None or volume == 0.0:
        return None
    return abs(cost / volume)


def optional_str(value: object) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


Adapter = KrakenFuturesAdapter
