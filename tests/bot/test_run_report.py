from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal
from io import StringIO
from pathlib import Path

from kolabi.bot.run_report import (
    DbFillSummary,
    EvidenceQuality,
    InstrumentSummary,
    PairKey,
    PnlKind,
    ReportOptions,
    RuntimeRoute,
    VolumeRow,
    _VolumeAccumulator,
    build_latent_rows,
    build_living_tail_rows,
    build_pair_evolution_rows,
    build_parameter_regimes,
    build_report_rows,
    build_report_table,
    build_run_report,
    fetch_fill_summaries,
    fetch_order_summaries,
    main,
    parse_log_text,
    parse_run_log_text,
    render_latent_table,
    render_living_tail_table,
    render_market_snapshot_table,
    render_org_table,
    render_pair_evolution_table,
    render_run_report,
    render_terminated_counts_line,
    render_terminated_summary_table,
    render_volume_table,
)
from kolabi.bot.run_report_html import render_html_report
from kolabi.shared.persistence import (
    AccountBalance,
    Base,
    ExchangeFill,
    ExchangeInstrument,
    ExchangeOrder,
    RawExchangeEvent,
)
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

SAMPLE_LOG = "\n".join(
    (
        "2026-06-17 23:05:38,000 MainThread~20 /strategy_runtime.py@1@x/ "
        "HEAD_SENT (MM_BUY#4): H4alpha buy L 12.00 0.1667 -",
        "2026-06-17 23:05:39,483 MainThread~20 /strategy_runtime.py@1@x/ "
        "UPDATE (MM_BUY#4): closed--hooked 12.0 0.1645 buy 12.00 0.1667 "
        "2026-06-17T23:05:39.422000+00:00",
        "2026-06-17 23:05:39,753 MainThread~20 /strategy_runtime.py@1@x/ "
        "UPDATE (MM_BUY#4): closed--living 12.0 0.1645 0.1645 T4beta "
        "a20c4f50 2026-06-17T23:05:39.505000+00:00",
        "2026-06-17 23:19:21,864 MainThread~20 /strategy_runtime.py@1@x/ "
        "AMEND_SENT (MM_BUY#4): 0.1645 0.1669 0.1671 last T4beta a20c4f50",
        "2026-06-17 23:20:00,001 MainThread~20 /strategy_runtime.py@1@x/ "
        "AMEND_SENT (MM_BUY#4): 0.1669 0.1669 0.1671 last T4beta a20c4f50",
        "2026-06-17 23:21:32,325 MainThread~20 /strategy_runtime.py@1@x/ "
        "UPDATE (MM_BUY#4): closed--closed 12.0 0.1669 0.1669 sell 12.00 "
        "0.1669 2026-06-17T23:21:31.902000+00:00",
        "2026-06-17 23:21:32,555 MainThread~20 /strategy_runtime.py@1@x/ "
        "UPDATE (MM_BUY#4): closed--closed 12.0 0.1669 0.1669 sell 12.00 "
        "0.1669 2026-06-17T23:21:31.902000+00:00",
    )
)


ROUTED_SAMPLE_LOG = "\n".join(
    (
        "2026-06-17 23:04:59,000 MainThread~20 /service.py@729@_wait_until_ready/ "
        "kraken runtime preflight routes=kraken:futures:PF_ADAUSD,"
        "kraken:futures:PI_XBTUSD,binance:futures:BTCUSDT,"
        "binance:futures:SOLUSDT env=live "
        "market_db=postgresql+psycopg://kolabi:***@127.0.0.1:15433/kolabi_market "
        "account_db=postgresql+psycopg://kolabi:***@127.0.0.1:15433/kolabi_account",
        SAMPLE_LOG,
    )
)


XBT_TOO_SMALL_LOG = "\n".join(
    (
        "2026-06-20 21:53:57,168 MainThread~20 /service.py@753@_wait_until_ready/ "
        "kraken runtime preflight routes=kraken:futures:PF_XBTUSD env=live "
        "market_db=postgresql+psycopg://kolabi:***@127.0.0.1:15433/kolabi_market "
        "account_db=postgresql+psycopg://kolabi:***@127.0.0.1:15433/kolabi_account",
        "2026-06-20 21:53:57,248 MainThread~20 /service.py@781@_wait_until_ready/ "
        "kraken runtime ready routes=kraken:futures:PF_XBTUSD "
        "public_ages=kraken:PF_XBTUSD:0.22s private_age=0.17s",
        "ValueError: Strategy 'MM_SEL' qty U5.0 is not placeable at startup "
        "for kraken:futures:PF_XBTUSD: QTY_USD_TOO_SMALL pair=MM_SEL "
        "nominal_usd=5.0 mark=63955.73455760697 contract_size=1 step=1 resolved=0",
    )
)


def test_parse_log_collects_terminated_pair_once() -> None:
    lifecycles = parse_log_text(SAMPLE_LOG)
    lifecycle = lifecycles[PairKey("MM_BUY", 4)]

    assert lifecycle.head_client_id == "H4alpha"
    assert lifecycle.tail_client_id == "T4beta"
    assert lifecycle.amend_count == 2
    assert [item.isoformat() for item in lifecycle.tail_amend_times] == [
        "2026-06-17T23:19:21.864000+00:00",
        "2026-06-17T23:20:00.001000+00:00",
    ]
    assert lifecycle.terminated
    assert lifecycle.tail_fill is not None
    assert lifecycle.tail_fill.price == Decimal("0.1669")
    assert lifecycle.tail_fill.filled_at.isoformat() == "2026-06-17T23:21:31.902000+00:00"


def test_terminal_zero_stop_placeholders_do_not_overwrite_last_amend() -> None:
    log = "\n".join(
        (
            "2026-06-18 15:26:47,467 MainThread~20 /strategy_runtime.py@1@x/ "
            "HEAD_SENT (MM_SEL#2): H2nimble sell L 5.00 0.1627 -",
            "2026-06-18 15:27:01,780 MainThread~20 /strategy_runtime.py@1@x/ "
            "UPDATE (MM_SEL#2): closed--hooked 5.0 0.1656 sell 5.00 0.1627 "
            "2026-06-18T15:27:01.600000+00:00",
            "2026-06-18 15:27:02,051 MainThread~20 /strategy_runtime.py@1@x/ "
            "UPDATE (MM_SEL#2): closed--living 5.0 0.1656 0.1656 T2tail "
            "tail-order 2026-06-18T15:27:01.817000+00:00",
            "2026-06-18 15:29:27,898 MainThread~20 /strategy_runtime.py@1@x/ "
            "AMEND_SENT (MM_SEL#2): 0.1656 0.1624 0.1621 last T2tail tail-order",
            "2026-06-18 15:29:28,153 MainThread~20 /strategy_runtime.py@1@x/ "
            "UPDATE (MM_SEL#2): closed--living 5.0 0.1624 0.1624 T2tail "
            "tail-order 2026-06-18T15:29:27.919000+00:00",
            "2026-06-18 15:31:25,723 MainThread~20 /strategy_runtime.py@1@x/ "
            "UPDATE (MM_SEL#2): closed--living 5.0 0.0000 0.0000 T2tail "
            "tail-order 2026-06-18T15:31:25.535000+00:00",
            "2026-06-18 15:31:45,102 MainThread~20 /strategy_runtime.py@1@x/ "
            "UPDATE (MM_SEL#2): closed--closed 5.0 0.0000 0.0000 buy 5.00 "
            "0.1624 2026-06-18T15:31:44.826000+00:00",
        )
    )

    lifecycle = parse_log_text(log)[PairKey("MM_SEL", 2)]
    rows = build_report_rows({lifecycle.key: lifecycle})
    table = render_org_table(rows)

    assert lifecycle.initial_tail_stop == Decimal("0.1656")
    assert lifecycle.latest_tail_stop == Decimal("0.1624")
    assert lifecycle.tail_fill is not None
    assert lifecycle.tail_fill.price == Decimal("0.1624")
    assert rows[0].amend_logbps == Decimal("-195.1281422358174130861852988")
    assert "|       -195 | +0.001500 |" in table


def test_terminal_dash_stop_placeholder_does_not_crash_or_overwrite() -> None:
    log = "\n".join(
        (
            "2026-07-04 16:35:55,176 MainThread~20 /strategy_runtime.py@1@x/ "
            "HEAD_SENT (DW_SEL0#253): H253tail sell L 7.00 0.1957 -",
            "2026-07-04 16:35:56,176 MainThread~20 /strategy_runtime.py@1@x/ "
            "UPDATE (DW_SEL0#253): closed--hooked 7.0 0.1987 sell 7.00 "
            "0.1957 2026-07-04T16:35:55.823000+00:00",
            "2026-07-04 16:35:56,666 MainThread~20 /strategy_runtime.py@1@x/ "
            "UPDATE (DW_SEL0#253): closed--closed 7.0 - 0.1987 buy 7.00 "
            "0.1960 2026-07-04T16:35:56.408000+00:00",
        )
    )

    lifecycle = parse_log_text(log)[PairKey("DW_SEL0", 253)]
    rows = build_report_rows({lifecycle.key: lifecycle})

    assert lifecycle.terminated
    assert lifecycle.initial_tail_stop == Decimal("0.1987")
    assert lifecycle.latest_tail_stop is None
    assert lifecycle.tail_fill is not None
    assert lifecycle.tail_fill.price == Decimal("0.1960")
    assert rows[0].tail_price == Decimal("0.1960")


