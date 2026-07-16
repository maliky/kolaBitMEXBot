from __future__ import annotations

import re
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Mapping, TypedDict

from kolabi.bot.domain import (
    HeadSpec,
    OrderPairSpec,
    StrategySpec,
    TailSpec,
    TimeWindow,
    normalize_side,
    opposite_side,
)
from kolabi.bot.exchange_routes import parse_exchange_code
from kolabi.bot.order_codes import parse_order_code, validate_order_code

NumberPair = tuple[float, float]
LOG_BPS_OPEN_BOUND = 100_000.0
DIFFERENTIAL_OPEN_BOUND = 1_000_000.0
ABSOLUTE_PRICE_OPEN_MAX = 1_000_000_000.0
ORG_PLACEHOLDER_RE = re.compile(r"<[^>]+>")
REQUIRED_ORG_COLUMNS = ("pGate", "hPrice")


class _TypedStrategyRow(TypedDict):
    tps_run: NumberPair
    essais: int | str | None
    dr_pause: float | None
    cooldown_minutes: float
    timeout: float | None
    side: str
    pGate: NumberPair
    head_price_type: str
    hPrice: float | None
    head_order_price_type: str
    quantity: int | float | Decimal
    quantity_type: str
    tPrice: float | None
    tail_price_type: str
    oType: str
    hDelta: float | None
    head_delta_type: str
    tDelta: float | None
    tUblk: float | None
    tUblk_type: str
    wUblk: float
    tType: str
    hook: str
    symbol: str | None
    exchange: str | None
    repeat_adjustment: str | None


def read_strategy_file(path: str | Path) -> StrategySpec:
    """Charge une table Org de strategie vers une StrategySpec canonique."""
    strategy_path = Path(path)
    rows = _read_org_strategy_rows(strategy_path)
    names = [str(row["name"]) for row in rows]
    duplicates = _duplicate_values(names)
    if duplicates:
        raise ValueError(f"Duplicate pair name(s) in strategy table: {', '.join(duplicates)}")
    pairs: list[OrderPairSpec] = []
    for row in rows:
        pair_name = str(row["name"])
        typed_row = normalize_typed_strategy_row(row)
        pairs.append(order_pair_from_typed_values(name=pair_name, **typed_row))
    return StrategySpec(name=strategy_path.stem, pairs=tuple(pairs))


def order_pair_from_typed_values(
    *,
    name: str,
    tps_run: NumberPair,
    essais: int | str | None,
    dr_pause: float | None,
    timeout: float | None,
    side: str,
    pGate: NumberPair,
    head_price_type: str,
    hPrice: float | None,
    head_order_price_type: str,
    quantity: int | float | Decimal,
    quantity_type: str,
    tPrice: float | None,
    tail_price_type: str,
    oType: str,
    hDelta: float | None,
    head_delta_type: str,
    tDelta: float | None,
    tType: str,
    hook: str,
    symbol: str | None = None,
    exchange: str | None = None,
    tUblk: float | None = None,
    tUblk_type: str = "uD",
    wUblk: float = 6.0,
    cooldown_minutes: float = 0.0,
    repeat_adjustment: str | None = None,
) -> OrderPairSpec:
    """Normalise des valeurs typees vers une paire canonique."""
    normalized_side = normalize_side(side)
    exchange_name, market_type = parse_exchange_code(exchange)
    window = TimeWindow(start_minutes=float(tps_run[0]), end_minutes=float(tps_run[1]))
    attempts = _validate_attempts(essais, name=name)
    cooldown = _validate_cooldown_minutes(cooldown_minutes, name=name)
    timeout_minutes = _resolve_timeout_minutes(
        timeout=timeout,
        attempts=attempts,
        window=window,
        pause_minutes=dr_pause,
        name=name,
    )
    validate_order_code(oType, role="head")
    validate_order_code(tType, role="tail")
    validate_price_interval(pGate)
    validate_head_price_fields(
        name=name,
        o_type=oType,
        h_delta=hDelta,
        h_price=hPrice,
    )
    validate_quantity(quantity)
    amount_type = (
        f"{quantity_type}{tail_price_type}{head_price_type}"
        f"{head_order_price_type}{head_delta_type}"
    )

    return OrderPairSpec(
        name=name,
        window=window,
        try_num=attempts,
        dr_pause=dr_pause,
        timeout=timeout_minutes,
        head=HeadSpec(
            side=normalized_side,
            order_type=oType.strip(),
            delta=hDelta,
            delta_type=head_delta_type,
        ),
        head_price=pGate,
        head_price_type=head_price_type,
        head_quantity=quantity,
        head_quantity_type=quantity_type,
        tail=TailSpec(
            side=opposite_side(normalized_side),
            order_type=tType.strip(),
            delta=tDelta,
        ),
        tail_price_spec=tPrice,
        tail_price_spec_type=tail_price_type,
        amount_type=amount_type,
        hook_name=hook.strip() or None,
        symbol=None if symbol is None or not symbol.strip() else symbol.strip(),
        exchange=exchange_name,
        market_type=market_type,
        tail_unblock_spec=tUblk,
        tail_unblock_spec_type=tUblk_type,
        tail_second_update_wait_seconds=wUblk,
        head_order_price_spec=hPrice,
        head_order_price_spec_type=head_order_price_type,
        cooldown_minutes=cooldown,
        repeat_adjustment=repeat_adjustment,
    )


