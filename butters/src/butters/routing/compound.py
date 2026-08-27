"""Bounded planner for independent read-only clauses in one request.

This is deliberately not an agent framework. It cannot invent an operation,
call a model, plan recursively, or compose a mutating action. It splits a
request the deterministic router could not answer as a whole into a few
independent clauses, routes each clause through that same deterministic
router, and keeps the result only when every clause resolved to a
non-mutating capability the registry already exposes.

Everything it can produce is therefore something the router would have
produced on its own for a shorter request, which is what keeps the surface
identical to the single-clause path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from butters.routing.model import RoutedIntent
from butters.skills.model import ActionClass, AuthenticationLevel, SkillAudience
from butters.skills.registry import SkillRegistry

# A compound answer is a convenience, not a workload. Three reads is enough for
# the requests this exists to serve and small enough that a pathological input
# cannot turn one turn into a fan-out.
MAX_COMPOUND_OPERATIONS = 3

# More clauses than this is not a compound request but a list, and is refused
# rather than truncated, so the user is never silently given a partial answer.
MAX_COMPOUND_CLAUSES = 4

_SPLIT = re.compile(r"\s+and\s+|\s+also\s+|\s*[;,]\s*|\s*\?\s*")


@dataclass(frozen=True, slots=True)
class CompoundOperation:
    skill: str
    arguments: dict[str, object]
    clause: str


@dataclass(frozen=True, slots=True)
class CompoundPlan:
    """The outcome of planning. `operations` is empty unless status is planned."""

    status: str
    operations: tuple[CompoundOperation, ...] = ()
    unresolved: tuple[str, ...] = ()
    clauses: tuple[str, ...] = field(default_factory=tuple)

    @property
    def planned(self) -> bool:
        return self.status == "planned" and bool(self.operations)


def _composable(registry: SkillRegistry, skill: str) -> bool:
    """Whether a routed clause may take part in a compound read.

    Mirrors the model-visibility policy: non-mutating, no authentication of its
    own, no side effects, and not an administrator-only observation. An action
    is never composable here, so a compound request can never become a way to
    sequence privileged operations.
    """

    spec = registry.get(skill)
    return (
        spec is not None
        and registry.is_enabled(skill)
        and spec.available
        and spec.action_class in {ActionClass.READ_ONLY, ActionClass.ANALYTICAL}
        and spec.authentication is AuthenticationLevel.NONE
        and spec.side_effects == "none"
        and spec.audience is SkillAudience.NORMAL
        and not spec.explicit_intent_required
        and not spec.confirmation_required
    )


def split_clauses(normalized: str) -> tuple[str, ...]:
    """Split on independent conjunctions only."""

    return tuple(
        clause
        for clause in (item.strip() for item in _SPLIT.split(normalized))
        if len(clause.split()) >= 2
    )


def plan_compound_request(
    router: object,
    normalized: str,
    registry: SkillRegistry,
) -> CompoundPlan:
    """Plan a request the router could not answer as a single intent.

    The caller must only reach this after the whole-text route failed, which is
    what keeps an already-supported request - such as a single sensor asked for
    several metrics at once - on its existing efficient single-call path.
    """

    clauses = split_clauses(normalized)
    if len(clauses) < 2:
        return CompoundPlan("not_compound", clauses=clauses)
    if len(clauses) > MAX_COMPOUND_CLAUSES:
        return CompoundPlan("too_broad", clauses=clauses)

    operations: list[CompoundOperation] = []
    unresolved: list[str] = []
    seen: set[tuple[str, str]] = set()
    for clause in clauses:
        intent: RoutedIntent = router.route(clause)
        if not intent.matched or intent.skill is None:
            unresolved.append(clause)
            continue
        if intent.action_plan:
            # A clause that expands into an action plan is never composed.
            return CompoundPlan("action_not_composable", clauses=clauses)
        if not _composable(registry, intent.skill):
            return CompoundPlan("action_not_composable", clauses=clauses)
        key = (intent.skill, repr(sorted(intent.arguments.items())))
        if key in seen:
            continue
        seen.add(key)
        operations.append(
            CompoundOperation(intent.skill, dict(intent.arguments), clause)
        )

    if len(operations) < 2:
        return CompoundPlan(
            "not_compound", clauses=clauses, unresolved=tuple(unresolved)
        )
    if len(operations) > MAX_COMPOUND_OPERATIONS:
        return CompoundPlan("too_broad", clauses=clauses)
    return CompoundPlan(
        "planned",
        tuple(operations),
        tuple(unresolved),
        clauses,
    )
