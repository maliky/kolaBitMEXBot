"""Pure identifier helpers for pair-cycle order emissions.

Purpose: derive stable, exchange-safe client identifiers for head and tail
orders emitted by the pair reducer.
Inputs: pair metadata and timestamp.
Outputs: normalized client order identifier strings.
Side effects: none.
Important types: `OrderPairSpec`.
Role: pure logic.
Transitional: yes, extracted from `pair_cycle.py` as part of reducer cleanup.
"""
from __future__ import annotations

import re
import secrets
import string
from datetime import datetime, timezone

import coolname

from kolabi.bot.domain import OrderPairSpec

_RUN_MARKER_ALPHABET = string.ascii_uppercase + string.digits
_RUN_MARKER_LENGTH = 4


def generate_run_marker() -> str:
    """Return one short marker for correlating all orders from a bot run."""

    return "".join(secrets.choice(_RUN_MARKER_ALPHABET) for _ in range(_RUN_MARKER_LENGTH))


def head_client_order_id(
    pair: OrderPairSpec,
    *,
    attempt_index: int = 1,
    at: datetime | None = None,
    run_marker: str | None = None,
) -> str:
    """Build a readable, exchange-safe client identifier for head submissions."""
    del pair
    return _client_order_id(
        "H",
        attempt_index=attempt_index,
        at=at,
        run_marker=run_marker,
    )


def tail_client_order_id(
    pair: OrderPairSpec,
    *,
    attempt_index: int = 1,
    at: datetime | None = None,
    run_marker: str | None = None,
) -> str:
    """Build a readable, exchange-safe client identifier for tail submissions."""
    del pair
    return _client_order_id(
        "T",
        attempt_index=attempt_index,
        at=at,
        run_marker=run_marker,
    )


def _client_order_id(
    prefix: str,
    *,
    attempt_index: int,
    at: datetime | None = None,
    run_marker: str | None = None,
) -> str:
    timestamp = at if at is not None else datetime.now(timezone.utc)
    stamp = timestamp.strftime("%y%m%d%H%M%S")
    word = _slug_word()
    attempt = max(1, int(attempt_index))
    marker = _normalise_run_marker(run_marker)
    marker_part = "" if marker is None else f"-{marker}"
    candidate = f"{prefix}{attempt}{word}{marker_part}-{stamp}"
    safe = re.sub(r"[^A-Za-z0-9-]+", "-", candidate).strip("-")
    return safe[:64]


def _normalise_run_marker(value: str | None) -> str | None:
    if value is None:
        return None
    marker = str(value).strip().upper()
    if len(marker) != _RUN_MARKER_LENGTH or not marker.isalnum():
        raise ValueError("run marker must contain exactly four ASCII letters or digits")
    if not marker.isascii():
        raise ValueError("run marker must contain exactly four ASCII letters or digits")
    return marker


def _slug_word() -> str:
    slug = str(coolname.generate_slug(2))
    words = [word for word in slug.split("-") if word]
    if words:
        return words[0]
    return "signal"
