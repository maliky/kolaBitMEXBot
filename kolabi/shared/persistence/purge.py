from __future__ import annotations

import argparse
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Mapping, Sequence, TextIO

from sqlalchemy import Table, text
from sqlalchemy.exc import OperationalError

from kolabi.shared.persistence.db import create_persistence_engine
from kolabi.shared.persistence.models import Base
from kolabi.shared.redaction import redact_url

_DB_ENV_RE = re.compile(
    r"^KOLABI(?:_[A-Z0-9]+)*_(?:MARKET|ACCOUNT|CRITICAL|AUDIT|TELEMETRY)_DB_URL$"
)
PRESERVED_TABLE_NAMES = frozenset({"exchange_instruments"})


@dataclass(frozen=True)
class DatabaseLane:
    names: tuple[str, ...]
    url: str

    @property
    def label(self) -> str:
        return "/".join(self.names)


def database_lanes_from_env(env: Mapping[str, str]) -> tuple[DatabaseLane, ...]:
    """Collect unique configured Kolabi PostgreSQL DB lanes."""

    grouped: dict[str, list[str]] = {}
    for name, value in sorted(env.items()):
        if not _DB_ENV_RE.match(name):
            continue
        if not value:
            continue
        grouped.setdefault(value, []).append(name)
    return tuple(
        DatabaseLane(names=tuple(names), url=url)
        for url, names in sorted(grouped.items(), key=lambda item: item[1][0])
    )


def mapped_table_names() -> tuple[str, ...]:
    return tuple(table.name for table in Base.metadata.sorted_tables)


def purged_table_names() -> tuple[str, ...]:
    return tuple(table.name for table in _purged_tables())


def purge_database(
    lane: DatabaseLane,
    *,
    dry_run: bool = False,
    connect_attempts: int = 1,
    connect_delay_seconds: float = 1.0,
) -> str:
    """Truncate every mapped runtime table in one PostgreSQL database lane."""

    safe_url = redact_url(lane.url)
    table_count = len(_purged_tables())
    if dry_run:
        return f"would purge {lane.label} url={safe_url} tables={table_count}"

    attempts = max(1, int(connect_attempts))
    for attempt in range(1, attempts + 1):
        try:
            _truncate_database(lane)
            return f"purged {lane.label} url={safe_url} tables={table_count}"
        except OperationalError:
            if attempt >= attempts:
                raise
            time.sleep(max(0.0, float(connect_delay_seconds)))
    raise RuntimeError("unreachable purge retry state")


def _truncate_database(lane: DatabaseLane) -> None:
    engine = create_persistence_engine(lane.url)
    try:
        if engine.dialect.name != "postgresql":
            raise ValueError(f"{lane.label} is not PostgreSQL: {redact_url(lane.url)}")

        Base.metadata.create_all(engine)
        preparer = engine.dialect.identifier_preparer
        table_sql = ", ".join(
            preparer.format_table(table) for table in _purged_tables()
        )
        with engine.begin() as connection:
            connection.execute(
                text(f"TRUNCATE TABLE {table_sql} RESTART IDENTITY CASCADE")
            )
    finally:
        engine.dispose()


def _purged_tables() -> tuple[Table, ...]:
    return tuple(
        table
        for table in Base.metadata.sorted_tables
        if table.name not in PRESERVED_TABLE_NAMES
    )


def purge_lanes(
    lanes: Sequence[DatabaseLane],
    *,
    dry_run: bool = False,
    connect_attempts: int = 1,
    connect_delay_seconds: float = 1.0,
) -> tuple[str, ...]:
    return tuple(
        purge_database(
            lane,
            dry_run=dry_run,
            connect_attempts=connect_attempts,
            connect_delay_seconds=connect_delay_seconds,
        )
        for lane in lanes
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Purge all mapped Kolabi runtime tables from configured PostgreSQL lanes.",
    )
    parser.add_argument(
        "--all-from-env",
        action="store_true",
        help="Purge every non-empty KOLABI_*_{MARKET,ACCOUNT,CRITICAL,AUDIT,TELEMETRY}_DB_URL.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the lanes that would be purged without connecting to PostgreSQL.",
    )
    parser.add_argument(
        "--connect-attempts",
        type=int,
        default=1,
        help="Connection attempts per DB lane before failing.",
    )
    parser.add_argument(
        "--connect-delay-seconds",
        type=float,
        default=1.0,
        help="Delay between connection attempts.",
    )
    return parser


def main(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    out = stdout or sys.stdout
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.all_from_env:
        parser.error("use --all-from-env")

    lanes = database_lanes_from_env(os.environ)
    if not lanes:
        print("kolabi-purge: no KOLABI_*_DB_URL lanes found", file=sys.stderr)
        return 2

    for line in purge_lanes(
        lanes,
        dry_run=bool(args.dry_run),
        connect_attempts=int(args.connect_attempts),
        connect_delay_seconds=float(args.connect_delay_seconds),
    ):
        print(f"kolabi-purge: {line}", file=out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
