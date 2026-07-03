from __future__ import annotations

from kolabi.shared.persistence.purge import (
    database_lanes_from_env,
    mapped_table_names,
    purge_database,
    purged_table_names,
)
from kolabi.shared.redaction import redact_url


def test_database_lanes_from_env_collects_only_runtime_db_urls() -> None:
    lanes = database_lanes_from_env(
        {
            "KOLABI_MARKET_DB_URL": "postgresql+psycopg://u:p@localhost/market",
            "KOLABI_ACCOUNT_DB_URL": "postgresql+psycopg://u:p@localhost/account",
            "KOLABI_ADVERS_ACCOUNT_DB_URL": "postgresql+psycopg://u:p@localhost/account",
            "KOLABI_PG": "postgresql+psycopg://u:p@localhost",
            "KOLABI_POSTGRES_DB": "kolabi",
            "KOLABI_TEST_PG_URL": "postgresql+psycopg://u:p@localhost/test",
        }
    )

    assert len(lanes) == 2
    assert lanes[0].names == (
        "KOLABI_ACCOUNT_DB_URL",
        "KOLABI_ADVERS_ACCOUNT_DB_URL",
    )
    assert lanes[1].names == ("KOLABI_MARKET_DB_URL",)


def test_purge_preserves_cached_exchange_instruments() -> None:
    assert "exchange_instruments" in mapped_table_names()
    assert "exchange_instruments" not in purged_table_names()


def test_purge_dry_run_counts_only_runtime_tables() -> None:
    lane = database_lanes_from_env(
        {"KOLABI_MARKET_DB_URL": "postgresql+psycopg://u:p@localhost/market"}
    )[0]

    message = purge_database(lane, dry_run=True)

    assert f"tables={len(purged_table_names())}" in message
    assert f"tables={len(mapped_table_names())}" not in message


def test_redact_url_hides_password() -> None:
    safe = redact_url("postgresql+psycopg://kolabi:secret@127.0.0.1:15433/kolabi")

    assert "secret" not in safe
    assert "***" in safe


def test_redact_url_handles_empty_and_invalid_values() -> None:
    assert redact_url(None) == "-"
    assert redact_url("not a url") == "<invalid url>"
