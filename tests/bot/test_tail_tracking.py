from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from kolabi.bot.domain import HeadSpec, OrderPairSpec, Side, TailSpec, TimeWindow
from kolabi.bot.tail_tracking import (
    TailTrailingConfig,
    initial_tail_trail,
    step_tail_trail,
)


def sample_pair(
    *,
    side: Side,
    tail: float = 99.50,
    tail_type: str = "tB",
    tail_unblock: float | None = None,
    tail_unblock_type: str = "uD",
    second_update_wait: float = 0.0,
) -> OrderPairSpec:
    return OrderPairSpec(
        name="pair-a",
        window=TimeWindow(start_minutes=-1.0, end_minutes=10.0),
        try_num=1,
        dr_pause=None,
        timeout=60,
        head=HeadSpec(side=side, order_type="Limit"),
        head_price=(100.0, 101.0),
        head_price_type="pA",
        head_quantity=2,
        head_quantity_type="qA",
        tail=TailSpec(side=Side.SELL if side == Side.BUY else Side.BUY, order_type="Stop"),
        tail_price_spec=tail,
        tail_price_spec_type=tail_type,
        amount_type=f"qA{tail_type}pD",
        tail_unblock_spec=tail_unblock,
        tail_unblock_spec_type=tail_unblock_type,
        tail_second_update_wait_seconds=second_update_wait,
    )


def test_sell_tail_does_not_amend_when_reference_moves_down() -> None:
    now = datetime.now(timezone.utc)
    pair = sample_pair(side=Side.BUY)
    trail = initial_tail_trail(pair, Decimal("100"), now)

    moved = step_tail_trail(pair, trail, Decimal("98"), now + timedelta(seconds=7))

    assert moved.current_stop_price == trail.current_stop_price


def test_sell_tail_first_unblock_immediately_amends_at_required_distance() -> None:
    now = datetime.now(timezone.utc)
    pair = sample_pair(side=Side.BUY)
    trail = initial_tail_trail(pair, Decimal("100"), now)
    first_unblocked_at = now + timedelta(seconds=14)

    blocked = step_tail_trail(pair, trail, Decimal("100.9"), now + timedelta(seconds=7))
    unblocked = step_tail_trail(
        pair,
        blocked,
        Decimal("102.2"),
        first_unblocked_at,
    )

    assert blocked.current_stop_price == trail.current_stop_price
    assert blocked.first_unblocked_at is None
    assert unblocked.first_unblocked_at == first_unblocked_at
    assert unblocked.current_stop_price > trail.current_stop_price
    assert unblocked.last_amended_at is not None


def test_sell_tail_first_unblock_requirement_uses_guard_width() -> None:
    now = datetime.now(timezone.utc)
    pair = sample_pair(side=Side.BUY, tail=1.0, tail_type="tD")
    trail = initial_tail_trail(pair, Decimal("100"), now)
    first_unblocked_at = now + timedelta(seconds=14)

    blocked = step_tail_trail(
        pair,
        trail,
        Decimal("101.0"),
        now + timedelta(seconds=7),
        spread=Decimal("1"),
    )
    unblocked = step_tail_trail(
        pair,
        blocked,
        Decimal("102.1"),
        first_unblocked_at,
        spread=Decimal("0.25"),
    )

    assert blocked.current_stop_price == trail.current_stop_price
    assert blocked.max_observed_spread == Decimal("1")
    assert unblocked.first_unblocked_at == first_unblocked_at
    assert unblocked.max_observed_spread == Decimal("1")
    assert unblocked.current_stop_price > trail.current_stop_price


def test_sell_tail_tublk_distance_unblocks_before_default_tail_distance() -> None:
    now = datetime.now(timezone.utc)
    default_pair = sample_pair(side=Side.BUY, tail=1.0, tail_type="tD")
    tublk_pair = sample_pair(
        side=Side.BUY,
        tail=1.0,
        tail_type="tD",
        tail_unblock=0.5,
    )
    default_trail = initial_tail_trail(default_pair, Decimal("100"), now)
    tublk_trail = initial_tail_trail(tublk_pair, Decimal("100"), now)
    default_moved = step_tail_trail(
        default_pair,
        default_trail,
        Decimal("100.6"),
        now + timedelta(seconds=7),
    )
    tublk_moved = step_tail_trail(
        tublk_pair,
        tublk_trail,
        Decimal("100.6"),
        now + timedelta(seconds=7),
    )

    assert default_moved.current_stop_price == default_trail.current_stop_price
    assert tublk_moved.current_stop_price > tublk_trail.current_stop_price


