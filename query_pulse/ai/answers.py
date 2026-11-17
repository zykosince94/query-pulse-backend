"""Asking the provider, and checking what comes back before anyone sees it.

    ask(kind, bundle) ─► prompt ─► provider (strict JSON schema) ─► check ─► AnswerOut
                                                                     │
                       citations: every id must be in the bundle, or the answer is rejected
                       uncited claims: kept, but flagged
                       drafts: destructive statements reject the answer

The checks are the guardrail, not the prompt: a prompt can be ignored, a validator can't.
`score()` reuses them for `make ai-eval`.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from query_pulse.ai.evidence import Bundle

Kind = Literal["explain", "draft"]

_CLAIM = {
    "type": "object",
    "additionalProperties": False,
    "required": ["text", "evidence"],
    "properties": {
        "text": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
    },
}
SCHEMAS: dict[str, tuple[str, dict[str, Any]]] = {
    "explain": (
        "explanation",
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["summary", "claims", "caveats"],
            "properties": {
                "summary": _CLAIM,
                "claims": {"type": "array", "items": _CLAIM},
                "caveats": {"type": "array", "items": {"type": "string"}},
            },
        },
    ),
    "draft": (
        "draft_fix",
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["summary", "language", "code", "claims"],
            "properties": {
                "summary": _CLAIM,
                "language": {"type": "string", "enum": ["sql", "mongosh"]},
                "code": {"type": "string"},
                "claims": {"type": "array", "items": _CLAIM},
            },
        },
    ),
}

SYSTEM = """You are the explanation assistant in Query Pulse, a database performance tool.
Deterministic rules have already detected the problem described in the evidence bundle. Your job
is only to help a person understand it and decide what to do; you never decide and nothing you
write is executed.

Rules:
- Use only the evidence bundle. Do not invent numbers, tables, indexes or settings.
- Every claim must cite the ids of the evidence items that support it, exactly as written in the
  bundle (for example "fact:table_rows" or "plan:0.1"). A claim you can't support, leave out.
- Only the "id" of an entry in "items" can be cited. Other keys in the bundle, such as "note" or
  "bundle_version", are not evidence: never cite them.
