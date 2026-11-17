# 0011. AI: one provider behind an interface, evidence bundles, checked citations

**Status:** accepted (Phase 7)

## Context

ADR 0008 settled that rules detect problems and AI only explains them. Phase 7 adds the
assistant, which raises four questions: which provider, what the model may see, how to stop it
inventing things, and how to test it without a key or a bill.

## Decision

- **One provider, OpenAI, behind a two-method interface** (`complete`, `check`) in
  `ai/provider.py`, called with plain HTTP: Chat Completions with a strict JSON schema, so the
  answer can only have the shape the server validates. The base URL is configurable
  (`QP_OPENAI_BASE_URL`) for compatible gateways such as OpenRouter: the model id may then be
  `author/model:tag` (validated and URL-encoded, never free path text), the key is checked at
  `GET /key` and the model against `GET /models`, and a model that ignores the strict schema
  is still held to it, because the answer is parsed and checked the same way. The key is the admin's own, Fernet-encrypted
  like source passwords, shown only by its last four characters, sent only to the provider it
  was saved for, and checked with the provider (retrieving the model) before AI is turned on.
- **Off by default.** No request is ever made until an admin saves a provider, a model and a
  key and turns it on. Only the admin can ask, because asking costs money; everyone can read
  answers already given.
- **The evidence bundle is built by code, never by the model** (`ai/evidence.py`): the rule,
  the facts, the operation's metrics over the finding's window, the cited plan node by node,
  and the table or index samples. SQL literals become `?`; MongoDB data is already redacted
  (ADR 0010); no host, port, username, database name or connection string is included; the
  finding's free-text summary is left out because it quotes plan filters. Every item has an id.
  The user can view the exact bundle.
- **Citations are checked, not requested.** Every claim must cite bundle ids. An answer citing
  an id that wasn't sent is rejected and shown as an error; claims without citations are
  flagged; a draft containing a destructive statement (DROP TABLE, DELETE, ALTER SYSTEM,
  `deleteMany`, ...) is rejected. Provider errors keep only our own message, never the
  provider's text, which can echo part of the key.
- **Cached by the evidence**: answers are stored by (bundle hash, task, provider, model). The
  hash is taken over a stable view of the bundle (numbers to two significant figures, no sample
  timestamps), so the per-minute drift of counts doesn't make every answer stale.
- **A fake provider** with canned, correctly cited answers (and modes that misbehave on purpose)
  runs every test and CI job. It's offered in the UI only when `QP_AI_ALLOW_FAKE_PROVIDER` is
  set, which production refuses.
- **An evaluation set** (`evals/bundles/`, saved from the demo) with `make ai-eval` scores the
  real model on the seven planted problems: right cause, valid citations, no forbidden action.

## Consequences

- With AI off, Query Pulse is exactly what it was: QP-AC-26 checks the provider request counter
  stays at zero through the whole end-to-end suite.
- A model can't put an unsupported claim in front of a user as fact, and can't get a
  destructive statement shown as advice. It can still be wrong about a cited fact's meaning,
  which is why the panel is labelled advisory.
- Adding a provider means one class with two methods, and a key re-entry when switching.
- The real-model scores need a person with a key to run `make ai-eval`; CI can't publish them.
