from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, cast

from kolabi.bot.exchange_routes import default_symbol_for_route
from kolabi.bot.domain import StrategySpec
from kolabi.bot.service import BotConfig, BotService
from kolabi.bot.tsv import (
    read_strategy_file,
    strategy_from_run_once_args,
    strategy_to_pretty_dict,
)
from kolabi.shared.pruning import DEFAULT_PRUNING


class RawTextDefaultsFormatter(
    argparse.ArgumentDefaultsHelpFormatter, argparse.RawTextHelpFormatter
):
    """Show defaults while preserving raw multiline epilog formatting."""


def add_runtime_options(parser: argparse.ArgumentParser) -> None:
    """Attach common runtime options shared by run and run-once."""
    parser.add_argument("--exchange", default="kraken", help="Exchange name.")
    parser.add_argument(
        "--symbol",
        default=argparse.SUPPRESS,
        help="Exchange product id. Default follows --exchange and --market-type.",
    )
    parser.add_argument(
        "--market-type",
        choices=("futures", "spot", "margin", "isolated_margin"),
        default="futures",
        help="Default market lane for strategy rows without an exchange route code.",
    )
    parser.add_argument(
        "--environment",
        choices=("demo", "live"),
        default="demo",
        help="Endpoint family.",
    )
    parser.add_argument(
        "--base-url",
        "--rest-url",
        dest="base_url",
        help="REST base URL override for the default exchange/market route.",
    )
    parser.add_argument("--db-url", help="Optional database URL for run persistence")
    parser.add_argument(
        "--market-db-url",
        help="Optional public market data DB URL. Defaults from selected exchange environment.",
    )
    parser.add_argument(
        "--account-db-url",
        help="Optional private account DB URL. Defaults from selected exchange environment.",
    )
    parser.add_argument(
        "--critical-db-url",
        dest="critical_account_db_url",
        help=(
            "Optional critical private DB URL for order/fill lifecycle. "
            "Defaults from selected exchange environment."
        ),
    )
    parser.add_argument(
        "--audit-db-url",
        help=(
            "Optional REST audit DB URL. Defaults to "
            "the PostgreSQL audit lane for the exchange environment."
        ),
    )
    parser.add_argument(
        "--telemetry-db-url",
        help=(
            "Optional bot telemetry DB URL. Defaults to "
            "the PostgreSQL telemetry lane for the exchange environment."
        ),
    )
    parser.add_argument(
        "--rest-audit-retention-minutes",
        type=int,
        default=DEFAULT_PRUNING.rest_audit.retention_minutes,
        help="REST audit retention window in minutes; 0 disables time cleanup.",
    )
    parser.add_argument(
        "--rest-audit-retention-limit",
        type=int,
        default=DEFAULT_PRUNING.rest_audit.retention_limit,
        help="Maximum REST audit rows kept per audit DB; 0 disables count cleanup.",
    )
    parser.add_argument(
        "--tail-telemetry-retention-minutes",
        type=int,
        default=DEFAULT_PRUNING.tail_telemetry.retention_minutes,
        help="Tail telemetry retention window in minutes; 0 disables time cleanup.",
    )
    parser.add_argument(
        "--tail-telemetry-retention-limit",
        type=int,
        default=DEFAULT_PRUNING.tail_telemetry.retention_limit,
        help="Maximum tail telemetry rows kept per telemetry DB; 0 disables count cleanup.",
    )
    parser.add_argument(
        "--account-scope",
        default="default",
        help="Logical account/persona label used for account-scoped persistence lanes.",
    )
    parser.add_argument(
        "--api-key-env",
        help=(
            "Environment variable name containing the exchange API key. "
            "Use this to select a second account without passing the secret value."
        ),
    )
    parser.add_argument(
        "--api-secret-env",
        help=(
            "Environment variable name containing the exchange API secret. "
            "Use this to select a second account without passing the secret value."
        ),
    )
    parser.add_argument(
        "--ready-timeout-seconds",
        type=float,
        default=45.0,
        help="Maximum wait for fresh exchange public/private state before the strategy loop starts.",
    )
    parser.add_argument(
        "--ready-poll-seconds",
        type=float,
        default=1.0,
        help="Polling cadence used while waiting for Kraken runtime readiness.",
    )
    parser.add_argument(
        "--max-public-age-seconds",
        type=float,
        default=15.0,
        help="Maximum acceptable age for the public market snapshot.",
    )
    parser.add_argument(
        "--max-private-age-seconds",
        type=float,
        default=30.0,
        help="Maximum acceptable age for the private websocket heartbeat.",
    )
    parser.add_argument(
        "--max-reconcile-age-seconds",
        type=float,
        default=300.0,
        help="Maximum acceptable age for the startup REST reconcile.",
    )
    parser.add_argument(
        "--skip-ready-check",
        action="store_true",
        help="Skip runtime freshness checks before starting the strategy.",
    )
    parser.add_argument("--log-level", default="INFO", help="Logging verbosity.")
    parser.add_argument("--update-pause", type=int, default=10, help="Update loop pause in seconds.")
    parser.add_argument("--log-pause", type=int, default=60, help="Periodic status log pause in seconds.")
    parser.add_argument(
        "--sync",
        action="store_true",
        help="Run orders synchronously (blocking) instead of spawning threads",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan the strategy through Dragon/Chronos/Isis/Horus without calling Ogun",
    )
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="Run the async supervisor path with simulated execution and confirmations",
    )
    parser.add_argument(
        "--max-active-pairs",
        type=int,
        default=4,
        help="Maximum live/demo pair attempts allowed to be active at once; use 0 for unlimited.",
    )
    parser.add_argument(
        "--rest-min-interval",
        type=float,
        default=0.1,
        help="Minimum seconds between live REST command launches; use 0 to disable pacing.",
    )
    parser.add_argument(
        "--rest-max-inflight",
        type=int,
        default=2,
        help="Maximum live REST commands concurrently waiting on the platform; use 0 for unlimited.",
    )


