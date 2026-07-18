"""Pure parsing and validation for strategy pair dependencies."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterable, Mapping

from kolabi.bot.domain import (
    HookEvidence,
    HookExpression,
    HookMode,
    HookTarget,
    HookTargetKind,
    OrderPairSpec,
)


class HookDependencyError(ValueError):
    """Raised when a strategy dependency expression or graph is invalid."""


@dataclass(frozen=True)
class DependencyGraph:
    """Compiled expressions and their reverse event-to-dependent index."""

    by_dependent: Mapping[str, HookExpression]
    dependents_by_target: Mapping[HookTarget, tuple[str, ...]]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "by_dependent",
            MappingProxyType(dict(self.by_dependent)),
        )
        object.__setattr__(
            self,
            "dependents_by_target",
            MappingProxyType(dict(self.dependents_by_target)),
        )


_EXPRESSION_RE = re.compile(r"^(all|any)\s*\((.*)\)$", re.IGNORECASE)


def parse_hook_expression(raw: str | None) -> HookExpression | None:
    """Parse a legacy target or an explicit ALL/ANY expression."""

    value = (raw or "").strip()
    if not value:
        return None

    match = _EXPRESSION_RE.fullmatch(value)
    if match is None:
        if any(character in value for character in "(),"):
            raise HookDependencyError(f"malformed dependency expression {value!r}")
        return HookExpression(mode=HookMode.ALL, targets=(parse_hook_target(value),))

    mode = HookMode(match.group(1).lower())
    body = match.group(2).strip()
    if not body:
        raise HookDependencyError(f"{mode.value}() requires at least one target")
    if "(" in body or ")" in body:
        raise HookDependencyError("nested dependency expressions are not supported")

    raw_targets = body.split(",")
    if any(not target.strip() for target in raw_targets):
        raise HookDependencyError("dependency expressions cannot contain empty targets")
    targets = tuple(parse_hook_target(target) for target in raw_targets)
    return HookExpression(mode=mode, targets=targets)


def parse_hook_target(raw: str) -> HookTarget:
    """Parse one pair lifecycle target, preserving legacy close semantics."""

    value = raw.strip()
    if not value:
        raise HookDependencyError("dependency target cannot be empty")
    if any(character in value for character in "(),"):
        raise HookDependencyError(f"malformed dependency target {value!r}")
    if value.endswith("-head-filled"):
        origin_name = value[: -len("-head-filled")]
        if not origin_name:
            raise HookDependencyError("head-filled dependency requires a pair name")
        return HookTarget(origin_name, HookTargetKind.HEAD_FILLED)
    for suffix in ("-tail-closed", "-closed"):
        if value.endswith(suffix):
            origin_name = value[: -len(suffix)]
            if not origin_name:
                raise HookDependencyError("tail-closed dependency requires a pair name")
            return HookTarget(origin_name, HookTargetKind.PAIR_CLOSED)
    return HookTarget(value, HookTargetKind.PAIR_CLOSED)


def compile_dependency_graph(pairs: Iterable[OrderPairSpec]) -> DependencyGraph:
    """Validate and compile all dependency expressions before runtime startup."""

    pair_list = tuple(pairs)
    names = [pair.name for pair in pair_list]
    duplicate_names = sorted(
        name for name, count in Counter(names).items() if count > 1
    )
    if duplicate_names:
        raise HookDependencyError(
            f"duplicate pair name(s): {', '.join(duplicate_names)}"
        )
    known_names = set(names)
    by_dependent: dict[str, HookExpression] = {}
    reverse: dict[HookTarget, list[str]] = {}

    for pair in pair_list:
        try:
            expression = parse_hook_expression(pair.hook_name)
        except HookDependencyError as exc:
            raise HookDependencyError(
                f"invalid hook for pair {pair.name!r}: {exc}"
            ) from exc
        if expression is None:
            continue
        seen_targets: set[HookTarget] = set()
        for target in expression.targets:
            if target.origin_pair_name not in known_names:
                raise HookDependencyError(
                    f"pair {pair.name!r} depends on missing pair "
                    f"{target.origin_pair_name!r}"
                )
            if target.origin_pair_name == pair.name:
                raise HookDependencyError(
                    f"pair {pair.name!r} cannot depend on itself"
                )
            if target in seen_targets:
                raise HookDependencyError(
                    f"pair {pair.name!r} repeats target "
                    f"{format_hook_target(target)!r}"
                )
            seen_targets.add(target)
            reverse.setdefault(target, []).append(pair.name)
        by_dependent[pair.name] = expression

    _validate_acyclic(names, by_dependent)
    return DependencyGraph(
        by_dependent=by_dependent,
        dependents_by_target={
            target: tuple(dependents) for target, dependents in reverse.items()
        },
    )


def dependency_satisfied(
    expression: HookExpression,
    evidence: Iterable[HookEvidence],
) -> bool:
    """Evaluate an expression against evidence from the current attempt."""

    satisfied_targets = {item.target for item in evidence}
    if expression.mode == HookMode.ANY:
        return any(target in satisfied_targets for target in expression.targets)
    return all(target in satisfied_targets for target in expression.targets)


def format_hook_target(target: HookTarget) -> str:
    """Return the canonical strategy spelling for one target."""

    suffix = (
        "-head-filled"
        if target.kind == HookTargetKind.HEAD_FILLED
        else "-tail-closed"
    )
    return f"{target.origin_pair_name}{suffix}"


def format_hook_expression(expression: HookExpression) -> str:
    """Return a compact, whitespace-free dependency expression."""

    targets = ",".join(format_hook_target(target) for target in expression.targets)
    if len(expression.targets) == 1:
        return targets
    return f"{expression.mode.value}({targets})"


def format_dependency_wait(
    expression: HookExpression,
    evidence: Iterable[HookEvidence],
) -> str:
    """Return compact operator-facing progress for a blocked dependency."""

    evidence_tuple = tuple(evidence)
    satisfied_targets = {item.target for item in evidence_tuple}
    if len(expression.targets) == 1:
        return format_hook_target(expression.targets[0])
    pending = [
        format_hook_target(target)
        for target in expression.targets
        if target not in satisfied_targets
    ]
    return (
        f"{expression.mode.value}:{len(satisfied_targets)}/{len(expression.targets)}"
        f":pending={','.join(pending) or '-'}"
    )


def format_dependency_release(
    expression: HookExpression,
    evidence: Iterable[HookEvidence],
) -> str:
    """Return compact evidence details for a newly released pair."""

    evidence_tuple = tuple(evidence)
    items = ",".join(
        f"{format_hook_target(item.target)}#{item.origin_attempt_index}"
        for item in evidence_tuple
    )
    if len(expression.targets) == 1:
        item = evidence_tuple[0]
        return f"{item.target.origin_pair_name}#{item.origin_attempt_index}"
    return f"{expression.mode.value}:{items}"


def _validate_acyclic(
    names: Iterable[str],
    expressions: Mapping[str, HookExpression],
) -> None:
    adjacency = {
        name: tuple(
            target.origin_pair_name
            for target in expressions.get(
                name,
                HookExpression(mode=HookMode.ALL, targets=()),
            ).targets
        )
        for name in names
    }
    visited: set[str] = set()
    active: set[str] = set()
    path: list[str] = []

    def visit(name: str) -> None:
        if name in visited:
            return
        if name in active:
            cycle_start = path.index(name)
            cycle = path[cycle_start:] + [name]
            raise HookDependencyError(
                f"dependency cycle detected: {' -> '.join(cycle)}"
            )
        active.add(name)
        path.append(name)
        for origin_name in adjacency[name]:
            visit(origin_name)
        path.pop()
        active.remove(name)
        visited.add(name)

    for pair_name in adjacency:
        visit(pair_name)
