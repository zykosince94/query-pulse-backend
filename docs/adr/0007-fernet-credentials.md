# 0007. Fernet for stored credentials

**Status:** accepted (Phase 2)

## Context

Source passwords must be stored so collection can run without the user present. The options were
a secrets manager (Vault, a cloud KMS), database-level encryption (pgcrypto), or
application-level encryption.

## Decision

Application-level encryption with Fernet (AES-128-CBC + HMAC-SHA256) through `MultiFernet`,
keyed by `QP_FERNET_KEY`. Several comma-separated keys rotate: the first encrypts, all decrypt.
Passwords are decrypted only in the service layer, right before an adapter connects. They are
never returned and never logged (the marker-secret test, QP-AC-08). A stored password is only
sent to the engine, host, port and username it was saved for.

## Consequences

- No extra service, and a stolen database dump without the key reveals nothing.
- The key lives in the environment. Losing it makes stored passwords unreadable, and they have to
  be entered again.
- A real deployment would load the key from a secrets manager; the interface stays the same.
