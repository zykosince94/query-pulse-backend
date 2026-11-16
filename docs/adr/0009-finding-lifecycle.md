# 0009. One finding per rule and target; dismissals stick

**Status:** accepted (Phase 5)

## Context

The rules run after every collection cycle, so the same problem is detected again every minute.
A naive design inserts a finding each time (thousands of duplicates), or deletes and recreates
them (losing the user's dismissal, and breaking links to a finding). Rules also see evidence
with a delay: right after someone adds the missing index, the stored plan is still the old one
for up to five minutes.

## Decision

- **One row per (source, rule, target)** for the life of the source, enforced by a unique
  constraint. Re-detection updates that row's evidence, facts and last-seen time.
- **Status is a small state machine** with an owner per transition:

  | From | Event | To | By |
  |---|---|---|---|
  | (none) | detected | open | rules |
  | open | not seen for `QP_FINDING_RESOLVE_AFTER_SECONDS` | resolved | rules |
  | open | dismiss / resolve | dismissed / resolved | user |
  | dismissed, resolved | reopen | open | user |
  | dismissed | detected again | dismissed (refreshed) | — |
  | resolved | detected on evidence newer than the resolution | open | rules |

- A detection carries `evidence_at`, the time of its newest evidence. A resolved finding reopens
  only when `evidence_at` is after `resolved_at`, so stale evidence never contradicts the user.
- Findings cite evidence by id (observation window, plan id and node path, relation sample id)
  and keep their **facts** (the numbers) with them, because retention eventually deletes the
  underlying data. Each evidence link reports whether it still resolves.

## Consequences

- Dismissal is durable (QP-AC-21), and the list never fills with duplicates (QP-AC-18).
- A finding's id is stable, so it can be linked to and, in Phase 7, explained by AI.
- A dismissed finding keeps updating silently. If its numbers get much worse, the user doesn't
  hear about it. That's acceptable for the MVP (no alerting, by scope).
- Automatic resolution can lag by up to the resolve-after period, which is shown honestly as
  "resolved automatically".