def test_lease_closed_supplies_tail_exchange_id_for_exact_fill(
    tmp_path: Path,
    postgres_url_factory,
) -> None:
    db_url = postgres_url_factory("run-report-lease-tail-fill")
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        head = ExchangeOrder(
            local_uuid="lease-order-head",
            exchange="kraken",
            environment="live",
            market_type="futures",
            account_scope="default",
            symbol="PF_ADAUSD",
            exchange_order_id="a22df6aa-469e-49c6-b3a2-63b461bf0c4e",
            client_order_id="H253therapeutic-260704163555",
            side="sell",
            order_type="limit",
            status="filled",
            price=0.19572,
            quantity=7,
            filled_quantity=7,
        )
        tail = ExchangeOrder(
            local_uuid="lease-order-tail",
            exchange="kraken",
            environment="live",
            market_type="futures",
            account_scope="default",
            symbol="PF_ADAUSD",
            exchange_order_id="a22df6ab-2b05-41e3-9e81-e8761c803e3e",
            client_order_id=None,
            side="buy",
            order_type="stop",
            status="filled",
            price=0.196,
            quantity=7,
            filled_quantity=7,
        )
        session.add_all([head, tail])
        session.flush()
        session.add_all(
            [
                ExchangeFill(
                    local_uuid="lease-fill-head",
                    order_id=head.id,
                    exchange="kraken",
                    exchange_fill_id="head-fill",
                    price=0.19572,
                    quantity=7,
                    fee=0.00068502,
                    fee_currency="USD",
                    liquidity_role="taker",
                ),
                ExchangeFill(
                    local_uuid="lease-fill-tail",
                    order_id=tail.id,
                    exchange="kraken",
                    exchange_fill_id="tail-fill",
                    price=0.19595,
                    quantity=7,
                    fee=0.000685825,
                    fee_currency="USD",
                    liquidity_role="taker",
                ),
            ]
        )
        session.commit()
    engine.dispose()

    log = "\n".join(
        (
            "2026-07-04 16:32:07,518 MainThread~20 /strategy_runtime.py@1@x/ "
            "LATENT_TIMEOUT_ARMED (DW_SEL0#253): "
            "2026-07-04T16:36:07.518460+00:00",
            "2026-07-04 16:32:07,711 MainThread~20 /strategy_runtime.py@1@x/ "
            "GATE_WAIT-2 (DW_SEL0#253): above last 0.1979 0.1979 0.0000 "
            "-100000.00..-120.00 pB L 0.0001 4.0",
            "2026-07-04 16:35:55,534 MainThread~20 /strategy_runtime.py@1@x/ "
            "GATE_WAIT-2 (DW_SEL0#253): ready last 0.1955 0.1979 -123.02 "
            "-100000.00..-120.00 pB L 0.0001 4.0",
            "2026-07-04 16:35:55,704 MainThread~20 /strategy_runtime.py@1@x/ "
            "HEAD_SENT (DW_SEL0#253): H253therapeutic-260704163555 sell L "
            "7.00 0.1956 -",
            "2026-07-04 16:35:56,083 MainThread~20 /strategy_runtime.py@1@x/ "
            "HEAD_ACK (DW_SEL0#253): H253therapeutic-260704163555 "
            "a22df6aa-469e-49c6-b3a2-63b461bf0c4e",
            "2026-07-04 16:35:56,088 MainThread~20 /strategy_runtime.py@1@x/ "
            "LEASE_CLOSED (DW_SEL0#253): head H253therapeutic-260704163555 "
            "a22df6aa-469e-49c6-b3a2-63b461bf0c4e CLOSED",
            "2026-07-04 16:35:56,108 MainThread~20 /strategy_runtime.py@1@x/ "
            "LEASE_OPEN (DW_SEL0#253): tail T253mustard-260704163556 - "
            "PENDING_PLACE",
            "2026-07-04 16:35:56,108 MainThread~20 /strategy_runtime.py@1@x/ "
            "UPDATE (DW_SEL0#253): closed--hooked 7.0 0.1987 sell 7.00 "
            "0.1957 2026-07-04T16:35:55.823000+00:00",
            "2026-07-04 16:35:56,519 MainThread~20 /strategy_runtime.py@1@x/ "
            "LEASE_CLOSED (DW_SEL0#253): tail T253mustard-260704163556 "
            "a22df6ab-2b05-41e3-9e81-e8761c803e3e CLOSED",
            "2026-07-04 16:35:56,571 MainThread~20 /strategy_runtime.py@1@x/ "
            "UPDATE (DW_SEL0#253): closed--closed 7.0 - 0.1987 buy 7.00 "
            "0.1960 2026-07-04T16:35:56.408000+00:00",
        )
    )
    log_path = tmp_path / "lease-tail.log"
    log_path.write_text(log, encoding="utf-8")

    report = build_report_table(log_path, db_url=db_url)
    row = next(line for line in report.splitlines() if "DW_SEL0 #253" in line)

    assert "07-04 16:32 | 00:03:48 | 0.19550" in row
    assert "| T/T |" in row
    assert "| 0.19572 | 0.19595 |" in row
    assert "0.19600" not in row


def test_lease_event_ignores_previous_attempt_client_id() -> None:
    log = "\n".join(
        (
            "2026-07-04 16:36:07,518 MainThread~20 /strategy_runtime.py@1@x/ "
            "HEAD_ACK (DW_SEL0#254): H254current current-head-order",
            "2026-07-04 16:36:08,518 MainThread~20 /strategy_runtime.py@1@x/ "
            "LEASE_CLOSED (DW_SEL0#254): head H253previous old-head-order CLOSED",
            "2026-07-04 16:36:09,518 MainThread~20 /strategy_runtime.py@1@x/ "
            "LEASE_CLOSED (DW_SEL0#254): tail T253previous old-tail-order CLOSED",
        )
    )

    lifecycle = parse_run_log_text(log).lifecycles[PairKey("DW_SEL0", 254)]

    assert lifecycle.head_client_id == "H254current"
    assert lifecycle.head_exchange_order_id == "current-head-order"
    assert lifecycle.tail_client_id is None
    assert lifecycle.tail_exchange_order_id is None


def test_non_positive_amend_stop_renders_blank_logbps() -> None:
    log = "\n".join(
        (
            "2026-06-18 15:26:47,467 MainThread~20 /strategy_runtime.py@1@x/ "
            "HEAD_SENT (MM_SEL#2): H2nimble sell L 5.00 0.1627 -",
            "2026-06-18 15:27:01,780 MainThread~20 /strategy_runtime.py@1@x/ "
            "UPDATE (MM_SEL#2): closed--hooked 5.0 0.1656 sell 5.00 0.1627 "
            "2026-06-18T15:27:01.600000+00:00",
            "2026-06-18 15:27:02,051 MainThread~20 /strategy_runtime.py@1@x/ "
            "UPDATE (MM_SEL#2): closed--living 5.0 0.1656 0.1656 T2tail "
            "tail-order 2026-06-18T15:27:01.817000+00:00",
            "2026-06-18 15:29:27,898 MainThread~20 /strategy_runtime.py@1@x/ "
            "AMEND_SENT (MM_SEL#2): 0.1656 0.0000 0.0000 last T2tail tail-order",
            "2026-06-18 15:31:45,102 MainThread~20 /strategy_runtime.py@1@x/ "
            "UPDATE (MM_SEL#2): closed--closed 5.0 0.0000 0.0000 buy 5.00 "
            "0.1624 2026-06-18T15:31:44.826000+00:00",
        )
    )

    rows = build_report_rows(parse_log_text(log))
    table = render_org_table(rows)

    assert rows[0].amend_logbps is None
    assert "|  1 | 15:29   |" in table
    assert "| +0.001500 |" in table


def test_render_log_only_table_aligns_pair_attempt() -> None:
    rows = build_report_rows(parse_log_text(SAMPLE_LOG))
    table = render_org_table(rows, options=ReportOptions())

    header = table.splitlines()[0]
    assert "| Start UTC" in header
    assert "H wait" in header
    assert "Ref" in header
    assert "Life" not in header
    assert "Tamend1" in header
    assert "Tamend1 UTC" not in table
    assert "Tamend2 UTC" not in table
    assert "| 06-17 23:05 | 00:00:01 |" in table
    assert "| MM_BUY #4 | B/S  |" in table
    assert "|  2 | 23:19   | 23:20   |       +145 |" in table
    assert "| 0.16670 | 0.16690 |" in table
    assert "|       +145 | +0.002400 |" in table
    assert "Cum est net" in table
    assert "| +0.002400 | +0.000398 | +0.0199 | +0.0753 |   +0.000398 |" in table


def test_terminated_rows_are_sorted_by_head_fill_time() -> None:
    log = "\n".join(
        (
            "2026-07-12 10:00:00,000 MainThread~20 /strategy_runtime.py@1@x/ "
            "UPDATE (FIRST#1): closed--hooked 1 99 buy 1 100 "
            "2026-07-12T10:00:00+00:00",
            "2026-07-12 10:01:00,000 MainThread~20 /strategy_runtime.py@1@x/ "
            "UPDATE (SECOND#1): closed--hooked 1 99 buy 1 100 "
            "2026-07-12T10:01:00+00:00",
            "2026-07-12 10:02:00,000 MainThread~20 /strategy_runtime.py@1@x/ "
            "UPDATE (SECOND#1): closed--closed 1 101 101 sell 1 101 "
            "2026-07-12T10:02:00+00:00",
            "2026-07-12 10:03:00,000 MainThread~20 /strategy_runtime.py@1@x/ "
            "UPDATE (FIRST#1): closed--closed 1 101 101 sell 1 101 "
            "2026-07-12T10:03:00+00:00",
        )
    )

    rows = build_report_rows(parse_log_text(log))

    assert [row.key.name for row in rows] == ["FIRST", "SECOND"]
    assert rows[0].head_fill_at < rows[1].head_fill_at
    assert rows[0].tail_fill_at > rows[1].tail_fill_at


