"""Score the AI assistant on the seven planted problems: `make ai-eval` (QP-AC-29).

    uv run python scripts/ai_eval.py                     # real provider (see below)
    uv run python scripts/ai_eval.py --fake              # the fake provider: checks the harness

For each saved evidence bundle in evals/bundles/ (P1-P4, M1-M3, captured from the demo with
scripts/save_eval_bundles.py), it asks for an explanation and a draft fix, runs the same checks
the API runs, and scores:

- cause: the expected cause is named (each problem lists the terms a right answer contains);
- citations: every claim cites only evidence from the bundle, and claims are cited;
- safe: no forbidden action (DROP TABLE, DELETE, ...) was proposed.

A real run needs QP_AI_EVAL_API_KEY (or OPENAI_API_KEY) and QP_AI_EVAL_MODEL; it costs a few
requests per problem. Results are printed and written to evals/results.md for the README.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from query_pulse.ai.answers import SCHEMAS, SYSTEM, Kind, check, prompt, score  # noqa: E402
from query_pulse.ai.evidence import Bundle  # noqa: E402
from query_pulse.ai.provider import AIProvider, FakeProvider, OpenAIProvider, ProviderError  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BUNDLES = ROOT / "evals" / "bundles"
RESULTS = ROOT / "evals" / "results.md"
KINDS: tuple[Kind, ...] = ("explain", "draft")


@dataclass(frozen=True)
class Row:
    problem: str
    kind: str
    status: str
    cause: float
    citations: float
    safe: float
    note: str


def load_cases(directory: Path = BUNDLES) -> list[dict[str, object]]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(directory.glob("*.json"))]


async def evaluate(provider: AIProvider, model: str, cases: list[dict[str, object]]) -> list[Row]:
    rows: list[Row] = []
    for case in cases:
        bundle = Bundle(case["bundle"])  # type: ignore[arg-type]
        terms: list[list[str]] = case["expected_terms"]  # type: ignore[assignment]
        for kind in KINDS:
            name, schema = SCHEMAS[kind]
            try:
                raw = await provider.complete(
                    model=model,
                    system=SYSTEM,
                    user=prompt(kind, bundle),
                    schema_name=name,
                    schema=schema,
                )
            except ProviderError as exc:
                rows.append(Row(str(case["problem"]), kind, "error", 0.0, 0.0, 0.0, str(exc)))
                continue
            checked = check(kind, raw, bundle)
            scores = score(checked, terms)
            rows.append(
                Row(
                    str(case["problem"]),
                    kind,
                    checked.status,
                    scores["cause"],
                    scores["citations"],
                    scores["safe"],
                    checked.error or "",
                )
            )
    return rows


def summary(rows: list[Row]) -> dict[str, float]:
    if not rows:
        return {"cause": 0.0, "citations": 0.0, "safe": 0.0}
    return {
        key: round(sum(getattr(r, key) for r in rows) / len(rows), 3)
        for key in ("cause", "citations", "safe")
    }


def report(rows: list[Row], provider: str, model: str) -> str:
    totals = summary(rows)
    lines = [
        "# AI evaluation results",
        "",
        f"Run {datetime.now(UTC):%Y-%m-%d %H:%M} UTC with provider `{provider}`, model `{model}`, "
        f"over {len({r.problem for r in rows})} planted problems (explain and draft each).",
        "",
        "| Problem | Task | Status | Cause | Citations | Safe | Note |",
        "|---|---|---|---|---|---|---|",
    ]
    lines += [
        f"| {r.problem} | {r.kind} | {r.status} | {r.cause:.2f} | {r.citations:.2f} | "
        f"{r.safe:.2f} | {r.note.replace('|', '/')} |"
        for r in rows
    ]
    lines += [
        "",
        f"**Overall:** cause {totals['cause']:.2f}, citations {totals['citations']:.2f}, "
        f"safe {totals['safe']:.2f} (1.00 is best).",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fake", action="store_true", help="use the fake provider (no network)")
    parser.add_argument("--no-write", action="store_true", help="don't write evals/results.md")
    args = parser.parse_args()
    provider: AIProvider
    if args.fake:
        provider, model = FakeProvider(), "fake"
    else:
        key = os.environ.get("QP_AI_EVAL_API_KEY") or os.environ.get("OPENAI_API_KEY")
        model = os.environ.get("QP_AI_EVAL_MODEL", "")
        if not key or not model:
            sys.exit(
                "Set QP_AI_EVAL_API_KEY (or OPENAI_API_KEY) and QP_AI_EVAL_MODEL, or pass --fake."
            )
        provider = OpenAIProvider(
            key, os.environ.get("QP_OPENAI_BASE_URL", "https://api.openai.com/v1"), timeout=120
        )
    rows = asyncio.run(evaluate(provider, model, load_cases()))
    text = report(rows, provider.name, model)
    print(text)
    if not args.no_write:
        RESULTS.write_text(text, encoding="utf-8")
        print(f"Wrote {RESULTS.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
