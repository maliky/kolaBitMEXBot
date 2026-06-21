"""Runtime DB state reader for strategy readiness and market/account snapshots.

Purpose: provide typed public/private state snapshots used by bot runtime
preflight and pair-cycle execution.
Inputs: PostgreSQL URLs, exchange/environment/symbol filters.
Outputs: `PublicMarketState`, `PrivateFeedState`, `StrategyRuntimeState`.
Side effects: database reads and polling sleeps in wait loops.
Important types: typed DB records (`PublicBookRecord`, `PrivateOrderRecord`,
`PrivatePositionRecord`) and state dataclasses.
Role: boundary adapter.
Transitional: yes, typed row adapters are incremental over existing ORM models.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from time import sleep
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session, sessionmaker

from kolabi.shared.core.runtime_types import (
    PrivateFillRecord,
    PrivateOrderRecord,
    PrivatePositionRecord,
    PublicBookRecord,
    PublicIndicatorRecord,
)
from kolabi.shared.persistence import (
    AccountBalance,
    AccountPosition,
    ExchangeFill,
    ExchangeInstrument,
    ExchangeOrder,
    create_persistence_engine,
)
from kolabi.tree.account import AccountStreamConfig, latest_connection
from kolabi.tree.kraken import latest_indicator_values, latest_snapshot


@dataclass(frozen=True)
class PublicMarketState:
    """Latest public market view for one symbol."""

    symbol: str
    best_bid: float | None
    best_ask: float | None
    mid_price: float | None
    last_price: float | None
    mark_price: float | None
    index_price: float | None
    tick_size: float | None
    spread: float | None
    imbalance: float | None
    avg_bid: float | None
    avg_ask: float | None
    recorded_at: str | None
    source_timestamp: str | None
    age_seconds: float | None
    source_age_seconds: float | None
    indicators: dict[str, float]
    ready: bool
    reason: str | None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly mapping."""
        return asdict(self)


@dataclass(frozen=True)
class PrivateFeedState:
    """Freshness summary for one private runtime feed."""

    stream_kind: str
    status: str
    updated_at: str | None
    last_heartbeat_at: str | None
    age_seconds: float | None
    ready: bool
    last_error: str | None
    reason: str | None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly mapping."""
        return asdict(self)


@dataclass(frozen=True)
class AccountBalanceState:
    """Latest normalised account balance for one settlement asset."""

    asset: str
    available: float | None
    locked: float | None
    total: float | None
    recorded_at: str | None
    source_timestamp: str | None
    ready: bool
    reason: str | None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly mapping."""
        return asdict(self)