def test_report_can_leave_net_blank_without_fee_estimates() -> None:
    options = ReportOptions(estimate_fees=False)
    rows = build_report_rows(parse_log_text(SAMPLE_LOG), options=options)
    table = render_org_table(rows, options=options)

    assert "Cum net" in table
    assert "Cum est net" not in table
    assert "| +0.002400 |         | +0.1200 | +0.4537 |         |" in table


def test_render_terminated_summary_table_uses_stat_rows() -> None:
    rows = build_report_rows(parse_log_text(SAMPLE_LOG))
    table = render_terminated_summary_table(rows, options=ReportOptions())

    assert "| Stat    |" in table
    assert "AmendLife" in table
    assert "Tamend1" not in table
    assert "Tamend2" not in table
    assert "amendLogbps" in table
    assert "ROI/h %" in table
    assert "| min     | 00:15:52 | 0.1667 | 0.1669 |  12 |       12 |  2 |  00:15:52 |        +145 |" in table
    assert "| average | 00:15:52 | 0.1667 | 0.1669 |  12 |       12 |  2 |  00:15:52 |        +145 |" in table


def test_terminated_summary_position_counts_overlapping_engagement() -> None:
    log = "\n".join(
        (
            "2026-06-17 23:00:00,000 MainThread~20 /strategy_runtime.py@1@x/ "
            "HEAD_SENT (MM_BUY#1): H1alpha buy L 10.00 0.1000 -",
            "2026-06-17 23:00:01,000 MainThread~20 /strategy_runtime.py@1@x/ "
            "UPDATE (MM_BUY#1): closed--hooked 10.0 0.1010 buy 10.00 0.1000 "
            "2026-06-17T23:00:01.000000+00:00",
            "2026-06-17 23:00:02,000 MainThread~20 /strategy_runtime.py@1@x/ "
            "HEAD_SENT (MM_BUY#2): H2alpha buy L 5.00 0.1000 -",
            "2026-06-17 23:00:03,000 MainThread~20 /strategy_runtime.py@1@x/ "
            "UPDATE (MM_BUY#2): closed--hooked 5.0 0.1010 buy 5.00 0.1000 "
            "2026-06-17T23:00:03.000000+00:00",
            "2026-06-17 23:00:04,000 MainThread~20 /strategy_runtime.py@1@x/ "
            "UPDATE (MM_BUY#1): closed--living 10.0 0.1010 0.1010 T1beta "
            "tail-order 2026-06-17T23:00:04.000000+00:00",
            "2026-06-17 23:00:05,000 MainThread~20 /strategy_runtime.py@1@x/ "
            "UPDATE (MM_BUY#2): closed--living 5.0 0.1010 0.1010 T2beta "
            "tail-order 2026-06-17T23:00:05.000000+00:00",
            "2026-06-17 23:10:00,000 MainThread~20 /strategy_runtime.py@1@x/ "
            "UPDATE (MM_BUY#1): closed--closed 10.0 0.1010 0.1010 sell 10.00 "
            "0.1010 2026-06-17T23:10:00.000000+00:00",
            "2026-06-17 23:11:00,000 MainThread~20 /strategy_runtime.py@1@x/ "
            "UPDATE (MM_BUY#2): closed--closed 5.0 0.1010 0.1010 sell 5.00 "
            "0.1010 2026-06-17T23:11:00.000000+00:00",
        )
    )

    rows = build_report_rows(parse_log_text(log))
    table = render_terminated_summary_table(rows, options=ReportOptions())

    assert "Position" in table
    assert "| min     | 00:09:59 |   0.1 | 0.101 |   5 |        5 |" in table
    assert "| max     | 00:10:57 |   0.1 | 0.101 |  10 |       15 |" in table


def test_terminated_summary_amend_life_uses_tail_placement() -> None:
    log = "\n".join(
        (
            "2026-06-17 23:00:00,000 MainThread~20 /strategy_runtime.py@1@x/ "
            "HEAD_SENT (MM_BUY#1): H1alpha buy L 1.00 0.1000 -",
            "2026-06-17 23:00:00,100 MainThread~20 /strategy_runtime.py@1@x/ "
            "UPDATE (MM_BUY#1): closed--hooked 1.0 0.1010 buy 1.00 0.1000 "
            "2026-06-17T23:00:00.000000+00:00",
            "2026-06-17 23:02:00,100 MainThread~20 /strategy_runtime.py@1@x/ "
            "UPDATE (MM_BUY#1): closed--living 1.0 0.1010 0.1010 T1beta "
            "tail-order 2026-06-17T23:02:00.000000+00:00",
            "2026-06-17 23:05:00,100 MainThread~20 /strategy_runtime.py@1@x/ "
            "UPDATE (MM_BUY#1): closed--closed 1.0 0.1010 0.1010 sell 1.00 "
            "0.1010 2026-06-17T23:05:00.000000+00:00",
        )
    )
    rows = build_report_rows(parse_log_text(log))
    table = render_terminated_summary_table(rows, options=ReportOptions())

    assert rows[0].tail_placed_at is not None
    assert rows[0].life_seconds == 300
    assert "| min     | 00:05:00 |   0.1 | 0.101 |   1 |        1 |  0 |  00:03:00 |" in table


def test_render_terminated_counts_line_counts_side_and_liquidity() -> None:
    rows = build_report_rows(parse_log_text(SAMPLE_LOG))

    assert render_terminated_counts_line(rows) == "Side: B/S=1 | Liq: ?/?=1"
    assert render_terminated_counts_line(()) == "Side: none | Liq: none"


def test_render_log_only_table_leaves_second_amend_time_blank() -> None:
    single_amend_log = "\n".join(
        line for line in SAMPLE_LOG.splitlines() if "23:20:00,001" not in line
    )
    rows = build_report_rows(parse_log_text(single_amend_log))
    table = render_org_table(rows, options=ReportOptions())

    assert "|  1 | 23:19   |" in table
    assert "|       +145 |" in table


def test_fetch_fill_summaries_aggregates_local_db_rows(postgres_url_factory) -> None:
    db_url = postgres_url_factory("run-report")
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        head = ExchangeOrder(
            local_uuid="order-head",
            exchange="kraken",
            environment="live",
            market_type="futures",
            account_scope="default",
            symbol="PF_ADAUSD",
            exchange_order_id="head-order",
            client_order_id="H4alpha",
            side="buy",
            order_type="limit",
            status="filled",
            price=0.1667,
            quantity=12,
            filled_quantity=12,
        )
        tail = ExchangeOrder(
            local_uuid="order-tail",
            exchange="kraken",
            environment="live",
            market_type="futures",
            account_scope="default",
            symbol="PF_ADAUSD",
            exchange_order_id="tail-order",
            client_order_id="T4beta",
            side="sell",
            order_type="stop",
            status="filled",
            price=0.1669,
            quantity=12,
            filled_quantity=12,
        )
        session.add_all([head, tail])
        session.flush()
        session.add_all(
            [
                ExchangeFill(
                    local_uuid="fill-head",
                    order_id=head.id,
                    exchange="kraken",
                    exchange_fill_id="head-fill",
                    price=0.16671,
                    quantity=12,
                    fee=0.0006,
                    fee_currency="USD",
                    liquidity_role="maker",
                ),
                ExchangeFill(
                    local_uuid="fill-tail",
                    order_id=tail.id,
                    exchange="kraken",
                    exchange_fill_id="tail-fill",
                    price=0.16685,
                    quantity=12,
                    fee=0.000801,
                    fee_currency="USD",
                    liquidity_role="taker",
                ),
            ]
        )
        session.commit()
    engine.dispose()

    fills = fetch_fill_summaries(db_url, ("H4alpha", "T4beta"))
    rows = build_report_rows(parse_log_text(SAMPLE_LOG), fill_summaries=fills, require_db=True)
    table = render_org_table(rows)

    assert "| 0.16671 | 0.16685 |" in table
    assert "| M/T |" in table
    row = next(line for line in table.splitlines() if "MM_BUY #4" in line)
    cells = [cell.strip() for cell in row.strip("|").split("|")]
    assert cells[-5:] == ["+0.001680", "+0.000279", "+0.0139", "+0.0527", "+0.000279"]


def test_exact_report_falls_back_to_filled_order_when_fill_row_is_missing(
    tmp_path: Path,
    postgres_url_factory,
) -> None:
    db_url = postgres_url_factory("run-report-missing-tail-fill")
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        head = ExchangeOrder(
            local_uuid="fallback-order-head",
            exchange="kraken",
            environment="live",
            market_type="futures",
            account_scope="default",
            symbol="PF_ADAUSD",
            exchange_order_id="head-order",
            client_order_id="H4alpha",
            side="buy",
            order_type="limit",
            status="filled",
            price=0.1667,
            quantity=12,
            filled_quantity=12,
        )
        tail = ExchangeOrder(
            local_uuid="fallback-order-tail",
            exchange="kraken",
            environment="live",
            market_type="futures",
            account_scope="default",
            symbol="PF_ADAUSD",
            exchange_order_id="a20c4f50",
            client_order_id="T4beta",
            side="sell",
            order_type="stop",
            status="filled",
            price=0.1669,
            quantity=12,
            filled_quantity=12,
        )
        session.add_all([head, tail])
        session.flush()
        session.add(
            ExchangeFill(
                local_uuid="fallback-fill-head",
                order_id=head.id,
                exchange="kraken",
                exchange_fill_id="head-fill",
                price=0.16671,
                quantity=12,
                fee=0.0006,
                fee_currency="USD",
                liquidity_role="maker",
            )
        )
        session.commit()
    engine.dispose()

    log_path = tmp_path / "sample.log"
    log_path.write_text(SAMPLE_LOG, encoding="utf-8")
    report = build_report_table(log_path, db_url=db_url)

    assert "Evidence note: exact DB fills 1/2 legs; order-only 1; log-only 0; unknown liquidity 1." in report
    assert "Estimated net uses maker=0.02% and taker/unknown=0.05%" in report
    assert "Missing fill rows: T4beta/a20c4f50." in report
    assert "| 0.16671 | 0.16690 |" in report
    assert "| M/? |" in report
    assert "Cum est net" in report


