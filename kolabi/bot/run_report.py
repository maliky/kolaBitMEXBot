"""Build operator-facing run-state reports from Kolabi runtime logs.

The report uses the runtime log as the lifecycle timeline and, by default,
uses the local private account DB as the canonical source for fills, fees, and
maker/taker roles.  This keeps post-run journals reproducible from data already
on disk, without depending on an exchange UI or other external witness.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shlex
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Iterable, Mapping, Sequence, TextIO

from sqlalchemy import create_engine, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from kolabi.bot.price_units import signed_logbps_move
from kolabi.bot.run_report_finance import (
    EvidenceQuality,
    PnlKind,
    calculate_finance,
    entry_notional_usd,
    pnl_kind,
)
from kolabi.shared.persistence import (
    AccountBalance,
    ExchangeFill,
    ExchangeInstrument,
    ExchangeOrder,
    RawExchangeEvent,
)
from kolabi.shared.redaction import redact_url

NO_GATE_LOG = "no_gate_log"
NO_DEADLINE_LOG = "no_deadline_log"
EVIDENCE_FILL_DB = "fill_db"
EVIDENCE_ORDER_DB = "order_db"
EVIDENCE_LOG = "log"
DEFAULT_MAKER_FEE_RATE = Decimal("0.0002")
DEFAULT_TAKER_FEE_RATE = Decimal("0.0005")
MISSING_FILL_NOTE_LIMIT = 12


class ReportError(RuntimeError):
    """Raised when a run report cannot be built from local data."""


@dataclass(frozen=True, order=True)
class PairKey:
    """Stable identity for one pair attempt in compact runtime logs."""

    name: str
    attempt: int


@dataclass(frozen=True)
class FillLeg:
    """One log-derived fill leg for a head or tail order."""

    side: str
    quantity: Decimal
    price: Decimal
    filled_at: datetime


@dataclass
class PairLifecycle:
    """Log-derived lifecycle facts needed to recognise terminated pairs."""

    key: PairKey
    started_at: datetime | None = None
    gate_reference_price: Decimal | None = None
    head_client_id: str | None = None
    head_exchange_order_id: str | None = None
    tail_client_id: str | None = None
    tail_exchange_order_id: str | None = None
    head_fill: FillLeg | None = None
    tail_fill: FillLeg | None = None
    tail_placed_at: datetime | None = None
    initial_tail_stop: Decimal | None = None
    latest_tail_stop: Decimal | None = None
    amend_count: int = 0
    tail_amend_times: list[datetime] = field(default_factory=list)

    @property
    def terminated(self) -> bool:
        return self.head_fill is not None and self.tail_fill is not None


@dataclass(frozen=True)
class TailTelemetry:
    """Latest log-derived tail telemetry for one flying tail."""

    key: PairKey
    recorded_at: datetime
    reference_price: Decimal
    stop_price: Decimal
    current_distance: Decimal


@dataclass(frozen=True)
class MarketSnapshot:
    """Latest log-derived public price snapshot seen in tail telemetry."""

    recorded_at: datetime
    source: str
    spread_guard: Decimal | None
    bid_price: Decimal | None
    ask_price: Decimal | None
    mid_price: Decimal | None
    last_price: Decimal | None
    mark_price: Decimal | None
    index_price: Decimal | None


@dataclass
class LatentAttempt:
    """Latest log-derived state for one not-yet-filled head attempt."""

    key: PairKey
    last_event_at: datetime
    last_event: str
    status: str = ""
    gate: str = ""
    reference_price: Decimal | None = None
    head_price: Decimal | None = None
    head_price_spec: Decimal | None = None
    timeout_minutes: Decimal | None = None
    parameters_observed: bool = False
    quantity: Decimal | None = None
    order_type: str = ""
    deadline_at: datetime | None = None
    head_client_id: str | None = None
    ended: bool = False
    timed_out: bool = False
    terminal_event: str | None = None


@dataclass(frozen=True, order=True)
class RuntimeRoute:
    """One exchange route declared by runtime preflight or ready logs."""

    exchange: str
    market_type: str
    symbol: str

    @property
    def label(self) -> str:
        return f"{self.exchange}:{self.market_type}:{self.symbol}"


@dataclass(frozen=True)
class RuntimeMetadata:
    """Run-level facts used to make report identity stable."""

    started_at: datetime | None = None
    environment: str | None = None
    strategy_path: str | None = None
    routes: tuple[RuntimeRoute, ...] = ()


@dataclass(frozen=True)
class RunLogSnapshot:
    """Parsed report state from one runtime log."""

    lifecycles: dict[PairKey, PairLifecycle]
    tail_telemetry: dict[PairKey, TailTelemetry]
    latent_attempts: dict[PairKey, LatentAttempt]
    quantity_diagnostics: tuple["QuantityDiagnostic", ...]
    market_snapshot: MarketSnapshot | None
    first_log_at: datetime | None
    last_log_at: datetime | None
    runtime_metadata: RuntimeMetadata = RuntimeMetadata()


@dataclass(frozen=True)
class ReportIdentity:
    """Stable operator-facing name for one runtime report."""

    name: str
    run_started_at: datetime
    command_line: str


@dataclass(frozen=True)
class DbFillSummary:
    """DB-derived fill summary for one client order id."""

    client_order_id: str
    exchange_order_id: str | None
    exchange: str
    environment: str
    market_type: str
    symbol: str
    side: str
    fill_count: int
    quantity: Decimal
    price: Decimal
    fee: Decimal
    fee_currency: str | None
    liquidity_role: str | None


@dataclass(frozen=True)
class DbOrderSummary:
    """DB-derived latest order state for one client order id."""

    client_order_id: str
    exchange_order_id: str | None
    side: str
    status: str
    price: Decimal | None
    quantity: Decimal
    filled_quantity: Decimal


@dataclass(frozen=True)
class OrderLegEvidence:
    """Resolved report evidence for one head or tail leg."""

    role: str
    client_order_id: str | None
    exchange_order_id: str | None
    side: str
    price: Decimal
    quantity: Decimal
    fee: Decimal | None
    fee_currency: str | None
    liquidity_role: str | None
    source: str


@dataclass(frozen=True)
class InstrumentSummary:
    """Local instrument metadata needed for volume and sizing context."""

    route: RuntimeRoute
    environment: str | None
    instrument_type: str | None
    tick_size: Decimal | None
    contract_size: Decimal
    min_quantity: Decimal | None
    quantity_step: Decimal | None


@dataclass(frozen=True)
class MarketVolumeSummary:
    """Local public trade volume observed for one route during the run."""

    base_volume: Decimal
    usd_volume: Decimal


@dataclass(frozen=True)
class VolumeRow:
    """Aggregated run volume attributed to one strategy pair and market route."""

    pair_name: str
    market: str
    fills: int
    quantity: Decimal
    bot_usd_volume: Decimal
    average_life_seconds: Decimal | None
    average_roi_percent: Decimal | None
    average_roi_per_hour_percent: Decimal | None
    market_base_volume: Decimal | None
    market_usd_volume: Decimal | None
    min_qty_base: Decimal | None
    min_qty_usd: Decimal | None
    tick_base: Decimal | None
    tick_usd: Decimal | None
    closed: int = 0
    net_usd: Decimal | None = None


@dataclass(frozen=True)
class QuantityDiagnostic:
    """Log-derived USD quantity sizing fact from runtime validation."""

    pair_name: str
    route: RuntimeRoute | None
    status: str
    nominal_usd: Decimal | None
    mark_price: Decimal | None
    contract_size: Decimal | None
    min_quantity: Decimal | None
    quantity_step: Decimal | None
    resolved_quantity: Decimal | None
    resolved_usd: Decimal | None
    source: str = "runtime"
    percent: Decimal | None = None
    available_usd: Decimal | None = None


@dataclass(frozen=True)
class SizingRow:
    """Operator-facing minimum/step sizing row for one route."""

    route: str
    pair_name: str
    nominal_usd: Decimal | None
    percent: Decimal | None
    available_usd: Decimal | None
    mark_price: Decimal | None
    contract_size: Decimal | None
    min_quantity: Decimal | None
    quantity_step: Decimal | None
    min_usd: Decimal | None
    step_usd: Decimal | None
    resolved_quantity: Decimal | None
    resolved_usd: Decimal | None
    market_base_volume: Decimal | None
    market_usd_volume: Decimal | None
    status: str
    source: str


@dataclass(frozen=True)
class ReportRow:
    """One rendered terminated-pair row.

    `amend_logbps` is the signed log basis point move from the initial tail
    stop to the latest amended tail stop. Gross and net are quote-currency amounts.
    """

    key: PairKey
    pair_started_at: datetime | None
    head_wait_seconds: int | None
    gate_reference_price: Decimal | None
    head_fill_at: datetime
    tail_fill_at: datetime
    tail_placed_at: datetime | None
    life_seconds: int
    side: str
    head_price: Decimal
    tail_price: Decimal
    quantity: Decimal
    liquidity: str
    head_source: str
    tail_source: str
    amend_count: int
    tail_amend_1_at: datetime | None
    tail_amend_2_at: datetime | None
    amend_logbps: Decimal | None
    gross_usd: Decimal
    net_usd: Decimal | None
    net_estimated: bool
    roi_percent: Decimal | None
    roi_per_hour_percent: Decimal | None
    cumulative_net: Decimal | None
    route: RuntimeRoute | None = None
    pnl_kind: PnlKind = PnlKind.UNKNOWN
    pnl_currency: str = "USD"
    gross_native: Decimal | None = None
    net_native: Decimal | None = None
    fees_usd: Decimal | None = None
    entry_notional_usd: Decimal | None = None
    finance_quality: EvidenceQuality = EvidenceQuality.UNAVAILABLE


@dataclass(frozen=True)
class LivingTailRow:
    """One head-filled pair whose tail is still flying."""

    key: PairKey
    head_fill_at: datetime
    age_seconds: int
    side: str
    head_price: Decimal
    quantity: Decimal
    head_liquidity: str
    tail_stop: Decimal | None
    reference_price: Decimal | None
    current_distance_logbps: Decimal | None
    tail_status: str
    tail_filled_quantity: Decimal | None
    route: RuntimeRoute | None = None
    entry_notional_usd: Decimal | None = None


@dataclass(frozen=True)
class LatentRow:
    """One latest active latent/head-pending attempt."""

    key: PairKey
    time: datetime
    status: str
    gate: str
    reference_price: Decimal | None
    head_price: Decimal | None
    quantity: Decimal | None
    order_type: str
    deadline_at: datetime | None
    last_event: str


@dataclass(frozen=True)
class PairEvolutionRow:
    """One initial or changed tOut/hPrice state and its attempt outcome."""

    key: PairKey
    change: str
    timeout_minutes: Decimal | None
    head_price_spec: Decimal | None
    terminal: str
    head_fill_at: datetime | None
    tail_fill_at: datetime | None
    net_usd: Decimal | None
    roi_percent: Decimal | None


@dataclass(frozen=True)
class ParameterRegime:
    """Contiguous attempts sharing one tOut and hPrice parameter state."""

    pair_name: str
    first_attempt: int
    last_attempt: int
    timeout_minutes: Decimal | None
    head_price_spec: Decimal | None
    attempts: int
    timeouts: int
    closed: int
    wins: int
    losses: int
    total_net_usd: Decimal | None
    average_roi_percent: Decimal | None
    final_state: str
    outcomes: tuple[PairEvolutionRow, ...]


@dataclass(frozen=True)
class FinancialOverview:
    """Run-level finance and evidence coverage for the overview."""

    closed: int
    wins: int
    losses: int
    gross_usd: Decimal | None
    fees_usd: Decimal | None
    net_usd: Decimal | None
    win_rate_percent: Decimal | None
    profit_factor: Decimal | None
    average_roi_percent: Decimal | None
    aggregate_roi_percent: Decimal | None
    roi_per_hour_percent: Decimal | None
    max_drawdown_usd: Decimal | None
    open_notional_usd: Decimal | None
    exact_rows: int
    estimated_rows: int
    unavailable_rows: int


@dataclass(frozen=True)
class RunReport:
    """Renderer-independent operator report model."""

    identity: ReportIdentity
    report_at: datetime
    market_snapshot: MarketSnapshot | None
    financial_overview: FinancialOverview
    regimes: tuple[ParameterRegime, ...]
    terminated_rows: tuple[ReportRow, ...]
    living_rows: tuple[LivingTailRow, ...]
    latent_rows: tuple[LatentRow, ...]
    volume_rows: tuple[VolumeRow, ...]
    sizing_rows: tuple[SizingRow, ...]
    sizing_notes: tuple[str, ...]
    report_notes: tuple[str, ...]


@dataclass(frozen=True)
class ReportOptions:
    """Presentation options for the Org report table."""

    price_places: int = 5
    diff_places: int = 5
    money_places: int = 6
    pct_places: int = 4
    estimate_fees: bool = True
    show_pair_regimes: bool = False
    maker_fee_rate: Decimal = DEFAULT_MAKER_FEE_RATE
    taker_fee_rate: Decimal = DEFAULT_TAKER_FEE_RATE


@dataclass
class _FillAccumulator:
    client_order_id: str
    exchange_order_id: str | None
    exchange: str
    environment: str
    market_type: str
    symbol: str
    side: str
    total_quantity: Decimal = Decimal("0")
    weighted_price: Decimal = Decimal("0")
    fee: Decimal = Decimal("0")
    fill_count: int = 0
    fee_currencies: set[str] = field(default_factory=set)
    liquidity_roles: list[str] = field(default_factory=list)
    seen_fill_ids: set[int] = field(default_factory=set)

    def add_fill(
        self,
        *,
        fill_id: int,
        price: Decimal,
        quantity: Decimal,
        fee: Decimal | None,
        fee_currency: str | None,
        liquidity_role: str | None,
    ) -> None:
        if fill_id in self.seen_fill_ids:
            return
        self.seen_fill_ids.add(fill_id)
        self.fill_count += 1
        self.total_quantity += quantity
        self.weighted_price += price * quantity
        if fee is not None:
            self.fee += fee
        if fee_currency:
            self.fee_currencies.add(fee_currency)
        if liquidity_role:
            self.liquidity_roles.append(liquidity_role)

    def summary(self) -> DbFillSummary:
        price = (
            self.weighted_price / self.total_quantity
            if self.total_quantity
            else Decimal("0")
        )
        fee_currency = None
        if len(self.fee_currencies) == 1:
            fee_currency = next(iter(self.fee_currencies))
        elif len(self.fee_currencies) > 1:
            fee_currency = "mixed"
        return DbFillSummary(
            client_order_id=self.client_order_id,
            exchange_order_id=self.exchange_order_id,
            exchange=self.exchange,
            environment=self.environment,
            market_type=self.market_type,
            symbol=self.symbol,
            side=self.side,
            fill_count=self.fill_count,
            quantity=self.total_quantity,
            price=price,
            fee=self.fee,
            fee_currency=fee_currency,
            liquidity_role=_summarise_liquidity(self.liquidity_roles),
        )


@dataclass
class _VolumeAccumulator:
    pair_name: str
    route: RuntimeRoute
    fills: int = 0
    quantity: Decimal = Decimal("0")
    weighted_price: Decimal = Decimal("0")
    bot_usd_volume: Decimal = Decimal("0")
    life_seconds: list[Decimal] = field(default_factory=list)
    roi_percentages: list[Decimal] = field(default_factory=list)
    net_values: list[Decimal] = field(default_factory=list)

    def add_fill(
        self,
        summary: DbFillSummary,
        *,
        instrument: InstrumentSummary | None,
    ) -> None:
        self.fills += max(summary.fill_count, 1)
        self.quantity += summary.quantity
        self.weighted_price += summary.price * summary.quantity
        self.bot_usd_volume += _usd_notional(
            summary.price,
            summary.quantity,
            instrument,
        )

    @property
    def reference_price(self) -> Decimal | None:
        if self.quantity <= 0:
            return None
        return self.weighted_price / self.quantity

    def add_report_row(self, row: ReportRow) -> None:
        self.life_seconds.append(Decimal(row.life_seconds))
        if row.roi_percent is not None:
            self.roi_percentages.append(row.roi_percent)
        if row.net_usd is not None:
            self.net_values.append(row.net_usd)

    @property
    def average_life_seconds(self) -> Decimal | None:
        return _average(self.life_seconds)

    @property
    def average_roi_percent(self) -> Decimal | None:
        return _average(self.roi_percentages)

    @property
    def average_roi_per_hour_percent(self) -> Decimal | None:
        average_roi = self.average_roi_percent
        average_life = self.average_life_seconds
        if average_roi is None or average_life is None or average_life <= 0:
            return None
        return average_roi * Decimal("3600") / average_life

    @property
    def total_net_usd(self) -> Decimal | None:
        if not self.net_values:
            return None
        return sum(self.net_values, Decimal("0"))


_EVENT_RE = re.compile(
    r"^(?P<log_ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+) .*?/ "
    r"(?P<event>[A-Z0-9_-]+) \((?P<pair>[^)]+)\): (?P<body>.*)$"
)
_RUNTIME_METADATA_RE = re.compile(
    r"^(?P<log_ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+) .*?/ "
    r"(?P<default_exchange>\S+) runtime (?P<phase>preflight|ready) "
    r"routes=(?P<routes>\S+)(?P<body>.*)$"
)
_QTY_USD_EXCEPTION_RE = re.compile(
    r"Strategy '(?P<pair>[^']+)' qty U(?P<nominal>[^\s]+) is not placeable "
    r"at startup for (?P<route>[^:]+:[^:]+:[^:]+): (?P<body>.*)$"
)
_QTY_PCT_EXCEPTION_RE = re.compile(
    r"Strategy '(?P<pair>[^']+)' qty %(?P<percent>[^\s]+) is not placeable "
    r"at startup for (?P<route>[^:]+:[^:]+:[^:]+): (?P<body>.*)$"
)
_ENV_REF_RE = re.compile(r"\$\{([^}]+)\}")
_USD_FEE_CURRENCIES = {"usd", "usdt", "zfusd", "zusd"}
_REPORT_CODENAMES = (
    "almond",
    "apple",
    "apricot",
    "avocado",
    "banana",
    "cashew",
    "cherry",
    "coconut",
    "fig",
    "guava",
    "hazelnut",
    "kiwi",
    "lemon",
    "lime",
    "mango",
    "melon",
    "olive",
    "orange",
    "papaya",
    "peach",
    "pear",
    "pineapple",
    "plum",
    "walnut",
)
_EXCHANGE_CODES = {
    "kraken": "kr",
    "binance": "bin",
    "bitmex": "bmx",
}
_MARKET_CODES = {
    "futures": "f",
    "spot": "s",
    "margin": "m",
    "isolated_margin": "im",
}
_QUOTE_SUFFIXES = (
    "USDT",
    "USDC",
    "BUSD",
    "USD",
    "ZUSD",
    "EUR",
    "ZEUR",
    "BTC",
    "XBT",
    "ETH",
)
_BASE_ALIASES = {"BTC": "XBT"}


def parse_log_file(path: str | Path) -> dict[PairKey, PairLifecycle]:
    """Parse a Kolabi runtime log file into pair lifecycles."""

    return parse_run_log_file(path).lifecycles


def parse_log_text(text: str) -> dict[PairKey, PairLifecycle]:
    """Parse a Kolabi runtime log text into pair lifecycles."""

    return parse_run_log_text(text).lifecycles


def parse_run_log_file(path: str | Path) -> RunLogSnapshot:
    """Parse a Kolabi runtime log file into all reportable run state."""

    return parse_run_log_text(Path(path).read_text(encoding="utf-8", errors="replace"))


def parse_run_log_text(text: str) -> RunLogSnapshot:
    """Parse compact runtime lifecycle lines from text.

    The parser intentionally ignores unrelated log lines.  Required events are
    `HEAD_SENT`, `HEAD_ACK`/`LEASE_*`, `UPDATE`, and `AMEND_SENT`; private DB rows fill in exact
    prices, fees, and liquidity when the final report is built.  `METRICS`
    rows provide living-tail distance, and latent-head events provide the
    latest active not-yet-filled attempts.  Terminal `closed--living` and
    `closed--closed` updates can carry `0.0000` stop placeholders after the
    exchange has already filled the tail; those placeholders are ignored for
    amendment-diff calculations.
    """

    lifecycles: dict[PairKey, PairLifecycle] = {}
    tail_telemetry: dict[PairKey, TailTelemetry] = {}
    latent_attempts: dict[PairKey, LatentAttempt] = {}
    quantity_diagnostics: list[QuantityDiagnostic] = []
    market_snapshot: MarketSnapshot | None = None
    first_log_at: datetime | None = None
    last_log_at: datetime | None = None
    runtime_metadata = RuntimeMetadata()
    for raw_line in text.splitlines():
        parsed_runtime = _parse_runtime_metadata_line(raw_line)
        if parsed_runtime is not None:
            runtime_metadata = _merge_runtime_metadata(
                runtime_metadata,
                parsed_runtime,
            )
            log_time = parsed_runtime.started_at
            if log_time is not None:
                first_log_at = _earliest_time(first_log_at, log_time)
                last_log_at = _latest_time(last_log_at, log_time)
            continue
        quantity_diagnostic = _parse_quantity_exception_line(raw_line)
        if quantity_diagnostic is not None:
            quantity_diagnostics.append(quantity_diagnostic)
            continue
        match = _EVENT_RE.match(raw_line)
        if match is None:
            continue
        log_time = _parse_log_utc(match.group("log_ts"))
        first_log_at = _earliest_time(first_log_at, log_time)
        last_log_at = _latest_time(last_log_at, log_time)
        event = match.group("event")
        body = match.group("body")
        parsed_quantity = _parse_quantity_event(event, match.group("pair"), body)
        if parsed_quantity is not None:
            quantity_diagnostics.append(parsed_quantity)
            if event.startswith(("QTY_USD_", "QTY_PCT_")):
                continue
        key = _parse_pair_key(match.group("pair"))
        if key is None:
            continue
        lifecycle = lifecycles.setdefault(key, PairLifecycle(key=key))
        _record_pair_started(lifecycle, log_time)
        if event == "HEAD_SENT":
            _parse_head_sent(lifecycle, body)
            _parse_latent_head_sent(latent_attempts, key, body, log_time)
        elif event == "HEAD_ACK":
            _parse_head_ack(lifecycle, body)
            _parse_latent_head_ack(latent_attempts, key, body, log_time)
        elif event in {"LEASE_OPEN", "LEASE_CLOSED"}:
            _parse_lease_event(lifecycle, body)
        elif event == "UPDATE":
            _parse_update(lifecycle, body)
        elif event == "AMEND_SENT":
            _parse_amend_sent(lifecycle, body, log_time)
        elif event == "METRICS":
            parsed_market = _parse_tail_metrics(tail_telemetry, key, body, log_time)
            if parsed_market is not None and (
                market_snapshot is None
                or parsed_market.recorded_at >= market_snapshot.recorded_at
            ):
                market_snapshot = parsed_market
        elif event == "REPEAT_READY":
            _parse_repeat_ready(latent_attempts, key, body, log_time)
        elif event == "LATENT_TIMEOUT_ARMED":
            _parse_latent_timeout_armed(latent_attempts, key, body, log_time)
        elif event == "HEAD_VISIBILITY_TIMEOUT_ARMED":
            _parse_latent_head_visibility_timeout_armed(
                latent_attempts,
                key,
                body,
                log_time,
            )
        elif event == "HEAD_VISIBILITY_PENDING":
            _parse_latent_head_visibility_pending(latent_attempts, key, body, log_time)
        elif event == "HEAD_VISIBILITY_TIMEOUT":
            _parse_latent_head_visibility_timeout(latent_attempts, key, body, log_time)
        elif event.startswith("GATE_WAIT"):
            _parse_lifecycle_gate_wait(lifecycle, event, body)
            _parse_gate_wait(latent_attempts, key, event, body, log_time)
            parsed_market = _parse_gate_market_snapshot(event, body, log_time)
            if parsed_market is not None and (
                market_snapshot is None
                or parsed_market.recorded_at >= market_snapshot.recorded_at
            ):
                market_snapshot = parsed_market
        elif event in {
            "COMMAND_FAILED",
            "HEAD_CANCELLED",
            "HEAD_CANCEL_SENT",
            "HEAD_TIMEOUT",
            "LATENT_TIMEOUT",
        }:
            _mark_latent_ended(latent_attempts, key, event, body, log_time)
    return RunLogSnapshot(
        lifecycles=lifecycles,
        tail_telemetry=tail_telemetry,
        latent_attempts=latent_attempts,
        quantity_diagnostics=tuple(quantity_diagnostics),
        market_snapshot=market_snapshot,
        first_log_at=first_log_at,
        last_log_at=last_log_at,
        runtime_metadata=runtime_metadata,
    )


def fetch_fill_summaries(
    db_url: str,
    client_order_ids: Iterable[str],
    exchange_order_ids: Iterable[str] = (),
) -> dict[str, DbFillSummary]:
    """Fetch exact fill facts for the requested order identities.

    Multiple fills for the same order are combined with a quantity-weighted
    average price and summed fees.  Liquidity is summarised as taker if any fill
    took liquidity, otherwise maker if any fill made liquidity.
    """

    ids = sorted({client_id for client_id in client_order_ids if client_id})
    exchange_ids = sorted(
        {exchange_id for exchange_id in exchange_order_ids if exchange_id}
    )
    if not ids and not exchange_ids:
        return {}
    predicates = []
    if ids:
        predicates.append(ExchangeOrder.client_order_id.in_(ids))
    if exchange_ids:
        predicates.append(ExchangeOrder.exchange_order_id.in_(exchange_ids))
    engine = create_engine(db_url, echo=False, future=True)
    try:
        with Session(engine) as session:
            rows = session.execute(
                select(ExchangeOrder, ExchangeFill)
                .join(ExchangeFill, ExchangeFill.order_id == ExchangeOrder.id)
                .where(or_(*predicates))
                .order_by(ExchangeFill.local_timestamp, ExchangeFill.id)
            ).all()
    except SQLAlchemyError as exc:
        raise ReportError(f"could not read local account DB: {_compact_error(exc)}") from exc
    finally:
        engine.dispose()

    accumulators: dict[tuple[str, str], _FillAccumulator] = {}
    for order, fill in rows:
        client_id = order.client_order_id or ""
        exchange_id = order.exchange_order_id or ""
        if not client_id and not exchange_id:
            continue
        key = (client_id, exchange_id)
        accumulator = accumulators.setdefault(
            key,
            _FillAccumulator(
                client_order_id=client_id,
                exchange_order_id=exchange_id or None,
                exchange=order.exchange,
                environment=order.environment,
                market_type=order.market_type,
                symbol=order.symbol,
                side=order.side,
            ),
        )
        accumulator.add_fill(
            fill_id=fill.id,
            price=_decimal(fill.price),
            quantity=_decimal(fill.quantity),
            fee=_optional_decimal(fill.fee),
            fee_currency=fill.fee_currency,
            liquidity_role=fill.liquidity_role,
        )
    summaries: dict[str, DbFillSummary] = {}
    for accumulator in accumulators.values():
        summary = accumulator.summary()
        for identity in _summary_identity_keys(summary):
            summaries[identity] = summary
    return summaries


def fetch_order_summaries(
    db_url: str,
    client_order_ids: Iterable[str],
    exchange_order_ids: Iterable[str] = (),
) -> dict[str, DbOrderSummary]:
    """Fetch latest local order state for requested order identities."""

    ids = sorted({client_id for client_id in client_order_ids if client_id})
    exchange_ids = sorted(
        {exchange_id for exchange_id in exchange_order_ids if exchange_id}
    )
    if not ids and not exchange_ids:
        return {}
    predicates = []
    if ids:
        predicates.append(ExchangeOrder.client_order_id.in_(ids))
    if exchange_ids:
        predicates.append(ExchangeOrder.exchange_order_id.in_(exchange_ids))
    engine = create_engine(db_url, echo=False, future=True)
    try:
        with Session(engine) as session:
            rows = session.execute(
                select(ExchangeOrder)
                .where(or_(*predicates))
                .order_by(ExchangeOrder.local_timestamp, ExchangeOrder.id)
            ).scalars()
            summaries: dict[str, DbOrderSummary] = {}
            for order in rows:
                client_id = order.client_order_id or ""
                exchange_id = order.exchange_order_id or ""
                if not client_id and not exchange_id:
                    continue
                summary = DbOrderSummary(
                    client_order_id=client_id,
                    exchange_order_id=exchange_id or None,
                    side=order.side,
                    status=order.status,
                    price=_optional_decimal(order.price),
                    quantity=_decimal(order.quantity),
                    filled_quantity=_decimal(order.filled_quantity),
                )
                for identity in _summary_identity_keys(summary):
                    summaries[identity] = summary
    except SQLAlchemyError as exc:
        raise ReportError(f"could not read local account DB: {_compact_error(exc)}") from exc
    finally:
        engine.dispose()
    return summaries


def fetch_instrument_summaries(
    db_url: str,
    routes: Iterable[RuntimeRoute],
    *,
    environment: str | None = None,
) -> dict[RuntimeRoute, InstrumentSummary]:
    """Fetch locally cached instrument sizing rules for the report routes."""

    wanted = tuple(sorted(set(routes)))
    if not wanted:
        return {}
    exchanges = tuple(sorted({route.exchange for route in wanted}))
    market_types = tuple(sorted({route.market_type for route in wanted}))
    symbols = tuple(sorted({route.symbol for route in wanted}))
    wanted_keys = {
        (route.exchange.lower(), route.market_type.lower(), route.symbol): route
        for route in wanted
    }
    engine = create_engine(db_url, echo=False, future=True)
    try:
        with Session(engine) as session:
            statement = (
                select(ExchangeInstrument)
                .where(
                    ExchangeInstrument.exchange.in_(exchanges),
                    ExchangeInstrument.market_type.in_(market_types),
                    ExchangeInstrument.symbol.in_(symbols),
                )
                .order_by(ExchangeInstrument.updated_at, ExchangeInstrument.id)
            )
            if environment:
                statement = statement.where(ExchangeInstrument.environment == environment)
            rows = session.execute(statement).scalars()
            summaries: dict[RuntimeRoute, InstrumentSummary] = {}
            for row in rows:
                key = (
                    row.exchange.lower(),
                    row.market_type.lower(),
                    row.symbol,
                )
                route = wanted_keys.get(key)
                if route is None:
                    continue
                summaries[route] = _instrument_summary_from_row(row, route)
    except SQLAlchemyError as exc:
        raise ReportError(f"could not read local market DB: {_compact_error(exc)}") from exc
    finally:
        engine.dispose()
    return summaries


def fetch_market_volumes(
    db_url: str,
    routes: Iterable[RuntimeRoute],
    *,
    started_at: datetime,
    ended_at: datetime,
    environment: str | None = None,
    instrument_summaries: Mapping[RuntimeRoute, InstrumentSummary] | None = None,
) -> dict[RuntimeRoute, MarketVolumeSummary]:
    """Aggregate local public trade volume for each route.

    The market DB is an optional local witness.  Only raw public trade events
    that already exist on disk inside the run window are counted.
    """

    wanted = tuple(sorted(set(routes)))
    if not wanted:
        return {}
    exchanges = tuple(sorted({route.exchange for route in wanted}))
    market_types = tuple(sorted({route.market_type for route in wanted}))
    symbols = tuple(sorted({route.symbol for route in wanted}))
    wanted_keys = {
        (route.exchange.lower(), route.market_type.lower(), route.symbol): route
        for route in wanted
    }
    instrument_summaries = instrument_summaries or {}
    totals: dict[RuntimeRoute, MarketVolumeSummary] = {}
    engine = create_engine(db_url, echo=False, future=True)
    try:
        with Session(engine) as session:
            statement = (
                select(RawExchangeEvent)
                .where(
                    RawExchangeEvent.exchange.in_(exchanges),
                    RawExchangeEvent.market_type.in_(market_types),
                    RawExchangeEvent.symbol.in_(symbols),
                    RawExchangeEvent.received_at >= _as_utc(started_at),
                    RawExchangeEvent.received_at <= _as_utc(ended_at),
                    or_(
                        RawExchangeEvent.account_scope.is_(None),
                        RawExchangeEvent.account_scope == "public",
                    ),
                )
                .order_by(RawExchangeEvent.received_at, RawExchangeEvent.id)
            )
            if environment:
                statement = statement.where(RawExchangeEvent.environment == environment)
            for event in session.execute(statement).scalars():
                if not _raw_event_is_public_trade(event):
                    continue
                key = (
                    event.exchange.lower(),
                    str(event.market_type or "").lower(),
                    str(event.symbol or ""),
                )
                route = wanted_keys.get(key)
                if route is None:
                    continue
                instrument = instrument_summaries.get(route)
                for price, quantity in _raw_trade_price_quantities(event.payload):
                    previous = totals.get(
                        route,
                        MarketVolumeSummary(
                            base_volume=Decimal("0"),
                            usd_volume=Decimal("0"),
                        ),
                    )
                    totals[route] = MarketVolumeSummary(
                        base_volume=previous.base_volume + quantity,
                        usd_volume=previous.usd_volume
                        + _usd_notional(
                        price,
                        quantity,
                        instrument,
                        ),
                    )
    except SQLAlchemyError as exc:
        raise ReportError(f"could not read local market DB: {_compact_error(exc)}") from exc
    finally:
        engine.dispose()
    return totals


def fetch_account_available_usd(
    db_url: str,
    routes: Iterable[RuntimeRoute],
    *,
    environment: str | None = None,
) -> dict[RuntimeRoute, Decimal]:
    """Fetch latest locally persisted USD availability for each route exchange."""

    wanted = tuple(sorted(set(routes)))
    if not wanted:
        return {}
    exchanges = tuple(sorted({route.exchange for route in wanted}))
    latest_by_exchange: dict[str, Decimal] = {}
    engine = create_engine(db_url, echo=False, future=True)
    try:
        with Session(engine) as session:
            statement = (
                select(AccountBalance)
                .where(
                    AccountBalance.exchange.in_(exchanges),
                    AccountBalance.asset.in_(("USD", "USDT", "ZFUSD", "ZUSD")),
                )
                .order_by(AccountBalance.local_timestamp, AccountBalance.id)
            )
            if environment:
                statement = statement.where(AccountBalance.environment == environment)
            for balance in session.execute(statement).scalars():
                latest_by_exchange[balance.exchange.lower()] = _decimal(balance.available)
    except SQLAlchemyError as exc:
        raise ReportError(f"could not read local account balances: {_compact_error(exc)}") from exc
    finally:
        engine.dispose()
    return {
        route: available
        for route in wanted
        if (available := latest_by_exchange.get(route.exchange.lower())) is not None
    }


def build_report_rows(
    lifecycles: Mapping[PairKey, PairLifecycle],
    *,
    fill_summaries: Mapping[str, DbFillSummary] | None = None,
    order_summaries: Mapping[str, DbOrderSummary] | None = None,
    instrument_summaries: Mapping[RuntimeRoute, InstrumentSummary] | None = None,
    default_routes: Sequence[RuntimeRoute] = (),
    require_db: bool = False,
    options: ReportOptions | None = None,
) -> tuple[ReportRow, ...]:
    """Build head-fill-sorted rows and a running cumulative net value."""

    options = options or ReportOptions()
    fill_summaries = fill_summaries or {}
    order_summaries = order_summaries or {}
    instrument_summaries = instrument_summaries or {}
    rows: list[ReportRow] = []
    cumulative_net: Decimal | None = Decimal("0")
    for lifecycle in sorted(
        (item for item in lifecycles.values() if item.terminated),
        key=lambda item: (
            item.head_fill.filled_at
            if item.head_fill
            else datetime.max.replace(tzinfo=timezone.utc),
            item.key,
        ),
    ):
        row = _build_report_row(
            lifecycle,
            fill_summaries,
            order_summaries,
            instrument_summaries=instrument_summaries,
            default_routes=default_routes,
            require_db=require_db,
            options=options,
        )
        if row.net_usd is None:
            cumulative = None
            cumulative_net = None
        elif cumulative_net is None:
            cumulative = None
        else:
            cumulative_net += row.net_usd
            cumulative = cumulative_net
        rows.append(
            ReportRow(
                key=row.key,
                pair_started_at=row.pair_started_at,
                head_wait_seconds=row.head_wait_seconds,
                gate_reference_price=row.gate_reference_price,
                head_fill_at=row.head_fill_at,
                tail_fill_at=row.tail_fill_at,
                tail_placed_at=row.tail_placed_at,
                life_seconds=row.life_seconds,
                side=row.side,
                head_price=row.head_price,
                tail_price=row.tail_price,
                quantity=row.quantity,
                liquidity=row.liquidity,
                head_source=row.head_source,
                tail_source=row.tail_source,
                amend_count=row.amend_count,
                tail_amend_1_at=row.tail_amend_1_at,
                tail_amend_2_at=row.tail_amend_2_at,
                amend_logbps=row.amend_logbps,
                gross_usd=row.gross_usd,
                net_usd=row.net_usd,
                net_estimated=row.net_estimated,
                roi_percent=row.roi_percent,
                roi_per_hour_percent=row.roi_per_hour_percent,
                cumulative_net=cumulative,
                route=row.route,
                pnl_kind=row.pnl_kind,
                pnl_currency=row.pnl_currency,
                gross_native=row.gross_native,
                net_native=row.net_native,
                fees_usd=row.fees_usd,
                entry_notional_usd=row.entry_notional_usd,
                finance_quality=row.finance_quality,
            )
        )
    return tuple(rows)


def render_org_table(
    rows: Sequence[ReportRow],
    *,
    options: ReportOptions | None = None,
) -> str:
    """Render report rows as an aligned Org table."""

    options = options or ReportOptions()
    cumulative_header = "Cum est net" if any(row.net_estimated for row in rows) else "Cum net"
    headers = (
        "Start UTC",
        "H wait",
        "Ref",
        "H fill UTC",
        "T fill UTC",
        "Pair",
        "Side",
        "Hfill",
        "Tfill",
        "Qty",
        "Liq",
        "A#",
        "Tamend1",
        "Tamend2",
        "Amd logbps",
        "Gross USD",
        "Net USD",
        "ROI %",
        "ROI/h %",
        cumulative_header,
    )
    pair_name_width = max((len(row.key.name) for row in rows), default=4)
    pair_attempt_width = max((len(f"#{row.key.attempt}") for row in rows), default=2)
    body = [
        (
            _format_optional_time(row.pair_started_at),
            _format_optional_life_seconds(row.head_wait_seconds),
            _format_optional_decimal(row.gate_reference_price, options.price_places),
            _format_time(row.head_fill_at),
            _format_time(row.tail_fill_at),
            _format_pair(row.key, pair_name_width, pair_attempt_width),
            row.side,
            _format_fill_price(row.head_price, options.price_places),
            _format_fill_price(row.tail_price, options.price_places),
            _format_quantity(row.quantity),
            row.liquidity,
            str(row.amend_count),
            _format_optional_clock_time(row.tail_amend_1_at),
            _format_optional_clock_time(row.tail_amend_2_at),
            _format_logbps_optional(row.amend_logbps),
            _format_signed(row.gross_usd, options.money_places),
            _format_signed_optional(row.net_usd, options.money_places),
            _format_signed_optional(row.roi_percent, options.pct_places),
            _format_signed_optional(row.roi_per_hour_percent, options.pct_places),
            _format_signed_optional(row.cumulative_net, options.money_places),
        )
        for row in rows
    ]
    align_right = {
        "H wait",
        "Ref",
        "Hfill",
        "Tfill",
        "Qty",
        "A#",
        "Amd logbps",
        "Gross USD",
        "Net USD",
        "ROI %",
        "ROI/h %",
        cumulative_header,
    }
    return _format_table(headers, body, align_right=align_right)


def render_terminated_summary_table(
    rows: Sequence[ReportRow],
    *,
    options: ReportOptions | None = None,
) -> str:
    """Render a compact stats table for terminated-pair numeric columns."""

    options = options or ReportOptions()
    headers = (
        "Stat",
        "Life",
        "Hfill",
        "Tfill",
        "Qty",
        "Position",
        "A#",
        "AmendLife",
        "amendLogbps",
        "Gross USD",
        "Net USD",
        "ROI %",
        "ROI/h %",
    )
    stats = ("min", "max", "median", "mode", "average")
    body = [
        (
            stat,
            _format_stat_life(_stat_value(_row_life_values(rows), stat)),
            _format_stat_decimal(
                _stat_value((row.head_price for row in rows), stat),
                options.price_places,
            ),
            _format_stat_decimal(
                _stat_value((row.tail_price for row in rows), stat),
                options.price_places,
            ),
            _format_stat_decimal(
                _stat_value((row.quantity for row in rows), stat),
                options.diff_places,
            ),
            _format_stat_decimal(
                _stat_value(_position_values(rows), stat),
                options.diff_places,
            ),
            _format_stat_decimal(
                _stat_value((Decimal(row.amend_count) for row in rows), stat),
                2,
            ),
            _format_stat_life(_stat_value(_row_amend_phase_values(rows), stat)),
            _format_logbps_optional(
                _stat_value(_optional_values(row.amend_logbps for row in rows), stat)
            ),
            _format_stat_decimal(
                _stat_value((row.gross_usd for row in rows), stat),
                options.money_places,
                signed=True,
            ),
            _format_stat_decimal(
                _stat_value(_optional_values(row.net_usd for row in rows), stat),
                options.money_places,
                signed=True,
            ),
            _format_stat_decimal(
                _stat_value(_optional_values(row.roi_percent for row in rows), stat),
                options.pct_places,
                signed=True,
            ),
            _format_stat_decimal(
                _summary_roi_per_hour(rows, stat),
                options.pct_places,
                signed=True,
            ),
        )
        for stat in stats
    ]
    return _format_table(headers, body, align_right=set(headers) - {"Stat"})


def _summary_roi_per_hour(
    rows: Sequence[ReportRow],
    stat: str,
) -> Decimal | None:
    if stat != "average":
        return _stat_value(
            _optional_values(row.roi_per_hour_percent for row in rows),
            stat,
        )
    roi = _average([row.roi_percent for row in rows if row.roi_percent is not None])
    life = _average(
        [Decimal(row.life_seconds) for row in rows if row.roi_percent is not None]
    )
    if roi is None or life is None or life <= 0:
        return None
    return roi * Decimal("3600") / life


def build_living_tail_rows(
    lifecycles: Mapping[PairKey, PairLifecycle],
    *,
    fill_summaries: Mapping[str, DbFillSummary] | None = None,
    order_summaries: Mapping[str, DbOrderSummary] | None = None,
    tail_telemetry: Mapping[PairKey, TailTelemetry] | None = None,
    instrument_summaries: Mapping[RuntimeRoute, InstrumentSummary] | None = None,
    default_routes: Sequence[RuntimeRoute] = (),
    snapshot_at: datetime | None = None,
) -> tuple[LivingTailRow, ...]:
    """Build rows for head-filled pairs whose tail is still flying."""

    fill_summaries = fill_summaries or {}
    order_summaries = order_summaries or {}
    tail_telemetry = tail_telemetry or {}
    instrument_summaries = instrument_summaries or {}
    rows: list[LivingTailRow] = []
    for lifecycle in sorted(
        (
            item
            for item in lifecycles.values()
            if item.head_fill is not None
            and item.tail_fill is None
            and item.tail_client_id is not None
        ),
        key=lambda item: item.head_fill.filled_at if item.head_fill else datetime.min,
    ):
        head_summary = _fill_summary_for(
            lifecycle.head_client_id,
            lifecycle.head_exchange_order_id,
            fill_summaries,
        )
        tail_order = _order_summary_for(
            lifecycle.tail_client_id,
            lifecycle.tail_exchange_order_id,
            order_summaries,
        )
        route = (
            _fill_summary_route(head_summary)
            if head_summary is not None
            else default_routes[0]
            if len(default_routes) == 1
            else None
        )
        instrument = instrument_summaries.get(route) if route is not None else None
        telemetry = tail_telemetry.get(lifecycle.key)
        head_fill = lifecycle.head_fill
        if head_fill is None:
            continue
        head_price = head_summary.price if head_summary is not None else head_fill.price
        quantity = (
            head_summary.quantity
            if head_summary is not None and head_summary.quantity
            else head_fill.quantity
        )
        tail_stop = lifecycle.latest_tail_stop
        if tail_stop is None and telemetry is not None:
            tail_stop = telemetry.stop_price
        age_seconds = 0
        if snapshot_at is not None:
            age_seconds = int(
                (
                    snapshot_at.replace(microsecond=0)
                    - head_fill.filled_at.replace(microsecond=0)
                ).total_seconds()
            )
        rows.append(
            LivingTailRow(
                key=lifecycle.key,
                head_fill_at=head_fill.filled_at,
                age_seconds=age_seconds,
                side=_side_abbrev(head_fill.side, _opposite_side(head_fill.side)),
                head_price=head_price,
                quantity=quantity,
                head_liquidity=_liquidity_abbrev(
                    head_summary.liquidity_role if head_summary is not None else None
                ),
                tail_stop=tail_stop,
                reference_price=telemetry.reference_price if telemetry else None,
                current_distance_logbps=(
                    _signed_logbps_or_none(telemetry.reference_price, tail_stop)
                    if telemetry is not None
                    else None
                ),
                tail_status=tail_order.status if tail_order is not None else "",
                tail_filled_quantity=(
                    tail_order.filled_quantity if tail_order is not None else None
                ),
                route=route,
                entry_notional_usd=entry_notional_usd(
                    head_price,
                    quantity,
                    route=route,
                    instrument=instrument,
                ),
            )
        )
    return tuple(rows)


def render_living_tail_table(
    rows: Sequence[LivingTailRow],
    *,
    options: ReportOptions | None = None,
) -> str:
    """Render living tail-flying rows as an aligned Org table."""

    options = options or ReportOptions()
    headers = (
        "H fill UTC",
        "Age",
        "Pair",
        "Side",
        "Hfill",
        "Qty",
        "Hliq",
        "Tstop",
        "Dist uBlk",
        "T status",
        "T filled",
    )
    pair_name_width = max((len(row.key.name) for row in rows), default=4)
    pair_attempt_width = max((len(f"#{row.key.attempt}") for row in rows), default=2)
    body = [
        (
            _format_time(row.head_fill_at),
            _format_life(row.age_seconds),
            _format_pair(row.key, pair_name_width, pair_attempt_width),
            row.side,
            _format_fill_price(row.head_price, options.price_places),
            _format_quantity(row.quantity),
            row.head_liquidity,
            _format_optional_decimal(row.tail_stop, options.price_places),
            _format_logbps_optional(row.current_distance_logbps),
            row.tail_status,
            _format_optional_quantity(row.tail_filled_quantity),
        )
        for row in rows
    ]
    return _format_table(
        headers,
        body,
        align_right={"Hfill", "Qty", "Tstop", "Dist uBlk", "T filled"},
    )


def build_latent_rows(
    lifecycles: Mapping[PairKey, PairLifecycle],
    latent_attempts: Mapping[PairKey, LatentAttempt],
) -> tuple[LatentRow, ...]:
    """Build rows for latest active latent/head-pending attempts."""

    latest_by_name: dict[str, LatentAttempt] = {}
    for attempt in latent_attempts.values():
        current = latest_by_name.get(attempt.key.name)
        if current is None or attempt.key.attempt > current.key.attempt:
            latest_by_name[attempt.key.name] = attempt

    rows: list[LatentRow] = []
    for attempt in sorted(latest_by_name.values(), key=lambda item: item.key):
        lifecycle = lifecycles.get(attempt.key)
        if attempt.ended:
            continue
        if lifecycle is not None and lifecycle.head_fill is not None:
            continue
        rows.append(
            LatentRow(
                key=attempt.key,
                time=attempt.last_event_at,
                status=_latent_status(attempt),
                gate=_latent_gate_display(attempt),
                reference_price=attempt.reference_price,
                head_price=(
                    attempt.head_price
                    if attempt.head_price is not None
                    else attempt.head_price_spec
                ),
                quantity=attempt.quantity,
                order_type=attempt.order_type,
                deadline_at=attempt.deadline_at,
                last_event=attempt.last_event,
            )
        )
    return tuple(rows)


def render_latent_table(
    rows: Sequence[LatentRow],
    *,
    options: ReportOptions | None = None,
) -> str:
    """Render latest active latent rows as an aligned Org table."""

    options = options or ReportOptions()
    headers = (
        "Time",
        "Pair",
        "Status",
        "Gate",
        "H price",
        "Qty",
        "Type",
        "Deadline",
        "Last event",
    )
    pair_name_width = max((len(row.key.name) for row in rows), default=4)
    pair_attempt_width = max((len(f"#{row.key.attempt}") for row in rows), default=2)
    body = [
        (
            _format_time(row.time),
            _format_pair(row.key, pair_name_width, pair_attempt_width),
            row.status,
            row.gate,
            _format_optional_decimal(row.head_price, options.price_places),
            _format_optional_quantity(row.quantity),
            row.order_type,
            _format_latent_deadline(row),
            row.last_event,
        )
        for row in rows
    ]
    return _format_table(
        headers,
        body,
        align_right={"H price", "Qty"},
    )


def build_pair_evolution_rows(
    lifecycles: Mapping[PairKey, PairLifecycle],
    latent_attempts: Mapping[PairKey, LatentAttempt],
    report_rows: Sequence[ReportRow] = (),
) -> tuple[PairEvolutionRow, ...]:
    """Build initial and changed tOut/hPrice states by pair and attempt."""

    reports_by_key = {row.key: row for row in report_rows}
    attempts_by_name: dict[str, list[LatentAttempt]] = {}
    for attempt in latent_attempts.values():
        if attempt.parameters_observed:
            attempts_by_name.setdefault(attempt.key.name, []).append(attempt)

    rows: list[PairEvolutionRow] = []
    for pair_name in sorted(attempts_by_name):
        previous: tuple[Decimal | None, Decimal | None] | None = None
        for attempt in sorted(
            attempts_by_name[pair_name],
            key=lambda item: item.key.attempt,
        ):
            current = (attempt.timeout_minutes, attempt.head_price_spec)
            if previous is not None and current == previous:
                continue
            change = _pair_parameter_change(previous, current)
            report_row = reports_by_key.get(attempt.key)
            lifecycle = lifecycles.get(attempt.key)
            rows.append(
                PairEvolutionRow(
                    key=attempt.key,
                    change=change,
                    timeout_minutes=attempt.timeout_minutes,
                    head_price_spec=attempt.head_price_spec,
                    terminal=_pair_evolution_terminal(
                        attempt,
                        lifecycle,
                        report_row,
                    ),
                    head_fill_at=(
                        report_row.head_fill_at
                        if report_row is not None
                        else lifecycle.head_fill.filled_at
                        if lifecycle is not None and lifecycle.head_fill is not None
                        else None
                    ),
                    tail_fill_at=(
                        report_row.tail_fill_at if report_row is not None else None
                    ),
                    net_usd=report_row.net_usd if report_row is not None else None,
                    roi_percent=(
                        report_row.roi_percent if report_row is not None else None
                    ),
                )
            )
            previous = current
    return tuple(rows)


def build_parameter_regimes(
    lifecycles: Mapping[PairKey, PairLifecycle],
    latent_attempts: Mapping[PairKey, LatentAttempt],
    report_rows: Sequence[ReportRow] = (),
) -> tuple[ParameterRegime, ...]:
    """Group all observed attempts into contiguous parameter regimes."""

    reports_by_key = {row.key: row for row in report_rows}
    attempts_by_name: dict[str, list[LatentAttempt]] = {}
    for attempt in latent_attempts.values():
        if attempt.parameters_observed:
            attempts_by_name.setdefault(attempt.key.name, []).append(attempt)

    regimes: list[ParameterRegime] = []
    for pair_name in sorted(attempts_by_name):
        current: list[PairEvolutionRow] = []
        current_parameters: tuple[Decimal | None, Decimal | None] | None = None
        for attempt in sorted(attempts_by_name[pair_name], key=lambda item: item.key.attempt):
            parameters = (attempt.timeout_minutes, attempt.head_price_spec)
            if current and parameters != current_parameters:
                regimes.append(_parameter_regime(pair_name, current))
                current = []
            current_parameters = parameters
            current.append(
                _attempt_outcome_row(
                    attempt,
                    lifecycles.get(attempt.key),
                    reports_by_key.get(attempt.key),
                )
            )
        if current:
            regimes.append(_parameter_regime(pair_name, current))
    return tuple(regimes)


def _attempt_outcome_row(
    attempt: LatentAttempt,
    lifecycle: PairLifecycle | None,
    report_row: ReportRow | None,
) -> PairEvolutionRow:
    return PairEvolutionRow(
        key=attempt.key,
        change="",
        timeout_minutes=attempt.timeout_minutes,
        head_price_spec=attempt.head_price_spec,
        terminal=_pair_evolution_terminal(attempt, lifecycle, report_row),
        head_fill_at=(
            report_row.head_fill_at
            if report_row is not None
            else lifecycle.head_fill.filled_at
            if lifecycle is not None and lifecycle.head_fill is not None
            else None
        ),
        tail_fill_at=report_row.tail_fill_at if report_row is not None else None,
        net_usd=report_row.net_usd if report_row is not None else None,
        roi_percent=report_row.roi_percent if report_row is not None else None,
    )


def _parameter_regime(
    pair_name: str,
    outcomes: Sequence[PairEvolutionRow],
) -> ParameterRegime:
    net_values = [row.net_usd for row in outcomes if row.net_usd is not None]
    roi_values = [row.roi_percent for row in outcomes if row.roi_percent is not None]
    wins = sum(1 for value in roi_values if value >= 0)
    losses = sum(1 for value in roi_values if value < 0)
    first = outcomes[0]
    last = outcomes[-1]
    return ParameterRegime(
        pair_name=pair_name,
        first_attempt=first.key.attempt,
        last_attempt=last.key.attempt,
        timeout_minutes=first.timeout_minutes,
        head_price_spec=first.head_price_spec,
        attempts=len(outcomes),
        timeouts=sum(1 for row in outcomes if row.terminal == "tOut"),
        closed=wins + losses,
        wins=wins,
        losses=losses,
        total_net_usd=(sum(net_values, Decimal("0")) if net_values else None),
        average_roi_percent=_average(roi_values),
        final_state=last.terminal,
        outcomes=tuple(outcomes),
    )


def build_financial_overview(
    rows: Sequence[ReportRow],
    living_rows: Sequence[LivingTailRow] = (),
) -> FinancialOverview:
    """Aggregate decision-facing realised finance for the run overview."""

    net_values = [row.net_usd for row in rows if row.net_usd is not None]
    gross_values = [row.gross_usd for row in rows if row.finance_quality != EvidenceQuality.UNAVAILABLE]
    fee_values = [row.fees_usd for row in rows if row.fees_usd is not None]
    roi_values = [row.roi_percent for row in rows if row.roi_percent is not None]
    life_values = [Decimal(row.life_seconds) for row in rows if row.roi_percent is not None]
    notional_values = [
        row.entry_notional_usd
        for row in rows
        if row.entry_notional_usd is not None and row.net_usd is not None
    ]
    wins = sum(1 for value in net_values if value >= 0)
    losses = sum(1 for value in net_values if value < 0)
    gains = sum((value for value in net_values if value > 0), Decimal("0"))
    losses_abs = -sum((value for value in net_values if value < 0), Decimal("0"))
    average_roi = _average(roi_values)
    average_life = _average(life_values)
    total_net = sum(net_values, Decimal("0")) if net_values else None
    total_notional = sum(notional_values, Decimal("0")) if notional_values else None
    return FinancialOverview(
        closed=len(rows),
        wins=wins,
        losses=losses,
        gross_usd=sum(gross_values, Decimal("0")) if gross_values else None,
        fees_usd=sum(fee_values, Decimal("0")) if fee_values else None,
        net_usd=total_net,
        win_rate_percent=(
            Decimal(wins) / Decimal(wins + losses) * Decimal("100")
            if wins + losses
            else None
        ),
        profit_factor=(gains / losses_abs if losses_abs > 0 else None),
        average_roi_percent=average_roi,
        aggregate_roi_percent=(
            total_net / total_notional * Decimal("100")
            if total_net is not None and total_notional
            else None
        ),
        roi_per_hour_percent=(
            average_roi * Decimal("3600") / average_life
            if average_roi is not None and average_life
            else None
        ),
        max_drawdown_usd=_maximum_drawdown(net_values),
        open_notional_usd=(
            sum(
                (
                    row.entry_notional_usd
                    for row in living_rows
                    if row.entry_notional_usd is not None
                ),
                Decimal("0"),
            )
            if any(row.entry_notional_usd is not None for row in living_rows)
            else None
        ),
        exact_rows=sum(1 for row in rows if row.finance_quality == EvidenceQuality.EXACT),
        estimated_rows=sum(
            1
            for row in rows
            if row.finance_quality in {EvidenceQuality.ESTIMATED, EvidenceQuality.ASSUMED}
        ),
        unavailable_rows=sum(
            1 for row in rows if row.finance_quality == EvidenceQuality.UNAVAILABLE
        ),
    )


def _maximum_drawdown(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    cumulative = Decimal("0")
    peak = Decimal("0")
    drawdown = Decimal("0")
    for value in values:
        cumulative += value
        peak = max(peak, cumulative)
        drawdown = max(drawdown, peak - cumulative)
    return drawdown


def render_pair_evolution_table(
    rows: Sequence[PairEvolutionRow],
    *,
    options: ReportOptions | None = None,
) -> str:
    """Render the compact tOut/hPrice evolution history as an Org table."""

    options = options or ReportOptions()
    headers = (
        "Pair",
        "Attempt",
        "Change",
        "tOut",
        "hPrice",
        "Terminal",
        "H fill UTC",
        "T fill UTC",
        "Net USD",
        "ROI %",
    )
    body = [
        (
            row.key.name,
            str(row.key.attempt),
            row.change,
            _format_parameter_value(row.timeout_minutes),
            _format_parameter_value(row.head_price_spec),
            row.terminal,
            _format_optional_time(row.head_fill_at),
            _format_optional_time(row.tail_fill_at),
            _format_signed_optional(row.net_usd, options.money_places),
            _format_signed_optional(row.roi_percent, options.pct_places),
        )
        for row in rows
    ]
    return _format_table(
        headers,
        body,
        align_right={"Attempt", "tOut", "hPrice", "Net USD", "ROI %"},
    )


def render_financial_overview_table(
    overview: FinancialOverview,
    *,
    options: ReportOptions | None = None,
) -> str:
    """Render the compact decision-facing run finance summary."""

    options = options or ReportOptions()
    headers = (
        "Closed",
        "W/L",
        "Win %",
        "Gross USD",
        "Fees USD",
        "Net USD",
        "Profit factor",
        "Avg ROI %",
        "Agg ROI %",
        "ROI/h %",
        "Max DD USD",
        "Open USD",
        "Evidence E/E/U",
    )
    body = (
        (
            str(overview.closed),
            f"{overview.wins}/{overview.losses}",
            _format_optional_decimal(overview.win_rate_percent, 2),
            _format_signed_optional(overview.gross_usd, options.money_places),
            _format_optional_decimal(overview.fees_usd, options.money_places),
            _format_signed_optional(overview.net_usd, options.money_places),
            _format_optional_decimal(overview.profit_factor, 3),
            _format_signed_optional(overview.average_roi_percent, options.pct_places),
            _format_signed_optional(overview.aggregate_roi_percent, options.pct_places),
            _format_signed_optional(overview.roi_per_hour_percent, options.pct_places),
            _format_optional_decimal(overview.max_drawdown_usd, options.money_places),
            _format_optional_decimal(overview.open_notional_usd, options.money_places),
            f"{overview.exact_rows}/{overview.estimated_rows}/{overview.unavailable_rows}",
        ),
    )
    return _format_table(headers, body, align_right=set(headers) - {"W/L", "Evidence E/E/U"})


def render_parameter_regime_table(
    rows: Sequence[ParameterRegime],
    *,
    options: ReportOptions | None = None,
) -> str:
    """Render one compact row per contiguous parameter regime."""

    options = options or ReportOptions()
    headers = (
        "Pair",
        "Attempts",
        "tOut",
        "hPrice",
        "N",
        "tOuts",
        "Closed",
        "W/L",
        "Net USD",
        "Avg ROI %",
        "Final",
    )
    body = [
        (
            row.pair_name,
            _format_attempt_range(row.first_attempt, row.last_attempt),
            _format_parameter_value(row.timeout_minutes),
            _format_parameter_value(row.head_price_spec),
            str(row.attempts),
            str(row.timeouts),
            str(row.closed),
            f"{row.wins}/{row.losses}",
            _format_signed_optional(row.total_net_usd, options.money_places),
            _format_signed_optional(row.average_roi_percent, options.pct_places),
            row.final_state,
        )
        for row in rows
    ]
    return _format_table(
        headers,
        body,
        align_right={"tOut", "hPrice", "N", "tOuts", "Closed", "Net USD", "Avg ROI %"},
    )


def render_parameter_regime_details(
    regimes: Sequence[ParameterRegime],
    *,
    options: ReportOptions | None = None,
) -> str:
    """Render foldable Org details for material attempts in each pair."""

    options = options or ReportOptions()
    sections: list[str] = []
    by_pair: dict[str, list[PairEvolutionRow]] = {}
    for regime in regimes:
        material = [
            outcome
            for index, outcome in enumerate(regime.outcomes)
            if index == 0
            or index == len(regime.outcomes) - 1
            or outcome.terminal != "tOut"
        ]
        by_pair.setdefault(regime.pair_name, []).extend(material)
    for pair_name in sorted(by_pair):
        sections.extend(
            (
                f"**** {pair_name}",
                render_pair_evolution_table(by_pair[pair_name], options=options),
            )
        )
    return "\n".join(sections)


def _format_attempt_range(first: int, last: int) -> str:
    return str(first) if first == last else f"{first}-{last}"


def _pair_parameter_change(
    previous: tuple[Decimal | None, Decimal | None] | None,
    current: tuple[Decimal | None, Decimal | None],
) -> str:
    if previous is None:
        return "initial"
    timeout_changed = previous[0] != current[0]
    head_price_changed = previous[1] != current[1]
    if timeout_changed and head_price_changed:
        return "both"
    if timeout_changed:
        return "tOut"
    return "hPrice"


def _pair_evolution_terminal(
    attempt: LatentAttempt,
    lifecycle: PairLifecycle | None,
    report_row: ReportRow | None,
) -> str:
    if report_row is not None:
        if report_row.roi_percent is None:
            return "closed roi?"
        return "roi>=0" if report_row.roi_percent >= 0 else "roi<0"
    if lifecycle is not None and lifecycle.head_fill is not None:
        return "living"
    if attempt.timed_out:
        return "tOut"
    if not attempt.ended:
        return "active"
    if attempt.terminal_event == "COMMAND_FAILED":
        return "failed"
    return "cancelled"


def build_volume_rows(
    lifecycles: Mapping[PairKey, PairLifecycle],
    *,
    fill_summaries: Mapping[str, DbFillSummary] | None = None,
    instrument_summaries: Mapping[RuntimeRoute, InstrumentSummary] | None = None,
    market_volumes: Mapping[RuntimeRoute, MarketVolumeSummary] | None = None,
    report_rows: Sequence[ReportRow] = (),
) -> tuple[VolumeRow, ...]:
    """Aggregate bot and market volume by strategy pair and route."""

    fill_summaries = fill_summaries or {}
    instrument_summaries = instrument_summaries or {}
    market_volumes = market_volumes or {}
    accumulators: dict[tuple[str, RuntimeRoute], _VolumeAccumulator] = {}
    for lifecycle in sorted(lifecycles.values(), key=lambda item: item.key):
        for client_id, exchange_order_id in (
            (lifecycle.head_client_id, lifecycle.head_exchange_order_id),
            (lifecycle.tail_client_id, lifecycle.tail_exchange_order_id),
        ):
            summary = _fill_summary_for(
                client_id,
                exchange_order_id,
                fill_summaries,
            )
            if summary is None:
                continue
            route = _fill_summary_route(summary)
            instrument = instrument_summaries.get(route)
            key = (lifecycle.key.name, route)
            accumulator = accumulators.setdefault(
                key,
                _VolumeAccumulator(pair_name=lifecycle.key.name, route=route),
            )
            accumulator.add_fill(summary, instrument=instrument)

    for row in report_rows:
        lifecycle = lifecycles.get(row.key)
        if lifecycle is None:
            continue
        route = _route_for_lifecycle_fills(lifecycle, fill_summaries)
        if route is None:
            continue
        accumulator = accumulators.get((row.key.name, route))
        if accumulator is not None:
            accumulator.add_report_row(row)

    rows: list[VolumeRow] = []
    for accumulator in sorted(
        accumulators.values(),
        key=lambda item: (item.route.label, item.pair_name),
    ):
        instrument = instrument_summaries.get(accumulator.route)
        reference_price = accumulator.reference_price
        min_qty_base = instrument.min_quantity if instrument is not None else None
        tick_base = _instrument_quantity_tick(instrument)
        market_volume = market_volumes.get(accumulator.route)
        rows.append(
            VolumeRow(
                pair_name=accumulator.pair_name,
                market=accumulator.route.label,
                fills=accumulator.fills,
                quantity=accumulator.quantity,
                bot_usd_volume=accumulator.bot_usd_volume,
                average_life_seconds=accumulator.average_life_seconds,
                average_roi_percent=accumulator.average_roi_percent,
                average_roi_per_hour_percent=accumulator.average_roi_per_hour_percent,
                market_base_volume=(
                    market_volume.base_volume if market_volume is not None else None
                ),
                market_usd_volume=(
                    market_volume.usd_volume if market_volume is not None else None
                ),
                min_qty_base=min_qty_base,
                min_qty_usd=_quantity_usd_value(
                    min_qty_base,
                    reference_price=reference_price,
                    instrument=instrument,
                ),
                tick_base=tick_base,
                tick_usd=_quantity_usd_value(
                    tick_base,
                    reference_price=reference_price,
                    instrument=instrument,
                ),
                closed=len(accumulator.life_seconds),
                net_usd=accumulator.total_net_usd,
            )
        )
    return tuple(rows)


def render_volume_table(
    rows: Sequence[VolumeRow],
    *,
    options: ReportOptions | None = None,
) -> str:
    """Render volume and local sizing context by strategy pair and market."""

    options = options or ReportOptions()
    headers = (
        "Market",
        "Pair",
        "Closed",
        "Fills",
        "Executed Qty",
        "USD Turnover",
        "Net USD",
        "Avg Life",
        "Avg ROI %",
        "Avg ROI/h %",
        "Mkt Base Vol",
        "Mkt USD Vol",
    )
    body = [
        (
            row.market,
            row.pair_name,
            str(row.closed),
            str(row.fills),
            _format_quantity(row.quantity),
            _format_decimal(row.bot_usd_volume, options.money_places),
            _format_signed_optional(row.net_usd, options.money_places),
            _format_optional_life(row.average_life_seconds),
            _format_signed_optional(row.average_roi_percent, options.pct_places),
            _format_signed_optional(
                row.average_roi_per_hour_percent,
                options.pct_places,
            ),
            _format_optional_quantity_word(row.market_base_volume),
            _format_optional_money_word(row.market_usd_volume, options.money_places),
        )
        for row in sorted(rows, key=_volume_row_sort_key)
    ]
    return _format_table(
        headers,
        body,
        align_right=set(headers) - {"Pair", "Market"},
    )


def _volume_row_sort_key(row: VolumeRow) -> tuple[bool, Decimal, str, str]:
    roi_per_hour = row.average_roi_per_hour_percent
    if roi_per_hour is None:
        return (True, Decimal("0"), row.market, row.pair_name)
    return (False, -roi_per_hour, row.market, row.pair_name)


def build_sizing_rows(
    routes: Iterable[RuntimeRoute],
    *,
    quantity_diagnostics: Sequence[QuantityDiagnostic] = (),
    instrument_summaries: Mapping[RuntimeRoute, InstrumentSummary] | None = None,
    market_snapshot: MarketSnapshot | None = None,
    market_volumes: Mapping[RuntimeRoute, MarketVolumeSummary] | None = None,
    account_available_usd: Mapping[RuntimeRoute, Decimal] | None = None,
) -> tuple[SizingRow, ...]:
    """Build operator sizing rows from runtime logs and cached instruments."""

    instrument_summaries = instrument_summaries or {}
    market_volumes = market_volumes or {}
    account_available_usd = account_available_usd or {}
    rows: list[SizingRow] = []
    for diagnostic in quantity_diagnostics:
        instrument = (
            instrument_summaries.get(diagnostic.route)
            if diagnostic.route is not None
            else None
        )
        market_volume = (
            market_volumes.get(diagnostic.route)
            if diagnostic.route is not None
            else None
        )
        contract_size = diagnostic.contract_size or (
            instrument.contract_size if instrument is not None else None
        )
        rows.append(
            SizingRow(
                route=diagnostic.route.label if diagnostic.route is not None else "-",
                pair_name=diagnostic.pair_name or "-",
                nominal_usd=diagnostic.nominal_usd,
                percent=diagnostic.percent,
                available_usd=_diagnostic_available_usd(
                    diagnostic,
                    account_available_usd,
                ),
                mark_price=diagnostic.mark_price,
                contract_size=contract_size,
                min_quantity=diagnostic.min_quantity,
                quantity_step=diagnostic.quantity_step,
                min_usd=_sizing_usd(
                    diagnostic.min_quantity,
                    mark_price=diagnostic.mark_price,
                    contract_size=contract_size,
                ),
                step_usd=_sizing_usd(
                    diagnostic.quantity_step,
                    mark_price=diagnostic.mark_price,
                    contract_size=contract_size,
                ),
                resolved_quantity=diagnostic.resolved_quantity,
                resolved_usd=diagnostic.resolved_usd
                or _sizing_usd(
                    diagnostic.resolved_quantity,
                    mark_price=diagnostic.mark_price,
                    contract_size=contract_size,
                ),
                market_base_volume=(
                    market_volume.base_volume if market_volume is not None else None
                ),
                market_usd_volume=(
                    market_volume.usd_volume if market_volume is not None else None
                ),
                status=diagnostic.status,
                source=diagnostic.source,
            )
        )

    diagnostic_marks = _diagnostic_marks_by_route(quantity_diagnostics)
    route_set = set(routes)
    route_set.update(instrument_summaries)
    route_set.update(account_available_usd)
    route_set.update(market_volumes)
    for route in sorted(route_set):
        instrument = instrument_summaries.get(route)
        market_volume = market_volumes.get(route)
        mark_price = (
            diagnostic_marks.get(route)
            or _market_snapshot_reference_price(market_snapshot)
        )
        min_quantity = instrument.min_quantity if instrument is not None else None
        quantity_step = _instrument_quantity_tick(instrument)
        contract_size = instrument.contract_size if instrument is not None else None
        rows.append(
            SizingRow(
                route=route.label,
                pair_name="-",
                nominal_usd=None,
                percent=None,
                available_usd=account_available_usd.get(route),
                mark_price=mark_price,
                contract_size=contract_size,
                min_quantity=min_quantity,
                quantity_step=quantity_step,
                min_usd=_sizing_usd(
                    min_quantity,
                    mark_price=mark_price,
                    contract_size=contract_size,
                ),
                step_usd=_sizing_usd(
                    quantity_step,
                    mark_price=mark_price,
                    contract_size=contract_size,
                ),
                resolved_quantity=None,
                resolved_usd=None,
                market_base_volume=(
                    market_volume.base_volume if market_volume is not None else None
                ),
                market_usd_volume=(
                    market_volume.usd_volume if market_volume is not None else None
                ),
                status="cached",
                source="market_db",
            )
        )
    return tuple(rows)


def render_sizing_table(
    rows: Sequence[SizingRow],
    *,
    options: ReportOptions | None = None,
) -> str:
    """Render minimum/step sizing diagnostics as an aligned Org table."""

    options = options or ReportOptions()
    headers = (
        "Route",
        "Avail USD",
        "Mark",
        "Contract",
        "Min Qty",
        "Step Qty",
        "Min USD",
        "Step USD",
        "Mkt Base Vol",
        "Mkt USD Vol",
    )
    body = [
        (
            row.route,
            _format_optional_money_word(row.available_usd, options.money_places),
            _format_optional_money_word(row.mark_price, options.money_places),
            _format_optional_quantity_word(row.contract_size),
            _format_optional_quantity_word(row.min_quantity),
            _format_optional_quantity_word(row.quantity_step),
            _format_optional_money_word(row.min_usd, options.money_places),
            _format_optional_money_word(row.step_usd, options.money_places),
            _format_optional_quantity_word(row.market_base_volume),
            _format_optional_money_word(row.market_usd_volume, options.money_places),
        )
        for row in rows
    ]
    return _format_table(
        headers,
        body,
        align_right=set(headers) - {"Route"},
    )


def render_market_snapshot_table(
    snapshot: MarketSnapshot | None,
    *,
    options: ReportOptions | None = None,
) -> str:
    """Render the latest parsed mark and market prices as a one-row table."""

    options = options or ReportOptions()
    headers = (
        "Latest prices",
        "Mark",
        "Last",
        "Spread",
        "Max spread",
        "Bid",
        "Ask",
        "Mid",
        "Index",
        "Src",
    )
    if snapshot is None:
        body = (("unavailable", "", "", "", "", "", "", "", "", ""),)
    else:
        body = (
            (
                _format_time(snapshot.recorded_at),
                _format_optional_price_word(snapshot.mark_price, options.price_places),
                _format_optional_price_word(snapshot.last_price, options.price_places),
                _format_optional_price_word(
                    _snapshot_spread(snapshot), options.price_places
                ),
                _format_optional_price_word(snapshot.spread_guard, options.price_places),
                _format_optional_price_word(snapshot.bid_price, options.price_places),
                _format_optional_price_word(snapshot.ask_price, options.price_places),
                _format_optional_price_word(snapshot.mid_price, options.price_places),
                _format_optional_price_word(snapshot.index_price, options.price_places),
                snapshot.source or "-",
            ),
        )
    return _format_table(
        headers,
        body,
        align_right=set(headers) - {"Latest prices", "Src"},
    )


def render_run_report(
    terminated_rows: Sequence[ReportRow],
    living_rows: Sequence[LivingTailRow],
    latent_rows: Sequence[LatentRow],
    volume_rows: Sequence[VolumeRow] = (),
    *,
    evolution_rows: Sequence[PairEvolutionRow] = (),
    regimes: Sequence[ParameterRegime] = (),
    financial_overview: FinancialOverview | None = None,
    sizing_rows: Sequence[SizingRow] = (),
    sizing_notes: Sequence[str] = (),
    report_notes: Sequence[str] = (),
    market_snapshot: MarketSnapshot | None = None,
    report_at: datetime | None = None,
    identity: ReportIdentity | None = None,
    options: ReportOptions | None = None,
) -> str:
    """Render the full operator report as Org sections."""

    options = options or ReportOptions()
    timestamp = _report_timestamp(
        terminated_rows,
        living_rows,
        latent_rows,
        market_snapshot=market_snapshot,
        report_at=report_at,
    )
    sections = [
        _format_org_heading(timestamp, report_name=identity.name if identity else None),
        "** Overview",
        (
            render_financial_overview_table(financial_overview, options=options)
            if financial_overview is not None
            else ""
        ),
        render_market_snapshot_table(market_snapshot, options=options),
        _render_optional_summary(terminated_rows, options=options),
        "*** Volume by market/pair",
        _render_section_table(render_volume_table, volume_rows, options=options),
    ]
    sections.extend(
        (
            "",
            "*** Sizing diagnostics",
            _render_section_table(render_sizing_table, sizing_rows, options=options),
        )
    )
    sizing_note = _render_sizing_note(sizing_rows, sizing_notes)
    if sizing_note:
        sections.append(sizing_note)
    rendered_report_notes = _render_report_notes(report_notes)
    if rendered_report_notes:
        sections.append(rendered_report_notes)
    sections.extend(
        (
            "",
            "** Terminated pairs",
            render_terminated_counts_line(terminated_rows),
            "",
            _render_section_table(render_org_table, terminated_rows, options=options),
        )
    )
    if options.show_pair_regimes:
        sections.extend(
            (
                "",
                "*** Pair parameter regimes",
                (
                    _render_section_table(
                        render_parameter_regime_table,
                        regimes,
                        options=options,
                    )
                    if regimes
                    else _render_section_table(
                        render_pair_evolution_table,
                        evolution_rows,
                        options=options,
                    )
                ),
            )
        )
        if regimes:
            sections.extend(
                (
                    "",
                    "*** Pair attempt details",
                    render_parameter_regime_details(regimes, options=options),
                )
            )
    sections.extend(
        (
            "",
            "** Living tail-flying pairs",
            _render_section_table(render_living_tail_table, living_rows, options=options),
            "",
            "** Latest latent pairs",
            _render_section_table(render_latent_table, latent_rows, options=options),
        )
    )
    latent_gate_note = _render_latent_gate_note(latent_rows)
    if latent_gate_note:
        sections.append(latent_gate_note)
    latent_deadline_note = _render_latent_deadline_note(latent_rows)
    if latent_deadline_note:
        sections.append(latent_deadline_note)
    if identity is not None:
        sections.insert(1, _format_report_provenance(identity))
    return "\n".join(sections)


def build_run_report(
    log_path: str | Path,
    *,
    db_url: str | None = None,
    market_db_url: str | None = None,
    log_only: bool = False,
    report_command: str | None = None,
    run_started_at: datetime | None = None,
    options: ReportOptions | None = None,
) -> RunReport:
    """Build the renderer-independent report from a runtime log and local DBs."""

    options = options or ReportOptions()
    log_path = Path(log_path)
    snapshot = parse_run_log_file(log_path)
    resolved_run_started_at = run_started_at or _report_run_started_at(snapshot, log_path)
    identity = build_report_identity(
        snapshot.runtime_metadata,
        log_path=log_path,
        command_line=report_command or f"kolabi-run-report {shlex.quote(str(log_path))}",
        run_started_at=resolved_run_started_at,
    )
    fill_summaries: Mapping[str, DbFillSummary] = {}
    order_summaries: Mapping[str, DbOrderSummary] = {}
    instrument_summaries: Mapping[RuntimeRoute, InstrumentSummary] = {}
    market_volumes: Mapping[RuntimeRoute, MarketVolumeSummary] = {}
    account_available_usd: Mapping[RuntimeRoute, Decimal] = {}
    sizing_notes: list[str] = []
    report_notes: list[str] = []
    if not log_only:
        if not db_url:
            raise ReportError(
                "account DB URL is required for exact reports; pass --account-db-url "
                "or --log-only"
            )
        client_ids, exchange_order_ids = _order_identity_ids(
            snapshot.lifecycles.values()
        )
        fill_summaries = fetch_fill_summaries(
            db_url,
            client_ids,
            exchange_order_ids,
        )
        order_summaries = fetch_order_summaries(
            db_url,
            client_ids,
            exchange_order_ids,
        )
    routes = _routes_for_report(
        snapshot.runtime_metadata.routes,
        fill_summaries=fill_summaries.values(),
        quantity_diagnostics=snapshot.quantity_diagnostics,
    )
    if not log_only and db_url and routes:
        try:
            account_available_usd = fetch_account_available_usd(
                db_url,
                routes,
                environment=snapshot.runtime_metadata.environment,
            )
        except ReportError:
            sizing_notes.append(
                "Account DB unavailable for available USD; showing runtime log values only."
            )
    if market_db_url and routes:
        try:
            instrument_summaries = fetch_instrument_summaries(
                market_db_url,
                routes,
                environment=snapshot.runtime_metadata.environment,
            )
        except ReportError:
            sizing_notes.append(
                "Market DB unavailable for sizing; showing runtime log values only."
            )
        else:
            try:
                market_volumes = fetch_market_volumes(
                    market_db_url,
                    routes,
                    started_at=resolved_run_started_at,
                    ended_at=snapshot.last_log_at or datetime.now(timezone.utc),
                    environment=snapshot.runtime_metadata.environment,
                    instrument_summaries=instrument_summaries,
                )
            except ReportError:
                sizing_notes.append(
                    "Market DB unavailable for market volume; Mkt Base Vol and Mkt USD Vol may be n/a."
                )
    terminated_rows = build_report_rows(
        snapshot.lifecycles,
        fill_summaries=fill_summaries,
        order_summaries=order_summaries,
        instrument_summaries=instrument_summaries,
        default_routes=routes,
        require_db=not log_only,
        options=options,
    )
    if not log_only:
        report_notes.extend(
            _fill_fallback_report_notes(
                snapshot.lifecycles.values(),
                fill_summaries,
                order_summaries,
                options=options,
            )
        )
    living_rows = build_living_tail_rows(
        snapshot.lifecycles,
        fill_summaries=fill_summaries,
        order_summaries=order_summaries,
        tail_telemetry=snapshot.tail_telemetry,
        instrument_summaries=instrument_summaries,
        default_routes=routes,
        snapshot_at=snapshot.last_log_at,
    )
    latent_rows = build_latent_rows(snapshot.lifecycles, snapshot.latent_attempts)
    regimes = build_parameter_regimes(
        snapshot.lifecycles,
        snapshot.latent_attempts,
        terminated_rows,
    )
    sizing_rows = build_sizing_rows(
        routes,
        quantity_diagnostics=snapshot.quantity_diagnostics,
        instrument_summaries=instrument_summaries,
        market_snapshot=snapshot.market_snapshot,
        market_volumes=market_volumes,
        account_available_usd=account_available_usd,
    )
    volume_rows = build_volume_rows(
        snapshot.lifecycles,
        fill_summaries=fill_summaries,
        instrument_summaries=instrument_summaries,
        market_volumes=market_volumes,
        report_rows=terminated_rows,
    )
    report_at = (
        snapshot.market_snapshot.recorded_at
        if snapshot.market_snapshot is not None
        else snapshot.last_log_at
        or resolved_run_started_at
    )
    return RunReport(
        identity=identity,
        report_at=report_at,
        market_snapshot=snapshot.market_snapshot,
        financial_overview=build_financial_overview(terminated_rows, living_rows),
        regimes=regimes,
        terminated_rows=terminated_rows,
        living_rows=living_rows,
        latent_rows=latent_rows,
        volume_rows=volume_rows,
        sizing_rows=sizing_rows,
        sizing_notes=tuple(sizing_notes),
        report_notes=tuple(report_notes),
    )


def render_org_report(
    report: RunReport,
    *,
    options: ReportOptions | None = None,
) -> str:
    """Render one shared report model as the established Org interface."""

    return render_run_report(
        report.terminated_rows,
        report.living_rows,
        report.latent_rows,
        report.volume_rows,
        regimes=report.regimes,
        financial_overview=report.financial_overview,
        sizing_rows=report.sizing_rows,
        sizing_notes=report.sizing_notes,
        report_notes=report.report_notes,
        market_snapshot=report.market_snapshot,
        report_at=report.report_at,
        identity=report.identity,
        options=options,
    )


def build_report_table(
    log_path: str | Path,
    *,
    db_url: str | None = None,
    market_db_url: str | None = None,
    log_only: bool = False,
    report_command: str | None = None,
    run_started_at: datetime | None = None,
    options: ReportOptions | None = None,
) -> str:
    """Build and render the backwards-compatible Org report."""

    options = options or ReportOptions()
    report = build_run_report(
        log_path,
        db_url=db_url,
        market_db_url=market_db_url,
        log_only=log_only,
        report_command=report_command,
        run_started_at=run_started_at,
        options=options,
    )
    return render_org_report(report, options=options)


def build_report_identity(
    metadata: RuntimeMetadata,
    *,
    log_path: str | Path,
    command_line: str,
    run_started_at: datetime,
) -> ReportIdentity:
    """Build the stable report identity used in headings and provenance."""

    return ReportIdentity(
        name=_runtime_report_name(metadata, Path(log_path)),
        run_started_at=_as_utc(run_started_at),
        command_line=command_line,
    )


def resolve_account_db_url(
    explicit_url: str | None,
    *,
    env_file: str | Path | None,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Resolve the account DB URL from CLI, environment, then env file."""

    if explicit_url:
        return explicit_url
    env_mapping = env or os.environ
    if env_mapping.get("KOLABI_ACCOUNT_DB_URL"):
        return env_mapping["KOLABI_ACCOUNT_DB_URL"]
    if env_file is None:
        return None
    values = _load_env_file(Path(env_file), env=env_mapping)
    return values.get("KOLABI_ACCOUNT_DB_URL")