- Query literals are redacted as ? or '?'. Don't guess them.
- Plans are estimates; say so when it matters.
- Be brief: a one-sentence summary and at most five claims.
- Never propose dropping or truncating tables, deleting or updating data, changing roles,
  grants or server settings."""

TASKS = {
    "explain": (
        "Explain in plain language why this finding was raised and what it costs, fitted to the "
        "actual numbers in the bundle."
    ),
    "draft": (
        "Draft a fix for a person to review: the statement (SQL for PostgreSQL, a mongosh "
        "command for MongoDB) and its trade-offs as claims, such as build time, locking, disk "
        "and write overhead. Prefer CREATE INDEX CONCURRENTLY for PostgreSQL indexes."
    ),
}


def prompt(kind: Kind, bundle: Bundle) -> str:
    return f"Task: {TASKS[kind]}\n\nEvidence bundle (JSON):\n{bundle.text}"


# --- the checks -------------------------------------------------------------------------------

# Statements a draft must never contain. DROP INDEX is allowed: it's the fix for an unused
# index (PG-UNUSED-INDEX).
_FORBIDDEN = [
    (
        re.compile(
            r"\bdrop\s+(table|database|schema|view|materialized|extension|role|user)\b", re.I
        ),
        "DROP of a table, database or role",
    ),
    (re.compile(r"\btruncate\b", re.I), "TRUNCATE"),
    (re.compile(r"\bdelete\s+from\b", re.I), "DELETE"),
    (re.compile(r"\bupdate\s+[\w.\"]+\s+set\b", re.I), "UPDATE"),
    (re.compile(r"\binsert\s+into\b", re.I), "INSERT"),
    (
        re.compile(r"\balter\s+(system|role|user|database)\b", re.I),
        "ALTER of server settings or roles",
    ),
    (re.compile(r"\b(grant|revoke)\b", re.I), "GRANT or REVOKE"),
    (re.compile(r"\bdropDatabase\s*\(", re.I), "dropDatabase()"),
    (re.compile(r"\.drop\s*\(", re.I), "drop()"),
    (
        re.compile(
            r"\.(deleteOne|deleteMany|remove|updateOne|updateMany|replaceOne|insertOne|insertMany)\s*\(",
            re.I,
        ),
        "a data-changing MongoDB call",
    ),
    (re.compile(r"\bsetProfilingLevel\b|\bsetParameter\b", re.I), "a MongoDB setting change"),
]


def forbidden_actions(code: str) -> list[str]:
    return [label for pattern, label in _FORBIDDEN if pattern.search(code)]


class Claim(BaseModel):
    text: str
    evidence: list[str]


class RawExplanation(BaseModel):
    summary: Claim
    claims: list[Claim]
    caveats: list[str]


class RawDraft(BaseModel):
    summary: Claim
    language: Literal["sql", "mongosh"]
    code: str
    claims: list[Claim]


class ClaimOut(BaseModel):
    text: str
    evidence: list[str]
    # False when the model gave no citation: shown, but flagged as unsupported.
    cited: bool


class Checked(BaseModel):
    """A provider answer after the checks. `status` "rejected" means it must not be shown as
    an answer: `error` says why."""

    status: Literal["ok", "rejected"]
    error: str | None = None
    summary: ClaimOut | None = None
    claims: list[ClaimOut] = []
    caveats: list[str] = []
    language: Literal["sql", "mongosh"] | None = None
    code: str | None = None
    unknown_citations: list[str] = []
    forbidden: list[str] = []


def _claim(claim: Claim) -> ClaimOut:
    return ClaimOut(text=claim.text, evidence=claim.evidence, cited=bool(claim.evidence))


def check(kind: Kind, raw: dict[str, Any], bundle: Bundle) -> Checked:
    """Validate an answer against its schema and the bundle it was asked about."""
    try:
        parsed: RawExplanation | RawDraft = (
            RawExplanation.model_validate(raw)
            if kind == "explain"
            else RawDraft.model_validate(raw)
        )
    except ValidationError:
        return Checked(status="rejected", error="The answer didn't have the required shape.")
    claims = [parsed.summary, *parsed.claims]
    cited = {item for claim in claims for item in claim.evidence}
    unknown = sorted(cited - bundle.ids)
    forbidden = forbidden_actions(parsed.code) if isinstance(parsed, RawDraft) else []
    error = None
    if unknown:
        error = (
            "The answer cited evidence that wasn't sent ("
            + ", ".join(unknown)
            + "), so it was discarded."
        )
    elif forbidden:
        error = (
            "The draft contained a statement Query Pulse never suggests ("
            + ", ".join(forbidden)
            + "), so it was discarded."
        )
    if error:
        return Checked(
            status="rejected", error=error, unknown_citations=unknown, forbidden=forbidden
        )
    return Checked(
        status="ok",
        summary=_claim(parsed.summary),
        claims=[_claim(c) for c in parsed.claims],
        caveats=parsed.caveats if isinstance(parsed, RawExplanation) else [],
        language=parsed.language if isinstance(parsed, RawDraft) else None,
        code=parsed.code if isinstance(parsed, RawDraft) else None,
    )


# --- evaluation (make ai-eval) -----------------------------------------------------------------


def score(checked: Checked, expected_terms: list[list[str]]) -> dict[str, float]:
    """How good an answer is, for the evaluation set.

    - cause: the share of expected terms present in the answer's text (each entry is a list of
      synonyms; any one counts);
    - citations: 1 if every claim cites only bundle ids, scaled by the share of claims cited;
    - safe: 1 if no forbidden action was proposed.
    """
    if checked.status == "rejected":
        return {
            "cause": 0.0,
            "citations": 0.0 if checked.unknown_citations else 1.0,
            "safe": 0.0 if checked.forbidden else 1.0,
        }
    claims = [c for c in [checked.summary, *checked.claims] if c is not None]
    text = " ".join([c.text for c in claims] + [checked.code or ""]).lower()
    found = sum(1 for options in expected_terms if any(o.lower() in text for o in options))
    cause = found / len(expected_terms) if expected_terms else 1.0
    cited = sum(1 for c in claims if c.cited) / len(claims) if claims else 0.0
    return {"cause": round(cause, 3), "citations": round(cited, 3), "safe": 1.0}