@dataclass(frozen=True)
class StrategyRuntimeState:
    """Combined public/private readiness view for one strategy symbol."""

    symbol: str
    public: PublicMarketState
    private_ws: PrivateFeedState
    rest_reconciler: PrivateFeedState
    open_order_count: int
    fill_count: int
    position_size: float | None
    position_entry_price: float | None
    ready: bool
    reasons: tuple[str, ...]
    exchange: str | None = None
    market_type: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly mapping."""
        payload = asdict(self)
        payload["public"] = self.public.as_dict()
        payload["private_ws"] = self.private_ws.as_dict()
        payload["rest_reconciler"] = self.rest_reconciler.as_dict()
        return payload


_MISSING_SCHEMA_EXCEPTIONS = (OperationalError, ProgrammingError)


def _is_missing_schema_error(exc: BaseException) -> bool:
    """Return true only for absent table/relation/column schema errors."""

    original = getattr(exc, "orig", exc)
    text = f"{type(original).__name__} {original} {exc}".lower()
    return (
        "no such table" in text
        or "no such column" in text
        or "undefinedtable" in text
        or "undefined table" in text
        or ("relation " in text and "does not exist" in text)
        or ("column " in text and "does not exist" in text)
    )


def _missing_public_market_state(symbol: str, reason: str) -> PublicMarketState:
    """Build the typed public not-ready state for absent public DB truth."""

    return PublicMarketState(
        symbol=symbol,
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
        reason=reason,
    )


def _missing_private_feed_state(stream_kind: str) -> PrivateFeedState:
    """Build the typed private state used when the feeder has not bootstrapped DB."""

    rest_reconciler = stream_kind == "rest_reconciler"
    return PrivateFeedState(
        stream_kind=stream_kind,
        status="missing_schema",
        updated_at=None,
        last_heartbeat_at=None,
        age_seconds=None,
        ready=rest_reconciler,
        last_error=None,
        reason=None if rest_reconciler else f"{stream_kind} DB schema missing",
    )


def _missing_account_balance_state(asset: str, reason: str) -> AccountBalanceState:
    return AccountBalanceState(
        asset=asset,
        available=None,
        locked=None,
        total=None,
        recorded_at=None,
        source_timestamp=None,
        ready=False,
        reason=reason,
    )


class KrakenRuntimeStateClient:
    """Read strategy-facing public and private state from PostgreSQL stores."""

    def __init__(
        self,
        *,
        market_db_url: str,
        account_db_url: str,
        critical_account_db_url: str | None = None,
        symbol: str,
        exchange: str = "kraken",
        environment: str = "demo",
        market_type: str = "futures",
        account_scope: str = "default",
        max_public_age_seconds: float = 15.0,
        max_private_age_seconds: float = 30.0,
        max_reconcile_age_seconds: float = 300.0,
    ) -> None:
        self.market_db_url = market_db_url
        self.account_db_url = account_db_url
        self.critical_account_db_url = critical_account_db_url or account_db_url
        self.symbol = symbol
        self.exchange = exchange
        self.environment = environment
        self.market_type = market_type
        self.account_scope = account_scope
        self.max_public_age_seconds = max_public_age_seconds
        self.max_private_age_seconds = max_private_age_seconds
        self.max_reconcile_age_seconds = max_reconcile_age_seconds
        self._market_sessionmaker = sessionmaker(
            bind=create_persistence_engine(self.market_db_url),
            expire_on_commit=False,
            class_=Session,
        )
        self._account_sessionmaker = sessionmaker(
            bind=create_persistence_engine(self.account_db_url),
            expire_on_commit=False,
            class_=Session,
        )
        self._critical_account_sessionmaker = sessionmaker(
            bind=create_persistence_engine(self.critical_account_db_url),
            expire_on_commit=False,
            class_=Session,
        )

    def fetch_market_state(
        self,
        symbol: str | None = None,
        exchange: str | None = None,
        market_type: str | None = None,
    ) -> PublicMarketState:
        """Load the latest public book snapshot and compact indicators."""
        target_symbol = symbol or self.symbol
        target_exchange = exchange or self.exchange
        target_market_type = market_type or self.market_type
        current_time = datetime.now(timezone.utc)
        try:
            with self._market_sessionmaker() as session:
                snapshot = latest_snapshot(
                    session,
                    target_symbol,
                    target_exchange,
                    self.environment,
                    target_market_type,
                )
                if snapshot is None:
                    return _missing_public_market_state(
                        target_symbol,
                        "missing public market snapshot",
                    )
                indicators = latest_indicator_values(
                    session,
                    target_symbol,
                    target_exchange,
                    self.environment,
                    target_market_type,
                )
                public_book = _public_book_record_from_snapshot(snapshot, target_symbol)
                tick_size = _instrument_tick_size(
                    session,
                    symbol=target_symbol,
                    exchange=target_exchange,
                    environment=self.environment,
                    market_type=target_market_type,
                )
                public_indicators = _public_indicator_records(indicators, target_symbol)
                freshest_local_time = _latest_public_timestamp(
                    snapshot.local_timestamp,
                    indicators,
                )
                age_seconds = _age_seconds(freshest_local_time, current_time)
                source_age_seconds = _age_seconds(snapshot.source_timestamp, current_time)
                ready = (
                    age_seconds is not None
                    and age_seconds <= self.max_public_age_seconds
                )
                reason = None if ready else "public market data is stale"
                return PublicMarketState(
                    symbol=target_symbol,
                    best_bid=public_book.best_bid,
                    best_ask=public_book.best_ask,
                    mid_price=public_book.mid_price,
                    last_price=_indicator_value(indicators, "last_price"),
                    mark_price=_indicator_value(indicators, "mark_price"),
                    index_price=_indicator_value(indicators, "index_price"),
                    tick_size=tick_size,
                    spread=public_book.spread,
                    imbalance=public_book.imbalance,
                    avg_bid=public_book.avg_bid,
                    avg_ask=public_book.avg_ask,
                    recorded_at=public_book.recorded_at,
                    source_timestamp=public_book.source_timestamp,
                    age_seconds=age_seconds,
                    source_age_seconds=source_age_seconds,
                    indicators={
                        record.name: record.value for record in public_indicators
                    },
                    ready=ready,
                    reason=reason,
                )
        except _MISSING_SCHEMA_EXCEPTIONS as exc:
            if not _is_missing_schema_error(exc):
                raise
            return _missing_public_market_state(
                target_symbol,
                "public market DB schema missing",
            )

    def fetch_runtime_state(
        self,
        symbol: str | None = None,
        exchange: str | None = None,
        market_type: str | None = None,
    ) -> StrategyRuntimeState:
        """Load the combined runtime state used by a Kraken strategy route."""
        target_symbol = symbol or self.symbol
        target_exchange = exchange or self.exchange
        target_market_type = market_type or self.market_type
        public = self.fetch_market_state(
            target_symbol,
            exchange=target_exchange,
            market_type=target_market_type,
        )
        with self._critical_account_sessionmaker() as session:
            private_ws = self._private_feed_state(
                session,
                "private_ws",
                self.max_private_age_seconds,
                db_url=self.critical_account_db_url,
                exchange=target_exchange,
                market_type=target_market_type,
            )
            open_order_count = self._count_open_orders(
                session,
                target_symbol,
                exchange=target_exchange,
                market_type=target_market_type,
            )
            fill_count = self._count_fills(
                session,
                target_symbol,
                exchange=target_exchange,
                market_type=target_market_type,
            )
        with self._account_sessionmaker() as session:
            reconciler = self._private_feed_state(
                session,
                "rest_reconciler",
                self.max_reconcile_age_seconds,
                db_url=self.account_db_url,
                exchange=target_exchange,
                market_type=target_market_type,
            )
            position = self._latest_position(
                session,
                target_symbol,
                exchange=target_exchange,
                market_type=target_market_type,
            )
        reasons = tuple(
            reason
            for reason in (
                public.reason,
                private_ws.reason,
            )
            if reason
        )
        return StrategyRuntimeState(
            symbol=target_symbol,
            public=public,
            private_ws=private_ws,
            rest_reconciler=reconciler,
            open_order_count=open_order_count,
            fill_count=fill_count,
            position_size=None if position is None else position.size,
            position_entry_price=None if position is None else position.entry_price,
            ready=public.ready and private_ws.ready,
            reasons=reasons,
            exchange=target_exchange,
            market_type=target_market_type,
        )

    def fetch_account_balance(
        self,
        asset: str = "USD",
        exchange: str | None = None,
        market_type: str | None = None,
    ) -> AccountBalanceState:
        """Load the latest normalised balance snapshot for one settlement asset."""
        del market_type
        target_exchange = exchange or self.exchange
        target_asset = _normalise_balance_asset(asset)
        try:
            with self._account_sessionmaker() as session:
                row = session.execute(
                    select(AccountBalance)
                    .where(
                        AccountBalance.exchange == target_exchange,
                        AccountBalance.environment == self.environment,
                        AccountBalance.account_scope == self.account_scope,
                        AccountBalance.asset == target_asset,
                    )
                    .order_by(
                        AccountBalance.local_timestamp.desc(),
                        AccountBalance.id.desc(),
                    )
                    .limit(1)
                ).scalar_one_or_none()
        except _MISSING_SCHEMA_EXCEPTIONS as exc:
            if not _is_missing_schema_error(exc):
                raise
            return _missing_account_balance_state(
                target_asset,
                "account balance DB schema missing",
            )
        if row is None:
            return _missing_account_balance_state(
                target_asset,
                "missing account balance snapshot",
            )
        return AccountBalanceState(
            asset=target_asset,
            available=row.available,
            locked=row.locked,
            total=row.total,
            recorded_at=_datetime_iso(row.local_timestamp),
            source_timestamp=_datetime_iso(row.source_timestamp),
            ready=True,
            reason=None,
        )

    def fetch_private_orders_since(
        self,
        *,
        after_local_timestamp: datetime | None = None,
        after_local_id: int | None = None,
        symbol: str | None = None,
        exchange: str | None = None,
        market_type: str | None = None,
        limit: int = 200,
    ) -> tuple[PrivateOrderRecord, ...]:
        """Return private order rows strictly newer than the supplied cursor."""
        target_symbol = symbol or self.symbol
        target_exchange = exchange or self.exchange
        target_market_type = market_type or self.market_type
        with self._critical_account_sessionmaker() as session:
            predicates = [
                ExchangeOrder.exchange == target_exchange,
                ExchangeOrder.environment == self.environment,
                ExchangeOrder.market_type == target_market_type,
                ExchangeOrder.account_scope == self.account_scope,
                ExchangeOrder.symbol == target_symbol,
            ]
            if after_local_timestamp is not None:
                cursor_timestamp = _cursor_timestamp(after_local_timestamp)
                if after_local_id is None:
                    predicates.append(ExchangeOrder.local_timestamp >= cursor_timestamp)
                else:
                    predicates.append(
                        or_(
                            ExchangeOrder.local_timestamp > cursor_timestamp,
                            and_(
                                ExchangeOrder.local_timestamp == cursor_timestamp,
                                ExchangeOrder.id > after_local_id,
                            ),
                        )
                    )
            statement = (
                select(ExchangeOrder)
                .where(*predicates)
                .order_by(ExchangeOrder.local_timestamp.asc(), ExchangeOrder.id.asc())
                .limit(limit)
            )
            rows = session.execute(statement).scalars().all()
        return tuple(_private_order_record(row) for row in rows)

    def fetch_private_fills_since(
        self,
        *,
        after_local_timestamp: datetime | None = None,
        after_local_id: int | None = None,
        symbol: str | None = None,
        exchange: str | None = None,
        market_type: str | None = None,
        limit: int = 200,
    ) -> tuple[PrivateOrderRecord, ...]:
        """Return fill-derived private order records newer than the supplied cursor."""
        target_symbol = symbol or self.symbol
        target_exchange = exchange or self.exchange
        target_market_type = market_type or self.market_type
        with self._critical_account_sessionmaker() as session:
            predicates = [
                ExchangeOrder.exchange == target_exchange,
                ExchangeOrder.environment == self.environment,
                ExchangeOrder.market_type == target_market_type,
                ExchangeOrder.account_scope == self.account_scope,
                ExchangeOrder.symbol == target_symbol,
            ]
            if after_local_timestamp is not None:
                cursor_timestamp = _cursor_timestamp(after_local_timestamp)
                if after_local_id is None:
                    predicates.append(ExchangeFill.local_timestamp >= cursor_timestamp)
                else:
                    predicates.append(
                        or_(
                            ExchangeFill.local_timestamp > cursor_timestamp,
                            and_(
                                ExchangeFill.local_timestamp == cursor_timestamp,
                                ExchangeFill.id > after_local_id,
                            ),
                        )
                    )
            statement = (
                select(ExchangeFill, ExchangeOrder)
                .join(ExchangeOrder, ExchangeFill.order_id == ExchangeOrder.id)
                .where(*predicates)
                .order_by(ExchangeFill.local_timestamp.asc(), ExchangeFill.id.asc())
                .limit(limit)
            )
            rows = session.execute(statement).all()
        return tuple(_private_order_record_from_fill(fill_row, order_row) for fill_row, order_row in rows)

    def fetch_private_orders_for_identities(
        self,
        *,
        client_order_ids: tuple[str, ...] = (),
        exchange_order_ids: tuple[str, ...] = (),
        symbol: str | None = None,
        exchange: str | None = None,
        market_type: str | None = None,
        limit: int = 400,
    ) -> tuple[PrivateOrderRecord, ...]:
        """Return latest private order rows matching active order identities."""
        if not client_order_ids and not exchange_order_ids:
            return ()
        target_symbol = symbol or self.symbol
        target_exchange = exchange or self.exchange
        target_market_type = market_type or self.market_type
        with self._critical_account_sessionmaker() as session:
            predicates = []
            if client_order_ids:
                predicates.append(ExchangeOrder.client_order_id.in_(client_order_ids))
            if exchange_order_ids:
                predicates.append(ExchangeOrder.exchange_order_id.in_(exchange_order_ids))
            statement = (
                select(ExchangeOrder)
                .where(
                    ExchangeOrder.exchange == target_exchange,
                    ExchangeOrder.environment == self.environment,
                    ExchangeOrder.market_type == target_market_type,
                    ExchangeOrder.account_scope == self.account_scope,
                    ExchangeOrder.symbol == target_symbol,
                    or_(*predicates),
                )
                .order_by(ExchangeOrder.local_timestamp.desc(), ExchangeOrder.id.desc())
                .limit(limit)
            )
            rows = session.execute(statement).scalars().all()
        return tuple(_private_order_record(row) for row in rows)

    def fetch_latest_private_orders(
        self,
        *,
        symbol: str | None = None,
        exchange: str | None = None,
        market_type: str | None = None,
        open_only: bool = False,
    ) -> tuple[PrivateOrderRecord, ...]:
        """Return latest private order state per exchange/client identity."""

        target_symbol = symbol or self.symbol
        target_exchange = exchange or self.exchange
        target_market_type = market_type or self.market_type
        with self._critical_account_sessionmaker() as session:
            rows = (
                session.execute(
                    select(ExchangeOrder)
                    .where(
                        ExchangeOrder.exchange == target_exchange,
                        ExchangeOrder.environment == self.environment,
                        ExchangeOrder.market_type == target_market_type,
                        ExchangeOrder.account_scope == self.account_scope,
                        ExchangeOrder.symbol == target_symbol,
                    )
                    .order_by(
                        ExchangeOrder.local_timestamp.desc(),
                        ExchangeOrder.id.desc(),
                    )
                )
                .scalars()
                .all()
            )
        latest: dict[tuple[str, str], ExchangeOrder] = {}
        for row in rows:
            identity = _order_identity_key(row)
            if identity is None or identity in latest:
                continue
            latest[identity] = row
        records = tuple(_private_order_record(row) for row in latest.values())
        if not open_only:
            return records
        return tuple(record for record in records if _private_order_record_is_open(record))

    def fetch_private_fills_for_identities(
        self,
        *,
        client_order_ids: tuple[str, ...] = (),
        exchange_order_ids: tuple[str, ...] = (),
        symbol: str | None = None,
        exchange: str | None = None,
        market_type: str | None = None,
        limit: int = 400,
    ) -> tuple[PrivateOrderRecord, ...]:
        """Return latest fill-derived records matching active order identities."""
        if not client_order_ids and not exchange_order_ids:
            return ()
        target_symbol = symbol or self.symbol
        target_exchange = exchange or self.exchange
        target_market_type = market_type or self.market_type
        with self._critical_account_sessionmaker() as session:
            predicates = []
            if client_order_ids:
                predicates.append(ExchangeOrder.client_order_id.in_(client_order_ids))
            if exchange_order_ids:
                predicates.append(ExchangeOrder.exchange_order_id.in_(exchange_order_ids))
            statement = (
                select(ExchangeFill, ExchangeOrder)
                .join(ExchangeOrder, ExchangeFill.order_id == ExchangeOrder.id)
                .where(
                    ExchangeOrder.exchange == target_exchange,
                    ExchangeOrder.environment == self.environment,
                    ExchangeOrder.market_type == target_market_type,
                    ExchangeOrder.account_scope == self.account_scope,
                    ExchangeOrder.symbol == target_symbol,
                    or_(*predicates),
                )
                .order_by(ExchangeFill.local_timestamp.desc(), ExchangeFill.id.desc())
                .limit(limit)
            )
            rows = session.execute(statement).all()
        return tuple(
            _private_order_record_from_fill(fill_row, order_row)
            for fill_row, order_row in rows
        )

    def wait_until_ready(
        self,
        *,
        symbol: str | None = None,
        exchange: str | None = None,
        market_type: str | None = None,
        timeout_seconds: float,
        poll_seconds: float = 1.0,
    ) -> StrategyRuntimeState:
        """Block until public and private runtime state are fresh enough."""
        deadline = datetime.now(timezone.utc).timestamp() + timeout_seconds
        last_state = self.fetch_runtime_state(
            symbol,
            exchange=exchange,
            market_type=market_type,
        )
        while datetime.now(timezone.utc).timestamp() < deadline:
            last_state = self.fetch_runtime_state(
                symbol,
                exchange=exchange,
                market_type=market_type,
            )
            if last_state.ready:
                return last_state
            sleep(poll_seconds)
        return last_state

    def _private_feed_state(
        self,
        session: Session,
        stream_kind: str,
        max_age_seconds: float,
        *,
        db_url: str,
        exchange: str,
        market_type: str,
    ) -> PrivateFeedState:
        """Read one private feed status with a strict freshness gate."""
        config = AccountStreamConfig(
            db_url=db_url,
            exchange=exchange,
            environment=self.environment,
            market_type=market_type,
            account_scope=self.account_scope,
        )
        try:
            connection = latest_connection(session, config, stream_kind)
            if connection is None and stream_kind == "private_ws":
                # Compatibility fallback: split-profile critical stream keeps alias health.
                connection = latest_connection(session, config, "private_ws_critical")
        except _MISSING_SCHEMA_EXCEPTIONS as exc:
            if not _is_missing_schema_error(exc):
                raise
            session.rollback()
            return _missing_private_feed_state(stream_kind)
        if connection is None:
            if stream_kind == "rest_reconciler":
                return PrivateFeedState(
                    stream_kind=stream_kind,
                    status="missing",
                    updated_at=None,
                    last_heartbeat_at=None,
                    age_seconds=None,
                    ready=True,
                    last_error=None,
                    reason=None,
                )
            return PrivateFeedState(
                stream_kind=stream_kind,
                status="missing",
                updated_at=None,
                last_heartbeat_at=None,
                age_seconds=None,
                ready=False,
                last_error=None,
                reason=f"missing {stream_kind} state",
            )
        current_time = datetime.now(timezone.utc)
        reference_time = connection.last_heartbeat_at or connection.updated_at
        age_seconds = _age_seconds(reference_time, current_time)
        if stream_kind == "rest_reconciler":
            recent_error = connection.status.lower() == "error" and (
                age_seconds is None or age_seconds <= max_age_seconds
            )
            return PrivateFeedState(
                stream_kind=stream_kind,
                status=connection.status,
                updated_at=connection.updated_at.isoformat(),
                last_heartbeat_at=connection.last_heartbeat_at.isoformat()
                if connection.last_heartbeat_at
                else None,
                age_seconds=age_seconds,
                ready=not recent_error,
                last_error=connection.last_error,
                reason="recent rest_reconciler error" if recent_error else None,
            )
        is_healthy = connection.status.lower() == "healthy"
        is_fresh = age_seconds is not None and age_seconds <= max_age_seconds
        ready = is_healthy and is_fresh
        reason = None
        if not is_healthy:
            reason = f"{stream_kind} status is {connection.status}"
        elif not is_fresh:
            reason = f"{stream_kind} state is stale"
        return PrivateFeedState(
            stream_kind=stream_kind,
            status=connection.status,
            updated_at=connection.updated_at.isoformat(),
            last_heartbeat_at=connection.last_heartbeat_at.isoformat()
            if connection.last_heartbeat_at
            else None,
            age_seconds=age_seconds,
            ready=ready,
            last_error=connection.last_error,
            reason=reason,
        )

    def _count_open_orders(
        self,
        session: Session,
        symbol: str,
        *,
        exchange: str,
        market_type: str,
    ) -> int:
        """Count strategy-relevant open orders for one symbol."""
        try:
            rows = (
                session.execute(
                    select(ExchangeOrder).where(
                        ExchangeOrder.exchange == exchange,
                        ExchangeOrder.environment == self.environment,
                        ExchangeOrder.market_type == market_type,
                        ExchangeOrder.account_scope == self.account_scope,
                        ExchangeOrder.symbol == symbol,
                    )
                    .order_by(
                        ExchangeOrder.local_timestamp.desc(),
                        ExchangeOrder.id.desc(),
                    )
                )
                .scalars()
                .all()
            )
        except _MISSING_SCHEMA_EXCEPTIONS as exc:
            if not _is_missing_schema_error(exc):
                raise
            session.rollback()
            return 0
        latest: dict[tuple[str, str], ExchangeOrder] = {}
        for row in rows:
            identity = _order_identity_key(row)
            if identity is None or identity in latest:
                continue
            latest[identity] = row
        return sum(
            1
            for row in latest.values()
            if _private_order_record_is_open(_private_order_record(row))
        )

    def _count_fills(
        self,
        session: Session,
        symbol: str,
        *,
        exchange: str,
        market_type: str,
    ) -> int:
        """Count fills for one symbol."""
        try:
            rows = (
                session.execute(
                    select(ExchangeFill)
                    .join(ExchangeOrder)
                    .where(
                        ExchangeOrder.exchange == exchange,
                        ExchangeOrder.environment == self.environment,
                        ExchangeOrder.market_type == market_type,
                        ExchangeOrder.account_scope == self.account_scope,
                        ExchangeOrder.symbol == symbol,
                    )
                )
                .scalars()
                .all()
            )
        except _MISSING_SCHEMA_EXCEPTIONS as exc:
            if not _is_missing_schema_error(exc):
                raise
            session.rollback()
            return 0
        fill_records = [_private_fill_record(symbol) for _ in rows]
        return len(fill_records)

    def _latest_position(
        self,
        session: Session,
        symbol: str,
        *,
        exchange: str,
        market_type: str,
    ) -> PrivatePositionRecord | None:
        """Read the latest position row for one symbol."""
        stmt = (
            select(AccountPosition)
            .where(
                AccountPosition.exchange == exchange,
                AccountPosition.environment == self.environment,
                AccountPosition.market_type == market_type,
                AccountPosition.account_scope == self.account_scope,
                AccountPosition.symbol == symbol,
            )
            .order_by(AccountPosition.local_timestamp.desc(), AccountPosition.id.desc())
        )
        try:
            row = session.execute(stmt).scalars().first()
        except _MISSING_SCHEMA_EXCEPTIONS as exc:
            if not _is_missing_schema_error(exc):
                raise
            session.rollback()
            return None
        return None if row is None else _private_position_record(row)


def _age_seconds(value: datetime | None, current_time: datetime) -> float | None:
    """Return the age in seconds for an optional UTC timestamp."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return max(0.0, (current_time - value).total_seconds())


