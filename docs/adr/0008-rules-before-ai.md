# 0008. Rules detect problems; AI only explains

**Status:** accepted (Phase 1; implemented in Phases 5 and 7)

## Context

An LLM could look at workload data and "find problems". It could also be wrong, inconsistent and
impossible to test.

## Decision

Problems are detected by declarative rules over evidence (plans, statistics, relation samples).
Each rule has an ID, required evidence, a confidence and a manual suggested action, and each is
proven by an acceptance test with a healthy control that must stay silent. AI is optional and off
by default. It may only explain a finding the rules produced and draft a fix as text, citing
evidence IDs that the server validates. Nothing it writes is executed.

## Consequences

- Findings are reproducible and testable; the demo's planted problems are the test suite.
- AI output can be wrong without making a finding wrong, and citations make each claim checkable.
- The product works fully with AI disabled (QP-AC-26).