def test_sell_tail_tublk_requirement_uses_first_jump_guard() -> None:
    now = datetime.now(timezone.utc)
    pair = sample_pair(
        side=Side.BUY,
        tail=1.0,
        tail_type="tD",
        tail_unblock=0.5,
    )
    trail = initial_tail_trail(pair, Decimal("100"), now)
    blocked = step_tail_trail(
        pair,
        trail,
        Decimal("100.5"),
        now + timedelta(seconds=7),
        spread=Decimal("0.2"),
    )
    moved = step_tail_trail(
        pair,
        blocked,
        Decimal("100.8"),
        now + timedelta(seconds=14),
        spread=Decimal("0.1"),
    )

    assert blocked.current_stop_price == trail.current_stop_price
    assert blocked.max_observed_spread == Decimal("0.2")
    assert moved.current_stop_price > trail.current_stop_price
    assert moved.max_observed_spread == Decimal("0.2")


def test_sell_tail_first_amend_jumps_to_entry_plus_guard_width() -> None:
    now = datetime.now(timezone.utc)
    pair = sample_pair(
        side=Side.BUY,
        tail=1.0,
        tail_type="tD",
        tail_unblock=0.5,
    )
    trail = initial_tail_trail(pair, Decimal("100"), now)
    cfg = TailTrailingConfig(max_factor=Decimal("0"))

    moved = step_tail_trail(
        pair,
        trail,
        Decimal("100.8"),
        now + timedelta(seconds=7),
        tick_size=Decimal("0.00001"),
        spread=Decimal("0.2"),
        config=cfg,
    )

    assert moved.current_stop_price == Decimal("100.3")
    assert moved.last_amended_at is not None


def test_sell_tail_respects_six_second_update_gate() -> None:
    now = datetime.now(timezone.utc)
    pair = sample_pair(side=Side.BUY)
    trail = initial_tail_trail(pair, Decimal("100"), now)
    first = step_tail_trail(
        pair,
        trail,
        Decimal("102.5"),
        now + timedelta(seconds=7),
    )
    second = step_tail_trail(
        pair,
        first,
        Decimal("104"),
        now + timedelta(seconds=8),
    )
    third = step_tail_trail(
        pair,
        second,
        Decimal("104.5"),
        now + timedelta(seconds=10),
    )
    fourth = step_tail_trail(
        pair,
        third,
        Decimal("105"),
        now + timedelta(seconds=14),
    )

    assert second.current_stop_price > first.current_stop_price
    assert third.current_stop_price == second.current_stop_price
    assert fourth.current_stop_price > third.current_stop_price


def test_sell_tail_wublk_blocks_second_amend_until_wait_elapsed() -> None:
    now = datetime.now(timezone.utc)
    pair = sample_pair(
        side=Side.BUY,
        tail=1.0,
        tail_type="tD",
        tail_unblock=0.5,
        second_update_wait=60,
    )
    trail = initial_tail_trail(pair, Decimal("100"), now)
    cfg = TailTrailingConfig(max_factor=Decimal("0"))
    first = step_tail_trail(
        pair,
        trail,
        Decimal("100.8"),
        now + timedelta(seconds=7),
        tick_size=Decimal("0.00001"),
        spread=Decimal("0.2"),
        config=cfg,
    )
    early = step_tail_trail(
        pair,
        first,
        Decimal("102"),
        now + timedelta(seconds=30),
        tick_size=Decimal("0.00001"),
        spread=Decimal("0.2"),
        config=cfg,
    )
    second = step_tail_trail(
        pair,
        early,
        Decimal("103"),
        now + timedelta(seconds=67),
        tick_size=Decimal("0.00001"),
        spread=Decimal("0.2"),
        config=cfg,
    )

    assert first.local_amend_count == 1
    assert first.catch_basis_width == Decimal("100.8") - first.current_stop_price
    assert early.current_stop_price == first.current_stop_price
    assert early.local_amend_count == 1
    assert early.catch_basis_width == first.catch_basis_width
    assert second.current_stop_price > early.current_stop_price
    assert second.local_amend_count == 2
    assert second.catch_basis_width == first.catch_basis_width