def add_single_order_options(parser: argparse.ArgumentParser) -> None:
    """Expose one typed strategy row on the active kolabi.bot CLI."""
    parser.add_argument(
        "--tps_run",
        "-t",
        type=float,
        nargs=2,
        default=[0, 1440],
        help=(
            "Strategy validity window in minutes from launch, as start end. "
            "Example: -t 0 60 means active now for one hour."
        ),
    )
    parser.add_argument(
        "--name",
        "-m",
        default="NaDef",
        help="Internal order name shown in logs.",
    )
    parser.add_argument(
        "--tOut",
        "-O",
        type=float,
        default=None,
        help="Validation timeout in minutes for the main order lifecycle.",
    )
    parser.add_argument(
        "--pause",
        "-p",
        type=float,
        default=None,
        help="Pause between repeated attempts, in minutes.",
    )
    parser.add_argument(
        "--cool",
        dest="cool",
        type=float,
        default=None,
        help="Extra cooldown after a successful tail fill before repeating, in minutes.",
    )
    parser.add_argument(
        "--pGate",
        "-x",
        dest="pGate",
        type=str,
        default="D- +",
        help=(
            "Typed activation gate interval as one string. Use D, %%, or A before two bounds, "
            "for example --pGate 'D- +' or --pGate '%%-1 1'."
        ),
    )
    parser.add_argument(
        "--hPrice",
        dest="hPrice",
        type=str,
        default=None,
        help=(
            "Typed lazy head order price. Use D, %%, or A before one value, "
            "for example --hPrice D10 or --hPrice '%%0.2'."
        ),
    )
    parser.add_argument(
        "--qty",
        type=str,
        required=True,
        help=(
            "Typed quantity, for example A17 for absolute size, %%5 for percent, "
            "or U15 for USD notional converted from mark price."
        ),
    )
    parser.add_argument(
        "--tPrice",
        dest="tPrice",
        type=str,
        default=None,
        help="Typed tail distance, for example D20, %%1.2, or A0.25.",
    )
    parser.add_argument(
        "--hDelta",
        dest="hDelta",
        type=str,
        default=None,
        help="Typed SL/LT trigger-to-limit offset, for example D6 or %%0.20.",
    )
    parser.add_argument(
        "--tDelta",
        type=str,
        default=None,
        help="Typed tail trigger-to-limit difference, for example D0.0001.",
    )
    parser.add_argument(
        "--tUblk",
        type=str,
        default=None,
        help=(
            "Tail first-unblock favourable movement. Use D for nominal distance "
            "or %% for percent of the head-fill reference, for example D5 or %%0.2."
        ),
    )
    parser.add_argument(
        "--wUblk",
        type=float,
        default=None,
        help=(
            "Minutes to wait before the second tail update. "
            "Blank or omitted means 6 minutes."
        ),
    )
    parser.add_argument(
        "--essais",
        "-n",
        default="1",
        help="Number of attempts, or * to repeat until the time window closes.",
    )
    parser.add_argument(
        "--oType",
        "-o",
        type=str,
        default="M",
        help=(
            "Head order type. "
            "Examples: M = Market, L = Limit, S = Stop, SL = StopLimit, "
            "MT = MarketIfTouched, LT = LimitIfTouched."
        ),
    )
    parser.add_argument(
        "--tType",
        "-y",
        type=str,
        default="Si-",
        help=(
            "Tail order type. "
            "Examples: S- = reduce-only Stop on last price, "
            "Sm- = reduce-only Stop on mark price, "
            "Si- = reduce-only Stop on index price, "
            "SL- = reduce-only StopLimit."
        ),
    )
    parser.add_argument(
        "--side",
        "-c",
        type=str,
        default="buy",
        help="Order side.",
    )
    parser.add_argument(
        "--hook",
        type=str,
        default="",
        help="Optional hook dependency, for example XBrx-S_T.",
    )