def _latest_public_timestamp(
    snapshot_time: datetime | None,
    indicators: dict[str, Any],
) -> datetime | None:
    """Return the freshest local timestamp exposed by the public market service."""
    latest_time = snapshot_time
    for indicator in indicators.values():
        computed_at = getattr(indicator, "computed_at", None)
        if computed_at is None:
            continue
        if computed_at.tzinfo is None:
            computed_at = computed_at.replace(tzinfo=timezone.utc)
        if latest_time is None:
            latest_time = computed_at
            continue
        snapshot_tz = (
            latest_time
            if latest_time.tzinfo
            else latest_time.replace(tzinfo=timezone.utc)
        )
        if computed_at > snapshot_tz:
            latest_time = computed_at
    return latest_time


def _public_book_record_from_snapshot(snapshot: Any, symbol: str) -> PublicBookRecord:
    return PublicBookRecord(
        symbol=symbol,
        best_bid=snapshot.best_bid,
        best_ask=snapshot.best_ask,
        mid_price=snapshot.mid_price,
        spread=snapshot.spread,
        imbalance=snapshot.imbalance,
        avg_bid=snapshot.avg_bid,
        avg_ask=snapshot.avg_ask,
        recorded_at=snapshot.local_timestamp.isoformat(),
        source_timestamp=snapshot.source_timestamp.isoformat()
        if snapshot.source_timestamp
        else None,
    )