def test_exact_report_resolves_fill_by_exchange_order_id(
    tmp_path: Path,
    postgres_url_factory,
) -> None:
    db_url = postgres_url_factory("run-report-exchange-order-fill")
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        head = ExchangeOrder(
            local_uuid="exchange-id-order-head",
            exchange="kraken",
            environment="live",
            market_type="futures",
            account_scope="default",
            symbol="PF_ADAUSD",
            exchange_order_id="head-order",
            client_order_id="H4alpha",
            side="buy",
            order_type="limit",
            status="filled",
            price=0.1667,
            quantity=12,
            filled_quantity=12,
        )
        tail = ExchangeOrder(
            local_uuid="exchange-id-order-tail",
            exchange="kraken",
            environment="live",
            market_type="futures",
            account_scope="default",
            symbol="PF_ADAUSD",
            exchange_order_id="a20c4f50",
            client_order_id=None,
            side="sell",
            order_type="stop",
            status="filled",
            price=0.1669,
            quantity=12,
            filled_quantity=12,
        )
        session.add_all([head, tail])
        session.flush()
        session.add_all(
            [
                ExchangeFill(
                    local_uuid="exchange-id-fill-head",
                    order_id=head.id,
                    exchange="kraken",
                    exchange_fill_id="head-fill",
                    price=0.16671,
                    quantity=12,
                    fee=0.0006,
                    fee_currency="USD",
                    liquidity_role="maker",
                ),
                ExchangeFill(
                    local_uuid="exchange-id-fill-tail",
                    order_id=tail.id,
                    exchange="kraken",
                    exchange_fill_id="tail-fill",
                    price=0.16685,
                    quantity=12,
                    fee=0.000801,
                    fee_currency="USD",
                    liquidity_role="taker",
                ),
            ]
        )
        session.commit()
    engine.dispose()

    log_path = tmp_path / "sample.log"
    log_path.write_text(SAMPLE_LOG, encoding="utf-8")
    report = build_report_table(log_path, db_url=db_url)

    assert "Exact note:" not in report
    assert "| 0.16671 | 0.16685 |" in report
    assert "| M/T |" in report


def test_exact_report_uses_log_rows_when_db_has_no_order_or_fill_evidence(
    tmp_path: Path,
    postgres_url_factory,
) -> None:
    db_url = postgres_url_factory("run-report-no-fill-evidence")
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    engine.dispose()

    log_path = tmp_path / "sample.log"
    log_path.write_text(SAMPLE_LOG, encoding="utf-8")
    report = build_report_table(log_path, db_url=db_url)

    assert "Evidence note: exact DB fills 0/2 legs; order-only 0; log-only 2; unknown liquidity 2." in report
    assert "Missing fill rows: H4alpha, T4beta/a20c4f50." in report
    assert "| 0.16670 | 0.16690 |" in report
    assert "| ?/? |" in report


def test_report_adds_volume_by_pair_and_market_from_local_dbs(
    tmp_path: Path,
    postgres_url_factory,
) -> None:
    account_db_url = postgres_url_factory("run-report-volume-account")
    account_engine = create_engine(account_db_url)
    Base.metadata.create_all(account_engine)
    with Session(account_engine) as session:
        head = ExchangeOrder(
            local_uuid="volume-order-head",
            exchange="kraken",
            environment="live",
            market_type="futures",
            account_scope="default",
            symbol="PF_ADAUSD",
            exchange_order_id="head-order",
            client_order_id="H4alpha",
            side="buy",
            order_type="limit",
            status="filled",
            price=0.1667,
            quantity=12,
            filled_quantity=12,
        )
        tail = ExchangeOrder(
            local_uuid="volume-order-tail",
            exchange="kraken",
            environment="live",
            market_type="futures",
            account_scope="default",
            symbol="PF_ADAUSD",
            exchange_order_id="tail-order",
            client_order_id="T4beta",
            side="sell",
            order_type="stop",
            status="filled",
            price=0.1669,
            quantity=12,
            filled_quantity=12,
        )
        session.add_all([head, tail])
        session.flush()
        session.add_all(
            [
                ExchangeFill(
                    local_uuid="volume-fill-head",
                    order_id=head.id,
                    exchange="kraken",
                    exchange_fill_id="head-fill",
                    price=0.16671,
                    quantity=12,
                    fee=0.0006,
                    fee_currency="USD",
                    liquidity_role="maker",
                ),
                ExchangeFill(
                    local_uuid="volume-fill-tail",
                    order_id=tail.id,
                    exchange="kraken",
                    exchange_fill_id="tail-fill",
                    price=0.16685,
                    quantity=12,
                    fee=0.000801,
                    fee_currency="USD",
                    liquidity_role="taker",
                ),
            ]
        )
        session.add(
            AccountBalance(
                exchange="kraken",
                environment="live",
                account_scope="default",
                asset="USD",
                available=25.0,
                locked=0.0,
                total=25.0,
                raw_payload={},
                local_timestamp=datetime(2026, 6, 17, 23, 13, tzinfo=timezone.utc),
            )
        )
        session.commit()
    account_engine.dispose()

    market_db_url = postgres_url_factory("run-report-volume-market")
    market_engine = create_engine(market_db_url)
    Base.metadata.create_all(market_engine)
    with Session(market_engine) as session:
        session.add(
            ExchangeInstrument(
                exchange="kraken",
                environment="live",
                market_type="futures",
                symbol="PF_ADAUSD",
                instrument_type="flexible_futures",
                tradeable=True,
                tick_size=0.0001,
                contract_size=1,
                min_quantity=1,
                raw_payload={"quantityStep": "0.5"},
                updated_at=datetime(2026, 6, 17, 23, 0, tzinfo=timezone.utc),
            )
        )
        session.add_all(
            [
                RawExchangeEvent(
                    exchange="kraken",
                    environment="live",
                    market_type="futures",
                    account_scope="public",
                    symbol="PF_ADAUSD",
                    stream_kind="public_ws",
                    event_type="trade",
                    payload={"price": "0.166", "qty": "100"},
                    received_at=datetime(2026, 6, 17, 23, 10, tzinfo=timezone.utc),
                ),
                RawExchangeEvent(
                    exchange="kraken",
                    environment="live",
                    market_type="futures",
                    account_scope="public",
                    symbol="PF_ADAUSD",
                    stream_kind="public_ws",
                    event_type="trade",
                    payload={"data": [{"price": "0.167", "qty": "50"}]},
                    received_at=datetime(2026, 6, 17, 23, 12, tzinfo=timezone.utc),
                ),
            ]
        )
        session.commit()
    market_engine.dispose()

    log_path = tmp_path / "sample.log"
    log_path.write_text(SAMPLE_LOG, encoding="utf-8")
    report = build_report_table(
        log_path,
        db_url=account_db_url,
        market_db_url=market_db_url,
    )

    assert "*** Volume by market/pair" in report
    volume_line = next(
        line
        for line in report.splitlines()
        if line.startswith("| kraken:futures:PF_ADAUSD ")
        and "MM_BUY" in line
    )
    assert [cell.strip() for cell in volume_line.strip("|").split("|")] == [
        "kraken:futures:PF_ADAUSD",
        "MM_BUY",
        "1",
        "2",
        "24",
        "4.002720",
        "+0.000279",
        "00:15:52",
        "+0.0139",
        "+0.0527",
        "150",
        "24.950000",
    ]
    sizing_line = next(
        line
        for line in report.splitlines()
        if line.startswith("| kraken:futures:PF_ADAUSD ")
        and "25.000000" in line
    )
    assert [cell.strip() for cell in sizing_line.strip("|").split("|")] == [
        "kraken:futures:PF_ADAUSD",
        "25.000000",
        "n/a",
        "1",
        "1",
        "0.5",
        "n/a",
        "n/a",
        "150",
        "24.950000",
    ]


def test_report_renders_runtime_sizing_failure_without_fills(tmp_path: Path) -> None:
    log_path = tmp_path / "krf_xbt.log"
    log_path.write_text(XBT_TOO_SMALL_LOG, encoding="utf-8")

    report = build_report_table(log_path, log_only=True)

    assert "*** Sizing diagnostics" in report
    sizing_line = next(
        line for line in report.splitlines() if line.startswith("| kraken:futures:PF_XBTUSD ")
    )
    assert [cell.strip() for cell in sizing_line.strip("|").split("|")] == [
        "kraken:futures:PF_XBTUSD",
        "n/a",
        "63955.734558",
        "1",
        "n/a",
        "1",
        "n/a",
        "63955.734558",
        "n/a",
        "n/a",
    ]
    assert "*** Volume by market/pair\nNo rows." in report


def test_report_compares_runtime_sizing_with_cached_instrument_rules(
    tmp_path: Path,
    postgres_url_factory,
) -> None:
    log_path = tmp_path / "krf_xbt.log"
    log_path.write_text(XBT_TOO_SMALL_LOG, encoding="utf-8")
    market_db_url = postgres_url_factory("run-report-sizing-market")
    market_engine = create_engine(market_db_url)
    Base.metadata.create_all(market_engine)
    with Session(market_engine) as session:
        session.add(
            ExchangeInstrument(
                exchange="kraken",
                environment="live",
                market_type="futures",
                symbol="PF_XBTUSD",
                instrument_type="flexible_futures",
                tradeable=True,
                tick_size=0.5,
                contract_size=1,
                min_quantity=0.0001,
                raw_payload={"quantityStep": "0.0001"},
                updated_at=datetime(2026, 6, 20, 21, 50, tzinfo=timezone.utc),
            )
        )
        session.commit()
    market_engine.dispose()

    report = build_report_table(
        log_path,
        db_url=postgres_url_factory("run-report-sizing-account"),
        market_db_url=market_db_url,
    )

    market_line = next(
        line for line in report.splitlines() if "0.0001" in line and "kraken" in line
    )
    assert [cell.strip() for cell in market_line.strip("|").split("|")] == [
        "kraken:futures:PF_XBTUSD",
        "n/a",
        "63955.734558",
        "1",
        "0.0001",
        "0.0001",
        "6.395573",
        "6.395573",
        "n/a",
        "n/a",
    ]
    assert "Sizing note: runtime rows show the values that actually accepted or rejected strategy quantities" in report


