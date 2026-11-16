"""MongoDB plans: redact `explain` output (queryPlanner verbosity), then read it into stages.

explain at queryPlanner verbosity never executes the query; it reports the plan MongoDB
would choose (the winning plan) and the ones it rejected. Its raw output, however, repeats
the query's values: `parsedQuery`, each stage's `filter`, index bounds like "[42, 42]",
limit amounts. Query Pulse stores no query values (ADR 0010), so `redact()` runs before
anything is stored:

- filters go through the shape normaliser (values become "?");
- index bounds are kept only when they contain no value (the full ranges MinKey..MaxKey and
  "" .. {} for strings); any other bound becomes "?". A full range is exactly what the
  rules need to know: the scan covers the whole index;
- the slot-based (SBE) plan text, server details and the echoed command are dropped;
- every other stage field is kept only if it's a number or a boolean, or a known field
  that holds structure (stage names, index names, key patterns, sort patterns).

`parse_plan()` then reads the redacted document into `MongoPlanStage`s for the viewer and
the rules, with positional paths ("0", "0.0", ...) like PostgreSQL plans.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from query_pulse.workload.mongo_shape import PLACEHOLDER, canonical_json, normalise
from query_pulse.workload.schemas import MongoPlanStage

# Index bounds that hold no query value: an unbounded field, or every string.
FULL_RANGES = frozenset({"[MinKey, MaxKey]", "[MaxKey, MinKey]", '["", {})', '({}, ""]'})

# Stage fields that hold structure, not values (kept as they are).
_STRUCTURE = {
    "stage",
    "indexName",
    "keyPattern",
    "sortPattern",
    "direction",
    "type",
    "isMultiKey",
    "multiKeyPaths",
    "isUnique",
    "isSparse",
    "isPartial",
    "indexVersion",
    "memLimit",
    "planNodeId",
}
_VALUES = {"limitAmount", "skipAmount"}  # how many: a value from the query
_CHILDREN = ("inputStage", "inputStages", "outerStage", "innerStage", "thenStage", "elseStage")


class MongoPlanFormatError(ValueError):
    """The document isn't a MongoDB queryPlanner explain."""


def query_planner(explain: dict[str, Any]) -> dict[str, Any] | None:
    """The queryPlanner section: at the top for find/count/distinct, and for aggregations
    either at the top (pushed down to the query engine) or in the first stage's $cursor."""
    planner = explain.get("queryPlanner")
    if isinstance(planner, dict):
        return planner
    stages = explain.get("stages")
    if isinstance(stages, list) and stages and isinstance(stages[0], dict):
        cursor = stages[0].get("$cursor")
        if isinstance(cursor, dict) and isinstance(cursor.get("queryPlanner"), dict):
            planner = cursor["queryPlanner"]
            return planner if isinstance(planner, dict) else None
    return None


def _winning(plan: Any) -> Any:
    # With the slot-based engine (SBE), the stage tree sits under `queryPlan`.
    if isinstance(plan, dict) and isinstance(plan.get("queryPlan"), dict):
        return plan["queryPlan"]
    return plan


def _redact_bounds(bounds: Any) -> dict[str, list[str]]:
    if not isinstance(bounds, dict):
        return {}
    return {
        str(field): [b if b in FULL_RANGES else PLACEHOLDER for b in ranges]
        if isinstance(ranges, list)
        else [PLACEHOLDER]
        for field, ranges in bounds.items()
    }


