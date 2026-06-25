from __future__ import annotations

import subprocess
from pathlib import Path

from kolabi.bot.fresh_run import (
    feeder_plan_for_strategy,
    route_lines,
    strategy_pair_count,
)

SCRIPT = Path("scripts/kolabi-fresh-run")


def run_script(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        check=False,
        text=True,
        capture_output=True,
    )


def test_cross_exchange_strategy_resolves_feed_routes() -> None:
    plan = feeder_plan_for_strategy(
        "orders/demo_cross_exchange_chain.org",
        default_exchange="kraken",
        default_market_type="futures",
        default_symbol="PI_XBTUSD",
        environment="demo",
    )

    assert route_lines(plan) == (
        "binance\tspot\tADAUSDT\tBINS_DEMO_API_KEY\tBINS_DEMO_API_SECRET",
        "bitmex\tfutures\tXBTUSD\tBTX_DEMO_API_KEY\tBTX_DEMO_API_SECRET",
        "kraken\tfutures\tPI_ADAUSD\tKRKF_DEMO_API_KEY\tKRKF_DEMO_API_SECRET",
    )


def test_strategy_pair_count_reads_all_strategy_pairs() -> None:
    assert strategy_pair_count("orders/demo_cross_exchange_chain.org") == 3


def test_fresh_run_route_resolution_accepts_decimal_absolute_quantity(tmp_path: Path) -> None:
    strategy = tmp_path / "decimal_abs.org"
    strategy.write_text(
        "\n".join(
            [
                "| exchg | symbol | name | tps_run | essais | tOut | pause | cool | side | oType | hDelta | qty | tType | tDelta | pGate | hPrice | tPrice | tUblk | wUblk | hook |",
                "|---+---+---+---+---+---+---+---+---+---+---+---+---+---+---+---+---+---+---+---|",
                "| KRKF | PF_XBTUSD | DEC | 0 60 | 1 | 4 |  |  | buy | L |  | A.0001 | S |  | D- + | D.5 | B99.50 |  |  |  |",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    plan = feeder_plan_for_strategy(
        strategy,
        default_exchange="kraken",
        default_market_type="futures",
        default_symbol="PF_XBTUSD",
        environment="live",
    )

    assert route_lines(plan) == (
        "kraken\tfutures\tPF_XBTUSD\tKRKF_API_KEY\tKRKF_API_SECRET",
    )


def test_fresh_run_dry_run_restarts_all_strategy_feed_routes() -> None:
    result = run_script(
        "--dry-run",
        "--env-file",
        "docker/postgres/kolabi-postgres.env.example",
        "--strategy",
        "orders/demo_cross_exchange_chain.org",
        "--environment",
        "demo",
        "--symbol",
        "PI_XBTUSD",
    )

    assert result.returncode == 0
    output = result.stdout + result.stderr
    assert "kolabi-purge: would purge KOLABI_MARKET_DB_URL" in result.stdout
    assert "kolabi:***@" in output
    assert "kolabi:kolabi@" not in output
    assert "exchange=binance market_type=spot" in output
    assert "api_key_env=BINS_DEMO_API_KEY" in output
    assert "exchange=bitmex market_type=futures" in output
    assert "api_key_env=BTX_DEMO_API_KEY" in output
    assert "exchange=kraken market_type=futures" in output
    assert "api_key_env=KRKF_DEMO_API_KEY" in output
    assert "python -m kolabi.bot run" in result.stdout
    assert "--max-active-pairs 3" in result.stdout


def test_fresh_run_max_active_pairs_override_wins() -> None:
    result = run_script(
        "--dry-run",
        "--env-file",
        "docker/postgres/kolabi-postgres.env.example",
        "--strategy",
        "orders/demo_cross_exchange_chain.org",
        "--environment",
        "demo",
        "--symbol",
        "PI_XBTUSD",
        "--max-active-pairs",
        "1",
    )

    assert result.returncode == 0
    assert "--max-active-pairs 1" in result.stdout


def test_fresh_run_validates_strategy_before_purging(tmp_path: Path) -> None:
    strategy = tmp_path / "duplicate.org"
    strategy.write_text(
        "\n".join(
            [
                "| exchg | symbol | name | tps_run | essais | tOut | pause | side | oType | hDelta | qty | tType | tDelta | pGate | hPrice | tPrice | tUblk | wUblk | hook |",
                "|---+---+---+---+---+---+---+---+---+---+---+---+---+---+---+---+---+---+---|",
                "| KRKF | PF_ADAUSD | DUP | 0 60 | 1 | 4 |  | buy | L |  | A1 | S |  | D- + | D.0001 | B99.50 |  |  |  |",
                "| KRKF | PF_ADAUSD | DUP | 0 60 | 1 | 4 |  | sell | L |  | A1 | S |  | D- + | D.0001 | B99.50 |  |  |  |",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = run_script(
        "--dry-run",
        "--env-file",
        "docker/postgres/kolabi-postgres.env.example",
        "--strategy",
        str(strategy),
        "--environment",
        "demo",
        "--symbol",
        "PF_ADAUSD",
    )

    output = result.stdout + result.stderr
    assert result.returncode == 2
    assert "Duplicate pair name(s) in strategy table: DUP" in output
    assert "strategy route resolution failed" in output
    assert "kolabi-purge:" not in output
    assert "--max-active-pairs ''" not in output
