from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, cast

from kolabi.bot.__main__ import build_single_strategy
from kolabi.bot.domain import OrderMove, OrderState, StrategySpec
from kolabi.bot.tsv import order_pair_from_typed_values, read_strategy_file
from pytest_bdd import given, scenario, then, when


def _org_row(values: list[str]) -> str:
    return "| " + " | ".join(values) + " |"


def _write_equivalent_org_strategy(tmp_path: Path) -> Path:
    path = tmp_path / "one_pair.tsv"
    columns = [
        "name",
        "tps_run",
        "essais",
        "tOut",
        "pause",
        "side",
        "oType",
        "hDelta",
        "tType",
        "tDelta",
        "qty",
        "tPrice",
        "pGate",
        "hPrice",
        "hook",
    ]
    row = [
        "XSellTail",
        "0 1440",
        "1",
        "60",
        "",
        "sell",
        "L",
        "",
        "S-",
        "",
        "A1",
        "B50",
        "D1 2",
        "",
        "",
    ]
    path.write_text(
        "\n".join(
            [
                _org_row(columns),
                "|" + "+".join("---" for _ in columns) + "|",
                _org_row(row),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return path


@scenario("features/strategy_spec_monolith.feature", "Org strategy table row maps to canonical StrategySpec")
def test_org_strategy_table_row_maps_to_canonical_strategy_spec() -> None:
    pass


@scenario("features/strategy_spec_monolith.feature", "run-once arguments map to canonical StrategySpec")
def test_run_once_args_map_to_canonical_strategy_spec() -> None:
    pass


@scenario(
    "features/strategy_spec_monolith.feature",
    "Org strategy table and run-once equivalent intent produce same canonical pair",
)
def test_org_strategy_table_and_run_once_equivalence() -> None:
    pass


@scenario("features/strategy_spec_monolith.feature", "Strict price interval validation")
def test_strict_price_interval_validation() -> None:
    pass


@scenario("features/strategy_spec_monolith.feature", "Lifecycle vocabulary contracts remain stable")
def test_lifecycle_vocabulary_contracts() -> None:
    pass


@given("a valid Org strategy table row payload", target_fixture="payload")
def given_valid_org_strategy_table_payload(tmp_path: Path) -> Path:
    return _write_equivalent_org_strategy(tmp_path)


@given("a valid run-once argument payload", target_fixture="payload")
def given_valid_run_once_payload() -> argparse.Namespace:
    return argparse.Namespace(
        name="XSellTail",
        tps_run=[0.0, 1440.0],
        essais="1",
        pause=None,
        tOut=60,
        side="sell",
        pGate="D1 2",
        hPrice=None,
        qty="A1",
        tPrice="B50",
        oType="L",
        hDelta=None,
        tDelta=None,
        tType="S-",
        hook="",
    )


@given("equivalent Org strategy table and run-once payloads", target_fixture="payloads")
def given_equivalent_payloads(tmp_path: Path) -> tuple[Path, argparse.Namespace]:
    return (
        _write_equivalent_org_strategy(tmp_path),
        argparse.Namespace(
            name="XSellTail",
            tps_run=[0.0, 1440.0],
            essais="1",
            pause=None,
            tOut=60,
            side="sell",
            pGate="D1 2",
            hPrice=None,
            qty="A1",
            tPrice="B50",
            oType="L",
            hDelta=None,
            tDelta=None,
            tType="S-",
            hook="",
        ),
    )


@given("an invalid price interval payload with equal bounds", target_fixture="payload")
def given_invalid_interval_payload() -> dict[str, object]:
    return {
        "name": "BadPair",
        "tps_run": (0.0, 60.0),
        "essais": 1,
        "dr_pause": None,
        "timeout": 60,
        "side": "sell",
        "pGate": (1.0, 1.0),
        "head_price_type": "pD",
        "hPrice": None,
        "head_order_price_type": "hD",
        "quantity": 1,
        "quantity_type": "qA",
        "tPrice": 0.5,
        "tail_price_type": "tB",
        "oType": "L",
        "hDelta": None,
        "head_delta_type": "oD",
        "tDelta": None,
        "tType": "S-",
        "hook": "",
    }


@given("the domain lifecycle enums", target_fixture="payload")
def given_domain_lifecycle_enums() -> None:
    return None


@when("the payload is normalized into the canonical strategy layer", target_fixture="result")
def when_payload_is_normalized(payload: object) -> object:
    if isinstance(payload, Path):
        return read_strategy_file(payload)
    if isinstance(payload, argparse.Namespace):
        return build_single_strategy(payload)
    try:
        return order_pair_from_typed_values(**cast(Any, payload))
    except ValueError as exc:
        return exc


@when("both payloads are normalized into the canonical strategy layer", target_fixture="result")
def when_both_payloads_are_normalized(payloads: tuple[Path, argparse.Namespace]) -> tuple[StrategySpec, StrategySpec]:
    return read_strategy_file(payloads[0]), build_single_strategy(payloads[1])


@when("I read exported string values", target_fixture="result")
def when_i_read_exported_values(payload: None) -> tuple[list[str], list[str]]:
    del payload
    return [state.value for state in OrderState], [move.value for move in OrderMove]


@then("a typed StrategySpec should be produced")
def then_typed_strategy_spec_is_produced(result: object) -> None:
    assert isinstance(result, StrategySpec)
    assert len(result.pairs) == 1


@then("the normalized OrderPairSpec values should match")
def then_normalized_pair_values_should_match(result: tuple[StrategySpec, StrategySpec]) -> None:
    left, right = result
    assert len(left.pairs) == 1
    assert len(right.pairs) == 1
    assert left.pairs[0] == right.pairs[0]


@then("normalization should fail with a deterministic validation error")
def then_normalization_should_fail_with_validation_error(result: object) -> None:
    assert isinstance(result, Exception)


@then("they should match the agreed strategy vocabulary contracts")
def then_contract_values_match(result: tuple[list[str], list[str]]) -> None:
    order_states, order_moves = result
    assert order_states == [
        "latent",
        "hooked",
        "submitted",
        "unadmitted",
        "admitted",
        "confirmed",
        "new",
        "living",
        "closed",
        "failed",
    ]
    assert order_moves == [
        "latent_to_hooked",
        "hooked_to_submitted",
        "submitted_to_unadmitted",
        "submitted_to_admitted",
        "admitted_to_confirmed",
        "confirmed_to_new",
        "confirmed_to_living",
        "confirmed_to_closed",
        "confirmed_to_failed",
        "new_to_failed",
        "new_to_living",
        "new_to_new",
        "new_to_closed",
        "living_to_failed",
        "living_to_living",
        "living_to_close",
    ]