def test_sell_tail_respects_hysteresis() -> None:
    now = datetime.now(timezone.utc)
    pair = sample_pair(side=Side.BUY)
    trail = initial_tail_trail(pair, Decimal("100"), now)
    cfg = TailTrailingConfig(
        min_amend_ticks=2,
        min_amend_fraction_of_d0=Decimal("0"),
    )
    first = step_tail_trail(
        pair,
        trail,
        Decimal("102.2"),
        now + timedelta(seconds=7),
        tick_size=Decimal("0.5"),
        config=cfg,
    )
    second = step_tail_trail(
        pair,
        first,
        Decimal("102.4"),
        now + timedelta(seconds=14),
        tick_size=Decimal("0.5"),
        config=cfg,
    )
    assert second.current_stop_price == first.current_stop_price


def test_buy_tail_does_not_amend_when_reference_moves_up() -> None:
    now = datetime.now(timezone.utc)
    pair = sample_pair(side=Side.SELL)
    trail = initial_tail_trail(pair, Decimal("100"), now)

    moved = step_tail_trail(pair, trail, Decimal("103"), now + timedelta(seconds=7))

    assert moved.current_stop_price == trail.current_stop_price


def test_buy_tail_first_unblock_immediately_amends_at_required_distance() -> None:
    now = datetime.now(timezone.utc)
    pair = sample_pair(side=Side.SELL)
    trail = initial_tail_trail(pair, Decimal("100"), now)
    first_unblocked_at = now + timedelta(seconds=14)

    blocked = step_tail_trail(pair, trail, Decimal("99.2"), now + timedelta(seconds=7))
    unblocked = step_tail_trail(
        pair,
        blocked,
        Decimal("97.7"),
        first_unblocked_at,
    )

    assert blocked.current_stop_price == trail.current_stop_price
    assert blocked.first_unblocked_at is None
    assert unblocked.first_unblocked_at == first_unblocked_at
    assert unblocked.current_stop_price < trail.current_stop_price
    assert unblocked.last_amended_at is not None


def test_buy_tail_first_unblock_requirement_uses_guard_width() -> None:
    now = datetime.now(timezone.utc)
    pair = sample_pair(side=Side.SELL, tail=1.0, tail_type="tD")
    trail = initial_tail_trail(pair, Decimal("100"), now)
    first_unblocked_at = now + timedelta(seconds=14)

    blocked = step_tail_trail(
        pair,
        trail,
        Decimal("99.0"),
        now + timedelta(seconds=7),
        spread=Decimal("1"),
    )
    unblocked = step_tail_trail(
        pair,
        blocked,
        Decimal("97.9"),
        first_unblocked_at,
        spread=Decimal("0.25"),
    )

    assert blocked.current_stop_price == trail.current_stop_price
    assert blocked.max_observed_spread == Decimal("1")
    assert unblocked.first_unblocked_at == first_unblocked_at
    assert unblocked.max_observed_spread == Decimal("1")
    assert unblocked.current_stop_price < trail.current_stop_price


def test_buy_tail_tublk_uses_favourable_downward_move() -> None:
    now = datetime.now(timezone.utc)
    pair = sample_pair(
        side=Side.SELL,
        tail=1.0,
        tail_type="tD",
        tail_unblock=0.5,
    )
    trail = initial_tail_trail(pair, Decimal("100"), now)

    raw_up = step_tail_trail(
        pair,
        trail,
        Decimal("100.6"),
        now + timedelta(seconds=7),
    )
    favourable_down = step_tail_trail(
        pair,
        trail,
        Decimal("99.4"),
        now + timedelta(seconds=7),
    )

    assert raw_up.current_stop_price == trail.current_stop_price
    assert raw_up.first_unblocked_at is None
    assert favourable_down.current_stop_price < trail.current_stop_price
    assert favourable_down.last_amended_at is not None