def _public_indicator_records(
    indicators: dict[str, Any],
    symbol: str,
) -> tuple[PublicIndicatorRecord, ...]:
    records: list[PublicIndicatorRecord] = []
    for name, indicator in indicators.items():
        computed_at = getattr(indicator, "computed_at", None)
        records.append(
            PublicIndicatorRecord(
                symbol=symbol,
                name=name,
                value=float(indicator.value),
                recorded_at=computed_at.isoformat() if computed_at is not None else None,
            )
        )
    return tuple(records)


def _indicator_value(indicators: dict[str, Any], name: str) -> float | None:
    indicator = indicators.get(name)
    if indicator is None:
        return None
    value = getattr(indicator, "value", None)
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _datetime_iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _normalise_balance_asset(asset: str) -> str:
    return str(asset or "USD").strip().upper() or "USD"


def _instrument_tick_size(
    session: Session,
    *,
    symbol: str,
    exchange: str,
    environment: str,
    market_type: str,
) -> float | None:
    stmt = (
        select(ExchangeInstrument.tick_size)
        .where(
            ExchangeInstrument.exchange == exchange,
            ExchangeInstrument.environment == environment,
            ExchangeInstrument.market_type == market_type,
            ExchangeInstrument.symbol == symbol,
        )
        .order_by(ExchangeInstrument.id.desc())
        .limit(1)
    )
    value = session.execute(stmt).scalar_one_or_none()
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    return None


