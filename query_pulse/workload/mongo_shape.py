"""MongoDB query shapes: the fingerprint of a MongoDB operation.

PostgreSQL normalises statements itself (pg_stat_statements' queryid). MongoDB's profiler
records each operation with its real values, so Query Pulse normalises them here:

    {"find": "orders", "filter": {"customerId": 42}, "limit": 20, "lsid": ..., "$db": "shop"}
  → {"find": "orders", "filter": {"customerId": "?"}, "limit": "?"}

The rule (QP-AC-22): every *value* becomes "?", every *field name and operator* stays. Two
operations that differ only in values share a shape; a different field or operator gives a
different shape. The shape's sha256 is the fingerprint.

Three contexts decide what counts as a value:

- filter (find's `filter`, `$match`, `q`, ...): every scalar is a value, including strings
  starting with "$" (in a filter, {"name": "$x"} compares with the literal "$x"). Lists of
  values ($in: [1, 2, 3]) become one "?", so a list's length doesn't split the shape.
- expression ($project, $group, $expr, ...): "$field" strings are field paths, which are
  structure, so they stay; every other scalar becomes "?".
- literal (sort, hint, keyPattern, index and collection names): kept as is. Sort directions
  and index names are part of the query's structure, never user data.

Driver and session metadata (lsid, $clusterTime, $db, $readPreference, ...) is dropped: it
differs on every call and says nothing about the query. Keys keep their order (for sort,
order is meaning; for filters, a different order is simply a different shape).

Nothing here stores or logs the values. Under the "no stored values" rule (ADR 0010) the
shape is what's persisted; the original command lives only in collector memory, long enough
to explain it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal

PLACEHOLDER = "?"
Context = Literal["filter", "expression", "literal"]

# Operations the collector turns into shapes. Everything else in the profiler (getMore,
# killCursors, hello, createIndexes, ...) is administration or a continuation of an
# operation already counted, and is skipped.
READ_COMMANDS = ("find", "aggregate", "count", "distinct")
WRITE_COMMANDS = ("findAndModify", "update", "delete", "insert")

# Per command: which top-level fields form the shape, and in what context.
_COMMAND_FIELDS: dict[str, dict[str, Context | Literal["value", "pipeline", "update"]]] = {
    "find": {
        "filter": "filter",
        "sort": "literal",
        "projection": "expression",
        "hint": "literal",
        "collation": "literal",
        "limit": "value",
        "skip": "value",
    },
    "aggregate": {"pipeline": "pipeline", "hint": "literal", "collation": "literal"},
    "count": {"query": "filter", "hint": "literal", "limit": "value", "skip": "value"},
    "distinct": {"key": "literal", "query": "filter", "collation": "literal"},
    "findAndModify": {
        "query": "filter",
        "sort": "literal",
        "update": "update",
        "remove": "literal",
        "new": "literal",
        "upsert": "literal",
        "fields": "expression",
    },
    # The profiler records one update/delete statement per entry (op "update" / "remove"):
    # {"q": ..., "u": ..., "multi": ..., "upsert": ...} / {"q": ..., "limit": ...}.
    "update": {"q": "filter", "u": "update", "multi": "literal", "upsert": "literal"},
    "delete": {"q": "filter", "limit": "literal"},
    "insert": {},
}

# Aggregation stages whose argument is a filter, a literal, or a single value.
_FILTER_STAGES = {"$match"}
_LITERAL_STAGES = {"$sort", "$unwind", "$count", "$sortByCount", "$unset", "$out", "$merge"}
_VALUE_STAGES = {"$limit", "$skip", "$sample"}
_LOOKUP_NAMES = ("from", "localField", "foreignField", "as")
# Inside a filter, these operators hold expressions or sub-filters, not plain values.
_EXPRESSION_OPERATORS = {"$expr"}
_SUBFILTER_OPERATORS = {"$and", "$or", "$nor"}
_ELEM_MATCH = "$elemMatch"


def _is_scalar(value: Any) -> bool:
    return not isinstance(value, dict | list)


def normalise(value: Any, context: Context) -> Any:
    """Replace the values in one part of a command, keeping its structure."""
    if context == "literal":
        return value
    if isinstance(value, dict):
        return {key: _normalise_field(key, item, context) for key, item in value.items()}
    if isinstance(value, list):
        if context == "filter" and all(_is_scalar(v) for v in value):
            return PLACEHOLDER  # a list of values ($in: [...]) is one value
        if context == "expression" and all(_is_scalar(v) and not _is_field_path(v) for v in value):
            return PLACEHOLDER
        return [normalise(item, context) for item in value]
    if context == "expression" and _is_field_path(value):
        return value  # "$customerId" is a field path, i.e. structure
    return PLACEHOLDER


def _normalise_field(key: str, value: Any, context: Context) -> Any:
    if context == "filter":
        if key in _EXPRESSION_OPERATORS:
            return normalise(value, "expression")
        if key in _SUBFILTER_OPERATORS and isinstance(value, list):
            return [normalise(item, "filter") for item in value]
        if key == _ELEM_MATCH:
            return normalise(value, "filter")
    return normalise(value, context)


def _is_field_path(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("$") and len(value) > 1


def normalise_pipeline(pipeline: Any) -> Any:
    """Each aggregation stage normalised in the context its argument has."""
    if not isinstance(pipeline, list):
        return PLACEHOLDER
    return [_normalise_stage(stage) for stage in pipeline]


def _normalise_stage(stage: Any) -> Any:
    if not isinstance(stage, dict) or len(stage) != 1:
        return normalise(stage, "expression")
    ((name, argument),) = stage.items()
    if name in _FILTER_STAGES:
        return {name: normalise(argument, "filter")}
    if name in _LITERAL_STAGES:
        return {name: argument}
    if name in _VALUE_STAGES:
        return {name: PLACEHOLDER if _is_scalar(argument) else normalise(argument, "expression")}
    if name == "$lookup" and isinstance(argument, dict):
        out: dict[str, Any] = {}
        for key, item in argument.items():
            if key in _LOOKUP_NAMES:
                out[key] = item
            elif key == "pipeline":
                out[key] = normalise_pipeline(item)
            else:
                out[key] = normalise(item, "expression")
        return {name: out}
    if name == "$facet" and isinstance(argument, dict):
        return {name: {key: normalise_pipeline(item) for key, item in argument.items()}}
    if name in ("$unionWith",) and isinstance(argument, dict):
        return {
            name: {
                key: normalise_pipeline(item) if key == "pipeline" else item
                for key, item in argument.items()
            }
        }
    # $project, $group, $addFields, $set, $replaceRoot, $bucket, ... and anything unknown.
    return {name: normalise(argument, "expression")}


def _normalise_update(update: Any) -> Any:
    """An update document ({"$set": {...}}), a replacement, or an update pipeline."""
    if isinstance(update, list):
        return normalise_pipeline(update)
    return normalise(update, "expression")


@dataclass(frozen=True)
class Shape:
    """One normalised operation: what it does, to which collection, with what structure."""

    command: str  # find, aggregate, count, distinct, findAndModify, update, delete, insert
    database: str
    collection: str
    document: dict[str, Any]  # the normalised command, e.g. {"find": "orders", "filter": ...}
    hash: str

    @property
    def namespace(self) -> str:
        return f"{self.database}.{self.collection}"

    @property
    def text(self) -> str:
        """The shape as compact JSON: what the explorer shows and searches."""
        return canonical_json(self.document)

    @property
    def is_read(self) -> bool:
        return self.command in READ_COMMANDS


def canonical_json(document: Any) -> str:
    # Key order is preserved on purpose (see the module docstring); `default=str` covers any
    # BSON type that survives as a literal (an ObjectId in a hint, say) without failing.
    return json.dumps(document, separators=(", ", ": "), ensure_ascii=False, default=str)


def _command_name(op: str, command: dict[str, Any]) -> str | None:
    if op == "query":
        return "find"
    if op == "update":
        return "update"
    if op == "remove":
        return "delete"
    if op == "insert":
        return "insert"
    for name in (*READ_COMMANDS, "findAndModify"):
        if name in command:
            return name
    return None


def shape_of(op: str, ns: str, command: Any) -> Shape | None:
    """The shape of one profiler entry, or None if it isn't an operation we fingerprint.

    `op` and `ns` are the entry's fields ("query", "shop.orders"); `command` is its
    `command` document.
    """
    if not isinstance(command, dict) or "." not in ns:
        return None
    database, collection = ns.split(".", 1)
    name = _command_name(op, command)
    if name is None or collection.startswith("system."):
        return None
    if "$truncated" in command:
        # The profiler cuts very large commands down to a string. There's no structure left
        # to normalise, so all of this collection's truncated operations share one shape.
        document: dict[str, Any] = {name: collection, "$truncated": PLACEHOLDER}
    else:
        document = {name: collection}
        for key, how in _COMMAND_FIELDS[name].items():
            if key not in command:
                continue
            item = command[key]
            if how == "value":
                document[key] = PLACEHOLDER
            elif how == "pipeline":
                document[key] = normalise_pipeline(item)
            elif how == "update":
                document[key] = _normalise_update(item)
            else:
                document[key] = normalise(item, how)
    digest = hashlib.sha256(canonical_json({"ns": ns, "shape": document}).encode()).hexdigest()
    return Shape(name, database, collection, document, digest)


class NotExplainableError(Exception):
    """This operation can't be explained. The message says why, for the person reading it."""