def test_buy_tail_first_amend_jumps_to_entry_minus_guard_width() -> None:
    now = datetime.now(timezone.utc)
    pair = sample_pair(
        side=Side.SELL,
        tail=1.0,
        tail_type="tD",
        tail_unblock=0.5,
    )
    trail = initial_tail_trail(pair, Decimal("100"), now)
    cfg = TailTrailingConfig(max_factor=Decimal("0"))

    moved = step_tail_trail(
        pair,
        trail,
        Decimal("99.2"),
        now + timedelta(seconds=7),
        tick_size=Decimal("0.00001"),
        spread=Decimal("0.2"),
        config=cfg,
    )

    assert moved.current_stop_price == Decimal("99.7")
    assert moved.last_amended_at is not None


def test_first_jump_applies_only_to_first_tail_amend() -> None:
    now = datetime.now(timezone.utc)
    pair = sample_pair(
        side=Side.SELL,
        tail=1.0,
        tail_type="tD",
        tail_unblock=0.5,
    )
    trail = initial_tail_trail(pair, Decimal("100"), now)
    cfg = TailTrailingConfig(
        max_factor=Decimal("0.9"),
    )
    first = step_tail_trail(
        pair,
        trail,
        Decimal("99.2"),
        now + timedelta(seconds=7),
        tick_size=Decimal("0.00001"),
        spread=Decimal("0.2"),
        config=cfg,
    )
    second = step_tail_trail(
        pair,
        first,
        Decimal("98.0"),
        now + timedelta(seconds=14),
        tick_size=Decimal("0.00001"),
        spread=Decimal("0.2"),
        config=cfg,
    )

    assert first.current_stop_price == Decimal("99.7")
    assert second.current_stop_price < first.current_stop_price


def test_buy_tail_respects_six_second_update_gate() -> None:
    now = datetime.now(timezone.utc)
    pair = sample_pair(side=Side.SELL)
    trail = initial_tail_trail(pair, Decimal("100"), now)
    first = step_tail_trail(
        pair,
        trail,
        Decimal("97.5"),
        now + timedelta(seconds=7),
    )
    second = step_tail_trail(
        pair,
        first,
        Decimal("96.5"),
        now + timedelta(seconds=8),
    )
    third = step_tail_trail(
        pair,
        second,
        Decimal("96.0"),
        now + timedelta(seconds=10),
    )
    fourth = step_tail_trail(
        pair,
        third,
        Decimal("95.5"),
        now + timedelta(seconds=14),
    )

    assert second.current_stop_price < first.current_stop_price
    assert third.current_stop_price == second.current_stop_price
    assert fourth.current_stop_price < third.current_stop_price


def test_buy_tail_wublk_blocks_second_amend_until_wait_elapsed() -> None:
    now = datetime.now(timezone.utc)
    pair = sample_pair(
        side=Side.SELL,
        tail=1.0,
        tail_type="tD",
        tail_unblock=0.5,
        second_update_wait=60,
    )
    trail = initial_tail_trail(pair, Decimal("100"), now)
    cfg = TailTrailingConfig(max_factor=Decimal("0"))
    first = step_tail_trail(
        pair,
        trail,
        Decimal("99.2"),
        now + timedelta(seconds=7),
        tick_size=Decimal("0.00001"),
        spread=Decimal("0.2"),
        config=cfg,
    )
    early = step_tail_trail(
        pair,
        first,
        Decimal("98"),
        now + timedelta(seconds=30),
        tick_size=Decimal("0.00001"),
        spread=Decimal("0.2"),
        config=cfg,
    )
    second = step_tail_trail(
        pair,
        early,
        Decimal("97"),
        now + timedelta(seconds=67),
        tick_size=Decimal("0.00001"),
        spread=Decimal("0.2"),
        config=cfg,
    )

    assert first.local_amend_count == 1
    assert first.catch_basis_width == first.current_stop_price - Decimal("99.2")
    assert early.current_stop_price == first.current_stop_price
    assert early.local_amend_count == 1
    assert early.catch_basis_width == first.catch_basis_width
    assert second.current_stop_price < early.current_stop_price
    assert second.local_amend_count == 2
    assert second.catch_basis_width == first.catch_basis_width