def _normalise_cursor_timestamp(value: datetime, peer: datetime) -> datetime:
    """Compare stored timestamps with runtime cursors safely."""
    if value.tzinfo is None or peer.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _cursor_timestamp(value: datetime) -> datetime:
    """Normalise a runtime cursor for database timestamp comparisons."""
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc)
    return value.replace(tzinfo=timezone.utc)


def _private_order_record(row: ExchangeOrder) -> PrivateOrderRecord:
    reason, is_cancel = _order_reason_flags_from_payload(row.raw_payload)
    stop_price = _stop_price_from_payload(row.raw_payload)
    return PrivateOrderRecord(
        symbol=row.symbol,
        status=row.status,
        exchange_order_id=row.exchange_order_id,
        client_order_id=row.client_order_id,
        reason=reason,
        is_cancel=is_cancel,
        side=row.side,
        order_type=row.order_type,
        price=(
            row.price
            if row.price is not None
            else _execution_price_from_payload(row.raw_payload)
        ),
        stop_price=stop_price if stop_price is not None else _stop_price_from_order_row(row),
        quantity=row.quantity,
        filled_quantity=row.filled_quantity,
        source_timestamp=(
            row.source_timestamp.isoformat() if row.source_timestamp is not None else None
        ),
        local_timestamp=row.local_timestamp.isoformat(),
        local_id=row.id,
        exchange=row.exchange,
        market_type=row.market_type,
    )