def resolve_market_db_url(
    explicit_url: str | None,
    *,
    env_file: str | Path | None,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Resolve the market DB URL from CLI, environment, then env file."""

    if explicit_url:
        return explicit_url
    env_mapping = env or os.environ
    if env_mapping.get("KOLABI_MARKET_DB_URL"):
        return env_mapping["KOLABI_MARKET_DB_URL"]
    if env_file is None:
        return None
    values = _load_env_file(Path(env_file), env=env_mapping)
    return values.get("KOLABI_MARKET_DB_URL")


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser for `kolabi-run-report`."""

    parser = argparse.ArgumentParser(
        prog="kolabi-run-report",
        description=(
            "Generate aligned Org report tables for terminated, living "
            "tail-flying, and latest latent Kolabi pairs."
        ),
        epilog=(
            "Source order: logs provide lifecycle timing, amendments, tail "
            "telemetry, and latent gate events; the local account DB provides "
            "exact fills, fees, maker/taker roles, and open-tail state. Use "
            "--log-only only when DB rows are unavailable. ROI is return on "
            "head notional."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("log_file", help="Kolabi runtime log file to parse.")
    parser.add_argument(
        "--account-db-url",
        help="Local account DB URL used for exact fill, fee, and liquidity data.",
    )
    parser.add_argument(
        "--market-db-url",
        help=(
            "Local market DB URL used for cached instrument sizing and public "
            "trade-volume data."
        ),
    )
    parser.add_argument(
        "--env-file",
        default=".env.postgres",
        help=(
            "Env file to read KOLABI_ACCOUNT_DB_URL and KOLABI_MARKET_DB_URL "
            "from when needed."
        ),
    )
    parser.add_argument(
        "--log-only",
        action="store_true",
        help="Use only log data. DB-only columns are left blank where needed.",
    )
    parser.add_argument(
        "--estimate-fees",
        dest="estimate_fees",
        action="store_true",
        default=True,
        help="Estimate fees for order-only or log-only legs.",
    )
    parser.add_argument(
        "--no-estimate-fees",
        dest="estimate_fees",
        action="store_false",
        help="Leave net and cumulative net blank when exact USD fees are missing.",
    )
    parser.add_argument(
        "--output",
        "--ouput",
        "-o",
        help="Prepend the Org report to this file instead of stdout.",
    )
    parser.add_argument(
        "--html-output",
        help="Write a self-contained interactive HTML report to this path.",
    )
    parser.add_argument(
        "--pair-regimes",
        action="store_true",
        help="Show pair parameter regimes and per-attempt details.",
    )
    parser.add_argument(
        "--strategy",
        help=(
            "Strategy file to copy under the report entry when --output is used. "
            "If omitted, the report tries runtime metadata, then orders/<log-stem>.org."
        ),
    )
    parser.add_argument(
        "--price-dp",
        type=int,
        default=5,
        help="Decimal places for Hfill and Tfill.",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    raw_argv = tuple(argv if argv is not None else sys.argv[1:])
    command_line = _format_command_line(_report_program_name(argv), raw_argv)
    args = build_parser().parse_args(raw_argv)
    try:
        db_url = None
        market_db_url = None
        if not args.log_only:
            db_url = resolve_account_db_url(args.account_db_url, env_file=args.env_file)
            market_db_url = resolve_market_db_url(
                args.market_db_url,
                env_file=args.env_file,
            )
        options = ReportOptions(
            price_places=args.price_dp,
            estimate_fees=args.estimate_fees,
            show_pair_regimes=args.pair_regimes,
        )
        report = build_run_report(
            args.log_file,
            db_url=db_url,
            market_db_url=market_db_url,
            log_only=args.log_only,
            report_command=command_line,
            options=options,
        )
        table = render_org_report(report, options=options)
        if args.output:
            strategy_path = _resolve_strategy_copy_path(
                explicit_path=args.strategy,
                log_path=Path(args.log_file),
            )
            if strategy_path is not None:
                table = _append_strategy_copy(table, strategy_path)
            _prepend_output(Path(args.output), table)
        else:
            print(table, file=out)
        if args.html_output:
            from kolabi.bot.run_report_html import render_html_report

            _write_atomic(
                Path(args.html_output),
                render_html_report(
                    report,
                    show_pair_regimes=args.pair_regimes,
                ),
            )
    except ReportError as exc:
        print(f"kolabi-run-report: {exc}", file=err)
        return 2
    except OSError as exc:
        print(f"kolabi-run-report: {exc}", file=err)
        return 2
    return 0


def _write_atomic(path: Path, content: str) -> None:
    """Replace one generated artifact without exposing a partial file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _parse_pair_key(raw: str) -> PairKey | None:
    if "#" not in raw:
        return None
    name, attempt = raw.rsplit("#", 1)
    try:
        return PairKey(name=name, attempt=int(attempt))
    except ValueError:
        return None


def _parse_runtime_metadata_line(raw_line: str) -> RuntimeMetadata | None:
    match = _RUNTIME_METADATA_RE.match(raw_line)
    if match is None:
        return None
    routes = _parse_runtime_routes(match.group("routes"))
    body = match.group("body")
    environment = _parse_runtime_environment(body)
    strategy_path = _parse_runtime_strategy_path(body)
    return RuntimeMetadata(
        started_at=_parse_log_utc(match.group("log_ts")),
        environment=environment,
        strategy_path=strategy_path,
        routes=routes,
    )


def _parse_runtime_routes(raw_routes: str) -> tuple[RuntimeRoute, ...]:
    routes: list[RuntimeRoute] = []
    for raw_route in raw_routes.split(","):
        fields = raw_route.strip().split(":", 2)
        if len(fields) != 3:
            continue
        exchange, market_type, symbol = (field.strip() for field in fields)
        if not exchange or not market_type or not symbol:
            continue
        routes.append(
            RuntimeRoute(
                exchange=exchange.lower(),
                market_type=market_type.lower(),
                symbol=symbol,
            )
        )
    return tuple(routes)


def _parse_runtime_environment(body: str) -> str | None:
    return _parse_runtime_token(body, "env")


def _parse_runtime_strategy_path(body: str) -> str | None:
    return _parse_runtime_token(body, "strategy")


def _parse_runtime_token(body: str, key: str) -> str | None:
    prefix = f"{key}="
    for token in body.split():
        if token.startswith(prefix):
            value = token.split("=", 1)[1].strip()
            return value or None
    return None


def _merge_runtime_metadata(
    current: RuntimeMetadata,
    candidate: RuntimeMetadata,
) -> RuntimeMetadata:
    started_at = current.started_at
    if started_at is None or (
        candidate.started_at is not None and candidate.started_at < started_at
    ):
        started_at = candidate.started_at
    return RuntimeMetadata(
        started_at=started_at,
        environment=current.environment or candidate.environment,
        strategy_path=current.strategy_path or candidate.strategy_path,
        routes=current.routes or candidate.routes,
    )


def _parse_quantity_event(
    event: str,
    raw_pair: str,
    body: str,
) -> QuantityDiagnostic | None:
    if event in {
        "QTY_USD_READY",
        "QTY_USD_RESOLVED",
        "QTY_PCT_READY",
        "QTY_PCT_RESOLVED",
    }:
        fields = body.split()
        route = _parse_runtime_route_label(fields[0]) if fields else None
        nominal = _quantity_nominal_from_token(fields[1]) if len(fields) >= 2 else None
        values = _parse_key_value_fields(body)
        if nominal is None:
            nominal = _decimal_field(values, "nominal_usd", "nominal")
        return QuantityDiagnostic(
            pair_name=_pair_name_from_raw(raw_pair),
            route=route,
            status="resolved" if event.endswith("_RESOLVED") else "ready",
            nominal_usd=nominal,
            mark_price=_decimal_field(values, "mark"),
            contract_size=_decimal_field(values, "contract", "contract_size"),
            min_quantity=_decimal_field(values, "min", "min_quantity"),
            quantity_step=_decimal_field(values, "step", "quantity_step"),
            resolved_quantity=_decimal_field(values, "qty", "resolved"),
            resolved_usd=_decimal_field(values, "usd"),
            percent=(
                _quantity_percent_from_token(fields[1])
                if len(fields) >= 2
                else None
            ),
            available_usd=_decimal_field(values, "available", "available_usd"),
        )
    if "QTY_USD_TOO_SMALL" not in body and "QTY_PCT_TOO_SMALL" not in body:
        return None
    values = _parse_key_value_fields(body)
    pair_name = values.get("pair") or _pair_name_from_raw(raw_pair)
    route = _parse_runtime_route_label(values.get("route", ""))
    return QuantityDiagnostic(
        pair_name=pair_name,
        route=route,
        status="too_small",
        nominal_usd=_decimal_field(values, "nominal_usd"),
        mark_price=_decimal_field(values, "mark"),
        contract_size=_decimal_field(values, "contract", "contract_size"),
        min_quantity=_decimal_field(values, "min", "min_quantity"),
        quantity_step=_decimal_field(values, "step", "quantity_step"),
        resolved_quantity=_decimal_field(values, "qty", "resolved"),
        resolved_usd=_decimal_field(values, "usd"),
        percent=_decimal_field(values, "percent"),
        available_usd=_decimal_field(values, "available", "available_usd"),
    )


def _parse_quantity_exception_line(raw_line: str) -> QuantityDiagnostic | None:
    if "QTY_USD_TOO_SMALL" not in raw_line and "QTY_PCT_TOO_SMALL" not in raw_line:
        return None
    match = _QTY_USD_EXCEPTION_RE.search(raw_line)
    percent_match = None
    if match is None:
        percent_match = _QTY_PCT_EXCEPTION_RE.search(raw_line)
        if percent_match is None:
            return None
        match = percent_match
    values = _parse_key_value_fields(match.group("body"))
    nominal = (
        _optional_decimal_token(match.group("nominal"))
        if percent_match is None
        else _decimal_field(values, "nominal_usd", "nominal")
    )
    return QuantityDiagnostic(
        pair_name=values.get("pair") or match.group("pair"),
        route=_parse_runtime_route_label(match.group("route")),
        status="too_small",
        nominal_usd=_decimal_field(values, "nominal_usd") or nominal,
        mark_price=_decimal_field(values, "mark"),
        contract_size=_decimal_field(values, "contract", "contract_size"),
        min_quantity=_decimal_field(values, "min", "min_quantity"),
        quantity_step=_decimal_field(values, "step", "quantity_step"),
        resolved_quantity=_decimal_field(values, "qty", "resolved"),
        resolved_usd=_decimal_field(values, "usd"),
        percent=(
            None
            if percent_match is None
            else _optional_decimal_token(match.group("percent"))
        ),
        available_usd=_decimal_field(values, "available", "available_usd"),
    )


def _parse_key_value_fields(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for raw_token in text.split():
        token = raw_token.strip().strip(",;")
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        fields[key.strip()] = value.strip().strip(",;")
    return fields


def _decimal_field(values: Mapping[str, str], *keys: str) -> Decimal | None:
    for key in keys:
        if key not in values:
            continue
        parsed = _optional_decimal_token(values[key])
        if parsed is not None:
            return parsed
    return None


def _quantity_nominal_from_token(token: str) -> Decimal | None:
    if not token.startswith("U"):
        return None
    return _optional_decimal_token(token[1:])


def _quantity_percent_from_token(token: str) -> Decimal | None:
    if not token.startswith("%"):
        return None
    return _optional_decimal_token(token[1:])


def _optional_decimal_token(token: str) -> Decimal | None:
    text = token.strip().strip(",;")
    if not text or text == "-":
        return None
    try:
        return _decimal(text)
    except Exception:
        return None


def _pair_name_from_raw(raw_pair: str) -> str:
    return raw_pair.split("#", 1)[0]


def _parse_runtime_route_label(raw: str | None) -> RuntimeRoute | None:
    if not raw:
        return None
    fields = raw.strip().strip(",;").split(":", 2)
    if len(fields) != 3:
        return None
    exchange, market_type, symbol = (field.strip() for field in fields)
    if not exchange or not market_type or not symbol:
        return None
    return RuntimeRoute(
        exchange=exchange.lower(),
        market_type=market_type.lower(),
        symbol=symbol,
    )


def _parse_head_sent(lifecycle: PairLifecycle, body: str) -> None:
    fields = body.split()
    if fields:
        lifecycle.head_client_id = fields[0]


def _parse_head_ack(lifecycle: PairLifecycle, body: str) -> None:
    fields = body.split()
    if fields:
        lifecycle.head_client_id = fields[0]
    if len(fields) >= 2:
        lifecycle.head_exchange_order_id = fields[1]


def _record_pair_started(lifecycle: PairLifecycle, log_time: datetime) -> None:
    if lifecycle.started_at is None or log_time < lifecycle.started_at:
        lifecycle.started_at = log_time


def _parse_lifecycle_gate_wait(
    lifecycle: PairLifecycle,
    event: str,
    body: str,
) -> None:
    if event != "GATE_WAIT-2":
        return
    fields = body.split()
    if len(fields) < 3:
        return
    reference_price = _optional_positive_decimal(fields[2])
    if reference_price is None:
        return
    status = fields[0]
    if lifecycle.gate_reference_price is None or status == "ready":
        lifecycle.gate_reference_price = reference_price


def _parse_lease_event(lifecycle: PairLifecycle, body: str) -> None:
    fields = body.split()
    if len(fields) < 2:
        return
    role = fields[0]
    client_id = _identity_field(fields[1])
    exchange_order_id = _identity_field(fields[2]) if len(fields) >= 3 else None
    if role == "head":
        if not _client_id_matches_attempt(client_id, lifecycle.key, "H"):
            return
        if client_id is not None:
            lifecycle.head_client_id = client_id
        if exchange_order_id is not None:
            lifecycle.head_exchange_order_id = exchange_order_id
    elif role == "tail":
        if not _client_id_matches_attempt(client_id, lifecycle.key, "T"):
            return
        if client_id is not None:
            lifecycle.tail_client_id = client_id
        if exchange_order_id is not None:
            lifecycle.tail_exchange_order_id = exchange_order_id


def _identity_field(value: str) -> str | None:
    stripped = value.strip()
    if not stripped or stripped in {"-", "PENDING_PLACE"}:
        return None
    return stripped


def _client_id_matches_attempt(
    client_id: str | None,
    key: PairKey,
    prefix: str,
) -> bool:
    if client_id is None:
        return True
    match = re.match(rf"^{re.escape(prefix)}(\d+)", client_id)
    if match is None:
        return True
    return int(match.group(1)) == key.attempt


def _parse_update(lifecycle: PairLifecycle, body: str) -> None:
    fields = body.split()
    if not fields:
        return
    state = fields[0]
    if state == "closed--hooked" and len(fields) >= 7:
        initial_tail_stop = _optional_positive_decimal(fields[2])
        if lifecycle.initial_tail_stop is None and initial_tail_stop is not None:
            lifecycle.initial_tail_stop = initial_tail_stop
        lifecycle.head_fill = FillLeg(
            side=fields[3],
            quantity=_decimal(fields[4]),
            price=_decimal(fields[5]),
            filled_at=_parse_iso_utc(fields[6]),
        )
    elif state == "closed--living" and len(fields) >= 7:
        confirmed_stop = _optional_positive_decimal(fields[2])
        desired_stop = _optional_positive_decimal(fields[3])
        if lifecycle.initial_tail_stop is None and confirmed_stop is not None:
            lifecycle.initial_tail_stop = confirmed_stop
        if desired_stop is not None:
            lifecycle.latest_tail_stop = desired_stop
        if lifecycle.tail_placed_at is None:
            lifecycle.tail_placed_at = _parse_iso_utc(fields[6])
        lifecycle.tail_client_id = fields[4]
        lifecycle.tail_exchange_order_id = fields[5]
    elif state == "closed--closed" and len(fields) >= 8:
        if lifecycle.tail_fill is None:
            lifecycle.tail_fill = FillLeg(
                side=fields[4],
                quantity=_decimal(fields[5]),
                price=_decimal(fields[6]),
                filled_at=_parse_iso_utc(fields[7]),
            )
        latest_tail_stop = _optional_positive_decimal(fields[2])
        if latest_tail_stop is not None:
            lifecycle.latest_tail_stop = latest_tail_stop


def _parse_amend_sent(
    lifecycle: PairLifecycle,
    body: str,
    log_time: datetime,
) -> None:
    fields = body.split()
    if len(fields) < 6:
        return
    lifecycle.amend_count += 1
    lifecycle.tail_amend_times.append(log_time)
    lifecycle.latest_tail_stop = _decimal(fields[1])
    lifecycle.tail_client_id = fields[4]
    lifecycle.tail_exchange_order_id = fields[5]


def _parse_tail_metrics(
    tail_telemetry: dict[PairKey, TailTelemetry],
    key: PairKey,
    body: str,
    log_time: datetime,
) -> MarketSnapshot | None:
    fields = body.split()
    market_snapshot = _parse_market_snapshot(fields, log_time)
    if len(fields) < 7:
        return market_snapshot
    if not fields[0].endswith("--living"):
        return market_snapshot
    tail_telemetry[key] = TailTelemetry(
        key=key,
        recorded_at=log_time,
        reference_price=_decimal(fields[1]),
        stop_price=_decimal(fields[2]),
        current_distance=_decimal(fields[4]),
    )
    return market_snapshot


def _parse_market_snapshot(
    fields: Sequence[str],
    log_time: datetime,
) -> MarketSnapshot | None:
    if len(fields) < 15:
        return None
    return MarketSnapshot(
        recorded_at=log_time,
        source=fields[8],
        spread_guard=_field_decimal(fields[5]),
        bid_price=_field_decimal(fields[9]),
        ask_price=_field_decimal(fields[10]),
        mid_price=_field_decimal(fields[11]),
        last_price=_field_decimal(fields[12]),
        mark_price=_field_decimal(fields[13]),
        index_price=_field_decimal(fields[14]),
    )


def _parse_gate_market_snapshot(
    event: str,
    body: str,
    log_time: datetime,
) -> MarketSnapshot | None:
    if event != "GATE_WAIT-2":
        return None
    fields = body.split()
    if len(fields) < 3:
        return None
    source = fields[1].lower()
    price = _optional_positive_decimal(fields[2])
    if price is None:
        return None
    prices: dict[str, Decimal | None] = {
        "bid_price": None,
        "ask_price": None,
        "mid_price": None,
        "last_price": None,
        "mark_price": None,
        "index_price": None,
    }
    field_name = {
        "bid": "bid_price",
        "ask": "ask_price",
        "mid": "mid_price",
        "last": "last_price",
        "mark": "mark_price",
        "index": "index_price",
    }.get(source)
    if field_name is None:
        return None
    prices[field_name] = price
    return MarketSnapshot(
        recorded_at=log_time,
        source=source,
        spread_guard=None,
        bid_price=prices["bid_price"],
        ask_price=prices["ask_price"],
        mid_price=prices["mid_price"],
        last_price=prices["last_price"],
        mark_price=prices["mark_price"],
        index_price=prices["index_price"],
    )


def _parse_repeat_ready(
    latent_attempts: dict[PairKey, LatentAttempt],
    key: PairKey,
    body: str,
    log_time: datetime,
) -> None:
    attempt = _latent_attempt(latent_attempts, key, log_time, "REPEAT_READY")
    fields = body.split()
    attempt.status = fields[0] if fields else "repeat_ready"
    attempt.last_event_at = log_time
    attempt.last_event = "REPEAT_READY"


def _parse_latent_timeout_armed(
    latent_attempts: dict[PairKey, LatentAttempt],
    key: PairKey,
    body: str,
    log_time: datetime,
) -> None:
    attempt = _latent_attempt(latent_attempts, key, log_time, "LATENT_TIMEOUT_ARMED")
    fields = body.split()
    if fields:
        attempt.deadline_at = _parse_iso_utc(fields[0])
    attempt.last_event_at = log_time
    attempt.last_event = "LATENT_TIMEOUT_ARMED"


def _parse_gate_wait(
    latent_attempts: dict[PairKey, LatentAttempt],
    key: PairKey,
    event: str,
    body: str,
    log_time: datetime,
) -> None:
    attempt = _latent_attempt(latent_attempts, key, log_time, event)
    fields = body.split()
    if fields:
        attempt.status = fields[0]
    if event == "GATE_WAIT-2" and len(fields) >= 10:
        attempt.gate = f"{fields[0]} {fields[1]}"
        attempt.reference_price = _optional_positive_decimal(fields[2])
        attempt.order_type = fields[7]
        attempt.head_price_spec = _field_decimal(fields[8])
        attempt.timeout_minutes = _field_decimal(fields[9])
        attempt.parameters_observed = True
    elif event == "GATE_WAIT-1" and len(fields) >= 5:
        attempt.gate = fields[0]
        attempt.order_type = fields[2]
        attempt.head_price_spec = _field_decimal(fields[3])
        attempt.timeout_minutes = _field_decimal(fields[4])
        attempt.parameters_observed = True
    attempt.last_event_at = log_time
    attempt.last_event = event


def _parse_latent_head_sent(
    latent_attempts: dict[PairKey, LatentAttempt],
    key: PairKey,
    body: str,
    log_time: datetime,
) -> None:
    attempt = _latent_attempt(latent_attempts, key, log_time, "HEAD_SENT")
    fields = body.split()
    if len(fields) >= 5:
        attempt.head_client_id = fields[0]
        attempt.status = "head_sent"
        attempt.order_type = fields[2]
        attempt.quantity = _decimal(fields[3])
        attempt.head_price = _optional_positive_decimal(fields[4])
    attempt.last_event_at = log_time
    attempt.last_event = "HEAD_SENT"


def _parse_latent_head_ack(
    latent_attempts: dict[PairKey, LatentAttempt],
    key: PairKey,
    body: str,
    log_time: datetime,
) -> None:
    _parse_latent_head_deadline(
        latent_attempts,
        key,
        body,
        log_time,
        event="HEAD_ACK",
        status="head_acked",
    )


def _parse_latent_head_visibility_timeout_armed(
    latent_attempts: dict[PairKey, LatentAttempt],
    key: PairKey,
    body: str,
    log_time: datetime,
) -> None:
    _parse_latent_head_deadline(
        latent_attempts,
        key,
        body,
        log_time,
        event="HEAD_VISIBILITY_TIMEOUT_ARMED",
        status="head_visibility_timeout_armed",
    )


def _parse_latent_head_deadline(
    latent_attempts: dict[PairKey, LatentAttempt],
    key: PairKey,
    body: str,
    log_time: datetime,
    *,
    event: str,
    status: str,
) -> None:
    attempt = _latent_attempt(latent_attempts, key, log_time, event)
    fields = body.split()
    if fields:
        attempt.head_client_id = fields[0]
    if len(fields) >= 3:
        attempt.deadline_at = _parse_iso_utc(fields[2])
    attempt.status = status
    attempt.last_event_at = log_time
    attempt.last_event = event


def _parse_latent_head_visibility_pending(
    latent_attempts: dict[PairKey, LatentAttempt],
    key: PairKey,
    body: str,
    log_time: datetime,
) -> None:
    attempt = _latent_attempt(latent_attempts, key, log_time, "HEAD_VISIBILITY_PENDING")
    fields = body.split()
    if len(fields) >= 2:
        attempt.head_client_id = fields[1]
    attempt.status = "head_visibility_pending"
    attempt.last_event_at = log_time
    attempt.last_event = "HEAD_VISIBILITY_PENDING"


def _parse_latent_head_visibility_timeout(
    latent_attempts: dict[PairKey, LatentAttempt],
    key: PairKey,
    body: str,
    log_time: datetime,
) -> None:
    attempt = _latent_attempt(latent_attempts, key, log_time, "HEAD_VISIBILITY_TIMEOUT")
    fields = body.split()
    if len(fields) >= 3:
        attempt.head_client_id = fields[2]
    attempt.status = "head_visibility_timeout"
    attempt.last_event_at = log_time
    attempt.last_event = "HEAD_VISIBILITY_TIMEOUT"


def _mark_latent_ended(
    latent_attempts: dict[PairKey, LatentAttempt],
    key: PairKey,
    event: str,
    body: str,
    log_time: datetime,
) -> None:
    attempt = _latent_attempt(latent_attempts, key, log_time, event)
    attempt.ended = True
    if event in {"HEAD_TIMEOUT", "LATENT_TIMEOUT"} or (
        event == "HEAD_CANCEL_SENT" and "head_timeout" in body.split()
    ):
        attempt.timed_out = True
    attempt.terminal_event = event
    attempt.status = event.lower()
    attempt.last_event_at = log_time
    attempt.last_event = event


def _latent_attempt(
    latent_attempts: dict[PairKey, LatentAttempt],
    key: PairKey,
    log_time: datetime,
    event: str,
) -> LatentAttempt:
    return latent_attempts.setdefault(
        key,
        LatentAttempt(key=key, last_event_at=log_time, last_event=event),
    )


def _build_report_row(
    lifecycle: PairLifecycle,
    fill_summaries: Mapping[str, DbFillSummary],
    order_summaries: Mapping[str, DbOrderSummary],
    *,
    instrument_summaries: Mapping[RuntimeRoute, InstrumentSummary],
    default_routes: Sequence[RuntimeRoute],
    require_db: bool,
    options: ReportOptions,
) -> ReportRow:
    del require_db
    if lifecycle.head_fill is None or lifecycle.tail_fill is None:
        raise ReportError(f"pair {lifecycle.key} is not terminated")
    head_summary = _fill_summary_for(
        lifecycle.head_client_id,
        lifecycle.head_exchange_order_id,
        fill_summaries,
    )
    tail_summary = _fill_summary_for(
        lifecycle.tail_client_id,
        lifecycle.tail_exchange_order_id,
        fill_summaries,
    )
    head_order = _order_summary_for(
        lifecycle.head_client_id,
        lifecycle.head_exchange_order_id,
        order_summaries,
    )
    tail_order = _order_summary_for(
        lifecycle.tail_client_id,
        lifecycle.tail_exchange_order_id,
        order_summaries,
    )
    head_leg = _resolve_leg_evidence(
        role="head",
        client_id=lifecycle.head_client_id,
        exchange_order_id=lifecycle.head_exchange_order_id,
        fill_summary=head_summary,
        order_summary=head_order,
        log_fill=lifecycle.head_fill,
    )
    tail_leg = _resolve_leg_evidence(
        role="tail",
        client_id=lifecycle.tail_client_id,
        exchange_order_id=lifecycle.tail_exchange_order_id,
        fill_summary=tail_summary,
        order_summary=tail_order,
        log_fill=lifecycle.tail_fill,
    )
    quantity = (
        tail_leg.quantity
        if tail_leg.quantity
        else head_leg.quantity
    )
    side = _side_abbrev(head_leg.side, tail_leg.side)
    route = _report_row_route(head_summary, tail_summary, default_routes)
    finance = calculate_finance(
        head_leg,
        tail_leg,
        quantity=quantity,
        route=route,
        instrument=instrument_summaries.get(route) if route is not None else None,
        options=options,
    )
    amend_logbps = None
    if (
        lifecycle.amend_count > 0
        and lifecycle.initial_tail_stop is not None
        and lifecycle.latest_tail_stop is not None
    ):
        amend_logbps = _signed_logbps_or_none(
            lifecycle.latest_tail_stop,
            lifecycle.initial_tail_stop,
        )
    life_seconds = int(
        (
            lifecycle.tail_fill.filled_at.replace(microsecond=0)
            - lifecycle.head_fill.filled_at.replace(microsecond=0)
        ).total_seconds()
    )
    head_wait_seconds = None
    if lifecycle.started_at is not None:
        head_wait_seconds = int(
            (
                lifecycle.head_fill.filled_at.replace(microsecond=0)
                - lifecycle.started_at.replace(microsecond=0)
            ).total_seconds()
        )

    return ReportRow(
        key=lifecycle.key,
        pair_started_at=lifecycle.started_at,
        head_wait_seconds=head_wait_seconds,
        gate_reference_price=lifecycle.gate_reference_price,
        head_fill_at=lifecycle.head_fill.filled_at,
        tail_fill_at=lifecycle.tail_fill.filled_at,
        tail_placed_at=lifecycle.tail_placed_at,
        life_seconds=life_seconds,
        side=side,
        head_price=head_leg.price,
        tail_price=tail_leg.price,
        quantity=quantity,
        liquidity=_liquidity_pair_from_evidence(head_leg, tail_leg),
        head_source=head_leg.source,
        tail_source=tail_leg.source,
        amend_count=lifecycle.amend_count,
        tail_amend_1_at=_tail_amend_time(lifecycle, 0),
        tail_amend_2_at=_tail_amend_time(lifecycle, 1),
        amend_logbps=amend_logbps,
        gross_usd=finance.gross_usd or Decimal("0"),
        net_usd=finance.net_usd,
        net_estimated=finance.quality in {EvidenceQuality.ESTIMATED, EvidenceQuality.ASSUMED},
        roi_percent=finance.roi_percent,
        roi_per_hour_percent=_roi_per_hour_percent(finance.roi_percent, life_seconds),
        cumulative_net=None,
        route=finance.route,
        pnl_kind=finance.pnl_kind,
        pnl_currency=finance.pnl_currency,
        gross_native=finance.gross_native,
        net_native=finance.net_native,
        fees_usd=finance.fees_usd,
        entry_notional_usd=finance.entry_notional_usd,
        finance_quality=finance.quality,
    )


def _resolve_leg_evidence(
    *,
    role: str,
    client_id: str | None,
    exchange_order_id: str | None,
    fill_summary: DbFillSummary | None,
    order_summary: DbOrderSummary | None,
    log_fill: FillLeg,
) -> OrderLegEvidence:
    if fill_summary is not None:
        quantity = fill_summary.quantity if fill_summary.quantity else log_fill.quantity
        return OrderLegEvidence(
            role=role,
            client_order_id=fill_summary.client_order_id or client_id,
            exchange_order_id=fill_summary.exchange_order_id or exchange_order_id,
            side=fill_summary.side or log_fill.side,
            price=fill_summary.price if fill_summary.price else log_fill.price,
            quantity=quantity,
            fee=fill_summary.fee,
            fee_currency=fill_summary.fee_currency,
            liquidity_role=fill_summary.liquidity_role,
            source=EVIDENCE_FILL_DB,
        )
    if order_summary is not None and (
        order_summary.price is not None or order_summary.filled_quantity
    ):
        return OrderLegEvidence(
            role=role,
            client_order_id=order_summary.client_order_id or client_id,
            exchange_order_id=order_summary.exchange_order_id or exchange_order_id,
            side=order_summary.side or log_fill.side,
            price=order_summary.price if order_summary.price is not None else log_fill.price,
            quantity=(
                order_summary.filled_quantity
                if order_summary.filled_quantity
                else log_fill.quantity
            ),
            fee=None,
            fee_currency=None,
            liquidity_role=None,
            source=EVIDENCE_ORDER_DB,
        )
    return OrderLegEvidence(
        role=role,
        client_order_id=client_id,
        exchange_order_id=exchange_order_id,
        side=log_fill.side,
        price=log_fill.price,
        quantity=log_fill.quantity,
        fee=None,
        fee_currency=None,
        liquidity_role=None,
        source=EVIDENCE_LOG,
    )


def _order_identity_ids(
    lifecycles: Iterable[PairLifecycle],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    client_ids: set[str] = set()
    exchange_order_ids: set[str] = set()
    for lifecycle in lifecycles:
        if lifecycle.head_client_id:
            client_ids.add(lifecycle.head_client_id)
        if lifecycle.tail_client_id:
            client_ids.add(lifecycle.tail_client_id)
        if lifecycle.head_exchange_order_id:
            exchange_order_ids.add(lifecycle.head_exchange_order_id)
        if lifecycle.tail_exchange_order_id:
            exchange_order_ids.add(lifecycle.tail_exchange_order_id)
    return tuple(sorted(client_ids)), tuple(sorted(exchange_order_ids))


def _fill_summary_for(
    client_id: str | None,
    exchange_order_id: str | None,
    fill_summaries: Mapping[str, DbFillSummary],
) -> DbFillSummary | None:
    for identity in _identity_keys(client_id, exchange_order_id):
        summary = fill_summaries.get(identity)
        if summary is not None:
            return summary
    return None


def _order_summary_for(
    client_id: str | None,
    exchange_order_id: str | None,
    order_summaries: Mapping[str, DbOrderSummary],
) -> DbOrderSummary | None:
    for identity in _identity_keys(client_id, exchange_order_id):
        summary = order_summaries.get(identity)
        if summary is not None:
            return summary
    return None


def _identity_keys(
    client_id: str | None,
    exchange_order_id: str | None,
) -> tuple[str, ...]:
    return tuple(
        identity
        for identity in (client_id, exchange_order_id)
        if identity is not None and identity
    )


def _summary_identity_keys(
    summary: DbFillSummary | DbOrderSummary,
) -> tuple[str, ...]:
    return _identity_keys(summary.client_order_id, summary.exchange_order_id)


def _format_missing_identity(
    key: PairKey,
    role: str,
    client_id: str | None,
    exchange_order_id: str | None,
) -> str:
    identities = _identity_keys(client_id, exchange_order_id)
    if identities:
        return "/".join(identities)
    return f"{key.name}#{key.attempt}:{role}"


def _fill_summary_route(summary: DbFillSummary) -> RuntimeRoute:
    return RuntimeRoute(
        exchange=summary.exchange.lower(),
        market_type=summary.market_type.lower(),
        symbol=summary.symbol,
    )


def _route_for_lifecycle_fills(
    lifecycle: PairLifecycle,
    fill_summaries: Mapping[str, DbFillSummary],
) -> RuntimeRoute | None:
    for client_id, exchange_order_id in (
        (lifecycle.head_client_id, lifecycle.head_exchange_order_id),
        (lifecycle.tail_client_id, lifecycle.tail_exchange_order_id),
    ):
        summary = _fill_summary_for(
            client_id,
            exchange_order_id,
            fill_summaries,
        )
        if summary is not None:
            return _fill_summary_route(summary)
    return None


def _routes_from_fill_summaries(
    summaries: Iterable[DbFillSummary],
) -> tuple[RuntimeRoute, ...]:
    return tuple(sorted({_fill_summary_route(summary) for summary in summaries}))


def _routes_for_report(
    runtime_routes: Iterable[RuntimeRoute],
    *,
    fill_summaries: Iterable[DbFillSummary],
    quantity_diagnostics: Iterable[QuantityDiagnostic],
) -> tuple[RuntimeRoute, ...]:
    routes = set(runtime_routes)
    routes.update(_routes_from_fill_summaries(fill_summaries))
    routes.update(
        diagnostic.route
        for diagnostic in quantity_diagnostics
        if diagnostic.route is not None
    )
    return tuple(sorted(routes))


def _instrument_summary_from_row(
    row: ExchangeInstrument,
    route: RuntimeRoute,
) -> InstrumentSummary:
    payload = dict(row.raw_payload or {})
    min_quantity = _optional_positive_decimal(row.min_quantity)
    contract_size = (
        _optional_positive_decimal(row.contract_size)
        or _first_payload_decimal(payload, ("contractSize", "contract_size"))
        or Decimal("1")
    )
    return InstrumentSummary(
        route=route,
        environment=row.environment,
        instrument_type=row.instrument_type,
        tick_size=_optional_positive_decimal(row.tick_size),
        contract_size=contract_size,
        min_quantity=min_quantity,
        quantity_step=_quantity_step_from_payload(payload, min_quantity=min_quantity),
    )


def _quantity_step_from_payload(
    payload: Mapping[str, object],
    *,
    min_quantity: Decimal | None,
) -> Decimal | None:
    value = _first_payload_decimal(
        payload,
        (
            "quantityStep",
            "quantity_step",
            "quantityIncrement",
            "qtyIncrement",
            "orderQtyStep",
            "lotSize",
            "stepSize",
        ),
    )
    if value is not None:
        return value
    if min_quantity is not None and min_quantity > 0:
        return min_quantity
    return _first_payload_decimal(payload, ("contractSize", "contract_size"))


def _instrument_quantity_tick(instrument: InstrumentSummary | None) -> Decimal | None:
    if instrument is None:
        return None
    if instrument.quantity_step is not None and instrument.quantity_step > 0:
        return instrument.quantity_step
    return instrument.min_quantity


def _quantity_usd_value(
    quantity: Decimal | None,
    *,
    reference_price: Decimal | None,
    instrument: InstrumentSummary | None,
) -> Decimal | None:
    if quantity is None or reference_price is None:
        return None
    return _usd_notional(reference_price, quantity, instrument)


def _diagnostic_available_usd(
    diagnostic: QuantityDiagnostic,
    account_available_usd: Mapping[RuntimeRoute, Decimal],
) -> Decimal | None:
    if diagnostic.route is not None:
        account_available = account_available_usd.get(diagnostic.route)
        if account_available is not None:
            return account_available
    return diagnostic.available_usd


def _sizing_usd(
    quantity: Decimal | None,
    *,
    mark_price: Decimal | None,
    contract_size: Decimal | None,
) -> Decimal | None:
    if quantity is None or mark_price is None:
        return None
    multiplier = contract_size or Decimal("1")
    if multiplier <= 0:
        multiplier = Decimal("1")
    return quantity * mark_price * multiplier


def _diagnostic_marks_by_route(
    diagnostics: Sequence[QuantityDiagnostic],
) -> dict[RuntimeRoute, Decimal]:
    marks: dict[RuntimeRoute, Decimal] = {}
    for diagnostic in diagnostics:
        if diagnostic.route is None or diagnostic.mark_price is None:
            continue
        marks[diagnostic.route] = diagnostic.mark_price
    return marks


def _market_snapshot_reference_price(
    snapshot: MarketSnapshot | None,
) -> Decimal | None:
    if snapshot is None:
        return None
    return snapshot.mark_price or snapshot.last_price or snapshot.index_price


def _usd_notional(
    price: Decimal,
    quantity: Decimal,
    instrument: InstrumentSummary | None,
) -> Decimal:
    contract_size = instrument.contract_size if instrument is not None else Decimal("1")
    if contract_size <= 0:
        contract_size = Decimal("1")
    if instrument is not None and pnl_kind(instrument.route, instrument) == PnlKind.INVERSE:
        return quantity * contract_size
    return price * quantity * contract_size


def _average(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    return sum(values, Decimal("0")) / Decimal(len(values))


def _raw_event_is_public_trade(event: RawExchangeEvent) -> bool:
    stream = (event.stream_kind or "").lower()
    scope = (event.account_scope or "public").lower()
    if scope != "public" and "public" not in stream:
        return False
    payload = event.payload or {}
    fields = [
        event.event_type,
        stream,
        _payload_text(payload, "feed"),
        _payload_text(payload, "channel"),
        _payload_text(payload, "channelName"),
        _payload_text(payload, "topic"),
        _payload_text(payload, "type"),
    ]
    return "trade" in " ".join(field for field in fields if field).lower()


def _raw_trade_price_quantities(
    payload: object,
) -> tuple[tuple[Decimal, Decimal], ...]:
    pairs: list[tuple[Decimal, Decimal]] = []
    for item in _trade_payload_items(payload):
        price = _first_payload_decimal(
            item,
            ("price", "p", "last", "lastPrice", "tradePrice"),
        )
        quantity = _first_payload_decimal(
            item,
            ("quantity", "qty", "q", "size", "volume", "amount", "v"),
        )
        if price is not None and quantity is not None:
            pairs.append((price, quantity))
    return tuple(pairs)


def _trade_payload_items(payload: object) -> tuple[Mapping[str, object], ...]:
    if isinstance(payload, Mapping):
        items: list[Mapping[str, object]] = []
        if _payload_has_price_and_quantity(payload):
            items.append(payload)
        for key in ("trade", "trades", "data", "events", "items", "result"):
            if key in payload:
                items.extend(_trade_payload_items(payload[key]))
        return tuple(items)
    if isinstance(payload, list | tuple):
        items = []
        for item in payload:
            items.extend(_trade_payload_items(item))
        return tuple(items)
    return ()


def _payload_has_price_and_quantity(payload: Mapping[str, object]) -> bool:
    return (
        _first_payload_decimal(
            payload,
            ("price", "p", "last", "lastPrice", "tradePrice"),
        )
        is not None
        and _first_payload_decimal(
            payload,
            ("quantity", "qty", "q", "size", "volume", "amount", "v"),
        )
        is not None
    )


def _payload_text(payload: object, key: str) -> str:
    if isinstance(payload, Mapping):
        value = payload.get(key)
        if value is not None:
            return str(value)
    return ""


def _first_payload_decimal(
    payload: Mapping[str, object],
    keys: Sequence[str],
) -> Decimal | None:
    for key in keys:
        value = payload.get(key)
        if value in (None, ""):
            continue
        try:
            parsed = _decimal(value)
        except Exception:
            continue
        if parsed > 0:
            return parsed
    return None


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def _optional_decimal(value: object | None) -> Decimal | None:
    if value is None:
        return None
    return _decimal(value)


def _field_decimal(value: object) -> Decimal | None:
    text = str(value).strip()
    if not text or text == "-":
        return None
    return _decimal(text)


def _positive_decimal(value: object) -> Decimal | None:
    parsed = _decimal(value)
    if parsed <= 0:
        return None
    return parsed


def _optional_positive_decimal(value: object) -> Decimal | None:
    try:
        return _positive_decimal(value)
    except Exception:
        return None


def _parse_iso_utc(raw: str) -> datetime:
    value = raw.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_log_utc(raw: str) -> datetime:
    return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _earliest_time(current: datetime | None, candidate: datetime) -> datetime:
    candidate = _as_utc(candidate)
    if current is None or candidate < current:
        return candidate
    return current


def _latest_time(current: datetime | None, candidate: datetime) -> datetime:
    candidate = _as_utc(candidate)
    if current is None or candidate > current:
        return candidate
    return current


def _report_run_started_at(snapshot: RunLogSnapshot, log_path: Path) -> datetime:
    if snapshot.runtime_metadata.started_at is not None:
        return snapshot.runtime_metadata.started_at
    if snapshot.first_log_at is not None:
        return snapshot.first_log_at
    return datetime.fromtimestamp(log_path.stat().st_mtime, tz=timezone.utc)


def _tail_amend_time(lifecycle: PairLifecycle, index: int) -> datetime | None:
    if index >= len(lifecycle.tail_amend_times):
        return None
    return lifecycle.tail_amend_times[index]


def _side_abbrev(head_side: str, tail_side: str) -> str:
    return f"{head_side[:1].upper()}/{tail_side[:1].upper()}"


def _opposite_side(side: str) -> str:
    normalized = side.lower()
    if normalized == "buy":
        return "sell"
    if normalized == "sell":
        return "buy"
    return ""


def _report_row_route(
    head_summary: DbFillSummary | None,
    tail_summary: DbFillSummary | None,
    default_routes: Sequence[RuntimeRoute],
) -> RuntimeRoute | None:
    summary = head_summary or tail_summary
    if summary is not None:
        return _fill_summary_route(summary)
    routes = tuple(dict.fromkeys(default_routes))
    return routes[0] if len(routes) == 1 else None


def _gross_usd(
    head_side: str,
    head_price: Decimal,
    tail_price: Decimal,
    quantity: Decimal,
) -> Decimal:
    if head_side.lower() == "buy":
        return (tail_price - head_price) * quantity
    return (head_price - tail_price) * quantity


def _net_usd(
    gross: Decimal,
    head_summary: DbFillSummary,
    tail_summary: DbFillSummary,
) -> Decimal | None:
    if not _fee_is_usd(head_summary.fee_currency):
        return None
    if not _fee_is_usd(tail_summary.fee_currency):
        return None
    return gross - head_summary.fee - tail_summary.fee


def _net_usd_from_evidence(
    gross: Decimal,
    head_leg: OrderLegEvidence,
    tail_leg: OrderLegEvidence,
    *,
    options: ReportOptions,
) -> tuple[Decimal | None, bool]:
    head_fee, head_estimated = _leg_fee_usd(head_leg, options=options)
    tail_fee, tail_estimated = _leg_fee_usd(tail_leg, options=options)
    if head_fee is None or tail_fee is None:
        return None, False
    return gross - head_fee - tail_fee, head_estimated or tail_estimated


def _leg_fee_usd(
    leg: OrderLegEvidence,
    *,
    options: ReportOptions,
) -> tuple[Decimal | None, bool]:
    if leg.fee is not None and _fee_is_usd(leg.fee_currency):
        return leg.fee, False
    if not options.estimate_fees:
        return None, False
    rate = _estimated_fee_rate(leg, options=options)
    return leg.price * leg.quantity * rate, True


def _estimated_fee_rate(
    leg: OrderLegEvidence,
    *,
    options: ReportOptions,
) -> Decimal:
    return (
        options.maker_fee_rate
        if _liquidity_abbrev(leg.liquidity_role) == "M"
        else options.taker_fee_rate
    )


def _roi_percent(
    basis: Decimal | None,
    head_price: Decimal,
    quantity: Decimal,
) -> Decimal | None:
    if basis is None:
        return None
    notional = head_price * quantity
    if notional == 0:
        return None
    return basis / notional * Decimal("100")


def _roi_per_hour_percent(
    roi_percent: Decimal | None,
    life_seconds: int,
) -> Decimal | None:
    if roi_percent is None or life_seconds <= 0:
        return None
    return roi_percent * Decimal("3600") / Decimal(life_seconds)


def _signed_logbps_or_none(
    current_price: Decimal | None,
    baseline_price: Decimal | None,
) -> Decimal | None:
    if current_price is None or baseline_price is None:
        return None
    try:
        return signed_logbps_move(current_price, baseline_price)
    except ValueError:
        return None


def _fee_is_usd(currency: str | None) -> bool:
    return (currency or "").strip().lower() in _USD_FEE_CURRENCIES


def _liquidity_pair(
    head_summary: DbFillSummary | None,
    tail_summary: DbFillSummary | None,
) -> str:
    head = _liquidity_abbrev(head_summary.liquidity_role if head_summary else None)
    tail = _liquidity_abbrev(tail_summary.liquidity_role if tail_summary else None)
    if not head and not tail:
        return ""
    return f"{head}/{tail}"


def _liquidity_pair_from_evidence(
    head_leg: OrderLegEvidence,
    tail_leg: OrderLegEvidence,
) -> str:
    return f"{_leg_liquidity_abbrev(head_leg)}/{_leg_liquidity_abbrev(tail_leg)}"


def _leg_liquidity_abbrev(leg: OrderLegEvidence) -> str:
    value = _liquidity_abbrev(leg.liquidity_role)
    return value or "?"


def _summarise_liquidity(roles: Sequence[str]) -> str | None:
    abbreviations = [_liquidity_abbrev(role) for role in roles]
    if "T" in abbreviations:
        return "T"
    if "M" in abbreviations:
        return "M"
    return abbreviations[0] if abbreviations else None


def _liquidity_abbrev(role: str | None) -> str:
    normalized = (role or "").strip().lower()
    if not normalized:
        return ""
    if normalized.startswith("m"):
        return "M"
    if normalized.startswith("t"):
        return "T"
    return normalized[:1].upper()


def _format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%m-%d %H:%M")


def _format_clock_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%H:%M")


def _format_org_heading(value: datetime, *, report_name: str | None = None) -> str:
    local_value = value.astimezone(timezone.utc)
    weekdays = ("lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim.")
    heading = (
        f"* <{local_value:%Y-%m-%d} {weekdays[local_value.weekday()]} "
        f"{local_value:%H:%M}>"
    )
    if report_name:
        return f"{heading} {report_name}"
    return heading


def _format_report_provenance(identity: ReportIdentity) -> str:
    return (
        f"Run UTC: {identity.run_started_at:%Y-%m-%d %H:%M:%S} | "
        f"Command: {identity.command_line}"
    )


def _runtime_report_name(metadata: RuntimeMetadata, log_path: Path) -> str:
    seed = _runtime_seed(metadata, log_path)
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    codename = _REPORT_CODENAMES[int(digest[:8], 16) % len(_REPORT_CODENAMES)]
    return f"{codename}-{_runtime_token(metadata, log_path)}"


def _runtime_seed(metadata: RuntimeMetadata, log_path: Path) -> str:
    labels = ",".join(sorted(route.label for route in metadata.routes))
    started = metadata.started_at.isoformat() if metadata.started_at else ""
    return "|".join(
        (
            started,
            metadata.environment or "",
            labels,
            log_path.stem if not labels else "",
        )
    )


def _runtime_token(metadata: RuntimeMetadata, log_path: Path) -> str:
    if not metadata.routes:
        return _slug_token(log_path.stem) or "unknown"
    groups: list[tuple[str, list[str]]] = []
    group_index: dict[str, int] = {}
    for route in metadata.routes:
        platform = _platform_token(route)
        if platform not in group_index:
            group_index[platform] = len(groups)
            groups.append((platform, []))
        instruments = groups[group_index[platform]][1]
        instrument = _instrument_token(route.symbol)
        if instrument and instrument not in instruments:
            instruments.append(instrument)
    return "-".join(
        "-".join((platform, *instruments)) if instruments else platform
        for platform, instruments in groups
    )


def _platform_token(route: RuntimeRoute) -> str:
    exchange = _EXCHANGE_CODES.get(route.exchange, _slug_token(route.exchange))
    market = _MARKET_CODES.get(route.market_type, _slug_token(route.market_type))
    return f"{exchange}{market}"


def _instrument_token(symbol: str) -> str:
    text = symbol.strip().upper()
    if "/" in text:
        base = text.split("/", 1)[0]
    else:
        base = re.sub(r"^[A-Z]{2}_", "", text)
        base = re.sub(r"[^A-Z0-9]", "", base)
        for suffix in _QUOTE_SUFFIXES:
            if base.endswith(suffix) and len(base) > len(suffix):
                base = base[: -len(suffix)]
                break
    base = _BASE_ALIASES.get(base, base)
    return _slug_token(base)


def _slug_token(value: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", value.lower())).strip("-")


def _format_optional_time(value: datetime | None) -> str:
    if value is None:
        return ""
    return _format_time(value)


def _format_optional_clock_time(value: datetime | None) -> str:
    if value is None:
        return ""
    return _format_clock_time(value)


def _format_life(seconds: int) -> str:
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{sign}{hours:02d}:{minutes:02d}:{secs:02d}"


def _format_optional_life_seconds(seconds: int | None) -> str:
    if seconds is None:
        return ""
    return _format_life(seconds)


def _format_pair(key: PairKey, name_width: int, attempt_width: int) -> str:
    return f"{key.name:<{name_width}} {f'#{key.attempt}':>{attempt_width}}"


def _format_quantity(value: Decimal) -> str:
    integral = value.to_integral_value()
    if value == integral:
        return str(int(integral))
    return format(value.normalize(), "f")


def _format_parameter_value(value: Decimal | None) -> str:
    if value is None:
        return "-"
    return _format_quantity(value)


def _format_optional_quantity(value: Decimal | None) -> str:
    if value is None:
        return ""
    return _format_quantity(value)


def _format_optional_quantity_word(value: Decimal | None) -> str:
    if value is None:
        return "n/a"
    return _format_quantity(value)


def _format_decimal(value: Decimal, places: int) -> str:
    quant = Decimal("1").scaleb(-places)
    return f"{value.quantize(quant, rounding=ROUND_HALF_UP):.{places}f}"


def _format_fill_price(value: Decimal, places: int) -> str:
    quant = Decimal("1").scaleb(-places)
    return f"{value.quantize(quant, rounding=ROUND_DOWN):.{places}f}"


def _format_optional_decimal(value: Decimal | None, places: int) -> str:
    if value is None:
        return ""
    return _format_decimal(value, places)


def _format_optional_money_word(value: Decimal | None, places: int) -> str:
    if value is None:
        return "n/a"
    return _format_decimal(value, places)


def _format_optional_life(value: Decimal | None) -> str:
    if value is None:
        return "n/a"
    return _format_life(int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)))


def _format_optional_price_word(value: Decimal | None, places: int) -> str:
    if value is None:
        return "-"
    return _format_decimal(value, places)


def _format_stat_decimal(
    value: Decimal | None,
    places: int,
    *,
    signed: bool = False,
) -> str:
    if value is None:
        return ""
    quant = Decimal("1").scaleb(-places)
    rounded = value.quantize(quant, rounding=ROUND_HALF_UP)
    if rounded == rounded.to_integral_value():
        formatted = str(int(rounded))
    else:
        formatted = format(rounded.normalize(), "f")
    if signed and not formatted.startswith("-"):
        return f"+{formatted}"
    return formatted


def _format_stat_life(value: Decimal | None) -> str:
    if value is None:
        return ""
    return _format_life(int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)))


def _snapshot_spread(snapshot: MarketSnapshot) -> Decimal | None:
    if snapshot.bid_price is None or snapshot.ask_price is None:
        return None
    return snapshot.ask_price - snapshot.bid_price


def _format_signed(value: Decimal, places: int) -> str:
    formatted = _format_decimal(value, places)
    return formatted if formatted.startswith("-") else f"+{formatted}"


def _format_signed_optional(value: Decimal | None, places: int) -> str:
    if value is None:
        return ""
    return _format_signed(value, places)


def _format_logbps_optional(value: Decimal | None) -> str:
    if value is None:
        return ""
    rounded = value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    if rounded == 0:
        return "0"
    formatted = str(int(rounded))
    return formatted if rounded < 0 else f"+{formatted}"


def _format_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    *,
    align_right: set[str],
) -> str:
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows))
        if rows
        else len(headers[index])
        for index in range(len(headers))
    ]
    lines = [_format_table_row(headers, widths, align_right=align_right, headers=headers)]
    lines.append("|" + "+".join("-" * (width + 2) for width in widths) + "|")
    for row in rows:
        lines.append(_format_table_row(row, widths, align_right=align_right, headers=headers))
    return "\n".join(lines)


def _format_table_row(
    cells: Sequence[str],
    widths: Sequence[int],
    *,
    align_right: set[str],
    headers: Sequence[str],
) -> str:
    formatted = []
    for index, cell in enumerate(cells):
        header = headers[index]
        if header in align_right:
            formatted.append(f" {cell:>{widths[index]}} ")
        else:
            formatted.append(f" {cell:<{widths[index]}} ")
    return "|" + "|".join(formatted) + "|"


def _render_section_table(
    renderer,
    rows: Sequence[object],
    *,
    options: ReportOptions,
) -> str:
    if not rows:
        return "No rows."
    return renderer(rows, options=options)


def _render_optional_summary(
    rows: Sequence[ReportRow],
    *,
    options: ReportOptions,
) -> str:
    if not rows:
        return ""
    return (
        "\n"
        + render_terminated_summary_table(rows, options=options)
        + "\nMode note: mode is the most repeated value; blank means no value repeats."
    )


def _render_latent_gate_note(rows: Sequence[LatentRow]) -> str:
    if not any(row.gate == NO_GATE_LOG for row in rows):
        return ""
    return (
        f"Gate {NO_GATE_LOG} means HEAD_SENT was logged but no GATE_WAIT-* line "
        "was present for that attempt."
    )


def _render_latent_deadline_note(rows: Sequence[LatentRow]) -> str:
    if not any(_format_latent_deadline(row) == NO_DEADLINE_LOG for row in rows):
        return ""
    return (
        f"Deadline {NO_DEADLINE_LOG} means HEAD_SENT was logged but no "
        "LATENT_TIMEOUT_ARMED, HEAD_ACK, or HEAD_VISIBILITY_TIMEOUT_ARMED deadline "
        "was present for that attempt."
    )


def _render_sizing_note(
    rows: Sequence[SizingRow],
    notes: Sequence[str],
) -> str:
    rendered = [note for note in notes if note]
    if _sizing_runtime_market_disagrees(rows):
        rendered.append(
            "Sizing note: runtime rows show the values that actually accepted or "
            "rejected strategy quantities; market_db rows show cached instrument "
            "rules."
        )
    return "\n".join(rendered)


def _render_report_notes(notes: Sequence[str]) -> str:
    return "\n".join(note for note in notes if note)


def _fill_fallback_report_notes(
    lifecycles: Iterable[PairLifecycle],
    fill_summaries: Mapping[str, DbFillSummary],
    order_summaries: Mapping[str, DbOrderSummary],
    *,
    options: ReportOptions,
) -> tuple[str, ...]:
    missing: list[str] = []
    seen: set[str] = set()
    total_legs = 0
    exact_legs = 0
    order_only_legs = 0
    log_only_legs = 0
    unknown_liquidity_legs = 0
    for lifecycle in lifecycles:
        if not lifecycle.terminated:
            continue
        head_summary = _fill_summary_for(
            lifecycle.head_client_id,
            lifecycle.head_exchange_order_id,
            fill_summaries,
        )
        tail_summary = _fill_summary_for(
            lifecycle.tail_client_id,
            lifecycle.tail_exchange_order_id,
            fill_summaries,
        )
        head_order = _order_summary_for(
            lifecycle.head_client_id,
            lifecycle.head_exchange_order_id,
            order_summaries,
        )
        tail_order = _order_summary_for(
            lifecycle.tail_client_id,
            lifecycle.tail_exchange_order_id,
            order_summaries,
        )
        for role, fill_summary, order_summary, client_id, exchange_order_id in (
            (
                "head",
                head_summary,
                head_order,
                lifecycle.head_client_id,
                lifecycle.head_exchange_order_id,
            ),
            (
                "tail",
                tail_summary,
                tail_order,
                lifecycle.tail_client_id,
                lifecycle.tail_exchange_order_id,
            ),
        ):
            total_legs += 1
            if fill_summary is not None:
                exact_legs += 1
                if not _liquidity_abbrev(fill_summary.liquidity_role):
                    unknown_liquidity_legs += 1
                continue
            if order_summary is not None:
                order_only_legs += 1
            else:
                log_only_legs += 1
            unknown_liquidity_legs += 1
            label = _format_missing_identity(
                lifecycle.key,
                role,
                client_id,
                exchange_order_id,
            )
            if label not in seen:
                seen.add(label)
                missing.append(label)
    if not missing and unknown_liquidity_legs == 0:
        return ()
    notes = [
        "Evidence note: "
        f"exact DB fills {exact_legs}/{total_legs} legs; "
        f"order-only {order_only_legs}; log-only {log_only_legs}; "
        f"unknown liquidity {unknown_liquidity_legs}. "
        "Missing fill rows affect exact fees/liquidity, not log-observed trades."
    ]
    if options.estimate_fees and (order_only_legs or log_only_legs or unknown_liquidity_legs):
        notes.append(
            "Estimated net uses "
            f"maker={_format_fee_rate(options.maker_fee_rate)} and "
            f"taker/unknown={_format_fee_rate(options.taker_fee_rate)} "
            "when exact USD fees are missing."
        )
    if missing:
        notes.append(
            "Missing fill rows: "
            f"{_format_capped_identities(missing, limit=MISSING_FILL_NOTE_LIMIT)}."
        )
    return tuple(notes)


def _format_capped_identities(values: Sequence[str], *, limit: int) -> str:
    visible = list(values[:limit])
    hidden = len(values) - len(visible)
    if hidden > 0:
        visible.append(f"... +{hidden} more")
    return ", ".join(visible)


def _format_fee_rate(rate: Decimal) -> str:
    return f"{(rate * Decimal('100')).normalize()}%"


def _sizing_runtime_market_disagrees(rows: Sequence[SizingRow]) -> bool:
    runtime_rows = [row for row in rows if row.source == "runtime"]
    market_rows = {row.route: row for row in rows if row.source == "market_db"}
    for runtime_row in runtime_rows:
        market_row = market_rows.get(runtime_row.route)
        if market_row is None:
            continue
        if (
            runtime_row.min_quantity is not None
            and market_row.min_quantity is not None
            and runtime_row.min_quantity != market_row.min_quantity
        ):
            return True
        if (
            runtime_row.quantity_step is not None
            and market_row.quantity_step is not None
            and runtime_row.quantity_step != market_row.quantity_step
        ):
            return True
    return False


def _report_timestamp(
    terminated_rows: Sequence[ReportRow],
    living_rows: Sequence[LivingTailRow],
    latent_rows: Sequence[LatentRow],
    *,
    market_snapshot: MarketSnapshot | None,
    report_at: datetime | None,
) -> datetime:
    if report_at is not None:
        return report_at
    if market_snapshot is not None:
        return market_snapshot.recorded_at
    candidates: list[datetime] = []
    candidates.extend(row.tail_fill_at for row in terminated_rows)
    candidates.extend(row.head_fill_at for row in living_rows)
    candidates.extend(row.time for row in latent_rows)
    if candidates:
        return max(candidates)
    return datetime.now(timezone.utc).replace(microsecond=0)


def render_terminated_counts_line(rows: Sequence[ReportRow]) -> str:
    """Render compact side and liquidity counts for terminated rows."""

    if not rows:
        return "Side: none | Liq: none"
    side_counts = Counter(row.side or "-" for row in rows)
    liq_counts = Counter(row.liquidity or "-" for row in rows)
    return (
        f"Side: {_format_counts(side_counts)} | "
        f"Liq: {_format_counts(liq_counts)}"
    )


def _format_counts(counts: Counter[str]) -> str:
    return " ".join(f"{key}={counts[key]}" for key in sorted(counts))


def _stat_value(values: Iterable[Decimal], stat: str) -> Decimal | None:
    items = tuple(values)
    if not items:
        return None
    if stat == "min":
        return min(items)
    if stat == "max":
        return max(items)
    if stat == "median":
        return _median(items)
    if stat == "mode":
        return _mode(items)
    if stat == "average":
        return sum(items, Decimal("0")) / Decimal(len(items))
    raise ValueError(f"unknown stat {stat!r}")


def _median(values: Sequence[Decimal]) -> Decimal:
    items = sorted(values)
    midpoint = len(items) // 2
    if len(items) % 2:
        return items[midpoint]
    return (items[midpoint - 1] + items[midpoint]) / Decimal("2")


def _mode(values: Sequence[Decimal]) -> Decimal | None:
    counts = Counter(values)
    highest = max(counts.values())
    modes = sorted(value for value, count in counts.items() if count == highest)
    if highest == 1 and len(modes) > 1:
        return None
    return modes[0]


def _optional_values(values: Iterable[Decimal | None]) -> tuple[Decimal, ...]:
    return tuple(value for value in values if value is not None)


def _row_life_values(rows: Sequence[ReportRow]) -> tuple[Decimal, ...]:
    return tuple(Decimal(row.life_seconds) for row in rows)


def _position_values(rows: Sequence[ReportRow]) -> tuple[Decimal, ...]:
    position = Decimal("0")
    values: list[Decimal] = []
    events: list[tuple[datetime, int, Decimal]] = []
    for row in rows:
        head_delta = row.quantity if row.side.startswith("B/") else -row.quantity
        events.append((row.head_fill_at, 0, head_delta))
        events.append((row.tail_fill_at, 1, -head_delta))
    for _, _, delta in sorted(events):
        position += delta
        engaged = abs(position)
        if engaged:
            values.append(engaged)
    return tuple(values)


def _row_amend_phase_values(rows: Sequence[ReportRow]) -> tuple[Decimal, ...]:
    values: list[Decimal] = []
    for row in rows:
        if row.tail_placed_at is None:
            continue
        seconds = int(
            (
                row.tail_fill_at.replace(microsecond=0)
                - row.tail_placed_at.replace(microsecond=0)
            ).total_seconds()
        )
        if seconds >= 0:
            values.append(Decimal(seconds))
    return tuple(values)


def _latent_status(attempt: LatentAttempt) -> str:
    if attempt.status:
        return attempt.status
    if attempt.head_client_id:
        return "head_sent"
    if attempt.gate:
        return "gate_wait"
    return attempt.last_event.lower()


def _latent_gate_display(attempt: LatentAttempt) -> str:
    if attempt.gate:
        return attempt.gate
    if attempt.head_client_id:
        return NO_GATE_LOG
    return ""


def _format_latent_deadline(row: LatentRow) -> str:
    if row.deadline_at is not None:
        return _format_optional_time(row.deadline_at)
    if row.last_event in {
        "HEAD_SENT",
        "HEAD_ACK",
        "HEAD_VISIBILITY_PENDING",
        "HEAD_VISIBILITY_TIMEOUT",
    }:
        return NO_DEADLINE_LOG
    return ""


def _report_program_name(argv: Sequence[str] | None) -> str:
    if os.environ.get("KOLABI_RUN_REPORT_COMMAND_NAME"):
        return os.environ["KOLABI_RUN_REPORT_COMMAND_NAME"]
    if argv is not None:
        return "kolabi-run-report"
    return sys.argv[0]


def _format_command_line(program: str, argv: Sequence[str]) -> str:
    return " ".join(
        shlex.quote(part)
        for part in (program, *_redact_command_args(argv))
    )


def _redact_command_args(argv: Sequence[str]) -> tuple[str, ...]:
    redacted: list[str] = []
    redact_next = False
    for arg in argv:
        if redact_next:
            redacted.append(redact_url(arg))
            redact_next = False
            continue
        if arg in {"--account-db-url", "--market-db-url"}:
            redacted.append(arg)
            redact_next = True
            continue
        if arg.startswith("--account-db-url=") or arg.startswith("--market-db-url="):
            key, value = arg.split("=", 1)
            redacted.append(f"{key}={redact_url(value)}")
            continue
        redacted.append(arg)
    return tuple(redacted)


def _load_env_file(path: Path, *, env: Mapping[str, str]) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        raw_value = raw_value.strip().strip('"').strip("'")

        def expand(match: re.Match[str]) -> str:
            name = match.group(1)
            return values.get(name, env.get(name, ""))

        values[key] = _ENV_REF_RE.sub(expand, raw_value)
    return values


def _compact_error(exc: BaseException) -> str:
    return " ".join(str(exc).split())


def _resolve_strategy_copy_path(
    *,
    explicit_path: str | None,
    log_path: Path,
) -> Path | None:
    if explicit_path:
        return Path(explicit_path)
    metadata_path = parse_run_log_file(log_path).runtime_metadata.strategy_path
    if metadata_path:
        return Path(metadata_path)
    fallback = Path("orders") / f"{log_path.stem}.org"
    if fallback.exists():
        return fallback
    fallback = Path("orders") / f"{log_path.stem}.tsv"
    if fallback.exists():
        return fallback
    return None


def _append_strategy_copy(text: str, strategy_path: Path) -> str:
    strategy_text = strategy_path.read_text(encoding="utf-8").rstrip()
    return (
        text.rstrip()
        + "\n\n** Strategy\n"
        + f"Path: {strategy_path}\n\n"
        + "#+begin_src org\n"
        + strategy_text
        + "\n#+end_src\n"
    )


def _prepend_output(path: Path, text: str) -> None:
    rendered = text.rstrip() + "\n"
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if existing:
            rendered = rendered + "\n" + existing
    path.write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