def test_volume_table_orders_rows_by_roi_per_hour() -> None:
    rows = (
        VolumeRow(
            pair_name="SLOW",
            market="kraken:futures:PF_ADAUSD",
            fills=2,
            quantity=Decimal("10"),
            bot_usd_volume=Decimal("2"),
            average_life_seconds=Decimal("60"),
            average_roi_percent=Decimal("0.1"),
            average_roi_per_hour_percent=Decimal("6"),
            market_base_volume=None,
            market_usd_volume=None,
            min_qty_base=None,
            min_qty_usd=None,
            tick_base=None,
            tick_usd=None,
        ),
        VolumeRow(
            pair_name="FAST",
            market="kraken:futures:PF_ADAUSD",
            fills=2,
            quantity=Decimal("10"),
            bot_usd_volume=Decimal("2"),
            average_life_seconds=Decimal("30"),
            average_roi_percent=Decimal("0.2"),
            average_roi_per_hour_percent=Decimal("24"),
            market_base_volume=None,
            market_usd_volume=None,
            min_qty_base=None,
            min_qty_usd=None,
            tick_base=None,
            tick_usd=None,
        ),
        VolumeRow(
            pair_name="UNKNOWN",
            market="kraken:futures:PF_ADAUSD",
            fills=2,
            quantity=Decimal("10"),
            bot_usd_volume=Decimal("2"),
            average_life_seconds=None,
            average_roi_percent=None,
            average_roi_per_hour_percent=None,
            market_base_volume=None,
            market_usd_volume=None,
            min_qty_base=None,
            min_qty_usd=None,
            tick_base=None,
            tick_usd=None,
        ),
    )

    table = render_volume_table(rows)
    body = [
        line
        for line in table.splitlines()
        if line.startswith("| kraken:futures:PF_ADAUSD ")
    ]

    assert ["FAST", "SLOW", "UNKNOWN"] == [
        line.strip("|").split("|")[1].strip() for line in body
    ]


def test_report_keeps_log_sizing_when_market_db_is_unavailable(tmp_path: Path) -> None:
    log_path = tmp_path / "krf_xbt.log"
    log_path.write_text(XBT_TOO_SMALL_LOG, encoding="utf-8")

    report = build_report_table(
        log_path,
        db_url="sqlite://",
        market_db_url=f"sqlite:///{tmp_path / 'empty-market.sqlite'}",
    )

    assert "*** Sizing diagnostics" in report
    assert "kraken:futures:PF_XBTUSD" in report
    assert "Market DB unavailable for sizing; showing runtime log values only." in report


def test_living_tail_rows_include_latest_metrics_and_db_order_state(postgres_url_factory) -> None:
    log = "\n".join(
        (
            "2026-06-18 15:23:32,000 MainThread~20 /strategy_runtime.py@1@x/ "
            "HEAD_SENT (MM_BUY#1): H1alpha buy L 6.00 0.1625 -",
            "2026-06-18 15:23:34,000 MainThread~20 /strategy_runtime.py@1@x/ "
            "UPDATE (MM_BUY#1): closed--hooked 6.0 0.1594 buy 6.00 0.1625 "
            "2026-06-18T15:23:34.392000+00:00",
            "2026-06-18 15:23:35,000 MainThread~20 /strategy_runtime.py@1@x/ "
            "UPDATE (MM_BUY#1): closed--living 6.0 0.1594 0.1594 T1beta "
            "tail-order 2026-06-18T15:23:34.658000+00:00",
            "2026-06-18 18:50:31,793 MainThread~20 /strategy_runtime.py@1@x/ "
            "METRICS (MM_BUY#1): closed--living 0.1622 0.1594 0.0031 0.0029 "
            "0.0004 0.0037 2026-06-18T15:23:34.658510+00:00 last "
            "0.1607 0.1608 0.1607 0.1622 0.1623 0.1623",
        )
    )
    db_url = postgres_url_factory("living-report")
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        head = ExchangeOrder(
            local_uuid="living-head",
            exchange="kraken",
            environment="live",
            market_type="futures",
            account_scope="default",
            symbol="PF_ADAUSD",
            exchange_order_id="head-order",
            client_order_id="H1alpha",
            side="buy",
            order_type="limit",
            status="filled",
            price=0.1625,
            quantity=6,
            filled_quantity=6,
        )
        tail = ExchangeOrder(
            local_uuid="living-tail",
            exchange="kraken",
            environment="live",
            market_type="futures",
            account_scope="default",
            symbol="PF_ADAUSD",
            exchange_order_id="tail-order",
            client_order_id="T1beta",
            side="sell",
            order_type="stop",
            status="untouched",
            price=0.15937,
            quantity=6,
            filled_quantity=0,
        )
        session.add_all([head, tail])
        session.flush()
        session.add(
            ExchangeFill(
                local_uuid="living-fill-head",
                order_id=head.id,
                exchange="kraken",
                exchange_fill_id="head-fill",
                price=0.16247,
                quantity=6,
                fee=0.000243,
                fee_currency="USD",
                liquidity_role="maker",
            )
        )
        session.commit()
    engine.dispose()

    snapshot = parse_run_log_text(log)
    fills = fetch_fill_summaries(db_url, ("H1alpha", "T1beta"))
    orders = fetch_order_summaries(db_url, ("H1alpha", "T1beta"))
    rows = build_living_tail_rows(
        snapshot.lifecycles,
        fill_summaries=fills,
        order_summaries=orders,
        tail_telemetry=snapshot.tail_telemetry,
        snapshot_at=snapshot.last_log_at,
    )
    table = render_living_tail_table(rows)

    assert "Ref" not in table.splitlines()[0]
    assert "Dist uBlk" in table.splitlines()[0]
    row = next(line for line in table.splitlines() if "MM_BUY #1" in line)
    cells = [cell.strip() for cell in row.strip("|").split("|")]
    assert cells == [
        "06-18 15:23",
        "03:26:57",
        "MM_BUY #1",
        "B/S",
        "0.16247",
        "6",
        "M",
        "0.15940",
        "+174",
        "untouched",
        "0",
    ]
    prices = render_market_snapshot_table(snapshot.market_snapshot)
    assert "| Latest prices |    Mark |    Last |  Spread | Mark-Last | Max spread |" in prices
    assert (
        "| 06-18 18:50   | 0.16230 | 0.16220 | 0.00010 |   0.00010 |    0.00040 |"
        in prices
    )


def test_market_snapshot_table_renders_run_statistics_and_legend() -> None:
    log = "\n".join(
        (
            "2026-06-18 18:49:31,793 MainThread~20 /strategy_runtime.py@1@x/ "
            "METRICS (MM_BUY#1): closed--living 0.1621 0.1594 0.0031 "
            "0.0029 0.0005 0.0037 2026-06-18T15:23:34.658510+00:00 "
            "last 0.1605 0.1607 0.1606 0.1621 0.1622 0.1622",
            "2026-06-18 18:50:31,793 MainThread~20 /strategy_runtime.py@1@x/ "
            "METRICS (MM_BUY#1): closed--living 0.1622 0.1594 0.0031 "
            "0.0029 0.0004 0.0037 2026-06-18T15:23:34.658510+00:00 "
            "last 0.1607 0.1608 0.1607 0.1622 0.1623 0.1623",
        )
    )

    snapshot = parse_run_log_text(log)
    prices = render_market_snapshot_table(
        snapshot.market_snapshot,
        snapshots=snapshot.market_snapshots,
    )
    report = render_run_report(
        (),
        (),
        (),
        market_snapshot=snapshot.market_snapshot,
        market_snapshots=snapshot.market_snapshots,
    )

    assert len(snapshot.market_snapshots) == 2
    assert "| min           | 0.16220 | 0.16210 | 0.00010 |   0.00010 |" in prices
    assert "| max           | 0.16230 | 0.16220 | 0.00020 |   0.00010 |" in prices
    assert "| average       | 0.16225 | 0.16215 | 0.00015 |   0.00010 |" in prices
    assert "| median        | 0.16225 | 0.16215 | 0.00015 |   0.00010 |" in prices
    assert "Index is the exchange index/reference price when supplied" in report
    assert "Src is the market-price source used by the runtime row" in report


def test_report_overview_records_latest_log_time_and_price_age(tmp_path: Path) -> None:
    log = "\n".join(
        (
            "2026-06-18 18:50:31,793 MainThread~20 /strategy_runtime.py@1@x/ "
            "METRICS (MM_BUY#1): closed--living 0.1622 0.1594 0.0031 "
            "0.0029 0.0004 0.0037 2026-06-18T15:23:34.658510+00:00 "
            "last 0.1607 0.1608 0.1607 0.1622 0.1623 0.1623",
            "2026-06-18 19:05:31,793 MainThread~20 /strategy_runtime.py@1@x/ "
            "GATE_WAIT-2 (MM_SEL#6): above last 0.1626 0.1624 12.30 "
            "-100000.00..-120.00 pB L 0.0001 1.0",
        )
    )
    log_path = tmp_path / "stale-prices.log"
    log_path.write_text(log, encoding="utf-8")

    report = build_report_table(log_path, log_only=True)

    assert report.startswith("* <2026-06-18 jeu. 18:50> ")
    assert "| Run start          | 2026-06-18 18:50:31 |" in report
    assert "| Latest log         | 2026-06-18 19:05:31 |" in report
    assert "| Latest full prices | 2026-06-18 18:50:31 |   00:15:00 |" in report
    assert "| 06-18 18:50   | 0.16230 | 0.16220 |" in report


