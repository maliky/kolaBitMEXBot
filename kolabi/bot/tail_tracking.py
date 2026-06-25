"""Pure tail tracking for strategy-managed stop tails.

Purpose: keep the protective tail at its entry width, shorten it on fast
favourable moves, and emit no exchange side effects.
Inputs: immutable pair specification, existing tail trail state, market ticks.
Outputs: immutable tail trail state.
Side effects: none.
Important types: `TailTrailState`, `TailTrailSample`, `OrderPairSpec`.
Role: pure logic.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

from kolabi.bot.domain import OrderPairSpec, Side, TailTrailSample, TailTrailState
from kolabi.bot.price_units import logbps_price_distance
from kolabi.shared.core.runtime_types import to_decimal

DEFAULT_MAX_SAMPLES = 40


@dataclass(frozen=True)
class TailTrailingConfig:
    update_interval_seconds: int = 6
    first_jump_ticks: int = 8
    taker_fee_fraction: Decimal = Decimal("0.0005")
    unblock_multiplier: Decimal = Decimal("2")
    response_denominator_multiplier: Decimal = Decimal("1.9")
    max_r: Decimal = Decimal("2")
    max_factor: Decimal = Decimal("0.9")
    min_amend_ticks: int = 1
    min_amend_fraction_of_d0: Decimal = Decimal("0")
    alp_left: float = 3.0
    alp_right: float = 1.0
    lam_left: float = 1.0
    lam_right: float = 0.5


@dataclass(frozen=True)
class SkewLomaxCurve:
    alp_left: float = 3.0
    alp_right: float = 1.0
    lam_left: float = 1.0
    lam_right: float = 0.5

    def _left_total(self) -> float:
        a, l = self.alp_left, self.lam_left
        return float(l / a * (1.0 - (1.0 + 1.0 / l) ** (-a)))

    def _right_total(self) -> float:
        a, l = self.alp_right, self.lam_right
        return float(l / a * (1.0 - (1.0 + 1.0 / l) ** (-a)))

    def cdf(self, r: float) -> float:
        r = max(0.0, min(2.0, r))
        z = r - 1.0
        left_total = self._left_total()
        total = left_total + self._right_total()

        if z < 0.0:
            a, l = self.alp_left, self.lam_left
            left_area = l / a * (
                (1.0 + (-z) / l) ** (-a)
                - (1.0 + 1.0 / l) ** (-a)
            )
            return float(left_area / total)

        a, l = self.alp_right, self.lam_right
        right_area = l / a * (1.0 - (1.0 + z / l) ** (-a))
        return float((left_total + right_area) / total)

    def factor(self, r: float, *, max_factor: Decimal) -> Decimal:
        return max_factor * Decimal(str(self.cdf(r)))


DEFAULT_TAIL_TRAILING_CONFIG = TailTrailingConfig()


def tail_unblock_distance(
    pair: OrderPairSpec,
    trail: TailTrailState,
    *,
    config: TailTrailingConfig = DEFAULT_TAIL_TRAILING_CONFIG,
) -> Decimal:
    spec = pair.tail_unblock_spec
    if spec is None:
        multiplier = config.unblock_multiplier - Decimal("1")
        return Decimal("0") if multiplier <= 0 else multiplier * trail.baseline_width
    value = to_decimal(spec)
    if value <= 0:
        return Decimal("0")
    unblock_type = (pair.tail_unblock_spec_type or "uD").lower()
    if "ub" in unblock_type:
        return logbps_price_distance(trail.entry_reference_price, value)
    return value


def tail_unblock_requirement(
    pair: OrderPairSpec,
    trail: TailTrailState,
    spread_guard: Decimal | int | float | str | None = None,
    *,
    config: TailTrailingConfig = DEFAULT_TAIL_TRAILING_CONFIG,
) -> Decimal:
    spread = Decimal("0") if spread_guard is None else to_decimal(spread_guard)
    if spread < 0:
        spread = Decimal("0")
    return trail.baseline_width + tail_unblock_distance(pair, trail, config=config) + spread


def initial_tail_trail(
    pair: OrderPairSpec,
    reference_price: Decimal | int | float | str,
    occurred_at: datetime,
) -> TailTrailState:
    """Create initial tail trail from the active pair tail price grammar."""
    occurred_at = _as_utc_aware(occurred_at)
    reference = to_decimal(reference_price)
    stop = _initial_stop_price(pair, reference)
    sample = TailTrailSample(occurred_at=occurred_at, reference_price=reference)
    return TailTrailState(
        entry_reference_price=reference,
        baseline_width=abs(reference - stop),
        current_stop_price=stop,
        previous_stop_price=stop,
        samples=(sample,),
        initial_stop_price=stop,
        last_reference_price=reference,
        last_amended_at=None,
        last_stop_update_at=occurred_at,
    )


def step_tail_trail(
    pair: OrderPairSpec,
    trail: TailTrailState,
    reference_price: Decimal | int | float | str,
    occurred_at: datetime,
    *,
    tick_size: Decimal | int | float | str | None = None,
    spread: Decimal | int | float | str | None = None,
    config: TailTrailingConfig = DEFAULT_TAIL_TRAILING_CONFIG,
    symbol: str | None = None,
) -> TailTrailState:
    """Advance tail trail by one market tick and amend only on signed improvement."""
    del symbol
    occurred_at = _as_utc_aware(occurred_at)
    reference = to_decimal(reference_price)
    current_spread = _spread_or_none(spread)
    max_spread = trail.max_observed_spread
    if current_spread is not None and current_spread > max_spread:
        max_spread = current_spread
    samples = _bounded_samples(
        trail.samples
        + (
            TailTrailSample(
                occurred_at=occurred_at,
                reference_price=reference,
                spread=current_spread,
            ),
        )
    )
    next_state = replace(
        trail,
        samples=samples,
        last_reference_price=reference,
        max_observed_spread=max_spread,
    )

    if trail.baseline_width <= 0:
        return next_state

    if trail.last_amended_at is not None:
        elapsed = occurred_at - _as_utc_aware(trail.last_amended_at)
        wait_seconds = _tail_update_wait_seconds(pair, trail, config)
        if elapsed < timedelta(seconds=wait_seconds):
            return next_state

    curve = SkewLomaxCurve(
        alp_left=config.alp_left,
        alp_right=config.alp_right,
        lam_left=config.lam_left,
        lam_right=config.lam_right,
    )
    d0 = trail.baseline_width
    previous_ref = trail.last_reference_price
    current_stop = trail.current_stop_price
    tick = _tick_size_or_none(tick_size)
    min_improvement = _min_improvement(d0, tick, config)

    guard_width = tail_first_jump_guard_width(next_state, tick, config=config)
    max_lag = tail_unblock_requirement(pair, next_state, guard_width, config=config)

    if pair.tail.side == Side.SELL:
        d_raw = reference - current_stop
        favorable = previous_ref is None or reference > previous_ref
        if d_raw <= 0 or not favorable:
            return next_state
        next_state, first_unblock_ready = _first_unblock_ready(
            trail,
            next_state,
            d_raw=d_raw,
            max_lag=max_lag,
            occurred_at=occurred_at,
        )
        if not first_unblock_ready:
            return next_state
        response_width = _response_basis_width(trail)
        r = _clamp_r(
            d_raw / (config.response_denominator_multiplier * response_width),
            config.max_r,
        )
        factor = curve.factor(float(r), max_factor=config.max_factor)
        candidate = current_stop + factor * d_raw
        # Hard invariant: include spread guard before tightening the stop.
        lag_cap_candidate = reference - max_lag
        if candidate < lag_cap_candidate:
            candidate = lag_cap_candidate
        first_jump = _first_jump_stop_price(trail, guard_width)
        if first_jump is not None and candidate < first_jump:
            candidate = first_jump
        if candidate < current_stop + min_improvement:
            return next_state
        rounded_current = _round_to_tick(current_stop, tick)
        rounded_candidate = _round_to_tick(candidate, tick)
        if rounded_candidate <= rounded_current:
            return next_state
        return _amended_tail_trail(
            next_state,
            trail=trail,
            previous_stop=current_stop,
            new_stop=rounded_candidate,
            reference=reference,
            occurred_at=occurred_at,
        )

    d_raw = current_stop - reference
    favorable = previous_ref is None or reference < previous_ref
    if d_raw <= 0 or not favorable:
        return next_state
    next_state, first_unblock_ready = _first_unblock_ready(
        trail,
        next_state,
        d_raw=d_raw,
        max_lag=max_lag,
        occurred_at=occurred_at,
    )
    if not first_unblock_ready:
        return next_state
    response_width = _response_basis_width(trail)
    r = _clamp_r(
        d_raw / (config.response_denominator_multiplier * response_width),
        config.max_r,
    )
    factor = curve.factor(float(r), max_factor=config.max_factor)
    candidate = current_stop - factor * d_raw
    # Hard invariant: include spread guard before tightening the stop.
    lag_cap_candidate = reference + max_lag
    if candidate > lag_cap_candidate:
        candidate = lag_cap_candidate
    first_jump = _first_jump_stop_price(trail, guard_width)
    if first_jump is not None and candidate > first_jump:
        candidate = first_jump
    if candidate > current_stop - min_improvement:
        return next_state
    rounded_current = _round_to_tick(current_stop, tick)
    rounded_candidate = _round_to_tick(candidate, tick)
    if rounded_candidate >= rounded_current:
        return next_state
    return _amended_tail_trail(
        next_state,
        trail=trail,
        previous_stop=current_stop,
        new_stop=rounded_candidate,
        reference=reference,
        occurred_at=occurred_at,
    )


def _first_unblock_ready(
    trail: TailTrailState,
    next_state: TailTrailState,
    *,
    d_raw: Decimal,
    max_lag: Decimal,
    occurred_at: datetime,
) -> tuple[TailTrailState, bool]:
    if trail.last_amended_at is not None:
        return next_state, True
    if d_raw < max_lag:
        return next_state, False

    if trail.first_unblocked_at is None:
        next_state = replace(next_state, first_unblocked_at=occurred_at)
    return next_state, True


def _initial_stop_price(pair: OrderPairSpec, reference: Decimal) -> Decimal:
    spec = pair.tail_price_spec
    if spec is None:
        raise ValueError(f"Order pair '{pair.name}' needs a tail price specification")
    value = to_decimal(spec)
    tail_type = (pair.tail_price_spec_type or "").lower()
    if "tb" in tail_type or "tb" in pair.amount_type.lower():
        offset = logbps_price_distance(reference, value)
    elif "td" in tail_type or "td" in pair.amount_type.lower():
        offset = value
    else:
        return value
    return reference - offset if pair.head.side == Side.BUY else reference + offset


def _bounded_samples(
    samples: tuple[TailTrailSample, ...],
    *,
    max_samples: int = DEFAULT_MAX_SAMPLES,
) -> tuple[TailTrailSample, ...]:
    return samples[-max_samples:]


def _clamp_r(value: Decimal, max_r: Decimal) -> Decimal:
    if value <= 0:
        return Decimal("0")
    if value >= max_r:
        return max_r
    return value


def _tick_size_or_none(value: Decimal | int | float | str | None) -> Decimal | None:
    if value is None:
        return None
    tick = to_decimal(value)
    if tick <= 0:
        return None
    return tick


def _spread_or_none(value: Decimal | int | float | str | None) -> Decimal | None:
    if value is None:
        return None
    spread = to_decimal(value)
    if spread < 0:
        return None
    return spread


def _min_improvement(
    d0: Decimal,
    tick_size: Decimal | None,
    config: TailTrailingConfig,
) -> Decimal:
    tick_component = (
        Decimal(config.min_amend_ticks) * tick_size if tick_size is not None else Decimal("0")
    )
    fraction_component = config.min_amend_fraction_of_d0 * d0
    return tick_component if tick_component >= fraction_component else fraction_component


def _tail_update_wait_seconds(
    pair: OrderPairSpec,
    trail: TailTrailState,
    config: TailTrailingConfig,
) -> float:
    if trail.local_amend_count == 1:
        return max(0.0, float(pair.tail_second_update_wait_seconds or 0.0))
    return float(max(0, config.update_interval_seconds))


def _response_basis_width(trail: TailTrailState) -> Decimal:
    if trail.catch_basis_width is not None and trail.catch_basis_width > 0:
        return trail.catch_basis_width
    return trail.baseline_width


def _catch_basis_after_amend(
    trail: TailTrailState,
    new_stop: Decimal,
    reference: Decimal,
) -> Decimal | None:
    if trail.catch_basis_width is not None:
        return trail.catch_basis_width
    if trail.local_amend_count == 0:
        basis = abs(reference - new_stop)
        return basis if basis > 0 else None
    return None


def _amended_tail_trail(
    next_state: TailTrailState,
    *,
    trail: TailTrailState,
    previous_stop: Decimal,
    new_stop: Decimal,
    reference: Decimal,
    occurred_at: datetime,
) -> TailTrailState:
    return replace(
        next_state,
        current_stop_price=new_stop,
        previous_stop_price=previous_stop,
        last_amended_at=occurred_at,
        last_stop_update_at=occurred_at,
        local_amend_count=trail.local_amend_count + 1,
        catch_basis_width=_catch_basis_after_amend(
            trail,
            new_stop,
            reference,
        ),
    )


def tail_first_jump_guard_width(
    trail: TailTrailState,
    tick_size: Decimal | int | float | str | None,
    observed_spread: Decimal | int | float | str | None = None,
    *,
    config: TailTrailingConfig = DEFAULT_TAIL_TRAILING_CONFIG,
) -> Decimal:
    tick = _tick_size_or_none(tick_size)
    tick_count = max(config.first_jump_ticks, 0)
    tick_floor = Decimal(tick_count) * tick if tick is not None else Decimal("0")
    spread_floor = trail.max_observed_spread
    current_spread = _spread_or_none(observed_spread)
    if current_spread is not None and current_spread > spread_floor:
        spread_floor = current_spread
    if spread_floor < 0:
        spread_floor = Decimal("0")
    market_floor = spread_floor if spread_floor >= tick_floor else tick_floor
    fraction = config.taker_fee_fraction
    if fraction < 0:
        fraction = Decimal("0")
    return market_floor + Decimal("2") * trail.entry_reference_price * fraction


def _first_jump_stop_price(
    trail: TailTrailState,
    guard_width: Decimal,
) -> Decimal | None:
    if trail.last_amended_at is not None:
        return None
    if trail.current_stop_price >= trail.entry_reference_price:
        return trail.entry_reference_price - guard_width
    return trail.entry_reference_price + guard_width


def _round_to_tick(value: Decimal, tick_size: Decimal | None) -> Decimal:
    if tick_size is None or tick_size <= 0:
        return value
    return (value / tick_size).to_integral_value(rounding=ROUND_HALF_UP) * tick_size


def _as_utc_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
