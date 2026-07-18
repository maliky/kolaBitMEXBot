from __future__ import annotations

from dataclasses import replace

import pytest
from kolabi.bot.dependencies import (
    HookDependencyError,
    compile_dependency_graph,
    format_hook_expression,
    parse_hook_expression,
)
from kolabi.bot.domain import (
    HeadSpec,
    HookMode,
    HookTargetKind,
    OrderPairSpec,
    Side,
    TailSpec,
    TimeWindow,
)


def sample_pair(name: str, *, hook_name: str | None = None) -> OrderPairSpec:
    return OrderPairSpec(
        name=name,
        window=TimeWindow(start_minutes=0.0, end_minutes=10.0),
        try_num=1,
        dr_pause=None,
        timeout=1.0,
        head=HeadSpec(side=Side.BUY, order_type="L"),
        head_price=(0.0, 1.0),
        head_price_type="pD",
        head_quantity=1,
        head_quantity_type="qA",
        tail=TailSpec(side=Side.SELL, order_type="S-"),
        tail_price_spec=1.0,
        tail_price_spec_type="tD",
        amount_type="qAtDpD",
        hook_name=hook_name,
    )


def test_parse_legacy_hook_as_single_all_target() -> None:
    expression = parse_hook_expression("origin")

    assert expression is not None
    assert expression.mode == HookMode.ALL
    assert expression.targets[0].origin_pair_name == "origin"
    assert expression.targets[0].kind == HookTargetKind.PAIR_CLOSED
    assert format_hook_expression(expression) == "origin-tail-closed"


def test_parse_explicit_all_and_any_expressions() -> None:
    all_expression = parse_hook_expression(
        " ALL ( origin-a-head-filled , origin-b-tail-closed ) "
    )
    any_expression = parse_hook_expression(
        "any(origin-a-closed,origin-b-head-filled)"
    )

    assert all_expression is not None
    assert all_expression.mode == HookMode.ALL
    assert format_hook_expression(all_expression) == (
        "all(origin-a-head-filled,origin-b-tail-closed)"
    )
    assert any_expression is not None
    assert any_expression.mode == HookMode.ANY
    assert format_hook_expression(any_expression) == (
        "any(origin-a-tail-closed,origin-b-head-filled)"
    )


@pytest.mark.parametrize(
    "raw",
    [
        "all()",
        "any(origin,)",
        "all(origin,any(second,third))",
        "all(origin",
    ],
)
def test_parse_rejects_malformed_expressions(raw: str) -> None:
    with pytest.raises(HookDependencyError):
        parse_hook_expression(raw)


@pytest.mark.parametrize(
    ("pairs", "message"),
    [
        ((sample_pair("child", hook_name="missing"),), "missing pair"),
        ((sample_pair("self", hook_name="self"),), "depend on itself"),
        (
            (
                sample_pair("origin"),
                sample_pair(
                    "child",
                    hook_name="all(origin,origin-tail-closed)",
                ),
            ),
            "repeats target",
        ),
        (
            (
                sample_pair("a", hook_name="b"),
                sample_pair("b", hook_name="any(a,root)"),
                sample_pair("root"),
            ),
            "dependency cycle",
        ),
    ],
)
def test_compile_dependency_graph_rejects_invalid_graphs(
    pairs: tuple[OrderPairSpec, ...],
    message: str,
) -> None:
    with pytest.raises(HookDependencyError, match=message):
        compile_dependency_graph(pairs)


def test_compile_dependency_graph_indexes_fan_in_and_fan_out() -> None:
    origin = sample_pair("origin")
    second = sample_pair("second")
    child_all = sample_pair(
        "child-all",
        hook_name="all(origin-head-filled,second-tail-closed)",
    )
    child_any = replace(
        child_all,
        name="child-any",
        hook_name="any(origin-head-filled,second-tail-closed)",
    )

    graph = compile_dependency_graph((origin, second, child_all, child_any))

    assert set(graph.by_dependent) == {"child-all", "child-any"}
    origin_target = graph.by_dependent["child-all"].targets[0]
    assert graph.dependents_by_target[origin_target] == (
        "child-all",
        "child-any",
    )


def test_same_origin_with_distinct_conditions_is_valid() -> None:
    origin = sample_pair("origin")
    child = sample_pair(
        "child",
        hook_name="all(origin-head-filled,origin-tail-closed)",
    )

    graph = compile_dependency_graph((origin, child))

    assert len(graph.by_dependent["child"].targets) == 2