def build_service(args: argparse.Namespace) -> BotService:
    """Build a bot service from parsed CLI args."""
    return BotService(
        BotConfig(
            exchange=args.exchange,
            symbol=_resolved_symbol(args),
            market_type=getattr(args, "market_type", "futures"),
            environment=args.environment,
            updatepause=args.update_pause,
            logpause=args.log_pause,
            log_level=args.log_level,
            db_url=args.db_url,
            market_db_url=args.market_db_url,
            account_db_url=args.account_db_url,
            critical_account_db_url=getattr(args, "critical_account_db_url", None),
            audit_db_url=getattr(args, "audit_db_url", None),
            telemetry_db_url=getattr(args, "telemetry_db_url", None),
            account_scope=getattr(args, "account_scope", "default"),
            api_key_env=getattr(args, "api_key_env", None),
            api_secret_env=getattr(args, "api_secret_env", None),
            base_url=getattr(args, "base_url", None),
            require_ready=not args.skip_ready_check,
            ready_timeout_seconds=args.ready_timeout_seconds,
            ready_poll_seconds=args.ready_poll_seconds,
            max_public_age_seconds=args.max_public_age_seconds,
            max_private_age_seconds=args.max_private_age_seconds,
            max_reconcile_age_seconds=args.max_reconcile_age_seconds,
            max_active_pairs=getattr(args, "max_active_pairs", 4),
            rest_min_interval_seconds=getattr(args, "rest_min_interval", 0.1),
            rest_max_inflight=getattr(args, "rest_max_inflight", 2),
            rest_audit_retention_minutes=getattr(
                args,
                "rest_audit_retention_minutes",
                DEFAULT_PRUNING.rest_audit.retention_minutes,
            ),
            rest_audit_retention_limit=getattr(
                args,
                "rest_audit_retention_limit",
                DEFAULT_PRUNING.rest_audit.retention_limit,
            ),
            tail_telemetry_retention_minutes=getattr(
                args,
                "tail_telemetry_retention_minutes",
                DEFAULT_PRUNING.tail_telemetry.retention_minutes,
            ),
            tail_telemetry_retention_limit=getattr(
                args,
                "tail_telemetry_retention_limit",
                DEFAULT_PRUNING.tail_telemetry.retention_limit,
            ),
        )
    )