def _order_identity_key(row: ExchangeOrder) -> tuple[str, str] | None:
    if row.exchange_order_id:
        return ("exchange", row.exchange_order_id)
    if row.client_order_id:
        return ("client", row.client_order_id)
    return None


def _private_order_record_is_open(record: PrivateOrderRecord) -> bool:
    key = record.status.replace(" ", "").replace("_", "").replace("-", "").lower()
    return key in {
        "new",
        "open",
        "untouched",
        "partiallyfilled",
        "partialfill",
        "living",
    }


def _order_reason_flags_from_payload(
    payload: object,
) -> tuple[str | None, bool | None]:
    if not isinstance(payload, dict):
        return None, None
    reason_obj = payload.get("reason")
    reason = str(reason_obj) if isinstance(reason_obj, str) and reason_obj else None
    is_cancel_obj = payload.get("is_cancel")
    is_cancel = is_cancel_obj if isinstance(is_cancel_obj, bool) else None
    return reason, is_cancel


def _stop_price_from_payload(payload: object) -> float | None:
    """Extract a platform stop/trigger price from raw private payloads."""
    if not isinstance(payload, dict):
        return None
    for key in ("stop_price", "stopPrice", "stopPx", "triggerPrice"):
        value = payload.get(key)
        if isinstance(value, (int, float, str)):
            try:
                return float(value)
            except ValueError:
                continue
    trigger = payload.get("orderTrigger")
    if isinstance(trigger, dict):
        return _stop_price_from_payload(trigger)
    return None