def test_latest_latent_rows_exclude_failed_latest_attempts() -> None:
    log = "\n".join(
        (
            "2026-06-18 19:00:21,362 MainThread~20 /strategy_runtime.py@1@x/ "
            "REPEAT_READY (UP_BUY2#213): waiting_for_price_gate 0.0..1440.0 -",
            "2026-06-18 19:00:21,363 MainThread~20 /strategy_runtime.py@1@x/ "
            "LATENT_TIMEOUT_ARMED (UP_BUY2#213): 2026-06-18T19:06:21.363093+00:00",
            "2026-06-18 19:00:21,387 MainThread~20 /strategy_runtime.py@1@x/ "
            "GATE_WAIT-2 (UP_BUY2#213): ready ask 0.1625 - 0.1625 "
            "0.0000..1000000000.00 pA SL! 1.40 6.0",
            "2026-06-18 19:00:22,537 MainThread~20 /strategy_runtime.py@1@x/ "
            "HEAD_SENT (UP_BUY2#213): H213myrtle buy SL 16.00 0.1648 0.1648",
            "2026-06-18 19:00:22,882 MainThread~30 /strategy_runtime.py@1@x/ "
            "COMMAND_FAILED (UP_BUY2#213): head Kraken Futures post-only is only supported",
            "2026-06-18 19:00:38,386 MainThread~20 /strategy_runtime.py@1@x/ "
            "REPEAT_READY (RB_SEL#1): waiting_for_price_gate 0.0..1440.0 -",
            "2026-06-18 19:00:38,560 MainThread~20 /strategy_runtime.py@1@x/ "
            "GATE_WAIT-1 (RB_SEL#1): chain_wait UP_BUY2-tail-closed L! 0.0001 1.0",
        )
    )

    snapshot = parse_run_log_text(log)
    rows = build_latent_rows(snapshot.lifecycles, snapshot.latent_attempts)
    table = render_latent_table(rows)

    assert "Ref" not in table.splitlines()[0]
    assert "UP_BUY2" not in table
    assert "| 06-18 19:00 | RB_SEL #1 | chain_wait | chain_wait |" in table


def test_gate_wait_parses_tout_and_hprice_from_current_log_shape() -> None:
    log = "\n".join(
        (
            "2026-07-10 10:00:00,000 MainThread~20 /strategy_runtime.py@1@x/ "
            "GATE_WAIT-2 (PAIR_A#1): ready last 0.1671 - 0.1671 "
            "0.0000..1000000000.00 pA L! 60.00 3.0",
            "2026-07-10 10:00:01,000 MainThread~20 /strategy_runtime.py@1@x/ "
            "GATE_WAIT-1 (PAIR_B#1): chain_wait PAIR_A-tail-closed SL! 1.40 6.0",
        )
    )

    attempts = parse_run_log_text(log).latent_attempts
    ready = attempts[PairKey("PAIR_A", 1)]
    waiting = attempts[PairKey("PAIR_B", 1)]

    assert ready.order_type == "L!"
    assert ready.head_price_spec == Decimal("60.00")
    assert ready.timeout_minutes == Decimal("3.0")
    assert waiting.order_type == "SL!"
    assert waiting.head_price_spec == Decimal("1.40")
    assert waiting.timeout_minutes == Decimal("6.0")
    latent_rows = build_latent_rows(
        parse_run_log_text(log).lifecycles,
        attempts,
    )
    assert {row.key: row.head_price for row in latent_rows} == {
        PairKey("PAIR_A", 1): Decimal("60.00"),
        PairKey("PAIR_B", 1): Decimal("1.40"),
    }


def test_pair_evolution_rows_show_initial_changes_and_same_attempt_results() -> None:
    log = "\n".join(
        (
            "2026-07-10 10:00:00,000 MainThread~20 /strategy_runtime.py@1@x/ "
            "GATE_WAIT-2 (PAIR_A#1): ready last 100 - 100 0..1000 pA L! 60 3",
            "2026-07-10 10:03:00,000 MainThread~30 /strategy_runtime.py@1@x/ "
            "HEAD_TIMEOUT (PAIR_A#1): expired 180s H1 O1",
            "2026-07-10 10:03:00,100 MainThread~20 /strategy_runtime.py@1@x/ "
            "HEAD_CANCEL_SENT (PAIR_A#1): O1 head_timeout",
            "2026-07-10 10:03:01,000 MainThread~20 /strategy_runtime.py@1@x/ "
            "HEAD_CANCELLED (PAIR_A#1): 0 H1 O1",
            "2026-07-10 10:03:02,000 MainThread~20 /strategy_runtime.py@1@x/ "
            "GATE_WAIT-2 (PAIR_A#2): ready last 100 - 100 0..1000 pA L! 55 3",
            "2026-07-10 10:03:03,000 MainThread~20 /strategy_runtime.py@1@x/ "
            "UPDATE (PAIR_A#2): closed--hooked 1 110 buy 1 100 "
            "2026-07-10T10:03:03+00:00",
            "2026-07-10 10:04:00,000 MainThread~20 /strategy_runtime.py@1@x/ "
            "UPDATE (PAIR_A#2): closed--closed 1 110 110 sell 1 110 "
            "2026-07-10T10:04:00+00:00",
            "2026-07-10 10:04:01,000 MainThread~20 /strategy_runtime.py@1@x/ "
            "GATE_WAIT-2 (PAIR_A#3): ready last 100 - 100 0..1000 pA L! 55 4",
            "2026-07-10 10:04:02,000 MainThread~20 /strategy_runtime.py@1@x/ "
            "GATE_WAIT-2 (PAIR_A#4): ready last 100 - 100 0..1000 pA L! 55 4",
            "2026-07-10 10:04:03,000 MainThread~20 /strategy_runtime.py@1@x/ "
            "GATE_WAIT-2 (PAIR_A#5): ready last 100 - 100 0..1000 pA L! 50 5",
            "2026-07-10 10:04:04,000 MainThread~20 /strategy_runtime.py@1@x/ "
            "UPDATE (PAIR_A#5): closed--hooked 1 90 buy 1 100 "
            "2026-07-10T10:04:04+00:00",
            "2026-07-10 10:05:00,000 MainThread~20 /strategy_runtime.py@1@x/ "
            "UPDATE (PAIR_A#5): closed--closed 1 90 90 sell 1 90 "
            "2026-07-10T10:05:00+00:00",
        )
    )

    snapshot = parse_run_log_text(log)
    reports = build_report_rows(snapshot.lifecycles, require_db=False)
    rows = build_pair_evolution_rows(
        snapshot.lifecycles,
        snapshot.latent_attempts,
        reports,
    )

    assert [(row.key.attempt, row.change, row.terminal) for row in rows] == [
        (1, "initial", "tOut"),
        (2, "hPrice", "roi>=0"),
        (3, "tOut", "active"),
        (5, "both", "roi<0"),
    ]
    table = render_pair_evolution_table(rows)
    rendered = [
        [cell.strip() for cell in line.strip("|").split("|")]
        for line in table.splitlines()
        if line.startswith("| PAIR_A")
    ]
    assert rendered[0][1:6] == ["1", "initial", "3", "60", "tOut"]
    assert rendered[1][1:8] == [
        "2",
        "hPrice",
        "3",
        "55",
        "roi>=0",
        "07-10 10:03",
        "07-10 10:04",
    ]
    assert rendered[3][1:6] == ["5", "both", "5", "50", "roi<0"]


def test_volume_roi_per_hour_uses_average_roi_over_average_life() -> None:
    accumulator = _VolumeAccumulator(
        pair_name="PAIR_A",
        route=RuntimeRoute("kraken", "futures", "PF_ADAUSD"),
        life_seconds=[Decimal("7200"), Decimal("36")],
        roi_percentages=[Decimal("1"), Decimal("-0.5")],
    )

    assert accumulator.average_roi_percent == Decimal("0.25")
    assert accumulator.average_life_seconds == Decimal("3618")
    assert accumulator.average_roi_per_hour_percent == (
        Decimal("0.25") * Decimal("3600") / Decimal("3618")
    )
    assert accumulator.average_roi_per_hour_percent > 0


def test_parameter_regimes_keep_unchanged_completed_attempts() -> None:
    log = "\n".join(
        (
            "2026-07-10 10:00:00,000 MainThread~20 /strategy_runtime.py@1@x/ "
            "GATE_WAIT-2 (PAIR_A#1): ready last 100 - 100 0..1000 pA L! 60 3",
            "2026-07-10 10:03:00,000 MainThread~30 /strategy_runtime.py@1@x/ "
            "HEAD_TIMEOUT (PAIR_A#1): expired 180s H1 O1",
            "2026-07-10 10:03:01,000 MainThread~20 /strategy_runtime.py@1@x/ "
            "GATE_WAIT-2 (PAIR_A#2): ready last 100 - 100 0..1000 pA L! 60 3",
            "2026-07-10 10:03:02,000 MainThread~20 /strategy_runtime.py@1@x/ "
            "UPDATE (PAIR_A#2): closed--hooked 1 110 buy 1 100 "
            "2026-07-10T10:03:02+00:00",
            "2026-07-10 10:04:00,000 MainThread~20 /strategy_runtime.py@1@x/ "
            "UPDATE (PAIR_A#2): closed--closed 1 110 110 sell 1 110 "
            "2026-07-10T10:04:00+00:00",
            "2026-07-10 10:04:01,000 MainThread~20 /strategy_runtime.py@1@x/ "
            "GATE_WAIT-2 (PAIR_A#3): ready last 100 - 100 0..1000 pA L! 55 3",
        )
    )
    snapshot = parse_run_log_text(log)
    reports = build_report_rows(snapshot.lifecycles)
    regimes = build_parameter_regimes(
        snapshot.lifecycles,
        snapshot.latent_attempts,
        reports,
    )

    assert [(row.first_attempt, row.last_attempt) for row in regimes] == [(1, 2), (3, 3)]
    assert regimes[0].attempts == 2
    assert regimes[0].timeouts == 1
    assert regimes[0].closed == 1
    assert regimes[0].outcomes[1].terminal == "roi>=0"