def build_single_strategy(args: argparse.Namespace) -> StrategySpec:
    """Traduit la CLI typee vers une StrategySpec canonique."""
    return strategy_from_run_once_args(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kolabi.bot",
        description="kolaBiBot execution CLI",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser(
        "run",
        help="Run strategies defined in an Org strategy table",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    run_parser.add_argument(
        "--strategy",
        "-s",
        required=True,
        help="Path to Org strategy table file (e.g. orders/demo_ada.org)",
    )
    add_runtime_options(run_parser)

    run_once_parser = subparsers.add_parser(
        "run-once",
        help="Run one typed strategy-defined order pair from the command line",
        description=(
            "Run one typed order pair through kolabi.bot. The typed values match "
            "the Org strategy table columns: qty, pGate, hPrice, tPrice, hDelta, tDelta, tUblk, wUblk, cool."
        ),
        epilog=(
            "Examples:\n"
            "  Differential around current reference:\n"
            "    python -m kolabi.bot run-once --symbol PI_XBTUSD --environment demo "
            "-m XSellTail -t 0 1440 -O 60 -x 'D1 2' --hPrice D1 --qty A1 --tPrice %0.5 -o L -y S- -c sell --dry-run\n"
            "  Percent around current reference:\n"
            "    python -m kolabi.bot run-once --symbol PI_XBTUSD --environment demo "
            "-m XPct -t 0 60 -x '%-1 1' --hPrice %0.5 --qty A1 --tPrice %0.5 -o L -y S- -c buy --dry-run\n"
            "  Absolute interval:\n"
            "    python -m kolabi.bot run-once --symbol PI_XBTUSD --environment demo "
            "-m XAbs -t 0 60 -x 'A79320 79340' --hPrice A79350 --qty A1 --tPrice D0.5 -o L -y S- -c sell --dry-run"
        ),
        formatter_class=RawTextDefaultsFormatter,
    )
    add_runtime_options(run_once_parser)
    add_single_order_options(run_once_parser)

    preflight_parser = subparsers.add_parser(
        "preflight",
        help=(
            "Check exchange public/private DB state and route credentials before "
            "starting a strategy"
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    preflight_parser.add_argument("--exchange", default="kraken", help="Exchange name.")
    preflight_parser.add_argument(
        "--symbol",
        default=argparse.SUPPRESS,
        help="Exchange product id. Default follows --exchange and --market-type.",
    )
    preflight_parser.add_argument(
        "--market-type",
        choices=("futures", "spot", "margin", "isolated_margin"),
        default="futures",
        help="Default market lane for strategy rows without an exchange route code.",
    )
    preflight_parser.add_argument(
        "--strategy",
        help="Optional Org strategy table; checks every exchange/symbol route in the file.",
    )
    preflight_parser.add_argument(
        "--environment", choices=("demo", "live"), default="demo", help="Endpoint family."
    )
    preflight_parser.add_argument(
        "--base-url",
        "--rest-url",
        dest="base_url",
        help="REST base URL override for the default exchange/market route.",
    )
    preflight_parser.add_argument(
        "--market-db-url",
        help="Optional public market data DB URL. Defaults from selected exchange environment.",
    )
    preflight_parser.add_argument(
        "--account-db-url",
        help="Optional private account DB URL. Defaults from selected exchange environment.",
    )
    preflight_parser.add_argument(
        "--critical-db-url",
        dest="critical_account_db_url",
        help=(
            "Optional critical private DB URL for order/fill lifecycle. "
            "Defaults from selected exchange environment."
        ),
    )
    preflight_parser.add_argument(
        "--account-scope",
        default="default",
        help="Logical account/persona label for scoped private DB env lanes.",
    )
    preflight_parser.add_argument(
        "--api-key-env",
        help=(
            "Environment variable name containing the exchange API key. "
            "Use this to preflight the same account selected by run/run-once."
        ),
    )
    preflight_parser.add_argument(
        "--api-secret-env",
        help=(
            "Environment variable name containing the exchange API secret. "
            "Use this to preflight the same account selected by run/run-once."
        ),
    )
    preflight_parser.add_argument(
        "--ready-timeout-seconds",
        type=float,
        default=45.0,
        help="Maximum wait for readiness checks.",
    )
    preflight_parser.add_argument(
        "--ready-poll-seconds",
        type=float,
        default=1.0,
        help="Polling cadence while waiting for readiness.",
    )
    preflight_parser.add_argument(
        "--max-public-age-seconds",
        type=float,
        default=15.0,
        help="Maximum acceptable age of public market snapshot.",
    )
    preflight_parser.add_argument(
        "--max-private-age-seconds",
        type=float,
        default=30.0,
        help="Maximum acceptable age of private websocket heartbeat.",
    )
    preflight_parser.add_argument(
        "--max-reconcile-age-seconds",
        type=float,
        default=300.0,
        help="Maximum acceptable age of REST reconcile snapshot.",
    )
    return parser


def run_command(args: argparse.Namespace) -> int:
    strategy_path = Path(args.strategy)
    if not strategy_path.exists():
        raise FileNotFoundError(f"Strategy file not found: {strategy_path}")

    strategy = read_strategy_file(strategy_path)
    if not strategy.pairs:
        print("No strategies found in file.", file=sys.stderr)
        return 1

    service = build_service(args)
    try:
        result = service.run_strategy(strategy, dry_run=args.dry_run, simulate=args.simulate)
    except KeyboardInterrupt:
        print("Interrupted by operator.")
        return 130
    if args.dry_run:
        payload = {
            "strategy": strategy_to_pretty_dict(strategy),
            "commands": [_command_to_pretty_dict(command) for command in result.commands],
        }
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    elif args.simulate:
        print(
            f"Simulated {len(strategy.pairs)} order pair(s) through the async supervisor path."
        )
    else:
        print(f"Executed {len(strategy.pairs)} order pair(s) through the async supervisor path.")
    return 0


def run_once_command(args: argparse.Namespace) -> int:
    """Run one command-line strategy row using typed parameter names."""
    strategy = build_single_strategy(args)

    service = build_service(args)
    try:
        result = service.run_strategy(strategy, dry_run=args.dry_run, simulate=args.simulate)
    except KeyboardInterrupt:
        print("Interrupted by operator.")
        return 130
    if args.dry_run:
        payload = {
            "strategy": strategy_to_pretty_dict(strategy),
            "commands": [_command_to_pretty_dict(command) for command in result.commands],
        }
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    elif args.simulate:
        print("Simulated 1 order pair through the async supervisor path.")
    else:
        print("Executed 1 order pair through the async supervisor path.")
    return 0


def _command_to_pretty_dict(command) -> dict[str, object]:
    payload = {
        "kind": command.kind.value,
        "exchange": command.exchange,
        "market_type": command.market_type,
        "symbol": str(command.symbol),
        "pair_name": command.pair_name,
        "role": command.role.value,
        "reason": command.reason,
    }
    payload["request"] = (
        asdict(cast(Any, command.request)) if is_dataclass(command.request) else None
    )
    payload["legacy_order"] = (
        dict(command.legacy_order) if command.legacy_order is not None else None
    )
    return payload


def preflight_command(args: argparse.Namespace) -> int:
    """Print a readiness snapshot for strategy routes."""
    service = BotService(
        BotConfig(
            exchange=args.exchange,
            symbol=_resolved_symbol(args),
            market_type=args.market_type,
            environment=args.environment,
            market_db_url=args.market_db_url,
            account_db_url=args.account_db_url,
            critical_account_db_url=getattr(args, "critical_account_db_url", None),
            account_scope=getattr(args, "account_scope", "default"),
            api_key_env=getattr(args, "api_key_env", None),
            api_secret_env=getattr(args, "api_secret_env", None),
            base_url=getattr(args, "base_url", None),
            require_ready=True,
            ready_timeout_seconds=args.ready_timeout_seconds,
            ready_poll_seconds=args.ready_poll_seconds,
            max_public_age_seconds=args.max_public_age_seconds,
            max_private_age_seconds=args.max_private_age_seconds,
            max_reconcile_age_seconds=args.max_reconcile_age_seconds,
        )
    )
    strategy = None
    if getattr(args, "strategy", None):
        strategy_path = Path(args.strategy)
        if not strategy_path.exists():
            raise FileNotFoundError(f"Strategy file not found: {strategy_path}")
        strategy = read_strategy_file(strategy_path)
    payload = service.preflight(strategy)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if bool(payload.get("ready")) else 1


def _resolved_symbol(args: argparse.Namespace) -> str:
    return getattr(args, "symbol", None) or default_symbol_for_route(
        args.exchange,
        getattr(args, "market_type", "futures"),
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        return run_command(args)
    if args.command == "run-once":
        return run_once_command(args)
    if args.command == "preflight":
        return preflight_command(args)
    raise ValueError(f"Unknown command {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