def strategy_from_pairs(name: str, pairs: Iterable[OrderPairSpec]) -> StrategySpec:
    """Construit une StrategySpec a partir de paires deja canoniques."""
    return StrategySpec(name=name, pairs=tuple(pairs))


def strategy_from_run_once_args(args: object) -> StrategySpec:
    """Construit une StrategySpec canonique depuis les arguments CLI types."""
    row: dict[str, object] = {
        "tps_run": " ".join(str(value) for value in getattr(args, "tps_run")),
        "essais": getattr(args, "essais"),
        "pause": getattr(args, "pause"),
        "cool": getattr(args, "cool", None),
        "tOut": getattr(args, "tOut"),
        "side": getattr(args, "side"),
        "pGate": getattr(args, "pGate"),
        "hPrice": getattr(args, "hPrice", None),
        "qty": getattr(args, "qty"),
        "tPrice": getattr(args, "tPrice"),
        "oType": getattr(args, "oType"),
        "hDelta": getattr(args, "hDelta"),
        "tDelta": getattr(args, "tDelta"),
        "tUblk": getattr(args, "tUblk", None),
        "wUblk": getattr(args, "wUblk", None),
        "tType": getattr(args, "tType"),
        "hook": getattr(args, "hook", ""),
        "symbol": None,
        "exchg": None,
        "rFunc": None,
    }
    typed_row = normalize_typed_strategy_row(row)
    pair = order_pair_from_typed_values(name=str(getattr(args, "name")), **typed_row)
    return StrategySpec(name=pair.name, pairs=(pair,))


def strategy_to_pretty_dict(strategy: StrategySpec) -> dict[str, object]:
    """Retourne une structure dry-run stable avec alias canoniques."""
    payload = asdict(strategy)
    pairs = payload.get("pairs", [])
    if isinstance(pairs, (list, tuple)):
        normalized_pairs = []
        for pair in pairs:
            if not isinstance(pair, dict):
                normalized_pairs.append(pair)
                continue
            if "head_price" in pair:
                pair["head_price_spec"] = pair["head_price"]
            if "head_price_type" in pair:
                pair["head_price_spec_type"] = pair["head_price_type"]
            if "head_order_price_spec" in pair:
                pair["hPrice_spec"] = pair["head_order_price_spec"]
            if "head_order_price_spec_type" in pair:
                pair["hPrice_spec_type"] = pair["head_order_price_spec_type"]
            if "head_quantity" in pair:
                pair["head_quantity_spec"] = pair["head_quantity"]
            if "head_quantity_type" in pair:
                pair["head_quantity_spec_type"] = pair["head_quantity_type"]
            normalized_pairs.append(pair)
        payload["pairs"] = normalized_pairs
    return payload


