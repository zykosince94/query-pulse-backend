"""Turn PostgreSQL's EXPLAIN (FORMAT JSON) output into a typed node tree.

The raw JSON is kept as captured (it's the evidence). This module reads it into
`PlanNode`s with the fields the plan viewer shows and Phase 5's rules will test: node type,
relation, index, estimated cost and rows, filter conditions and sort keys. Anything else
PostgreSQL reports stays visible as `details`, so nothing in the plan is hidden.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from query_pulse.workload.schemas import PlanNode

# Keys mapped to named fields; every other scalar key goes into `details`.
_NAMED = {
    "Node Type",
    "Relation Name",
    "Schema",
    "Alias",
    "Index Name",
    "Startup Cost",
    "Total Cost",
    "Plan Rows",
    "Plan Width",
    "Filter",
    "Index Cond",
    "Recheck Cond",
    "Hash Cond",
    "Merge Cond",
    "Join Filter",
    "Sort Key",
    "Join Type",
    "Strategy",
    "Parent Relationship",
    "Plans",
}


class PlanFormatError(ValueError):
    """The JSON isn't an EXPLAIN (FORMAT JSON) result."""


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


def _node(raw: dict[str, Any], path: str) -> PlanNode:
    node_type = raw.get("Node Type")
    if not isinstance(node_type, str):
        raise PlanFormatError(f"plan node {path} has no 'Node Type'")
    sort_key = raw.get("Sort Key")
    children = raw.get("Plans") or []
    if not isinstance(children, list):
        raise PlanFormatError(f"plan node {path} has a 'Plans' that isn't a list")
    return PlanNode(
        path=path,
        node_type=node_type,
        relation_name=_text(raw.get("Relation Name")),
        schema_name=_text(raw.get("Schema")),
        alias=_text(raw.get("Alias")),
        index_name=_text(raw.get("Index Name")),
        startup_cost=float(raw.get("Startup Cost", 0.0)),
        total_cost=float(raw.get("Total Cost", 0.0)),
        plan_rows=float(raw.get("Plan Rows", 0.0)),
        plan_width=int(raw.get("Plan Width", 0)),
        filter=_text(raw.get("Filter")),
        index_cond=_text(raw.get("Index Cond")),
        recheck_cond=_text(raw.get("Recheck Cond")),
        join_cond=_text(raw.get("Hash Cond") or raw.get("Merge Cond") or raw.get("Join Filter")),
        sort_key=[str(k) for k in sort_key] if isinstance(sort_key, list) else [],
        join_type=_text(raw.get("Join Type")),
        strategy=_text(raw.get("Strategy")),
        parent_relationship=_text(raw.get("Parent Relationship")),
        details={
            key: str(value)
            for key, value in raw.items()
            if key not in _NAMED and isinstance(value, str | int | float | bool)
        },
        children=[
            _node(child, f"{path}.{i}")
            for i, child in enumerate(children)
            if isinstance(child, dict)
        ],
    )


def parse_plan(document: Any) -> PlanNode:
    """The root node of an EXPLAIN (FORMAT JSON) document: `[{"Plan": {...}}]`.

    Node paths are positional ("0", "0.0", "0.1", ...), stable for a given plan, so a finding
    can point at one node of one plan.
    """
    if not isinstance(document, list) or not document or not isinstance(document[0], dict):
        raise PlanFormatError("expected a list holding one object with a 'Plan'")
    root = document[0].get("Plan")
    if not isinstance(root, dict):
        raise PlanFormatError("the object has no 'Plan'")
    return _node(root, "0")


def walk(node: PlanNode) -> Iterator[PlanNode]:
    """Every node of the tree, parents before children."""
    yield node
    for child in node.children:
        yield from walk(child)