def test_inverse_finance_uses_reciprocal_pnl_and_base_currency_fees() -> None:
    log = "\n".join(
        (
            "2026-07-10 10:00:00,000 MainThread~20 /strategy_runtime.py@1@x/ "
            "UPDATE (INV#1): closed--hooked 1000 90 buy 1000 100 "
            "2026-07-10T10:00:00+00:00",
            "2026-07-10 10:05:00,000 MainThread~20 /strategy_runtime.py@1@x/ "
            "UPDATE (INV#1): closed--closed 1000 90 90 sell 1000 110 "
            "2026-07-10T10:05:00+00:00",
        )
    )
    route = RuntimeRoute("kraken", "futures", "PI_XBTUSD")
    instrument = InstrumentSummary(
        route=route,
        environment="live",
        instrument_type="futures_inverse",
        tick_size=Decimal("0.5"),
        contract_size=Decimal("1"),
        min_quantity=Decimal("1"),
        quantity_step=Decimal("1"),
    )
    head = DbFillSummary(
        "H1",
        "OH1",
        "kraken",
        "live",
        "futures",
        "PI_XBTUSD",
        "buy",
        1,
        Decimal("1000"),
        Decimal("100"),
        Decimal("0.001"),
        "BTC",
        "maker",
    )
    tail = DbFillSummary(
        "T1",
        "OT1",
        "kraken",
        "live",
        "futures",
        "PI_XBTUSD",
        "sell",
        1,
        Decimal("1000"),
        Decimal("110"),
        Decimal("0.001"),
        "BTC",
        "maker",
    )
    lifecycle = parse_log_text(log)[PairKey("INV", 1)]
    lifecycle.head_client_id = "H1"
    lifecycle.tail_client_id = "T1"
    rows = build_report_rows(
        {lifecycle.key: lifecycle},
        fill_summaries={"H1": head, "T1": tail},
        instrument_summaries={route: instrument},
        default_routes=(route,),
    )

    row = rows[0]
    expected_native = (Decimal("1") / Decimal("100") - Decimal("1") / Decimal("110")) * Decimal("1000")
    assert row.pnl_kind == PnlKind.INVERSE
    assert row.pnl_currency == "BTC"
    assert row.gross_native == expected_native
    assert row.fees_usd == Decimal("0.21")
    assert row.net_usd == expected_native * Decimal("110") - Decimal("0.21")
    assert row.finance_quality == EvidenceQuality.EXACT


