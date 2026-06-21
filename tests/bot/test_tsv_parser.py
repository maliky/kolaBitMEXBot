from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from kolabi.bot.tsv.parser import read_strategy_file

DEFAULT_COLUMNS = (
    "exchg",
    "symbol",
    "name",
    "tps_run",
    "essais",
    "tOut",
    "pause",
    "cool",
    "side",
    "oType",
    "hDelta",
    "qty",
    "tType",
    "tDelta",
    "pGate",
    "hPrice",
    "tPrice",
    "tUblk",
    "wUblk",
    "hook",
)


def _org_row(values: list[str]) -> str:
    return "| " + " | ".join(values) + " |"


def _write_strategy(
    path: Path,
    rows: list[dict[str, str]],
    *,
    columns: tuple[str, ...] = DEFAULT_COLUMNS,
    prefix: str = "",
    suffix: str = "",
) -> Path:
    lines = []
    if prefix:
        lines.extend(prefix.rstrip("\n").splitlines())
    lines.append(_org_row(list(columns)))
    lines.append("|" + "+".join("---" for _ in columns) + "|")
    for row in rows:
        lines.append(_org_row([row.get(column, "") for column in columns]))
    if suffix:
        lines.extend(suffix.rstrip("\n").splitlines())
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _base_row(**overrides: str) -> dict[str, str]:
    row = {
        "exchg": "KRKF",
        "symbol": "PI_XBTUSD",
        "name": "PAIR",
        "tps_run": "0 60",
        "essais": "1",
        "tOut": "4",
        "pause": "",
        "cool": "",
        "side": "buy",
        "oType": "L",
        "hDelta": "",
        "qty": "A3",
        "tType": "S-",
        "tDelta": "",
        "pGate": "D- +",
        "hPrice": "",
        "tPrice": "D8",
        "tUblk": "",
        "wUblk": "",
        "hook": "",
    }
    row.update(overrides)
    return row


def test_org_strategy_table_parses_and_ignores_surrounding_text(tmp_path: Path) -> None:
    path = tmp_path / "typed.tsv"
    _write_strategy(
        path,
        [
            _base_row(
                exchg="BTX",
                symbol="XBTUSD",
                name="XBTC_SEL",
                tps_run="0 1440",
                tOut="6",
                side="sell",
                hPrice="D6",
                qty="A1",
                tPrice="D20",
            ),
            _base_row(
                exchg="KRKF",
                symbol="PI_ADAUSD",
                name="KADA_SEL",
                tps_run="0 1440",
                tOut="6",
                side="sell",
                hPrice="%0.20",
                qty="A1",
                tPrice="%0.10",
                hook="XBTC_SEL-head-filled",
            ),
        ],
        prefix="* Demo table\nA note before the table.",
        suffix="# trailing note",
    )

    strategy = read_strategy_file(path)

    assert [pair.name for pair in strategy.pairs] == ["XBTC_SEL", "KADA_SEL"]
    assert [
        (pair.exchange, pair.market_type, pair.symbol)
        for pair in strategy.pairs
    ] == [
        ("bitmex", "futures", "XBTUSD"),
        ("kraken", "futures", "PI_ADAUSD"),
    ]
    assert strategy.pairs[0].head_order_price_spec == 6.0
    assert strategy.pairs[0].head_order_price_spec_type == "hD"
    assert strategy.pairs[0].head_quantity == 1
    assert strategy.pairs[0].head_quantity_type == "qA"
    assert strategy.pairs[0].tail_price_spec == 20.0
    assert strategy.pairs[0].tail_price_spec_type == "tD"
    assert strategy.pairs[1].head_order_price_spec == 0.2
    assert strategy.pairs[1].head_order_price_spec_type == "h%"
    assert strategy.pairs[1].tail_price_spec == 0.1
    assert strategy.pairs[1].tail_price_spec_type == "t%"


def test_org_strategy_table_accepts_usd_notional_quantity(tmp_path: Path) -> None:
    path = tmp_path / "usd.org"
    _write_strategy(path, [_base_row(qty="U15.5")])

    pair = read_strategy_file(path).pairs[0]

    assert pair.head_quantity == 15.5
    assert pair.head_quantity_type == "qU"
    assert pair.amount_type.startswith("qU")


