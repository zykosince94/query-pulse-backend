# 0005. Signed cookie sessions, same origin, no tokens

**Status:** accepted (Phase 1)

## Context

The API serves one first-party React app. The options were bearer tokens (JWTs) in browser
storage, or server-validated cookie sessions.

## Decision

Starlette's signed session cookie (`qp_session`): HttpOnly, SameSite=Lax, Secure required in
production, holding only the user id. The user is loaded on every request. The frontend and API
share one origin (Vite's proxy in development, Caddy in production), so there's no CORS. A fresh
session starts at every sign-in. Cross-site writes are refused by an Origin check, and sign-in is
rate-limited per client.

## Consequences

- No token sits in JavaScript-readable storage, so XSS can't steal a session, and deleted users
  are signed out immediately.
- Sessions can't be revoked individually: rotating `QP_SESSION_SECRET` signs everyone out (a
  documented, accepted risk).
- The rate limit is in memory, which is correct for one API instance (ADR 0006).