def _redact_stage(stage: Any) -> dict[str, Any]:
    if not isinstance(stage, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in stage.items():
        if key in _STRUCTURE:
            out[key] = value
        elif key in _VALUES:
            out[key] = PLACEHOLDER
        elif key == "filter":
            out[key] = normalise(value, "filter")
        elif key == "indexBounds":
            out[key] = _redact_bounds(value)
        elif key == "transformBy":
            out[key] = normalise(value, "expression")
        elif key in _CHILDREN:
            out[key] = (
                [_redact_stage(s) for s in value]
                if isinstance(value, list)
                else _redact_stage(value)
            )
        elif isinstance(value, bool | int | float):
            out[key] = value
        # Anything else (unknown strings and documents) could hold a value: dropped.
    return out


def redact(explain: dict[str, Any]) -> dict[str, Any]:
    """What Query Pulse stores of an explain: the plans and their structure, no values."""
    planner = query_planner(explain)
    if planner is None:
        raise MongoPlanFormatError("no queryPlanner section in the explain output")
    stages = explain.get("stages")
    pipeline = (
        [next(iter(s)) for s in stages[1:] if isinstance(s, dict) and s]
        if isinstance(stages, list)
        else []
    )
    return {
        "namespace": planner.get("namespace"),
        "parsedQuery": normalise(planner.get("parsedQuery", {}), "filter"),
        "indexFilterSet": bool(planner.get("indexFilterSet", False)),
        "winningPlan": _redact_stage(_winning(planner.get("winningPlan"))),
        "rejectedPlans": [_redact_stage(_winning(p)) for p in planner.get("rejectedPlans") or []],
        # Aggregation stages that run after the query engine ($group, $sort, ...), by name.
        "pipelineStages": pipeline,
    }


def _text(value: Any) -> str | None:
    return None if value is None else canonical_json(value)


def _children(raw: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for key in _CHILDREN:
        value = raw.get(key)
        if isinstance(value, dict):
            found.append(value)
        elif isinstance(value, list):
            found.extend(v for v in value if isinstance(v, dict))
    return found


def _stage(raw: dict[str, Any], path: str) -> MongoPlanStage:
    name = raw.get("stage")
    if not isinstance(name, str):
        raise MongoPlanFormatError(f"plan stage {path} has no 'stage'")
    bounds = raw.get("indexBounds") if isinstance(raw.get("indexBounds"), dict) else {}
    assert isinstance(bounds, dict)  # noqa: S101 - narrowed above
    full = bool(bounds) and all(
        isinstance(ranges, list) and any(b in FULL_RANGES for b in ranges)
        for ranges in bounds.values()
    )
    named = {"stage", "indexName", "keyPattern", "sortPattern", "direction", "filter"}
    named |= {"indexBounds", "isMultiKey", *_CHILDREN}
    return MongoPlanStage(
        path=path,
        stage=name,
        index_name=raw.get("indexName") if isinstance(raw.get("indexName"), str) else None,
        key_pattern=_text(raw.get("keyPattern")),
        sort_pattern=_text(raw.get("sortPattern")),
        direction=raw.get("direction") if isinstance(raw.get("direction"), str) else None,
        filter=_text(raw.get("filter")),
        index_bounds={str(k): [str(b) for b in v] for k, v in bounds.items()},
        full_index_scan=full,
        is_multi_key=raw.get("isMultiKey") if isinstance(raw.get("isMultiKey"), bool) else None,
        details={
            key: str(value)
            for key, value in raw.items()
            if key not in named and isinstance(value, str | int | float | bool)
        },
        children=[_stage(child, f"{path}.{i}") for i, child in enumerate(_children(raw))],
    )


def parse_plan(document: Any) -> MongoPlanStage:
    """The winning plan's root stage of a stored (redacted) MongoDB plan document."""
    planner = _stored(document)
    winning = planner.get("winningPlan")
    if not isinstance(winning, dict):
        raise MongoPlanFormatError("the plan has no 'winningPlan'")
    return _stage(winning, "0")


def rejected_count(document: Any) -> int:
    rejected = _stored(document).get("rejectedPlans")
    return len(rejected) if isinstance(rejected, list) else 0


def pipeline_stages(document: Any) -> list[str]:
    stages = _stored(document).get("pipelineStages")
    return [str(s) for s in stages] if isinstance(stages, list) else []


def _stored(document: Any) -> dict[str, Any]:
    # Stored like PostgreSQL plans, as a one-element list: [redacted explain].
    if not isinstance(document, list) or not document or not isinstance(document[0], dict):
        raise MongoPlanFormatError("expected a list holding one redacted explain document")
    stored: dict[str, Any] = document[0]
    return stored


def walk(stage: MongoPlanStage) -> Iterator[MongoPlanStage]:
    """Every stage, parents before children."""
    yield stage
    for child in stage.children:
        yield from walk(child)


def walk_with_ancestors(
    stage: MongoPlanStage, ancestors: tuple[MongoPlanStage, ...] = ()
) -> Iterator[tuple[MongoPlanStage, tuple[MongoPlanStage, ...]]]:
    yield stage, ancestors
    for child in stage.children:
        yield from walk_with_ancestors(child, (*ancestors, stage))
