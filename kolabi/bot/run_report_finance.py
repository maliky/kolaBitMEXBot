"""Instrument-aware realised finance for Kolabi run reports."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, TypeVar


class PnlKind(StrEnum):
    LINEAR = "linear"
    INVERSE = "inverse"
    UNKNOWN = "unknown"


class EvidenceQuality(StrEnum):
    EXACT = "exact"
    ESTIMATED = "estimated"
    ASSUMED = "assumed"
    UNAVAILABLE = "unavailable"


class RouteLike(Protocol):
    market_type: str
    symbol: str


class InstrumentLike(Protocol):
    instrument_type: str | None
    contract_size: Decimal


class LegLike(Protocol):
    side: str
    price: Decimal
    quantity: Decimal
    fee: Decimal | None
    fee_currency: str | None
    liquidity_role: str | None


class OptionsLike(Protocol):
    estimate_fees: bool
    maker_fee_rate: Decimal
    taker_fee_rate: Decimal


RouteT = TypeVar("RouteT", bound=RouteLike)


@dataclass(frozen=True)
class FinanceResult:
    route: RouteLike | None
    pnl_kind: PnlKind
    pnl_currency: str
    gross_native: Decimal | None
    net_native: Decimal | None
    gross_usd: Decimal | None
    net_usd: Decimal | None
    fees_usd: Decimal | None
    entry_notional_usd: Decimal | None
    roi_percent: Decimal | None
    quality: EvidenceQuality


def pnl_kind(route: RouteLike | None, instrument: InstrumentLike | None) -> PnlKind:
    token = ((instrument.instrument_type if instrument else "") or "").lower()
    symbol = route.symbol.upper() if route is not None else ""
    market_type = route.market_type.lower() if route is not None else ""
    if "inverse" in token or symbol.startswith("PI_"):
        return PnlKind.INVERSE
    if (
        "linear" in token
        or "flex" in token
        or symbol.startswith(("PF_", "FF_"))
        or market_type in {"spot", "margin"}
    ):
        return PnlKind.LINEAR
    return PnlKind.LINEAR if route is None else PnlKind.UNKNOWN


def calculate_finance(
    head_leg: LegLike,
    tail_leg: LegLike,
    *,
    quantity: Decimal,
    route: RouteT | None,
    instrument: InstrumentLike | None,
    options: OptionsLike,
) -> FinanceResult:
    kind = pnl_kind(route, instrument)
    multiplier = _multiplier(instrument)
    if kind == PnlKind.UNKNOWN:
        return FinanceResult(
            route, kind, "", None, None, None, None, None, None, None,
            EvidenceQuality.UNAVAILABLE,
        )
    direction = Decimal("1") if head_leg.side.lower() == "buy" else Decimal("-1")
    if kind == PnlKind.INVERSE:
        position_size = quantity * multiplier
        gross_native = direction * (
            Decimal("1") / head_leg.price - Decimal("1") / tail_leg.price
        ) * position_size
        gross_usd = gross_native * tail_leg.price
        entry_notional = position_size
        currency = inverse_base_currency(route)
    else:
        gross_native = direction * (tail_leg.price - head_leg.price) * quantity * multiplier
        gross_usd = gross_native
        entry_notional = head_leg.price * quantity * multiplier
        currency = "USD"
    head_fee, head_estimated = _fee_usd(
        head_leg, kind=kind, multiplier=multiplier, route=route, options=options
    )
    tail_fee, tail_estimated = _fee_usd(
        tail_leg, kind=kind, multiplier=multiplier, route=route, options=options
    )
    fees_usd = None if head_fee is None or tail_fee is None else head_fee + tail_fee
    net_usd = None if fees_usd is None else gross_usd - fees_usd
    net_native = (
        net_usd / tail_leg.price
        if kind == PnlKind.INVERSE and net_usd is not None
        else net_usd
    )
    roi = (
        (net_usd if net_usd is not None else gross_usd)
        / entry_notional
        * Decimal("100")
        if entry_notional
        else None
    )
    quality = (
        EvidenceQuality.UNAVAILABLE
        if fees_usd is None
        else EvidenceQuality.ESTIMATED
        if head_estimated or tail_estimated
        else EvidenceQuality.ASSUMED
        if instrument is None
        else EvidenceQuality.EXACT
    )
    return FinanceResult(
        route, kind, currency, gross_native, net_native, gross_usd, net_usd,
        fees_usd, entry_notional, roi, quality,
    )


def entry_notional_usd(
    price: Decimal,
    quantity: Decimal,
    *,
    route: RouteLike | None,
    instrument: InstrumentLike | None,
) -> Decimal | None:
    kind = pnl_kind(route, instrument)
    multiplier = _multiplier(instrument)
    if kind == PnlKind.INVERSE:
        return quantity * multiplier
    if kind == PnlKind.LINEAR:
        return price * quantity * multiplier
    return None


def inverse_base_currency(route: RouteLike | None) -> str:
    if route is None:
        return "BASE"
    symbol = route.symbol.upper()
    if symbol.startswith("PI_"):
        base = symbol[3:].split("USD", 1)[0]
        return "BTC" if base == "XBT" else base
    return "BASE"


def _fee_usd(
    leg: LegLike,
    *,
    kind: PnlKind,
    multiplier: Decimal,
    route: RouteLike | None,
    options: OptionsLike,
) -> tuple[Decimal | None, bool]:
    if leg.fee is not None:
        if (leg.fee_currency or "").strip().lower() in {"usd", "usdt", "zfusd", "zusd"}:
            return leg.fee, False
        if _is_base_currency(leg.fee_currency, route):
            return leg.fee * leg.price, False
        return None, False
    if not options.estimate_fees:
        return None, False
    role = (leg.liquidity_role or "").strip().lower()
    rate = options.maker_fee_rate if role in {"m", "maker"} else options.taker_fee_rate
    notional = (
        leg.quantity * multiplier
        if kind == PnlKind.INVERSE
        else leg.price * leg.quantity * multiplier
    )
    return notional * rate, True


def _is_base_currency(currency: str | None, route: RouteLike | None) -> bool:
    value = (currency or "").strip().upper()
    base = inverse_base_currency(route)
    aliases = {base, "XBT"} if base == "BTC" else {base}
    return bool(value) and value in aliases


def _multiplier(instrument: InstrumentLike | None) -> Decimal:
    value = instrument.contract_size if instrument is not None else Decimal("1")
    return value if value > 0 else Decimal("1")