def test_buy_tail_logbps_tublk_captures_first_jump_basis_before_wublk() -> None:
    now = datetime.now(timezone.utc)
    pair = sample_pair(
        side=Side.SELL,
        tail=246.93,
        tail_type="tB",
        tail_unblock=99.50,
        tail_unblock_type="uB",
        second_update_wait=60,
    )
    trail = initial_tail_trail(pair, Decimal("100"), now)
    cfg = TailTrailingConfig(max_factor=Decimal("0"))

    first = step_tail_trail(
        pair,
        trail,
        Decimal("98.8"),
        now + timedelta(seconds=7),
        tick_size=Decimal("0.00001"),
        config=cfg,
    )
    early = step_tail_trail(
        pair,
        first,
        Decimal("98"),
        now + timedelta(seconds=30),
        tick_size=Decimal("0.00001"),
        config=cfg,
    )

    assert first.current_stop_price == Decimal("99.89992")
    assert first.catch_basis_width == Decimal("1.09992")
    assert early.current_stop_price == first.current_stop_price
    assert early.catch_basis_width == first.catch_basis_width


def test_buy_tail_respects_hysteresis() -> None:
    now = datetime.now(timezone.utc)
    pair = sample_pair(side=Side.SELL)
    trail = initial_tail_trail(pair, Decimal("100"), now)
    cfg = TailTrailingConfig(
        min_amend_ticks=2,
        min_amend_fraction_of_d0=Decimal("0"),
    )
    first = step_tail_trail(
        pair,
        trail,
        Decimal("97.7"),
        now + timedelta(seconds=7),
        tick_size=Decimal("0.5"),
        config=cfg,
    )
    second = step_tail_trail(
        pair,
        first,
        Decimal("97.6"),
        now + timedelta(seconds=14),
        tick_size=Decimal("0.5"),
        config=cfg,
    )
    assert second.current_stop_price == first.current_stop_price


def test_tick_rounding_prevents_subtick_noop_amend() -> None:
    now = datetime.now(timezone.utc)
    pair = sample_pair(side=Side.BUY)
    trail = initial_tail_trail(pair, Decimal("100"), now)
    first = step_tail_trail(
        pair,
        trail,
        Decimal("102.2"),
        now + timedelta(seconds=7),
        tick_size=Decimal("0.5"),
    )
    second = step_tail_trail(
        pair,
        first,
        Decimal("102.2001"),
        now + timedelta(seconds=14),
        tick_size=Decimal("0.5"),
    )
    assert second.current_stop_price == first.current_stop_price


def test_tail_tracking_handles_mixed_naive_and_aware_timestamps() -> None:
    aware = datetime.now(timezone.utc)
    naive = aware.replace(tzinfo=None)
    pair = sample_pair(side=Side.BUY)
    trail = initial_tail_trail(pair, Decimal("100"), aware)

    moved = step_tail_trail(
        pair,
        trail,
        Decimal("102.2"),
        naive + timedelta(seconds=7),
    )
    moved_again = step_tail_trail(
        pair,
        moved,
        Decimal("103.2"),
        aware + timedelta(seconds=14),
    )

    assert moved_again.current_stop_price >= moved.current_stop_price


def test_tail_tracking_survives_extreme_reference_without_overflow() -> None:
    now = datetime.now(timezone.utc)
    pair = sample_pair(side=Side.BUY)
    trail = initial_tail_trail(pair, Decimal("100"), now)

    moved = step_tail_trail(
        pair,
        trail,
        Decimal("999999"),
        now + timedelta(seconds=7),
    )

    assert moved.current_stop_price >= trail.current_stop_price


def test_sell_tail_lag_is_capped_to_twice_initial_distance() -> None:
    now = datetime.now(timezone.utc)
    pair = sample_pair(side=Side.BUY, tail=1.0, tail_type="tD")
    trail = initial_tail_trail(pair, Decimal("100"), now)

    moved = step_tail_trail(
        pair,
        trail,
        Decimal("130"),
        now + timedelta(seconds=7),
    )
    lag = Decimal("130") - moved.current_stop_price

    assert lag <= Decimal("2.1")


def test_buy_tail_lag_is_capped_to_twice_initial_distance() -> None:
    now = datetime.now(timezone.utc)
    pair = sample_pair(side=Side.SELL, tail=1.0, tail_type="tD")
    trail = initial_tail_trail(pair, Decimal("100"), now)

    moved = step_tail_trail(
        pair,
        trail,
        Decimal("70"),
        now + timedelta(seconds=7),
    )
    lag = moved.current_stop_price - Decimal("70")

    assert lag <= Decimal("2.1")