def normalize_typed_strategy_row(row: Mapping[str, object]) -> _TypedStrategyRow:
    """Normalise une ligne de table Org en champs intermediaires stables."""
    _reject_legacy_columns(row)
    raw_q, quantity_type = _parse_required_typed_number(
        _row_value(row, "qty"),
        field="qty",
        token_prefix="q",
        allowed_suffixes={"A", "%", "U"},
        as_decimal=True,
    )
    if quantity_type == "q%":
        if raw_q <= 0 or raw_q >= 100:
            raise ValueError("Invalid qty value; expected 0 < percent < 100.")
    t_price, tail_price_type = _parse_optional_typed_number(
        _row_value(row, "tPrice"),
        field="tPrice",
        token_prefix="t",
        allowed_suffixes={"A", "B", "D"},
        default_type="tD",
    )
    p_gate, head_price_type = _parse_optional_typed_interval(
        _row_value(row, "pGate"),
        field="pGate",
        token_prefix="p",
        allowed_suffixes={"A", "B", "D"},
        default_type="pD",
    )
    h_price, head_order_price_type = _parse_optional_typed_number(
        _row_value(row, "hPrice"),
        field="hPrice",
        token_prefix="h",
        allowed_suffixes={"A", "B", "D"},
        default_type="hD",
    )
    h_delta, head_delta_type = _parse_optional_typed_number(
        _row_value(row, "hDelta"),
        field="hDelta",
        token_prefix="o",
        allowed_suffixes={"B", "D"},
        default_type="oD",
    )
    t_delta = _parse_optional_tail_delta(_row_value(row, "tDelta"))
    tail_unblock = _parse_optional_tail_unblock(_row_value(row, "tUblk"))
    tail_wait = _parse_optional_tail_wait(_row_value(row, "wUblk"))

    return {
        "tps_run": _parse_interval(str(_require_row_value(row, "tps_run")), field="tps_run"),
        "essais": _parse_essais(_row_value(row, "essais")),
        "dr_pause": _coerce_to("float", _row_value(row, "pause")),
        "cooldown_minutes": _parse_optional_cooldown(_row_value(row, "cool")),
        "timeout": _coerce_to("float", _row_value(row, "tOut")),
        "side": str(_require_row_value(row, "side")).strip(),
        "pGate": p_gate,
        "head_price_type": head_price_type,
        "hPrice": h_price,
        "head_order_price_type": head_order_price_type,
        "quantity": raw_q,
        "quantity_type": quantity_type,
        "tPrice": t_price,
        "tail_price_type": tail_price_type,
        "oType": str(_require_row_value(row, "oType")).strip(),
        "hDelta": h_delta,
        "head_delta_type": head_delta_type,
        "tDelta": t_delta,
        "tUblk": tail_unblock[0],
        "tUblk_type": tail_unblock[1],
        "wUblk": tail_wait,
        "tType": str(_require_row_value(row, "tType")).strip(),
        "hook": _optional_text(_row_value(row, "hook")) or "",
        "symbol": _optional_text(_row_value(row, "symbol")),
        "exchange": _optional_text(_row_value(row, "exchg")),
        "repeat_adjustment": _optional_text(_row_value(row, "rFunc")),
    }


def _read_org_strategy_rows(path: Path) -> list[dict[str, str]]:
    table = _first_strategy_table(path)
    if table is None:
        raise ValueError(
            "Strategy file must contain an Org table with a 'name' column; "
            "legacy TSV strategy files are no longer supported."
        )
    header, data_rows = table
    if "name" not in header:
        raise ValueError("Strategy table is missing required column 'name'.")
    _reject_legacy_columns({name: "" for name in header})
    _require_org_columns(header)
    rows: list[dict[str, str]] = []
    for line_number, cells in data_rows:
        if _is_ignored_org_data_row(cells):
            continue
        if len(cells) != len(header):
            raise ValueError(
                f"Invalid Org strategy table row at line {line_number}; "
                f"expected {len(header)} cells, saw {len(cells)}."
            )
        rows.append(dict(zip(header, cells, strict=True)))
    return rows