def test_org_strategy_table_accepts_decimal_absolute_quantity(tmp_path: Path) -> None:
    path = tmp_path / "absolute.org"
    _write_strategy(path, [_base_row(qty="A.0001")])

    pair = read_strategy_file(path).pairs[0]

    assert pair.head_quantity == Decimal("0.0001")
    assert pair.head_quantity_type == "qA"


def test_org_strategy_table_accepts_decimal_percent_quantity(tmp_path: Path) -> None:
    path = tmp_path / "percent.org"
    _write_strategy(path, [_base_row(qty="%0.5")])

    pair = read_strategy_file(path).pairs[0]

    assert pair.head_quantity == Decimal("0.5")
    assert pair.head_quantity_type == "q%"
    assert pair.amount_type.startswith("q%")


@pytest.mark.parametrize("qty", ["%0", "%100"])
def test_org_strategy_table_rejects_percent_quantity_out_of_range(
    tmp_path: Path,
    qty: str,
) -> None:
    path = tmp_path / "bad-percent.org"
    _write_strategy(path, [_base_row(qty=qty)])

    with pytest.raises(ValueError, match="0 < percent < 100"):
        read_strategy_file(path)


def test_raw_tsv_content_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "old.tsv"
    path.write_text(
        "name\ttps_run\tessais\ttOut\tpause\tside\toType\toDelta\tqty\ttType\ttDelta\tprix\ttp\n"
        "BAD\t0 60\t1\t4\t\tbuy\tL\t\tA3\tS-\t\tD- +\tD8\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="legacy TSV strategy files are no longer supported"):
        read_strategy_file(path)


@pytest.mark.parametrize(
    "legacy_column",
    ["atype", "quantity", "q", "exchange", "prix", "pgate", "hprice", "tp", "oDelta"],
)
def test_legacy_file_columns_are_rejected(tmp_path: Path, legacy_column: str) -> None:
    path = tmp_path / "legacy_column.tsv"
    columns = DEFAULT_COLUMNS + (legacy_column,)

    with pytest.raises(ValueError, match="Legacy strategy field"):
        read_strategy_file(_write_strategy(path, [_base_row()], columns=columns))


@pytest.mark.parametrize("missing_column", ["pGate", "hPrice"])
def test_required_new_org_columns_are_rejected_when_missing(
    tmp_path: Path,
    missing_column: str,
) -> None:
    path = tmp_path / "missing_column.tsv"
    columns = tuple(column for column in DEFAULT_COLUMNS if column != missing_column)

    with pytest.raises(ValueError, match="missing required column"):
        read_strategy_file(_write_strategy(path, [_base_row()], columns=columns))


def test_org_row_with_shifted_cell_count_fails(tmp_path: Path) -> None:
    path = tmp_path / "shifted.tsv"
    path.write_text(
        "\n".join(
            [
                _org_row(list(DEFAULT_COLUMNS)),
                "|---+---|",
                "| KRKF | PI_XBTUSD | BAD |",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="expected 20 cells, saw 3"):
        read_strategy_file(path)


def test_empty_org_strategy_rows_are_ignored(tmp_path: Path) -> None:
    path = tmp_path / "empty_rows.tsv"
    path.write_text(
        "\n".join(
            [
                _org_row(list(DEFAULT_COLUMNS)),
                "|" + "+".join("---" for _ in DEFAULT_COLUMNS) + "|",
                _org_row([_base_row(name="FIRST").get(column, "") for column in DEFAULT_COLUMNS]),
                "| | |",
                _org_row([_base_row(name="SECOND", side="sell", qty="A4").get(column, "") for column in DEFAULT_COLUMNS]),
                _org_row(["" for _ in DEFAULT_COLUMNS]),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    strategy = read_strategy_file(path)

    assert [pair.name for pair in strategy.pairs] == ["FIRST", "SECOND"]


def test_org_strategy_rows_with_angle_placeholder_are_ignored(tmp_path: Path) -> None:
    path = _write_strategy(
        tmp_path / "placeholder_rows.tsv",
        [
            _base_row(name="LIVE"),
            _base_row(name="TEMPLATE", qty="<qty>"),
            _base_row(name="AFTER", hook="<later>"),
        ],
    )

    strategy = read_strategy_file(path)

    assert [pair.name for pair in strategy.pairs] == ["LIVE"]


def test_ignored_rows_do_not_affect_duplicate_pair_names(tmp_path: Path) -> None:
    path = tmp_path / "duplicate_with_placeholders.tsv"
    path.write_text(
        "\n".join(
            [
                _org_row(list(DEFAULT_COLUMNS)),
                "|" + "+".join("---" for _ in DEFAULT_COLUMNS) + "|",
                _org_row([_base_row(name="LIVE").get(column, "") for column in DEFAULT_COLUMNS]),
                _org_row([_base_row(name="LIVE", qty="<qty>").get(column, "") for column in DEFAULT_COLUMNS]),
                "| | |",
                _org_row([_base_row(name="<PAIR_NAME>").get(column, "") for column in DEFAULT_COLUMNS]),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    strategy = read_strategy_file(path)

    assert [pair.name for pair in strategy.pairs] == ["LIVE"]


def test_typed_field_grammar_rejects_untyped_non_empty_values(tmp_path: Path) -> None:
    path = _write_strategy(
        tmp_path / "new_parse_grammar.tsv",
        [_base_row(name="BAD", hDelta="6")],
    )

    with pytest.raises(ValueError, match="hDelta value '6' must start"):
        read_strategy_file(path)


def test_typed_field_grammar_allows_empty_optional_values(tmp_path: Path) -> None:
    path = _write_strategy(
        tmp_path / "empty_optional.tsv",
        [
            _base_row(
                exchg="BINS",
                symbol="ADAUSDT",
                name="NADA_BUY",
                tps_run="0 1440",
                tOut="6",
                side="buy",
                oType="M",
                qty="A3",
                tType="L!",
                pGate="",
                hPrice="",
                tPrice="",
            )
        ],
    )

    strategy = read_strategy_file(path)

    pair = strategy.pairs[0]
    assert pair.head.delta is None
    assert pair.head.delta_type == "oD"
    assert pair.head_order_price_spec is None
    assert pair.head_order_price_spec_type == "hD"
    assert pair.head_price == (-1_000_000.0, 1_000_000.0)
    assert pair.head_price_type == "pD"
    assert pair.tail_price_spec is None
    assert pair.tail_price_spec_type == "tD"
    assert pair.tail_unblock_spec is None
    assert pair.tail_unblock_spec_type == "uD"
    assert pair.tail_second_update_wait_seconds == 360.0
    assert pair.cooldown_minutes == 0.0


def test_cool_field_parses_minutes_and_defaults_blank(tmp_path: Path) -> None:
    path = _write_strategy(
        tmp_path / "cool.tsv",
        [
            _base_row(name="COOL", side="sell", hPrice="D6", qty="A1", tPrice="D20", cool="2.5"),
            _base_row(name="NCOOL", side="buy", hPrice="%0.20", qty="A1", tPrice="%0.10", hook="COOL-tail-closed"),
        ],
    )

    strategy = read_strategy_file(path)

    assert strategy.pairs[0].cooldown_minutes == 2.5
    assert strategy.pairs[1].cooldown_minutes == 0.0


def test_cool_rejects_negative_value(tmp_path: Path) -> None:
    path = _write_strategy(tmp_path / "bad_cool.tsv", [_base_row(name="BAD", cool="-1")])

    with pytest.raises(ValueError, match="Invalid cool value"):
        read_strategy_file(path)


def test_uppercase_cool_column_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad_cool_header.tsv"
    columns = tuple("Cool" if column == "cool" else column for column in DEFAULT_COLUMNS)

    with pytest.raises(ValueError, match="Legacy strategy field"):
        read_strategy_file(_write_strategy(path, [_base_row()], columns=columns))


def test_tublk_typed_field_parses_distance_and_percent(tmp_path: Path) -> None:
    path = _write_strategy(
        tmp_path / "tublk.tsv",
        [
            _base_row(name="DIST", tps_run="0 1440", tOut="6", side="sell", hPrice="D6", qty="A1", tPrice="D20", tUblk="D5"),
            _base_row(name="PCT", tps_run="0 1440", tOut="6", side="buy", hPrice="%0.20", qty="A1", tPrice="%0.10", tUblk="%0.2", hook="DIST-tail-closed"),
        ],
    )

    strategy = read_strategy_file(path)

    assert strategy.pairs[0].tail_unblock_spec == 5.0
    assert strategy.pairs[0].tail_unblock_spec_type == "uD"
    assert strategy.pairs[1].tail_unblock_spec == 0.2
    assert strategy.pairs[1].tail_unblock_spec_type == "u%"
    assert strategy.pairs[0].tail_second_update_wait_seconds == 360.0
    assert strategy.pairs[1].tail_second_update_wait_seconds == 360.0


def test_wublk_typed_field_parses_minutes_and_defaults_blank(tmp_path: Path) -> None:
    path = _write_strategy(
        tmp_path / "wublk.tsv",
        [
            _base_row(name="WAIT", side="sell", hPrice="D6", qty="A1", tPrice="D20", tUblk="D5", wUblk="2.5"),
            _base_row(name="NOWAIT", side="buy", hPrice="%0.20", qty="A1", tPrice="%0.10", tUblk="%0.2", hook="WAIT-tail-closed"),
        ],
    )

    strategy = read_strategy_file(path)

    assert strategy.pairs[0].tail_second_update_wait_seconds == 150.0
    assert strategy.pairs[1].tail_second_update_wait_seconds == 360.0


def test_wublk_rejects_negative_value(tmp_path: Path) -> None:
    path = _write_strategy(tmp_path / "bad_wublk.tsv", [_base_row(name="BAD", tUblk="D5", wUblk="-1")])

    with pytest.raises(ValueError, match="Invalid wUblk value"):
        read_strategy_file(path)


def test_tublk_rejects_untyped_non_empty_value(tmp_path: Path) -> None:
    path = _write_strategy(tmp_path / "bad_tublk.tsv", [_base_row(name="BAD", tUblk="5")])

    with pytest.raises(ValueError, match="tUblk value '5' must start"):
        read_strategy_file(path)


def test_head_limit_price_suffix_is_valid(tmp_path: Path) -> None:
    strategy = read_strategy_file(
        _write_strategy(tmp_path / "valid.tsv", [_base_row(name="BUY_LM", oType="Lm", pGate="D- -5")])
    )

    assert strategy.pairs[0].head.order_type == "Lm"
    assert strategy.pairs[0].head_price == (-1_000_000.0, -5.0)
    assert strategy.pairs[0].head.delta_type == "oD"


def test_head_percent_price_type_is_valid(tmp_path: Path) -> None:
    strategy = read_strategy_file(
        _write_strategy(tmp_path / "valid.tsv", [_base_row(name="BUY_LM", oType="Lm!", hPrice="%1.5")])
    )

    assert strategy.pairs[0].head.order_type == "Lm!"
    assert strategy.pairs[0].head_order_price_spec == 1.5
    assert strategy.pairs[0].head_order_price_spec_type == "h%"


def test_invalid_head_offset_type_fails_clearly(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="hDelta value 'A1.5' must start"):
        read_strategy_file(
            _write_strategy(tmp_path / "invalid.tsv", [_base_row(name="BAD_O", oType="Lm!", hDelta="A1.5")])
        )


def test_plain_limit_rejects_odelta(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="hDelta is only valid for SL or LT heads"):
        read_strategy_file(
            _write_strategy(tmp_path / "invalid.tsv", [_base_row(name="BAD_O", oType="Lm!", hDelta="D1.5")])
        )


def test_market_head_price_suffix_fails_clearly(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="price suffix 'm'.*M head order type"):
        read_strategy_file(_write_strategy(tmp_path / "invalid.tsv", [_base_row(name="BAD_M", oType="Mm", pGate="D- -5")]))


def test_f_price_suffix_fails_clearly(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsupported order type suffix 'f'"):
        read_strategy_file(_write_strategy(tmp_path / "invalid.tsv", [_base_row(name="BAD_F", oType="Lf", pGate="D- -5")]))


def test_tail_limit_price_suffix_fails_clearly(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="price suffix 'm'.*L tail order type"):
        read_strategy_file(_write_strategy(tmp_path / "invalid.tsv", [_base_row(name="BAD_TAIL", tType="Lm", pGate="D- -5")]))


def test_post_only_market_fails_clearly(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="! is only allowed"):
        read_strategy_file(_write_strategy(tmp_path / "invalid.tsv", [_base_row(name="BAD_POST", oType="M!", pGate="D- -5")]))


def test_semio_open_price_bounds_are_fixed_large_intervals(tmp_path: Path) -> None:
    strategy = read_strategy_file(
        _write_strategy(
            tmp_path / "bounds.tsv",
            [
                _base_row(name="DOWN", side="buy", pGate="D- -5"),
                _base_row(name="UP", side="sell", qty="A4", pGate="D5 +"),
            ],
        )
    )

    assert strategy.pairs[0].head_price == (-1_000_000.0, -5.0)
    assert strategy.pairs[1].head_price == (5.0, 1_000_000.0)


def test_fractional_timeout_and_extra_interval_spaces_parse(tmp_path: Path) -> None:
    strategy = read_strategy_file(
        _write_strategy(
            tmp_path / "fractional.tsv",
            [_base_row(name="FAST", tps_run="0   10", tOut=".5", pGate="D-   +")],
        )
    )

    assert strategy.pairs[0].timeout == 0.5
    assert strategy.pairs[0].window.start_minutes == 0.0
    assert strategy.pairs[0].window.end_minutes == 10.0
    assert strategy.pairs[0].head_price == (-1_000_000.0, 1_000_000.0)


def test_missing_timeout_defaults_from_window_attempts_and_pause(tmp_path: Path) -> None:
    strategy = read_strategy_file(
        _write_strategy(tmp_path / "auto_timeout.tsv", [_base_row(name="AUTO", tps_run="0 60", essais="4", tOut="", pause="6")])
    )

    assert strategy.pairs[0].timeout == 9.0


def test_explicit_timeout_wins_over_automatic_default(tmp_path: Path) -> None:
    strategy = read_strategy_file(
        _write_strategy(tmp_path / "explicit_timeout.tsv", [_base_row(name="EXPLICIT", tps_run="0 60", essais="4", tOut="3", pause="6")])
    )

    assert strategy.pairs[0].timeout == 3.0


def test_star_essais_repeats_until_window_with_explicit_timeout(tmp_path: Path) -> None:
    strategy = read_strategy_file(
        _write_strategy(tmp_path / "repeat_until_window.tsv", [_base_row(name="STAR", tps_run="0 60", essais="*", tOut="6", pause="1")])
    )

    pair = strategy.pairs[0]
    assert pair.try_num is None
    assert pair.attempts is None
    assert pair.repeats_until_window is True
    assert pair.timeout == 6.0


def test_star_essais_requires_explicit_timeout(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="essais='\\*' requires an explicit"):
        read_strategy_file(
            _write_strategy(tmp_path / "missing_star_timeout.tsv", [_base_row(name="STAR", tps_run="0 60", essais="*", tOut="", pause="1")])
        )


def test_automatic_timeout_must_be_positive(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Invalid automatic tOut"):
        read_strategy_file(
            _write_strategy(tmp_path / "bad_auto_timeout.tsv", [_base_row(name="BAD", tps_run="0 60", essais="4", tOut="", pause="15")])
        )


def test_optional_symbol_column_is_parsed(tmp_path: Path) -> None:
    strategy = read_strategy_file(
        _write_strategy(
            tmp_path / "symbol.tsv",
            [
                _base_row(name="XBT", symbol="PI_XBTUSD", pGate="D- -5"),
                _base_row(name="ETH", symbol="PI_ETHUSD", qty="A5", pGate="D- -5"),
            ],
        )
    )

    assert [pair.symbol for pair in strategy.pairs] == ["PI_XBTUSD", "PI_ETHUSD"]


def test_optional_exchange_column_parses_futures_codes(tmp_path: Path) -> None:
    strategy = read_strategy_file(
        _write_strategy(
            tmp_path / "exchange.tsv",
            [
                _base_row(name="KRK", exchg="KRKF", pGate="D- -5"),
                _base_row(name="BIN", exchg="BINF", qty="A5", pGate="D- -5"),
            ],
        )
    )

    assert [(pair.exchange, pair.market_type) for pair in strategy.pairs] == [
        ("kraken", "futures"),
        ("binance", "futures"),
    ]


def test_binance_spot_and_margin_exchange_codes_parse(tmp_path: Path) -> None:
    strategy = read_strategy_file(
        _write_strategy(
            tmp_path / "spot_margin.tsv",
            [
                _base_row(name="SPOT", exchg="BINS", pGate="D- -5", tType="S"),
                _base_row(name="MARGIN", exchg="BINM", pGate="D- -5", tType="S"),
                _base_row(name="ISO", exchg="BINI", pGate="D- -5", tType="S"),
            ],
        )
    )

    assert [(pair.exchange, pair.market_type) for pair in strategy.pairs] == [
        ("binance", "spot"),
        ("binance", "margin"),
        ("binance", "isolated_margin"),
    ]


def test_kraken_spot_and_margin_exchange_codes_parse(tmp_path: Path) -> None:
    strategy = read_strategy_file(
        _write_strategy(
            tmp_path / "kraken_spot_margin.tsv",
            [
                _base_row(name="SPOT", exchg="KRKS", pGate="D- -5", tType="S"),
                _base_row(name="MARGIN", exchg="KRKM", pGate="D- -5", tType="S"),
            ],
        )
    )

    assert [(pair.exchange, pair.market_type) for pair in strategy.pairs] == [
        ("kraken", "spot"),
        ("kraken", "margin"),
    ]


def test_bitmex_spot_exchange_code_parse(tmp_path: Path) -> None:
    strategy = read_strategy_file(
        _write_strategy(tmp_path / "bitmex_spot.tsv", [_base_row(name="SPOT", exchg="BMXS", pGate="D- -5", tType="S")])
    )

    assert (strategy.pairs[0].exchange, strategy.pairs[0].market_type) == (
        "bitmex",
        "spot",
    )


def test_bitmex_btx_exchange_codes_parse(tmp_path: Path) -> None:
    strategy = read_strategy_file(
        _write_strategy(
            tmp_path / "bitmex_btx.tsv",
            [
                _base_row(name="FUT_A", exchg="BTX", pGate="D- -5", tType="S"),
                _base_row(name="FUT_B", exchg="BTXF", pGate="D- -5", tType="S"),
                _base_row(name="SPOT", exchg="BTXS", pGate="D- -5", tType="S"),
            ],
        )
    )

    assert [(pair.exchange, pair.market_type) for pair in strategy.pairs] == [
        ("bitmex", "futures"),
        ("bitmex", "futures"),
        ("bitmex", "spot"),
    ]


def test_demo_cross_exchange_chain_parses_route_symbols() -> None:
    strategy = read_strategy_file(Path("orders/demo_cross_exchange_chain.org"))

    assert strategy.name == "demo_cross_exchange_chain"
    assert [pair.name for pair in strategy.pairs] == [
        "XBTC_SEL",
        "KADA_SEL",
        "NADA_BUY",
    ]
    assert [
        (pair.exchange, pair.market_type, pair.symbol)
        for pair in strategy.pairs
    ] == [
        ("bitmex", "futures", "XBTUSD"),
        ("kraken", "futures", "PI_ADAUSD"),
        ("binance", "spot", "ADAUSDT"),
    ]
    assert strategy.pairs[1].hook_name == "XBTC_SEL-head-filled"
    assert strategy.pairs[2].hook_name == "KADA_SEL-tail-closed"
    assert strategy.pairs[2].tail.order_type == "L!"
    assert strategy.pairs[2].tail_price_spec == 0.0


def test_duplicate_pair_names_fail_at_parse_time(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Duplicate pair name"):
        read_strategy_file(
            _write_strategy(
                tmp_path / "duplicate.tsv",
                [
                    _base_row(name="SAME", side="buy", pGate="D- -5"),
                    _base_row(name="SAME", side="sell", qty="A4", pGate="D5 +"),
                ],
            )
        )