_WRITE_STAGES = {"$out", "$merge"}


def explain_command(shape: Shape, command: dict[str, Any]) -> dict[str, Any]:
    """The command to wrap in `explain` (at queryPlanner verbosity), or NotExplainableError.

    Only reads are explained: explaining a write needs write privileges, which a monitoring
    user shouldn't have, and a pipeline ending in $out/$merge is a write. The command is
    rebuilt from the fields that define the query, so session metadata (lsid, $clusterTime,
    $db) from the profiled call is never replayed.
    """
    if not shape.is_read:
        return _not_explainable(
            "Plans are captured for reads only (find, aggregate, count, distinct): "
            "explaining a write would need write privileges."
        )
    if "$truncated" in command:
        return _not_explainable("The profiler truncated this command, so it can't be replayed.")
    rebuilt: dict[str, Any] = {shape.command: shape.collection}
    for key in _COMMAND_FIELDS[shape.command]:
        if key in command:
            rebuilt[key] = command[key]
    if shape.command == "aggregate":
        pipeline = rebuilt.get("pipeline")
        if not isinstance(pipeline, list):
            return _not_explainable("The aggregation has no pipeline to explain.")
        if any(isinstance(s, dict) and _WRITE_STAGES & s.keys() for s in pipeline):
            return _not_explainable("The pipeline writes ($out or $merge), so it isn't explained.")
        rebuilt["cursor"] = {}
    return rebuilt


def _not_explainable(message: str) -> dict[str, Any]:
    raise NotExplainableError(message)