def _first_strategy_table(path: Path) -> tuple[list[str], list[tuple[int, list[str]]]] | None:
    current: list[tuple[int, list[str]]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped.startswith("|"):
            if current:
                table = _strategy_table_from_block(current)
                if table is not None:
                    return table
                current = []
            continue
        if _is_org_hline(stripped):
            continue
        current.append((line_number, _split_org_row(stripped)))
    if current:
        return _strategy_table_from_block(current)
    return None


def _strategy_table_from_block(
    block: list[tuple[int, list[str]]],
) -> tuple[list[str], list[tuple[int, list[str]]]] | None:
    if not block:
        return None
    header = _normalise_header(block[0][1])
    if "name" not in header:
        return None
    return header, block[1:]


def _normalise_header(cells: list[str]) -> list[str]:
    header: list[str] = []
    for cell in cells:
        name = cell.strip()
        if not name:
            continue
        header.append(name)
    return header


def _split_org_row(line: str) -> list[str]:
    raw = line.strip()
    if raw.startswith("|"):
        raw = raw[1:]
    if raw.endswith("|"):
        raw = raw[:-1]
    return [cell.strip() for cell in raw.split("|")]


def _is_org_hline(line: str) -> bool:
    raw = line.strip().strip("|").strip()
    return bool(raw) and all(char in "-+" for char in raw)


def _is_ignored_org_data_row(cells: list[str]) -> bool:
    return all(not cell.strip() for cell in cells) or any(
        ORG_PLACEHOLDER_RE.search(cell) for cell in cells
    )


def _duplicate_values(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    duplicates: list[str] = []
    for value in values:
        if value in seen and value not in duplicates:
            duplicates.append(value)
        seen.add(value)
    return tuple(duplicates)


def _reject_legacy_columns(row: Mapping[str, object]) -> None:
    legacy = {
        "atype",
        "Cool",
        "exchange",
        "hprice",
        "oDelta",
        "pgate",
        "prix",
        "q",
        "quantity",
        "tp",
    }
    present = sorted(name for name in legacy if name in row)
    if present:
        raise ValueError(
            "Legacy strategy field(s) are no longer supported: "
            f"{', '.join(present)}. Use typed Org fields such as qty, tPrice, pGate, hPrice, cool, and exchg."
        )


def _require_org_columns(header: list[str]) -> None:
    missing = [name for name in REQUIRED_ORG_COLUMNS if name not in header]
    if missing:
        raise ValueError(
            "Strategy table is missing required column(s): "
            f"{', '.join(missing)}."
        )


def _row_value(row: Mapping[str, object], *names: str) -> object:
    for name in names:
        if name in row:
            return row[name]
    return None


def _require_row_value(row: Mapping[str, object], name: str) -> object:
    value = _row_value(row, name)
    if _optional_text(value) is None:
        raise ValueError(f"Missing required strategy field '{name}'.")
    return value


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_to(kind: str, value: object) -> int | float | None:
    text = _optional_text(value)
    if text is None:
        return None
    if kind == "int":
        return int(text)
    if kind == "float":
        return float(text)
    raise ValueError(f"Unsupported coercion kind '{kind}'")


def _parse_essais(value: object) -> int | str | None:
    text = _optional_text(value)
    if text is None:
        return None
    if text == "*":
        return text
    return int(text)


def _validate_attempts(essais: int | str | None, *, name: str) -> int | None:
    if essais is None:
        return 1
    if essais == "*":
        return None
    attempts = int(essais)
    if attempts < 1:
        raise ValueError(f"Invalid essais for pair '{name}'; expected a positive integer or '*'.")
    return attempts


def _validate_cooldown_minutes(value: float | None, *, name: str) -> float:
    minutes = 0.0 if value is None else float(value)
    if minutes < 0:
        raise ValueError(f"Invalid cool for pair '{name}'; expected a non-negative wait in minutes.")
    return minutes


def _resolve_timeout_minutes(
    *,
    timeout: float | None,
    attempts: int | None,
    window: TimeWindow,
    pause_minutes: float | None,
    name: str,
) -> float | None:
    if timeout is not None:
        if timeout <= 0:
            raise ValueError(f"Invalid tOut for pair '{name}'; expected a positive number.")
        return timeout
    if attempts is None:
        raise ValueError(
            f"Missing tOut for pair '{name}'; essais='*' requires an explicit per-attempt tOut."
        )
    span_minutes = window.end_minutes - window.start_minutes
    pause_total = (pause_minutes or 0.0) * attempts
    computed = (span_minutes - pause_total) / attempts
    if computed <= 0:
        raise ValueError(
            f"Invalid automatic tOut for pair '{name}'; tps_run span minus pauses must be positive."
        )
    return computed


def _parse_interval(raw: str, *, field: str) -> NumberPair:
    parts = raw.strip().split()
    if len(parts) != 2:
        raise ValueError(
            f"Invalid {field} interval '{raw}'; expected exactly two whitespace-separated bounds."
        )
    low, high = parts
    return float(low), float(high)


def _parse_required_typed_number(
    value: object,
    *,
    field: str,
    token_prefix: str,
    allowed_suffixes: set[str],
    as_int: bool = False,
    as_decimal: bool = False,
) -> tuple[int | float | Decimal, str]:
    parsed, amount_type = _parse_optional_typed_number(
        value,
        field=field,
        token_prefix=token_prefix,
        allowed_suffixes=allowed_suffixes,
        default_type=None,
        as_int=as_int,
        as_decimal=as_decimal,
    )
    if parsed is None:
        raise ValueError(f"Missing required strategy field '{field}'.")
    return parsed, amount_type


def _parse_optional_typed_number(
    value: object,
    *,
    field: str,
    token_prefix: str,
    allowed_suffixes: set[str],
    default_type: str | None,
    as_int: bool = False,
    as_decimal: bool = False,
) -> tuple[int | float | Decimal | None, str]:
    text = _optional_text(value)
    if text is None:
        if default_type is None:
            raise ValueError(f"Missing required strategy field '{field}'.")
        return None, default_type
    suffix, payload = _split_typed_payload(
        text,
        field=field,
        allowed_suffixes=allowed_suffixes,
    )
    if not payload:
        raise ValueError(f"Invalid {field} value '{text}'; missing numeric payload.")
    number = Decimal(payload) if as_decimal else float(payload)
    is_integer = (
        number == number.to_integral_value()
        if isinstance(number, Decimal)
        else number.is_integer()
    )
    if as_int and not is_integer:
        raise ValueError(f"Invalid {field} value '{text}'; expected an integer payload.")
    return (int(number) if as_int else number), f"{token_prefix}{suffix}"


def _parse_optional_typed_interval(
    value: object,
    *,
    field: str,
    token_prefix: str,
    allowed_suffixes: set[str],
    default_type: str,
) -> tuple[NumberPair, str]:
    text = _optional_text(value)
    if text is None:
        return _open_interval_for_suffix(default_type[-1]), default_type
    suffix, payload = _split_typed_payload(
        text,
        field=field,
        allowed_suffixes=allowed_suffixes,
    )
    parts = payload.split()
    if len(parts) != 2:
        raise ValueError(
            f"Invalid {field} interval '{text}'; expected typed bounds like 'D- +'."
        )
    low, high = parts
    low_value, high_value = _bounds_for_suffix(suffix, low, high)
    return (float(low_value), float(high_value)), f"{token_prefix}{suffix}"


def _parse_optional_tail_delta(value: object) -> float | None:
    text = _optional_text(value)
    if text is None:
        return None
    suffix, payload = _split_typed_payload(
        text,
        field="tDelta",
        allowed_suffixes={"D"},
    )
    if suffix != "D":
        raise ValueError("Invalid tDelta type; only differential 'D' is supported.")
    if not payload:
        raise ValueError(f"Invalid tDelta value '{text}'; missing numeric payload.")
    return float(payload)


def _parse_optional_tail_unblock(value: object) -> tuple[float | None, str]:
    parsed, amount_type = _parse_optional_typed_number(
        value,
        field="tUblk",
        token_prefix="u",
        allowed_suffixes={"B", "D"},
        default_type="uD",
    )
    if parsed is not None and parsed < 0:
        raise ValueError("Invalid tUblk value; expected a non-negative distance.")
    return parsed, amount_type


def _parse_optional_tail_wait(value: object) -> float:
    text = _optional_text(value)
    if text is None:
        return 6.0 * 60.0
    minutes = float(text)
    if minutes < 0:
        raise ValueError("Invalid wUblk value; expected a non-negative wait in minutes.")
    return minutes * 60.0


def _parse_optional_cooldown(value: object) -> float:
    text = _optional_text(value)
    if text is None:
        return 0.0
    minutes = float(text)
    if minutes < 0:
        raise ValueError("Invalid cool value; expected a non-negative wait in minutes.")
    return minutes


def _split_typed_payload(
    text: str,
    *,
    field: str,
    allowed_suffixes: set[str],
) -> tuple[str, str]:
    suffix = text[0]
    if suffix not in allowed_suffixes:
        allowed = " or ".join(sorted(allowed_suffixes))
        raise ValueError(f"{field} value '{text}' must start with {allowed}.")
    return suffix, text[1:].strip()


def _open_interval_for_suffix(suffix: str) -> NumberPair:
    low, high = _bounds_for_suffix(suffix, "-", "+")
    return float(low), float(high)


def _bounds_for_suffix(suffix: str, low: str, high: str) -> tuple[str, str]:
    if suffix == "B":
        return (
            str(-LOG_BPS_OPEN_BOUND) if low == "-" else low,
            str(LOG_BPS_OPEN_BOUND) if high == "+" else high,
        )
    if suffix == "D":
        return (
            str(-DIFFERENTIAL_OPEN_BOUND) if low == "-" else low,
            str(DIFFERENTIAL_OPEN_BOUND) if high == "+" else high,
        )
    if suffix == "A":
        return (
            "0" if low == "-" else low,
            str(ABSOLUTE_PRICE_OPEN_MAX) if high == "+" else high,
        )
    raise ValueError(f"Unsupported interval type '{suffix}'.")


def validate_price_interval(interval: NumberPair) -> None:
    """Validate that the price gate interval is strictly ascending."""
    low, high = interval
    if low >= high:
        raise ValueError(
            f"Invalid canonical price interval: low={low} high={high}; expected low < high."
        )


def validate_head_price_fields(
    *,
    name: str,
    o_type: str,
    h_delta: float | None,
    h_price: float | None,
) -> None:
    """Keep the head price grammar aligned with exchange order families."""
    code = parse_order_code(o_type)
    if code.base_key == "M" and h_price is not None:
        raise ValueError(f"Invalid hPrice for pair '{name}'; market heads do not use hPrice.")
    if h_delta is not None and code.base_key not in {"SL", "LT"}:
        raise ValueError(
            f"Invalid hDelta for pair '{name}'; hDelta is only valid for SL or LT heads."
        )


def validate_quantity(quantity: int | float | Decimal | None) -> None:
    """Verifie qu'une quantite strategique est positive quand elle existe."""
    if quantity is not None and quantity <= 0:
        raise ValueError(
            f"Invalid canonical quantity: quantity={quantity}; expected a positive value."
        )