def test_html_report_contains_shared_regimes_and_finance_when_enabled(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "sample.log"
    log_path.write_text(SAMPLE_LOG, encoding="utf-8")
    report = build_run_report(log_path, log_only=True)
    rendered = render_html_report(report, show_pair_regimes=True)

    assert "<!doctype html>" in rendered
    assert "Parameter regimes" in rendered
    assert 'id="report-data"' in rendered
    assert "Cumulative net USD equivalent" in rendered
    assert "https://" not in rendered


def test_html_report_hides_pair_regimes_by_default(tmp_path: Path) -> None:
    log_path = tmp_path / "sample.log"
    log_path.write_text(SAMPLE_LOG, encoding="utf-8")
    report = build_run_report(log_path, log_only=True)

    rendered = render_html_report(report)

    assert '<section class="band" hidden><h2>Parameter regimes</h2>' in rendered
    assert '<section class="band" hidden><h2>Attempt details</h2>' in rendered
    assert '"regimes":[]' in rendered
    assert '"attempts":[]' in rendered


def test_gate_wait_price_populates_latest_price_snapshot() -> None:
    log = "\n".join(
        (
            "2026-06-18 19:00:21,362 MainThread~20 /strategy_runtime.py@1@x/ "
            "REPEAT_READY (UP_BUY2#213): waiting_for_price_gate 0.0..1440.0 -",
            "2026-06-18 19:00:21,387 MainThread~20 /strategy_runtime.py@1@x/ "
            "GATE_WAIT-2 (UP_BUY2#213): below last 0.1625 0.1627 "
            "-12.30 100.00..100000.00 pB L! 0.0001 6.0",
        )
    )

    snapshot = parse_run_log_text(log)
    prices = render_market_snapshot_table(snapshot.market_snapshot)

    assert "| 06-18 19:00   |    - | 0.16250 |" in prices
    assert prices.rstrip().endswith("| last |")


def test_latest_latent_rows_mark_missing_gate_wait_after_head_sent() -> None:
    log = "\n".join(
        (
            "2026-06-18 19:00:21,362 MainThread~20 /strategy_runtime.py@1@x/ "
            "REPEAT_READY (MM_SEL#6): waiting_for_price_gate 0.0..1440.0 -",
            "2026-06-18 19:00:22,537 MainThread~20 /strategy_runtime.py@1@x/ "
            "HEAD_SENT (MM_SEL#6): H6myrtle sell L 11.00 0.1627 -",
        )
    )

    snapshot = parse_run_log_text(log)
    rows = build_latent_rows(snapshot.lifecycles, snapshot.latent_attempts)
    table = render_latent_table(rows)

    assert len(rows) == 1
    assert rows[0].gate == "no_gate_log"
    assert "| 06-18 19:00 | MM_SEL #6 | head_sent | no_gate_log |" in table
    assert "no_deadline_log" in table


def test_latest_latent_rows_preserve_logged_gate_after_head_sent() -> None:
    log = "\n".join(
        (
            "2026-06-18 19:00:21,362 MainThread~20 /strategy_runtime.py@1@x/ "
            "REPEAT_READY (MM_SEL#6): waiting_for_price_gate 0.0..1440.0 -",
            "2026-06-18 19:00:21,387 MainThread~20 /strategy_runtime.py@1@x/ "
            "GATE_WAIT-2 (MM_SEL#6): ready bid 0.1627 - 0.0000 "
            "0.0000..1000000000.00 pA L 0.1627 6.0",
            "2026-06-18 19:00:22,537 MainThread~20 /strategy_runtime.py@1@x/ "
            "HEAD_SENT (MM_SEL#6): H6myrtle sell L 11.00 0.1627 -",
        )
    )

    snapshot = parse_run_log_text(log)
    rows = build_latent_rows(snapshot.lifecycles, snapshot.latent_attempts)
    report = render_run_report((), (), rows)

    assert len(rows) == 1
    assert rows[0].gate == "ready bid"
    assert "no_gate_log" not in report
    assert "| 06-18 19:00 | MM_SEL #6 | head_sent | ready bid |" in report


def test_latest_latent_rows_use_head_ack_deadline() -> None:
    log = "\n".join(
        (
            "2026-06-18 19:00:21,362 MainThread~20 /strategy_runtime.py@1@x/ "
            "REPEAT_READY (MM_SEL#6): waiting_for_price_gate 0.0..1440.0 -",
            "2026-06-18 19:00:22,537 MainThread~20 /strategy_runtime.py@1@x/ "
            "HEAD_SENT (MM_SEL#6): H6myrtle sell L 11.00 0.1627 -",
            "2026-06-18 19:00:23,537 MainThread~20 /strategy_runtime.py@1@x/ "
            "HEAD_ACK (MM_SEL#6): H6myrtle OID-H6 2026-06-18T19:01:23.537000+00:00",
        )
    )

    snapshot = parse_run_log_text(log)
    rows = build_latent_rows(snapshot.lifecycles, snapshot.latent_attempts)
    table = render_latent_table(rows)

    assert len(rows) == 1
    assert rows[0].deadline_at == datetime(2026, 6, 18, 19, 1, 23, 537000, tzinfo=timezone.utc)
    assert "head_acked" in table
    assert "06-18 19:01" in table
    assert "no_deadline_log" not in table


def test_latest_latent_rows_use_visibility_timeout_deadline() -> None:
    log = "\n".join(
        (
            "2026-06-18 19:00:21,362 MainThread~20 /strategy_runtime.py@1@x/ "
            "REPEAT_READY (MM_SEL#6): waiting_for_price_gate 0.0..1440.0 -",
            "2026-06-18 19:00:22,537 MainThread~20 /strategy_runtime.py@1@x/ "
            "HEAD_SENT (MM_SEL#6): H6myrtle sell L 11.00 0.1627 -",
            "2026-06-18 19:00:52,537 MainThread~30 /strategy_runtime.py@1@x/ "
            "HEAD_VISIBILITY_TIMEOUT_ARMED (MM_SEL#6): "
            "H6myrtle OID-H6 2026-06-18T19:01:22.537000+00:00",
        )
    )

    snapshot = parse_run_log_text(log)
    rows = build_latent_rows(snapshot.lifecycles, snapshot.latent_attempts)
    table = render_latent_table(rows)

    assert len(rows) == 1
    assert rows[0].deadline_at == datetime(2026, 6, 18, 19, 1, 22, 537000, tzinfo=timezone.utc)
    assert "head_visibility_timeout_armed" in table
    assert "06-18 19:01" in table
    assert "no_deadline_log" not in table


def test_full_report_explains_missing_gate_wait_marker() -> None:
    log = "\n".join(
        (
            "2026-06-18 19:00:21,362 MainThread~20 /strategy_runtime.py@1@x/ "
            "REPEAT_READY (MM_SEL#6): waiting_for_price_gate 0.0..1440.0 -",
            "2026-06-18 19:00:22,537 MainThread~20 /strategy_runtime.py@1@x/ "
            "HEAD_SENT (MM_SEL#6): H6myrtle sell L 11.00 0.1627 -",
        )
    )

    snapshot = parse_run_log_text(log)
    rows = build_latent_rows(snapshot.lifecycles, snapshot.latent_attempts)
    report = render_run_report((), (), rows)

    assert "Gate no_gate_log means HEAD_SENT was logged but no GATE_WAIT-* line was present for that attempt." in report
    assert "Deadline no_deadline_log means HEAD_SENT was logged but no LATENT_TIMEOUT_ARMED, HEAD_ACK, or HEAD_VISIBILITY_TIMEOUT_ARMED deadline was present for that attempt." in report


def test_full_report_renders_three_sections_in_log_only_mode(tmp_path: Path) -> None:
    log_path = tmp_path / "sample.log"
    log_path.write_text(SAMPLE_LOG, encoding="utf-8")

    table = build_report_table(log_path, log_only=True)

    lines = table.splitlines()
    assert lines[0].startswith("* <2026-06-17 mer. 23:05> ")
    assert lines[1].startswith("Run UTC: 2026-06-17 23:05:38 |")
    assert "Command: kolabi-run-report " in lines[1]
    assert lines[2] == "** Overview"
    assert "\n| Clock" in table
    assert "| Run start  | 2026-06-17 23:05:38 |" in table
    assert "| Latest log | 2026-06-17 23:21:32 |" in table
    assert "\n| Latest prices |" in table
    assert "\n| unavailable" in table
    assert any(line.startswith("| Latest prices |") for line in lines[:12])
    assert any(line.startswith("| unavailable") for line in lines[:16])
    assert "\n** Terminated pairs" in table
    assert table.count("Latest prices") == 1
    assert "Side: B/S=1 | Liq: ?/?=1" in table
    assert "\n| Stat" in table
    assert "Mode note: mode is the most repeated value; blank means no value repeats." in table
    assert "** Terminated pairs" in table
    assert "** Living tail-flying pairs\nNo rows." in table
    assert "** Latest latent pairs\nNo rows." in table
    assert "*** Sizing diagnostics\nNo rows." in table
    assert "*** Volume by market/pair\nNo rows." in table
    assert "*** Pair parameter regimes" not in table
    assert "*** Pair attempt details" not in table

    detailed = build_report_table(
        log_path,
        log_only=True,
        options=ReportOptions(show_pair_regimes=True),
    )
    assert detailed.index("** Terminated pairs") < detailed.index(
        "*** Pair parameter regimes"
    )
    assert detailed.index("*** Pair parameter regimes") < detailed.index(
        "** Living tail-flying pairs"
    )


def test_full_report_name_is_stable_for_same_runtime(tmp_path: Path) -> None:
    log_path = tmp_path / "routed.log"
    log_path.write_text(ROUTED_SAMPLE_LOG, encoding="utf-8")

    first = build_report_table(
        log_path,
        log_only=True,
        report_command="scripts/kolabi-run-report --log-only logs/routed.log",
    )
    second = build_report_table(
        log_path,
        log_only=True,
        report_command="scripts/kolabi-run-report --log-only logs/routed.log",
    )

    first_name = first.splitlines()[0].split("> ", 1)[1]
    second_name = second.splitlines()[0].split("> ", 1)[1]

    assert first_name == second_name
    assert "-krf-ada-xbt-binf-xbt-sol" in first_name
    suffix = first_name.rsplit("-", 1)[-1]
    assert len(suffix) != 6 or not all(char in "0123456789abcdef" for char in suffix)
    assert first.splitlines()[1] == (
        "Run UTC: 2026-06-17 23:04:59 | Command: "
        "scripts/kolabi-run-report --log-only logs/routed.log"
    )
    assert second.splitlines()[1] == first.splitlines()[1]


def test_full_report_run_time_falls_back_to_log_mtime(tmp_path: Path) -> None:
    log_path = tmp_path / "unstructured.log"
    log_path.write_text("no structured runtime lines\n", encoding="utf-8")
    mtime = datetime(2026, 6, 20, 8, 0, 0, tzinfo=timezone.utc).timestamp()
    os.utime(log_path, (mtime, mtime))

    table = build_report_table(log_path, log_only=True)

    assert table.splitlines()[1].startswith("Run UTC: 2026-06-20 08:00:00 |")


def test_cli_provenance_redacts_account_db_url(tmp_path: Path) -> None:
    log_path = tmp_path / "sample.log"
    log_path.write_text(SAMPLE_LOG, encoding="utf-8")
    out = StringIO()
    err = StringIO()

    result = main(
        [
            "--log-only",
            "--account-db-url",
            "postgresql+psycopg://kolabi:secret@127.0.0.1:15433/kolabi_account",
            "--market-db-url",
            "postgresql+psycopg://kolabi:other-secret@127.0.0.1:15433/kolabi_market",
            str(log_path),
        ],
        stdout=out,
        stderr=err,
    )

    assert result == 0
    assert err.getvalue() == ""
    provenance = out.getvalue().splitlines()[1]
    assert "secret" not in provenance
    assert "other-secret" not in provenance
    assert "postgresql+psycopg://kolabi:***@127.0.0.1:15433/kolabi_account" in provenance
    assert "postgresql+psycopg://kolabi:***@127.0.0.1:15433/kolabi_market" in provenance


def test_cli_log_only_writes_stdout(tmp_path: Path) -> None:
    log_path = tmp_path / "sample.log"
    log_path.write_text(SAMPLE_LOG, encoding="utf-8")
    out = StringIO()
    err = StringIO()

    result = main(["--log-only", str(log_path)], stdout=out, stderr=err)

    assert result == 0
    assert err.getvalue() == ""
    assert out.getvalue().startswith("* <2026-06-17 mer. 23:05> ")
    assert "\nRun UTC: " in out.getvalue()
    assert (
        "| 06-17 23:05 | 00:00:01 |     | 06-17 23:05 | 06-17 23:21 | "
        "MM_BUY #4 |"
    ) in out.getvalue()
    assert "Pair parameter regimes" not in out.getvalue()


def test_cli_pair_regimes_flag_shows_regimes_and_details(tmp_path: Path) -> None:
    log_path = tmp_path / "sample.log"
    log_path.write_text(
        "\n".join(
            (
                "2026-06-17 23:05:37,000 MainThread~20 "
                "/strategy_runtime.py@1@x/ GATE_WAIT-2 (MM_BUY#4): "
                "ready last 0.1667 - 0.1667 0..1000 pA L! 60 3",
                SAMPLE_LOG,
            )
        ),
        encoding="utf-8",
    )
    out = StringIO()
    err = StringIO()

    result = main(
        ["--log-only", "--pair-regimes", str(log_path)],
        stdout=out,
        stderr=err,
    )

    assert result == 0
    assert err.getvalue() == ""
    assert "*** Pair parameter regimes" in out.getvalue()
    assert "*** Pair attempt details" in out.getvalue()


def test_cli_writes_standalone_html_alongside_org_stdout(tmp_path: Path) -> None:
    log_path = tmp_path / "sample.log"
    html_path = tmp_path / "report.html"
    log_path.write_text(SAMPLE_LOG, encoding="utf-8")
    out = StringIO()
    err = StringIO()

    result = main(
        ["--log-only", str(log_path), "--html-output", str(html_path)],
        stdout=out,
        stderr=err,
    )

    assert result == 0
    assert err.getvalue() == ""
    assert out.getvalue().startswith("* <2026-06-17 mer. 23:05> ")
    rendered = html_path.read_text(encoding="utf-8")
    assert rendered.startswith("<!doctype html>")
    assert '<section class="band" hidden><h2>Parameter regimes</h2>' in rendered


def test_cli_output_prepends_report_without_erasing_existing_content(tmp_path: Path) -> None:
    log_path = tmp_path / "sample.log"
    output_path = tmp_path / "JOURNAL.org"
    strategy_path = tmp_path / "strategy.org"
    log_path.write_text(SAMPLE_LOG, encoding="utf-8")
    strategy_path.write_text(
        "| name | qty |\n|------+-----|\n| MM_BUY | A12 |\n",
        encoding="utf-8",
    )
    output_path.write_text("* older report\nolder body\n", encoding="utf-8")
    out = StringIO()
    err = StringIO()

    result = main(
        [
            "--log-only",
            str(log_path),
            "--ouput",
            str(output_path),
            "--strategy",
            str(strategy_path),
        ],
        stdout=out,
        stderr=err,
    )

    assert result == 0
    assert out.getvalue() == ""
    assert err.getvalue() == ""
    text = output_path.read_text(encoding="utf-8")
    assert text.startswith("* <2026-06-17 mer. 23:05> ")
    assert "\nRun UTC: " in text
    assert "\n** Strategy\n" in text
    assert f"Path: {strategy_path}\n" in text
    assert "| MM_BUY | A12 |" in text
    assert "\n\n* older report\nolder body\n" in text


def test_cli_requires_db_unless_log_only(tmp_path: Path) -> None:
    log_path = tmp_path / "sample.log"
    log_path.write_text(SAMPLE_LOG, encoding="utf-8")
    out = StringIO()
    err = StringIO()

    result = main([str(log_path), "--env-file", str(tmp_path / "missing.env")], stdout=out, stderr=err)

    assert result == 2
    assert out.getvalue() == ""
    assert "account DB URL is required" in err.getvalue()