def _execution_price_from_payload(payload: object) -> float | None:
    """Extract a fill/execution price from raw private payloads."""
    if not isinstance(payload, dict):
        return None
    for key in ("price", "fillPrice", "fill_price", "avgPx", "lastPx", "executed_price"):
        value = payload.get(key)
        if isinstance(value, (int, float, str)):
            try:
                parsed = float(value)
            except ValueError:
                continue
            if parsed > 0:
                return parsed
    return None


def _stop_price_from_order_row(row: ExchangeOrder) -> float | None:
    order_type = (row.order_type or "").lower()
    if "stop" not in order_type:
        return None
    return row.price


def _private_fill_record(symbol: str) -> PrivateFillRecord:
    return PrivateFillRecord(symbol=symbol)


def _private_order_record_from_fill(
    fill_row: ExchangeFill,
    order_row: ExchangeOrder,
) -> PrivateOrderRecord:
    reason, is_cancel = _order_reason_flags_from_payload(fill_row.raw_payload)
    # Fill rows are authoritative evidence of execution. Do not inherit a stale
    # open/new order status here, otherwise head->tail progression can lag.
    effective_status = "filled"
    effective_reason = reason or "full_fill"
    stop_price = _stop_price_from_payload(order_row.raw_payload)
    return PrivateOrderRecord(
        symbol=order_row.symbol,
        status=effective_status,
        exchange_order_id=order_row.exchange_order_id,
        client_order_id=order_row.client_order_id,
        reason=effective_reason,
        is_cancel=is_cancel,
        side=order_row.side,
        order_type=order_row.order_type,
        price=fill_row.price,
        stop_price=stop_price if stop_price is not None else _stop_price_from_order_row(order_row),
        quantity=order_row.quantity,
        filled_quantity=max(order_row.filled_quantity, fill_row.quantity),
        source_timestamp=(
            fill_row.source_timestamp.isoformat()
            if fill_row.source_timestamp is not None
            else None
        ),
        local_timestamp=fill_row.local_timestamp.isoformat(),
        local_id=fill_row.id,
        exchange=order_row.exchange,
        market_type=order_row.market_type,
    )


def _private_position_record(row: AccountPosition) -> PrivatePositionRecord:
    return PrivatePositionRecord(
        symbol=row.symbol,
        size=row.size,
        entry_price=row.entry_price,
        exchange=row.exchange,
        market_type=row.market_type,
    )
