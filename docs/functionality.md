# Query Pulse: functionality reference

What Query Pulse does today, across the backend (`query-pulse-backend`) and the frontend
(`query-pulse-frontend`). This page describes **current behaviour only**. Planned work is listed
at the end and marked as such.

*As of 16 November 2026: Phases 1–7 ("Foundation", "Data sources", "PostgreSQL collector",
"Workload explorer and plans", "Findings", "MongoDB", "AI assistant and release"), release
target v1.0.0.*

---

## 1. Summary

| Area | Works today | Placeholder (honest stub) |
|---|---|---|
| Sign-in | Username and password, "Try the demo", sign out, session expiry | — |
| Roles | Admin (full access) and read-only demo user | — |
| Access control | 401 without a session, 403 for the demo user on writes | — |
| App shell | Navigation, current user, read-only badge, not-found and error pages | — |
| Data sources | Add, edit, re-check and delete PostgreSQL and MongoDB sources; real connection test before saving; capability checklist with exact fixes; encrypted credentials | — |
| Workload | Health strip; one explorer ranking PostgreSQL statements and MongoDB query shapes by total time, mean or calls over a time range, with filters kept in the URL; operation detail with metric history; a plan viewer per engine; the "profiling is off" gap and its fix | — |
| Plans | PostgreSQL generic plans (`EXPLAIN (GENERIC_PLAN)`) and MongoDB winning plans (`explain` at queryPlanner verbosity), never executed, captured in the background for each source's busiest operations and stored as evidence; PostgreSQL re-capture on demand (admin) | — |
| Collector | Polls every enabled source. PostgreSQL: per-interval deltas from `pg_stat_statements` (resets, evictions, restarts), table/index statistics. MongoDB: the profiler's operations normalised into query shapes, `serverStatus` samples. Both: plans for busy operations, capability re-checks, retention | — |
| Findings | Seven declarative rules over collected evidence (P1–P4, M1–M3); one finding per rule and target with confidence, evidence kind, facts, a suggested fix to review (SQL or a mongosh command, never run) and evidence links; dismiss, resolve, reopen; automatic resolution | — |
| AI assistant | Optional and off by default: an admin saves an OpenAI key and model; then any finding can be explained or get a draft fix, from a redacted evidence bundle anyone can view, with every claim's citations checked; cached by evidence | — |
| Settings | AI provider, model and masked key (save, turn on and off, delete the key); collection and retention values (read-only, from the environment) | — |
| Ops | `/health`, `/ready`, OpenAPI docs, JSON logs with request IDs; API fuzzing, a version matrix and a nightly soak test | — |
| Local stack | App database, demo PostgreSQL 16 with `pg_stat_statements` and a seeded e-commerce database with planted problems P1–P4, MongoDB 7 replica set with orders and products, its profiler on and planted problems M1–M3, the collector, and a demo workload | — |

Query Pulse never writes to the databases it monitors. It connects only to test a connection,
check capabilities, collect statistics and plan statements, always with read-only sessions, and
it never changes a setting (for example, it
reports that MongoDB profiling is off but never turns it on).

Query Pulse stores no MongoDB query values: shapes replace every value with `?`, plans are
redacted before they're stored, and the commands needed for explain live only in collector
memory (ADR 0010).

---

## 2. Users and roles

There is no sign-up. Users are created from environment variables by the bootstrap command
(§7.2), which runs automatically when the API container starts.

| Role | Created from | Can read | Can write | Signs in with |
|---|---|---|---|---|
| `admin` | `QP_ADMIN_USERNAME`, `QP_ADMIN_PASSWORD` | Everything | Everything | Username and password |
| `demo` | `QP_DEMO_ENABLED=true`, `QP_DEMO_USERNAME`, `QP_DEMO_PASSWORD` | Everything | Nothing (403) | "Try the demo" (no password), or username and password |

- The environment is the only source of truth for accounts. The bootstrap removes every admin or
  demo account the environment doesn't name, and the removed account's sessions stop working on
  their next request:
  - Renaming `QP_ADMIN_USERNAME` creates the new admin and removes the old one.
  - Renaming `QP_DEMO_USERNAME` does the same for the demo user.
  - Setting `QP_DEMO_ENABLED=false` removes the demo user, and "Try the demo" returns 404.
- Changing a password in the environment and restarting updates the stored hash.

---

## 3. Frontend: screens and behaviour

The frontend is a single-page app served at `http://localhost:5173` in development. It calls the
backend through a same-origin proxy (`/api` → `http://localhost:8000`), so the session cookie
works without CORS.

### 3.1 Routes

| Path | Access | Shows |
|---|---|---|
| `/login` | Public | Sign-in form; "Try the demo" when enabled |
| `/` | Signed in | Workload: the source health strip and the explorer. Search params: `range`, `from`, `to`, `sort`, `source`, `q` |
| `/operations/:id` | Signed in | One operation: statement, metrics, history, findings and plan. Search params: `range`, `from`, `to`, and `plan`/`node` (the plan and node a finding cites) |
| `/sources` | Signed in | Data sources list |
| `/sources/new` | Signed in (admin) | Add a source; the demo user sees a read-only notice |
| `/sources/:id` | Signed in | Source detail and capability checklist |
| `/sources/:id/edit` | Signed in (admin) | Edit a source; the demo user sees a read-only notice |
| `/findings` | Signed in | Findings. Search params: `status` (default open), `source`, `rule` |
| `/settings` | Signed in | Settings (placeholder, Phase 7) |
| anything else | Signed in | "Page not found" with a link back to the workload |

### 3.2 Sign-in screen

- **Username and password.** The submit button stays disabled until both fields have text. While
  the request runs, it reads "Signing in…".
- **Wrong credentials** show the server's message, "Invalid username or password", in a red alert.
  The same message appears whether the username or the password was wrong.
- **"Try the demo"** appears only when the backend reports `demo_enabled: true`. It opens a
  read-only session without a password. A note below it explains that the demo is read-only.
- **Already signed in?** Visiting `/login` goes straight to the app.
- **Password managers** work: the fields use `autocomplete="username"` and
  `autocomplete="current-password"`.

### 3.3 Protected pages and returning after sign-in

- Opening any protected page while signed out sends you to
  `/login?redirect=<the page you asked for>`. After signing in you land on that page.
- Only same-origin paths are followed. The value is resolved the way a browser would resolve it
  and its origin compared, so `//evil.example`, `https://evil.example`, `javascript:…`,
  `/\evil.example` and values hiding a tab or newline are all ignored, and you land on `/`
  instead. A same-origin path keeps its query string and `#hash`.

### 3.4 App shell

- **Sidebar** (a horizontal bar on narrow screens) with Workload, Data sources, Findings and
  Settings. The current page is highlighted and marked `aria-current="page"`. Findings shows
  the number of open findings when there are any.
- **Header**: "Signed in as *username*", a **"Read-only demo"** badge for the demo user, and
  **Sign out**.
- **Sign out** ends the server session, clears all cached data in the browser, and shows the
  sign-in screen. It does this even if the sign-out request itself fails.

### 3.5 Session expiry and errors

- If any API request returns 401 (the session expired after 12 hours, or the server's session
  secret changed), the app marks you signed out and shows the sign-in screen.
- If the backend can't be reached, the app shows **"Can't reach the Query Pulse server"** with a
  **Try again** button that retries once the backend is back.
- Other errors show "Something went wrong" with the server's message and **Try again**.

### 3.6 Appearance

- **Light and dark mode and three colour themes** are chosen on the **Settings** page, in an
  **Appearance** card (everyone can use it, the read-only demo user too, because it only
  affects this browser). Mode is Light or Dark; until you choose, the app follows the operating
  system's setting (and its changes). The themes are Pulse (blue, the default), Lagoon (teal)
  and Dusk (violet). They are real radio buttons, so the arrow keys move between them. Mode and
  theme are independent, so there are six looks. The status colours (red,
  amber, green) are the same in every theme. Every text and chart colour pair meets WCAG AA in
  all six; the end-to-end axe check covers light and dark.
- The choice is applied before the first paint (no flash) and kept in `localStorage`; if
  storage is blocked or holds junk, the defaults apply and nothing breaks. Charts redraw in the
  new colours at once.
- All colours come from design tokens (`src/styles/tokens.css`).
- **Loading overlays**: a veil with a spinner and a message covers what is loading: the
  findings list while another tab or filter loads ("Updating findings…", not on the quiet
  30-second refresh), an AI panel while the provider answers (with the seconds waited), and the
  whole window when a page or the first sign-in check takes more than 300 ms.
- **Logo**: a database cylinder with a pulse (heartbeat) running through it
  (`src/components/Logo.tsx`; also the browser-tab icon, `public/favicon.svg`). It takes the
  current colour, so it follows the theme. It is decoration only (hidden from assistive
  technology), shown beside the name in the sidebar and on the sign-in screen, and as a faint
  background: very large and faint on the sign-in screen, much fainter behind every signed-in
  page. The faint layers never catch the pointer and sit at an opacity where text over them keeps
  its contrast (axe-checked in all six looks).
- **Sign-in screen**: on wide screens, a side panel carries the story beside the form: the
  headline "Your database is talking. We translate.", a description, and three short points in
  the order a finding travels: **Rules find it** (deterministic rules over the workload and
  plans, each finding citing its evidence), **AI explains it** (an optional assistant that
  explains a finding and drafts a fix, sees only a redacted evidence bundle, must cite it, and
  has its answer discarded if it cites anything that wasn't sent) and **You decide**
  (read-only connections; a draft is never run). On narrow screens only the form shows, under
  the brand. Below the demo button, a note says the demo is read-only, needs no password and
  has sample AI answers. Colours follow the theme.
- **Credit line**: "Developed by Rishabh Pandey | https://zykosince94.github.io" (the address is
  a link that opens in a new tab) on the sign-in screen and at the foot of every page.
- **Background refresh indicator** (bottom right, signed-in screens): a small pill shows
  **Updating…** while data is refreshed (kept up for a moment so a fast refresh is still seen),
  **Live · updated 12 s ago** otherwise (the time keeps counting), **Couldn't update · retry**
  when a refresh failed, and **Offline · updates paused** without a connection. Clicking it
  refreshes the visible data now. Screen readers are told only about failures and being offline,
  never about every tick.

### 3.7 Data sources

**List** (`/sources`)
- A table of sources: name (links to the detail page), engine badge, `host:port/database`,
  server version, status badge (Healthy, Degraded, Failing; hover for an explanation) and when it
  was last checked. On narrow screens only name, engine and status are shown.
- States: "Loading data sources…", an empty state that explains what adding a source does, and an
  error message with **Try again**. Server errors are retried twice before the error shows.
- **Add source** is disabled for the demo user.

**Add and edit** (`/sources/new`, `/sources/:id/edit`)
- Fields: name, engine, TLS mode (with an explanation), host, port, database, username, password,
  description, and an "Enabled" checkbox. Switching the engine swaps the default port (5432 /
  27017) unless you changed it. For MongoDB the username hint says it can be blank.
- **Test connection** shows either a green "Connected to version X in N ms" with the capability
  checklist, or the server's error message (for example "PostgreSQL rejected the username or
  password for user 'monitor'").
- **Save** is enabled only after a successful test of the *current* connection settings. Changing
  host, port, database, username, password, TLS mode or engine afterwards disables it and says
  "Test again before saving". Changing only the name or description doesn't.
- When **editing**, if the connection settings are exactly as saved, Save needs no test ("Connection
  settings unchanged: no test needed"), so a source whose database is down can still be renamed,
  described or disabled.
- A source with missing capabilities can still be saved; the result says how many features are
  unavailable and that they can be fixed later.
- Editing: the password field starts empty. Leaving it blank keeps the saved password (the test
  then uses the stored one); typing a new one replaces it. If the engine, host, port or username
  changes, the saved password can't be reused: the field asks for it again, and Test and Save stay
  disabled until it's typed. A saved password is only ever sent to the server it was saved for.
- Errors from saving (for example "A source named 'Orders DB' exists") appear above the buttons.

**Detail** (`/sources/:id`)
- Name, engine and status badges, description, and a connection summary: host and port,
  database, username, whether a password is saved ("Saved (encrypted)"), TLS mode, server version
  and last check time.
- The **capability checklist**: a tick or cross per feature, the reason when unavailable, and a
  "How to fix" box with the exact command and a **Copy** button.
- A failing source shows its last error. The checklist then reflects the last successful check.
- Admin actions: **Re-check**, **Edit**, and **Delete** (asks for confirmation, then returns to the
  list). The demo user doesn't see them.
- A missing source shows "This data source doesn't exist."; a non-numeric id shows the not-found
  page.

### 3.8 Workload home and collection

**Home (`/`): source health strip**
- One card per source: name (links to the source), engine and status badges, an **Incomplete**
  badge if pg_stat_statements has been evicting statements (hover for the explanation), the last
  hour's **calls** and **execution time**, a **calls-per-minute sparkline** with a crosshair
  tooltip, and when it was last collected.
- MongoDB cards count **profiled operations** instead of calls. A MongoDB source whose profiler
  is off says so ("Profiling is off, so no operations are collected"); disabled sources say
  collection is off.
- "Collector: running / not running" next to the heading. If the collector isn't running, a
  notice says the charts won't update.
- Data refreshes every 30 seconds. The current, unfinished minute is left out of every trend, so
  charts don't show a false drop at the right edge.

**Home (`/`): calls vs mean time**
- Above the explorer's table (when it lists three operations or more), a scatter chart of the
  same operations: **calls** (x) against **mean time** (y), both on logarithmic axes, each dot
  sized by its **total time**. Dashed lines mark the medians, so the top right is "frequent and
  slow": the best things to fix. The three costliest dots are labelled.
- A sentence says it in words: "Of the 34 shown, 9 are both frequent and slow, and together they
  take 99% of the time shown". That share comes from the operations' times, not from adding their
  per-source shares, which would pass 100% across two sources.
- Hovering a dot shows its operation, calls, mean, total and share; clicking it opens the
  operation over the same time range. **Show data** lists the same operations as a table with
  links (built only when opened). The chart follows the explorer's filters and time range.
- Sub-millisecond times keep two significant figures ("0.053 ms"), since the log axis separates
  them.

**Home (`/`): workload explorer**
- One table of operations from both engines, ranked by **total time** (default), **mean
  time** or **calls** over the chosen time range: rank, the operation (a PostgreSQL statement,
  or a MongoDB query shape as compact JSON with every value shown as `?`; links to its detail
  page), an **engine badge**, its source (and the collection, for MongoDB), total time, its
  **share** of the source's total time (with a bar), calls, mean time and rows.
- Filters: **time range** (last 15 minutes, hour (default), 6 hours, 24 hours, 7 days),
  **rank by**, **source**, and **statement or shape contains** (applied on Enter or "Filter";
  matches text literally and ignoring case).
- **Profiling off (QP-AC-24):** for each enabled MongoDB source whose profiler isn't available
  (all of them, or the one filtered to), a notice above the list says its operations aren't
  collected, why ("Profiling is off on database 'shop'"), that Query Pulse never turns
  profiling on, and the exact fix with a Copy button. Filtered to such a source, the empty
  list says "Nothing to rank from this source until its profiler is on", not "no operations".
- Every filter and the time range live in the URL, so a copied link reopens the same view
  (QP-AC-16). Defaults are left out to keep links short; invalid values fall back to defaults.
- **Lock range** turns a relative range ("last hour") into fixed start and end times, so a
  shared link shows the same data later. A locked range shows as its dates in the time-range
  list. **Copy link** copies the page's URL.
- Shows the top 50, with "Top 50 of N operations", the window's total execution time and the
  range below the table. Relative ranges refresh every 30 seconds; locked ones don't change.
- Empty states say whether nothing was collected in the range or nothing matches the filters.
- On narrow screens the table scrolls sideways inside its card.

**Operation detail (`/operations/:id`)**
- The full statement text (as `pg_stat_statements` normalised it, with `$1` placeholders), its
  source, and when it was first and last seen. Statements that run inside a function say so.
  A MongoDB operation is titled **Query shape** and shows the shape as indented JSON, its
  command and collection ("find on shop.orders"), and that every value shows as `?` because
  Query Pulse stores the shape, not the values.
- A time-range selector (the explorer's range is carried over, including a locked one).
- Metrics over the range: **total time** (with its share of the source's total), **calls**,
  **mean time**, **rows** (and rows per call), and **cache hit ratio** (shared buffer hits ÷
  hits + reads, with the number of blocks read from disk). For MongoDB: **operations**
  (recorded by the profiler), **mean time** (the profiler times in whole milliseconds, so very
  fast shapes show 0 ms), **documents returned** (and per operation), and **examined per
  returned** (the larger of index keys and documents examined ÷ documents returned, with both
  counts).
- Two charts: **execution time** and **calls**, per minute for ranges up to 6 hours and per hour
  beyond that, each with a **Show data** table. The unfinished current minute is left out.
- **Query plan** (the PostgreSQL plan viewer): the generic plan's node tree, each node with its
  type, table (and alias), index, estimated **cost** (startup..total), **rows** and width, its
  share of the whole plan's cost, and its filter, index condition, join condition or sort key.
  Sequential scans are highlighted with "Reads the whole table". The raw EXPLAIN JSON is
  available under "Raw plan". Below the tree: when the plan was captured and last re-checked.
- When there's no plan, the viewer says why: not captured yet ("Capture plan now" for the
  admin), planning failed (with PostgreSQL's reason, e.g. the missing `SELECT` grant, and "Try
  again"), or the source can't produce plans (with the fix, e.g. "Upgrade to PostgreSQL 16 or
  later"). The admin can **Re-capture** a plan; the demo user sees no capture buttons.
- **MongoDB plan viewer:** the winning plan's stages from the root down to the scan, each with
  its stage name, index (name and key pattern), direction, filter, index bounds and sort
  pattern; values appear as `?`. Stages are flagged with "Reads the whole collection"
  (COLLSCAN), "Sorts in memory" (SORT) and "Scans the whole index" (an IXSCAN whose bounds
  cover every key). Below: aggregation stages that run after the query engine, how many other
  plans MongoDB considered, when it was captured, and the stored (redacted) explain JSON. There
  is no capture button: MongoDB plans come only from the collector, from a command it holds in
  memory. A missing plan says the collector explains busy shapes from profiled operations, and
  a failed one gives the reason (e.g. writes aren't explained).
- An unknown operation id says the operation doesn't exist; a non-numeric one is "Page not found".

**Source detail: Collection panel**
- "Last collected …" (or "Waiting for the first collection": the collector takes a baseline
  first and records activity from the next cycle).
- Any eviction warning, with what to do (raise `pg_stat_statements.max`), or for MongoDB a
  full-`system.profile` warning (make it larger).
- Two charts over the last 60 minutes, one measure each: **Calls per minute** (**Profiled
  operations per minute** for MongoDB, which counts only what the profiler records) and
  **Execution time per minute**, each with its hour total, a crosshair tooltip and a **Show
  data** table of every minute's value.

**Source detail: Server panel (MongoDB)**
- From the latest `serverStatus` sample: uptime, replica-set role, connections (and how many
  more are available), WiredTiger cache in use (of its maximum), and a table of operations per
  second since the previous sample next to the totals since the server started (query, insert,
  update, delete, getmore, command).
- Rates need two samples from the same server run, so right after the first sample (or a
  restart) they show as "–" with a note. Before any sample: "No sample yet" and that it needs
  `clusterMonitor`.

**Sources list:** the "Last collected" column (shown on wide screens) and the Incomplete badge.

Charts are drawn with ECharts, loaded only when a chart is first shown. Their colour is the
`--chart-1` token, checked against the card colour in light and dark mode.

### 3.9 Findings

**Findings page (`/findings`)**
- A segmented switch for **Open** (default), **Dismissed** and **Resolved**, each with its
  count, plus **Source** and **Rule** filters and, while one is set, **Clear filters**. All
  three live in the URL. While the first load runs, grey placeholder cards keep the page's
  shape; an empty list shows a short, friendly explanation.
- One card per finding, most confident and most recently seen first. The key numbers are tiles,
  and the sections (suggested next step, evidence, AI assistant, history) are spaced apart. The
  **title is a link** for findings about an operation: it opens the operation on the plan the
  finding cites (or over the window it was raised on when there is no plan); index and table
  findings have no page of their own, so their title is plain text. The
  **AI assistant** is a tinted panel with an icon, a one-line invitation ("Want it in plain
  words?") and an **AI assistant** button. A finding that already has an explanation or a
  draft (asked for earlier, or the demo's samples) shows the panel **open**; one without stays
  collapsed until opened, and opening or closing it yourself always wins. Deciding that is one
  cheap read per card; the model is only called by the admin's Explain or Draft button:
  - the rule id, the **confidence** (high, medium, low), and the evidence kind: **Plan
    evidence** (read from a captured plan) or **Statistics heuristic** (inferred from counters;
    check before acting);
  - the title, the target (the statement or shape, linking to its operation, or the index
    name), its engine and source, and a plain-language summary with the actual numbers;
  - **facts**: the values behind it (share of time, calls, table rows, selectivity, index size,
    how long it was watched), kept with the finding so it stays readable after retention;
  - **Suggested next step**, with SQL (PostgreSQL) or a mongosh command (MongoDB) to review and
    a Copy button, and the note that Query Pulse never runs it;
  - **Evidence** links: the observation window (opens the operation over that exact range), the
    **cited plan** (opens the operation showing that exact plan with the cited node outlined),
    and table or index samples (shown as text). A link whose data retention has deleted says
    "(no longer stored)";
  - first and last seen, and for the admin **Mark resolved** / **Dismiss** (open findings) or
    **Reopen** (dismissed and resolved ones). A finding resolved or reopened by the rules says
    "automatically". The demo user sees no buttons.
- Empty states say whether there are no open findings yet (and that rules run after each
  collection) or nothing matches the filters.

**AI assistant on a finding** (collapsed until opened, so a list of findings loads nothing):
- An advisory note: the rules raised the finding; AI only explains it and drafts a fix from the
  evidence, citing it claim by claim.
- With AI off: "AI is off" (the admin gets a link to Settings).
- With AI on, the admin gets **Explain this finding** and **Draft a fix**. Everyone sees the
  latest explanation and draft already given (provider, model, when), each claim with the ids
  of the evidence it cites, claims without evidence flagged "Not backed by evidence", and a
  note when the evidence has changed since. A draft's SQL or mongosh command has a Copy
  button and says Query Pulse never runs it.
- A rejected answer (cited evidence that wasn't sent, or a destructive draft) shows as an
  error with the reason, never as advice. Provider errors (rate limit, quota, refused key) show
  their message.
- **View what is sent** shows the exact evidence bundle (how many items; literals as `?`; no
  credentials, hosts or connection details), for everyone, even with AI off.

**On the operation page:** the operation's findings (any status) appear above the plan. Opened
from a finding's plan link, the plan viewer shows "Showing plan #N, the one a finding cites"
(with when it was captured), outlines the cited node with "Cited by the finding", hides
re-capture, and offers **Show the latest plan**.

### 3.10 Settings (`/settings`)

- **AI assistant**: On/Off badge; provider (OpenAI); model (free text: the id from the
  provider's docs); API key (a password field; when a key is saved it shows only its last four
  characters, and leaving the field empty keeps it); "Turn the AI assistant on". **Save**
  checks the key and model with the provider when turning on ("Checking the key…") and saves
  nothing if they're refused, showing why. **Delete the key** turns AI off. Changing the
  provider asks for the key again. Below: how many provider requests this server has made
  since it started.
- **Collection and retention**: the collection interval, how often the rules run and how far
  back they look, and how long data is kept, read-only, with a note that they come from the
  backend's environment.
- The demo user sees both cards read-only.

---

## 4. Backend: HTTP API

Base URL in development: `http://localhost:8000`. Interactive documentation: `/docs`. The
machine-readable contract is `openapi.json`, committed in the backend repo.

**Conventions**

- Request and response bodies are JSON.
- Errors use FastAPI's shape: `{"detail": "message"}`. Invalid input returns **422** with a list of
  field errors in `detail`.
- Authentication is a signed session cookie named `qp_session` (§5).
- Every API endpoint requires a session unless it is marked **Public** below.

### 4.1 Operations

| Method and path | Access | Success | Errors |
|---|---|---|---|
| `GET /health` | Public | `200 {"status": "ok"}` | — |
| `GET /ready` | Public | `200 {"status": "ready", "database": "ok"}` | `503 {"status": "not_ready", "database": "unreachable"}` |

`/health` only says the process is up (liveness). `/ready` also runs `SELECT 1` against the app
database (readiness) and is what Docker's healthcheck uses.

### 4.2 Authentication

| Method and path | Access | Request | Success | Errors |
|---|---|---|---|---|
| `GET /api/auth/config` | Public | — | `200 {"demo_enabled": true}` | — |
| `POST /api/auth/login` | Public | `{"username": "…", "password": "…"}` | `200` user, sets cookie | `401` invalid credentials, `422` empty or too-long fields, `429` too many failed sign-ins (§5) |
| `POST /api/auth/demo` | Public | — | `200` demo user, sets cookie | `404 {"detail": "The demo is not enabled"}` |
| `POST /api/auth/logout` | Public | — | `204`, clears the session | — |
| `GET /api/auth/me` | Session | — | `200` user | `401 {"detail": "Not signed in"}` |

The user object returned by login, demo and me:

```json
{ "username": "admin", "role": "admin", "read_only": false }
```

Field limits: `username` 1–64 characters, `password` 1–256 characters.

Every state-changing request, these included, is refused with `403` when its `Origin` names
another site (§5).

Logout is public on purpose: the demo user must be able to sign out, and signing out without a
session is harmless.

### 4.3 Data sources

Sources are stored in the app database. Passwords are encrypted with Fernet before they're
stored and are decrypted only to connect. A source is saved only after a successful
connection test.

| Method and path | Access | Success | Errors |
|---|---|---|---|
| `GET /api/sources` | Session | `200` list of sources, sorted by name | `401` |
| `GET /api/sources/{source_id}` | Session | `200` source with its capability checklist | `401`, `404` |
| `POST /api/sources/test` | Admin | `200` connection test result (never saves) | `401`, `403`, `404` unknown `source_id`, `422` |
| `POST /api/sources` | Admin | `201` source with checklist | `400` connection failed (nothing saved), `401`, `403`, `409` name in use, `422` |
| `PUT /api/sources/{source_id}` | Admin | `200` source with checklist. If only `name`, `description` or `enabled` changed (and no password is sent), it saves without a connection test. | `400` connection failed or password must be re-entered (nothing changed), `401`, `403`, `404`, `409`, `422` |
| `POST /api/sources/{source_id}/check` | Admin | `200` source, re-checked with its stored settings | `401`, `403`, `404` |
| `DELETE /api/sources/{source_id}` | Admin | `204` | `401`, `403`, `404` |
| `GET /api/collector/status` | Session | See §4.4 | `401` |

**Fields** (create and update):

| Field | Type | Rules |
|---|---|---|
| `name` | string | 2–120 characters, unique (ignoring case) |
| `engine` | `"postgres"` or `"mongodb"` | required |
| `host` | string | hostname or IP only: no scheme, path or spaces |
| `port` | integer | 1–65535 |
| `database` | string | 1–120 characters |
| `username` | string | required for PostgreSQL; optional for MongoDB without auth |
| `password` | string | create: optional, default empty. Update: omit or `null` keeps the stored password, `""` removes it. Keeping it is refused (400, "Enter the password again…") if the engine, host, port or username changed. |
| `tls_mode` | `"disable"`, `"require"` or `"verify-full"` | default `disable`. `require` encrypts without checking the certificate; `verify-full` also checks the certificate chain and hostname against the operating system's trusted CAs (for PostgreSQL via `sslrootcert=system`). |
| `enabled` | boolean | default `true` (used by the collector from Phase 3) |
| `description` | string | up to 500 characters |

**A source in a response** has every field above except `password`, plus:

| Field | Meaning |
|---|---|
| `id` | Number used in URLs |
| `has_password` | Whether a password is stored. The password itself is never returned. |
| `server_version` | Version reported by the server at the last successful check |
| `status` | `healthy`: connected and every capability available. `degraded`: connected, some capabilities missing. `failing`: the last check couldn't connect. |
| `last_checked_at`, `last_error` | When the last check ran; the error message if it failed |
| `created_at`, `updated_at` | Timestamps |
| `last_collected_at` | When the collector last collected successfully (null before the first success) |
| `warnings` | Anything that makes results incomplete in the last hour: pg_stat_statements evicting statements, or MongoDB's `system.profile` overwriting operations before they were read |
| `capability_gaps` | The unavailable capabilities (with reason and fix), so lists can explain a gap without loading each source |
| `capabilities` | Detail responses only: the checklist below |

**Connection test.** Send the connection fields (`engine`, `host`, `port`, `database`,
`username`, `tls_mode`) and a `password`. When editing an existing source, send `source_id` and
leave out `password` to test with the stored one; this is refused with 400 if the engine, host,
port or username differ from the saved source (the stored password is only sent to its own
server). A failed connection is a normal result, not an HTTP error:

```json
{ "ok": false, "error": { "code": "auth_failed", "message": "PostgreSQL rejected the username or password for user 'monitor'." },
  "server_version": null, "latency_ms": null, "capabilities": [] }
```

Error codes: `unreachable` (connection refused or DNS failure), `timeout` (no answer within
`QP_SOURCE_CONNECT_TIMEOUT_SECONDS`, default 5), `auth_failed`, `tls`, `database_not_found`,
`permission_denied`, `unknown`. Every attempt gives up within the timeout plus a small margin.

**Capability checklist.** Each entry has `feature`, `label`, `available`, and, when unavailable,
a `reason` and the exact `fix`:

| Engine | Feature | Unavailable when | Fix shown |
|---|---|---|---|
| PostgreSQL | `pg_stat_statements` | Not in `shared_preload_libraries` | Add it to `shared_preload_libraries` and restart (then `CREATE EXTENSION` if needed) |
| | | Loaded but not created in the database | `CREATE EXTENSION pg_stat_statements;` in that database |
| | | Not created, and the user can't see whether it's loaded | Both steps |
| PostgreSQL | `read_all_stats` | User lacks `pg_monitor` (can only see its own queries) | `GRANT pg_monitor TO "<user>";` |
| PostgreSQL | `generic_plans` | Server older than 16 | Upgrade to PostgreSQL 16+ |
| MongoDB | `server_status` | User lacks `clusterMonitor` | `db.getSiblingDB("admin").grantRolesToUser(…clusterMonitor…)` |
| MongoDB | `explain` | User can't read the database | Grant `read` on the database |
| MongoDB | `profiler` | Profiling is off on the database | `db.setProfilingLevel(1, { slowms: 100 })`. Query Pulse never turns profiling on itself. |
| Both | `supported_version` | Shown only for PostgreSQL < 16 or MongoDB < 7.0 | Upgrade |

**How connections behave.** PostgreSQL sessions are read-only (`default_transaction_read_only`),
use a 5-second `statement_timeout`, and identify themselves as `application_name=query-pulse`.
MongoDB connections go directly to the given node (`directConnection`), authenticate against
`admin` when a username is set, and only read the profiling level, never change it.

**Names** are unique ignoring case ("Orders" and "orders" clash), enforced by a unique index on
`lower(name)` in the database, so two simultaneous requests can't both succeed.

**Accepted risk: network probing by the admin.** Test connection connects wherever the admin
points it, so different error codes reveal whether a host and port answer. That's inherent to the
feature and limited to the admin. If Query Pulse is ever hosted for others, add an allowlist of
reachable networks.

### 4.4 Workload and collector

| Method and path | Access | Success | Errors |
|---|---|---|---|
| `GET /api/workload/health?minutes=60` | Session | `200` one entry per source: `source` (as above), `calls`, `total_exec_time_ms`, `points` | `401`, `422` |
| `GET /api/sources/{source_id}/trend?minutes=60` | Session | `200 {"source_id", "minutes", "bucket_seconds": 60, "points": [...]}` | `401`, `404`, `422` |
| `GET /api/collector/status` | Session | `200 {"status": "running" \| "not_running", "last_beat_at", "source_count", "message"}` | `401` |
| `GET /api/sources/{source_id}/server` | Session | `200` a MongoDB source's latest `serverStatus` sample, or `null` before the first | `401`, `404` |

- `minutes` is 5–1440 (default 60). A point is `{"start", "calls", "total_exec_time_ms"}` for one
  minute; every minute in the window has a point (zeros included), and the current unfinished
  minute is excluded.
- The collector status is `running` when its heartbeat is less than three refresh periods old
  (90 seconds by default).
- A server sample: `captured_at`, `uptime_seconds`, `connections_current`,
  `connections_available`, `cache_bytes`, `cache_max_bytes`, `repl_state` (`primary`,
  `secondary`, `other` or null), `opcounters` (totals since the server started) and
  `rates_per_second` with `rate_seconds` (from the previous sample; null when there's none or
  the server restarted in between, which a lower uptime shows).

### 4.5 Workload explorer and plans

| Method and path | Access | Success | Errors |
|---|---|---|---|
| `GET /api/operations` | Session | `200 {"start", "end", "sort", "total_count", "total_exec_time_ms", "operations": [...]}` | `401`, `422` |
| `GET /api/operations/{operation_id}` | Session | `200 {"operation", "source", "toplevel", "start", "end", "bucket_seconds", "history": [...]}` | `401`, `404`, `422` |
| `GET /api/operations/{operation_id}/plan` | Session | `200` the latest plan, or why there isn't one (below) | `401`, `404` |
| `POST /api/operations/{operation_id}/plan` | Admin | `200` plans the statement now and returns it. For a MongoDB operation it returns the stored plan unchanged: only the collector holds the commands needed to explain one | `401`, `403`, `404`, `502` (the source can't be reached) |

**Time window** (list and detail): either `minutes` (5 to 44,640; default 60: the last N
minutes) or `start` with an optional `end` (ISO 8601; UTC when no zone is given; `end` defaults
to now). Giving both forms, an `end` without `start`, a window shorter than a minute or longer
than 31 days returns 422. An observation belongs to the window when its interval ended inside it.

**List parameters:** `sort` = `total_time` (default) | `mean_time` | `calls`; `source_id`; `q`
(up to 200 characters, matched literally and ignoring case, so `%` and `_` aren't wildcards);
`limit` 1–200 (default 50). Ties are ordered by operation id, so the order is stable.

**An operation** (`OperationSummary`): `id` (the fingerprint), `source_id`, `source_name`,
`engine`, `query`, the window's `calls`, `total_exec_time_ms`, `mean_exec_time_ms` (total ÷
calls over the window, not an average of per-interval means), `rows`, `shared_blks_hit`,
`shared_blks_read`, `share_of_total_time` (0–1, of its own source's total in the window),
`first_seen_at`, `last_seen_at`. MongoDB operations also have `namespace` (`shop.orders`),
`command` (`find`, `aggregate`, ...), `keys_examined` and `docs_examined` (null for
PostgreSQL); their `query` is the shape as compact JSON, `calls` counts profiled operations,
`rows` is documents returned, and the buffer counters are 0. `total_count` is how many operations matched before `limit`;
`total_exec_time_ms` is the window's total across the filtered sources (ignoring `q`).

**History:** one point per minute for windows up to 6 hours (`bucket_seconds` 60) and per hour
beyond (`3600`), bucketed with `date_trunc(..., 'UTC')` in SQL. Every bucket has a point (zeros
included; `mean_exec_time_ms` is null when there were no calls). Minute histories stop at the
last complete minute; hour histories include the current hour.

**Plan responses** (`OperationPlan`) have an `engine` (`postgres` or `mongodb`) and a
`status`:

| `status` | Meaning | Fields |
|---|---|---|
| `captured` | The latest plan | `plan_id`, `captured_at`, `checked_at`, `raw` (the stored JSON), and `root` (PostgreSQL's node tree) or `stage_root`, `rejected_plans` and `pipeline_stages` (MongoDB) |
| `failed` | EXPLAIN was tried and gave no plan | `plan_id`, `captured_at`, `checked_at`, `reason` |
| `not_captured` | Plans work here but none was captured yet | `reason` |
| `unavailable` | The source can't produce plans (older than PostgreSQL 16, or a MongoDB user without read) | `reason`, `fix` |

A plan node has `path` (its position, e.g. `0.1.0`, stable for a given plan), `node_type`,
`relation_name`, `schema_name`, `alias`, `index_name`, `startup_cost`, `total_cost`,
`plan_rows`, `plan_width`, `filter`, `index_cond`, `recheck_cond`, `join_cond`, `sort_key`,
`join_type`, `strategy`, `parent_relationship`, `details` (every other value PostgreSQL
reported, as text) and `children`.

A MongoDB stage (`MongoPlanStage`) has `path`, `stage`, `index_name`, `key_pattern`,
`sort_pattern`, `direction`, `filter` (JSON, values as `?`), `index_bounds` (per field; full
ranges kept, every other bound `?`), `full_index_scan` (every bounded field includes a full
range), `is_multi_key`, `details` and `children`.

`GET /api/plans/{plan_id}` (session; `404` if unknown) returns one stored plan by id in the same
shape: the exact plan a finding cites, even after a newer plan was captured.

### 4.6 Findings

| Method and path | Access | Success | Errors |
|---|---|---|---|
| `GET /api/findings` | Session | `200 {"counts": {"open", "resolved", "dismissed"}, "findings": [...]}` | `401`, `422` |
| `GET /api/findings/{finding_id}` | Session | `200` one finding | `401`, `404` |
| `PATCH /api/findings/{finding_id}` | Admin | `200` the finding, body `{"status": "open" \| "dismissed" \| "resolved"}` | `401`, `403`, `404`, `422` |
| `GET /api/rules` | Session | `200` the rule catalogue: `id`, `title`, `description`, `evidence_strength`, `confidence`, `thresholds` | `401` |

**List filters:** `status`, `source_id`, `rule_id`, `operation_id` (findings about that
operation). `counts` covers every status under the other filters. Order: confidence (high
first), then most recently seen.

**A finding:** `id`, `source_id`, `source_name`, `engine`, `rule_id`, `rule_title`, `target_kind`
(`operation` or `index`), `target_key`, `fingerprint_id`, `relation`, `query` (for operations),
`title`, `summary`, `confidence`, `evidence_strength` (`plan` or `statistics`),
`suggested_action`, `action_sql` (to review and run by hand: SQL for PostgreSQL, a mongosh
command for MongoDB; never executed), `facts` (`key`,
`label`, `value`), `evidence`, `status`, `status_changed_by` (`rules` or `user`),
`status_changed_at`, `first_seen_at`, `last_seen_at`, `resolved_at`.

**Evidence items:** `kind` = `operation` (observations of `fingerprint_id` between `start` and
`end`), `plan` (`plan_id` and the cited `node_path`) or `relation_stat` (`relation_stat_id`),
each with a `label` and `available` (false once retention deleted what it points at).

**Status changes** by the admin are recorded as `user`. Dismissed findings stay dismissed
however often the rule fires again; resolved ones reopen if the problem shows up in evidence
collected after the resolution (§6).

### 4.7 AI assistant and settings

| Method and path | Access | Success | Errors |
|---|---|---|---|
| `GET /api/ai/settings` | Session | `200 {"enabled", "provider", "model", "key_set", "key_hint", "providers", "provider_calls", "updated_at"}` | `401` |
| `PUT /api/ai/settings` | Admin | `200` the settings. Body `{"provider", "model", "api_key"?, "enabled"}`; omit `api_key` to keep the saved one | `400` (unknown provider; no key to turn on with; the provider refused the key or model; the provider changed without re-entering the key), `401`, `403`, `422` |
| `DELETE /api/ai/settings/key` | Admin | `200` the settings, key removed and AI off | `401`, `403` |
| `GET /api/settings` | Session | `200 {"retention_days", "collector_interval_seconds", "rules_interval_seconds", "rules_window_minutes"}` | `401` |
| `GET /api/findings/{finding_id}/ai/bundle` | Session | `200 {"finding_id", "evidence_hash", "items", "document"}`: exactly what would be sent | `401`, `404`, `422` |
| `GET /api/findings/{finding_id}/ai` | Session | `200 {"enabled", "explain", "draft"}`: the latest answers (or null) | `401`, `404`, `422` |
| `POST /api/findings/{finding_id}/ai/{kind}` | Admin | `200` an answer; `kind` is `explain` or `draft` | `401`, `403`, `404`, `409` (AI is off), `422`, `502` (the provider couldn't answer; the detail says why) |

- `key_hint` is `…` plus the key's last four characters; the key itself is never returned.
  `provider_calls` counts every provider request this API process has made.
- **An answer**: `finding_id`, `kind`, `status` (`ok` or `rejected`), `error` (why it was
  rejected), `summary` and `claims` (each `{"text", "evidence": [ids], "cited"}`), `caveats`,
  `language` and `code` (drafts: `sql` or `mongosh`), `provider`, `model`, `evidence_hash`,
  `current` (false once the finding's evidence has changed materially), `cached`, `created_at`.
- **Checks on every answer:** the JSON must match the schema; any cited id not in the bundle
  rejects the answer (QP-AC-27); claims with no citation are kept but `cited: false`; a draft
  containing DROP TABLE/DATABASE/SCHEMA/ROLE, TRUNCATE, DELETE, UPDATE, INSERT, ALTER
  SYSTEM/ROLE, GRANT/REVOKE, or MongoDB `drop`, `dropDatabase`, data-changing calls or
  setting changes is rejected. `DROP INDEX` is allowed (it's the unused-index fix).
- **Caching:** `ok` answers are stored by (evidence hash, kind, provider, model) and reused
  without a request. The hash covers a stable view of the bundle (numbers to two significant
  figures, no sample timestamps), so per-minute drift doesn't make answers stale. Rejected
  answers aren't stored.
- **The evidence bundle** (`bundle_version` 1): items `rule`, `finding` (title, confidence,
  evidence kind), `source` (engine and version only), `operation` (the statement or shape and
  its metrics over the finding's window), `fact:<key>` per fact, `plan:<path>` per plan node or
  stage, and `table:<schema.table>` / `index:<schema.index>` samples. SQL literals become `?`
  (`'...'` → `'?'`); the finding's free-text summary isn't included.
- **OpenAI** is called with Chat Completions and a strict JSON schema; the key is checked by
  retrieving the model. Errors become messages (refused key, unknown model, no quota, rate
  limit, server error, timeout) that never include the key or OpenAI's own error text.
- **Sample answers in the demo.** A fresh demo stack has AI off, so the demo stores answers
  written in advance for the seven planted problems (`query_pulse/demo/ai_samples.py`, run by
  the `demo-workload` service every 15 s). They are stored like real answers with the provider
  `sample`, and only after passing the same checks (every citation must be in the finding's
  bundle; no destructive statements). Claims cite fact ids, never their drifting values.
  There is one sample row per finding and kind: when the evidence hash changes (live counts
  cross the hash's two-significant-figure rounding every few minutes), the sample is re-pointed
  at the new evidence rather than duplicated. A real provider's answer is never touched. The
  panel badges them **Sample answer** ("Written in advance for the demo, not generated live"),
  doesn't call them out of date, and with AI off says they are samples and live answers need
  AI on. Deployments without the demo service never get them.
- **Gateways.** With `QP_OPENAI_BASE_URL` pointing at an OpenAI-compatible gateway (for example
  `https://openrouter.ai/api/v1`), the provider is still "OpenAI" in Settings, and model ids may
  look like `author/model:tag`. The key is checked at `GET /key` (where the gateway has it) and
  the model against `GET /models`, because gateways can't all retrieve one model by id. Messages
  name the gateway's host. A model that ignores the strict schema may wrap its JSON in a
  ```` ```json ```` fence (read as plain JSON) or answer in the wrong shape (rejected, never
  stored); prefer a model whose provider lists `structured_outputs`.
- **Model ids** are checked before they reach a request path: letters, digits, `.`, `_`, `:`,
  `-`, and `/` between parts that each start with a letter or digit. The id is URL-encoded when
  OpenAI itself is asked to retrieve it.

---

## 5. Sessions and security behaviour

| Property | Behaviour |
|---|---|
| Session storage | A cookie, `qp_session`, holding only the user id. It is **signed** (tamper-proof) but **not encrypted** (the browser can read it). |
| Cookie flags | `HttpOnly` (JavaScript can't read it), `SameSite=Lax` (not sent on cross-site POSTs), `Secure` when `QP_SESSION_HTTPS_ONLY=true` |
| Lifetime | 12 hours by default (`QP_SESSION_MAX_AGE_SECONDS`), checked by the server |
| Sign-in | Starts a fresh session every time, so a session set before sign-in can't be reused (session fixation) |
| Each request | Loads the user from the database, so a deleted user is signed out immediately |
| Passwords | Hashed with argon2id, never stored or logged in plain text. Hashes are upgraded on sign-in if the hashing parameters change. |
| Username probing | Wrong username and wrong password take the same time and return the same response |
| Credentials in responses | Never returned. Source responses only say `has_password`. A test checks that a marker password never appears in any response body or log line (QP-AC-08). |
| Credentials at rest | Encrypted with Fernet (AES-128-CBC + HMAC-SHA256) using `QP_FERNET_KEY`, decrypted only in the service layer right before an adapter connects. Several comma-separated keys allow rotation: the first encrypts, all decrypt. |
| Monitored databases | Connections are read-only and time out (§4.3). Log lines carry the source name and error code, never credentials. |
| Cross-site requests | Besides `SameSite=Lax`, every state-changing request (POST, PUT, PATCH, DELETE) with an `Origin` header that isn't this site gets **403** before any route runs. Same site means the Origin's host and port match the request's `Host` (true behind Vite's proxy and Caddy). `QP_ALLOWED_ORIGINS` adds others. Requests without an Origin (curl, scripts) aren't browser cross-site requests and pass. |
| Sign-in rate limit | After `QP_LOGIN_MAX_FAILURES` (10) failed sign-ins from one client address within `QP_LOGIN_WINDOW_SECONDS` (300), sign-in returns **429** with `Retry-After` until the oldest failure ages out. The password isn't checked while locked out. Successful sign-ins never count. In memory (one API instance). Behind a proxy, every client shares the proxy's address unless uvicorn trusts its `X-Forwarded-For`. |
| Logs | One JSON object per line (`QP_LOG_FORMAT=text` for development): `time`, `level`, `logger`, `message`, `request_id` and any structured fields. Each request gets an ID (the caller's `X-Request-ID` if it's 1–64 safe characters, otherwise a new one), returned in the `X-Request-ID` response header and on every log line written while handling it. One access line per request: method, path (never the query string), status, duration. Health checks aren't logged. |
| Planning collected statements | Statement text comes from `pg_stat_statements`, which any user of the monitored database can influence. EXPLAIN without ANALYZE never executes it and the session is read-only, but a second statement after a `;` would run, so text containing a `;` (other than a trailing one) is never sent. Only SELECT, INSERT, UPDATE, DELETE, MERGE, VALUES, TABLE and WITH are planned, and cut-off text (10,000+ characters) isn't. |
| MongoDB query values | Never stored or logged (ADR 0010). Profiled commands become shapes (values `?`); the newest command of each read shape is held only in collector memory, per source and tied to the server it came from, for `explain`; explain output is redacted before storing; explain and collection errors keep only MongoDB's error code. PyMongo's debug loggers, which print whole commands and replies, are held at WARNING whatever `QP_LOG_LEVEL` says. An integration test checks that a marker value used in profiled queries appears nowhere in the app database or logs. |
| AI | Off by default; no provider request until an admin saves a key and turns it on (QP-AC-26 checks the counter stays at 0 with AI off). The key is Fernet-encrypted, shown only by its last four characters, only sent to the provider it was saved for, never returned or logged (a marker-key test checks responses and logs). Evidence bundles never hold literals, credentials or connection details (QP-AC-28). Only the admin can ask (it costs money). Nothing an answer contains is ever executed. The fake provider is refused in production. |
| Request ids | Path ids are bounded to the database's `integer` (1 to 2,147,483,647): a larger id is a 422, not a server error (found by fuzzing). Every 422 has FastAPI's validation shape (`detail` is a list). |
| Secrets in memory | Held as `SecretStr`, shown as `**********` in logs and error output |
| Revoking sessions | Not possible per session. Changing `QP_SESSION_SECRET` signs everyone out. |

---

## 6. The collector

`python -m query_pulse.collector` (the `collector` service in compose). One instance only.

**What it does for each enabled PostgreSQL source**, every `QP_COLLECTOR_INTERVAL_SECONDS`
(60 by default, 15 in the local demo, 2 in tests; MongoDB sources below):

1. Connects read-only (same connection rules as the connection test) and reads
   `pg_stat_statements` for the source's database, plus `pg_stat_statements_info` (`stats_reset`,
   `dealloc`). Its own queries carry a `/* query-pulse */` comment and are excluded.
2. Compares with the previous snapshot and stores the **difference per statement** as one
   observation per active statement: calls, total and mean execution time, rows, shared buffer
   hits and reads. Statements are identified by PostgreSQL's own
   `(dbid, userid, queryid, toplevel)`.
3. Saves the new snapshot as the baseline for the next cycle, in the app database, so a restart
   continues correctly.

**Rules that keep the numbers honest**

| Situation | What happens |
|---|---|
| First cycle for a source | Baseline only; nothing is recorded yet |
| `pg_stat_statements_reset()` (detected by `stats_reset` changing, or any counter going down) | That interval is discarded, so no negative or inflated numbers; the current snapshot becomes the new baseline |
| A statement that wasn't in the previous snapshot | All its counters belong to this interval |
| Evictions (`dealloc` rose) | The source shows a warning for an hour and becomes **degraded**: some activity wasn't recorded; raise `pg_stat_statements.max` |
| Collector restart | Continues from the stored baseline |
| A source's database, host, port, engine or username changes, or it's re-enabled | The baseline is cleared, so the next cycle starts fresh instead of counting a lifetime as one interval |

**What it does for each enabled MongoDB source**, on the same schedule:

1. Connects directly to the node (read-only reads only) and reads the server's clock, a
   `serverStatus` sample (if the user has `clusterMonitor`) and the database's profiling
   level. If profiling was switched on or off since the last check, capabilities are
   re-checked at once, so the "profiling is off" state is current.
2. Reads `system.profile` for the source's database: entries after the stored **cursor** (a
   server timestamp) up to one second before the server's "now" (an operation that just
   finished may not be written yet), oldest first, at most `QP_COLLECTOR_PROFILE_MAX_ENTRIES`
   (10,000; when more are waiting, the next cycle continues after the last one read). Its own
   operations are excluded by their `appName` (`query-pulse`), like PostgreSQL's comment marker.
3. Normalises each entry into a **query shape** and sums per shape: profiled operations, total
   milliseconds, documents returned, documents and index keys examined. One observation per
   shape for the interval since the previous cycle; the shape's sha256 is the fingerprint.
   getMore, administrative commands and `system.*` collections are skipped.
4. Keeps the newest command of each read shape in memory (`QP_COLLECTOR_SAMPLE_CACHE_SIZE`,
   500 shapes per source), for explain. Never written or logged; forgotten when the source is
   disabled, removed or pointed at another server.

| Situation | What happens |
|---|---|
| First cycle for a source | Sets the cursor; nothing is recorded yet (a backlog has no interval) |
| Profiling off | Nothing new in `system.profile`; the source is **degraded** with the profiler fix, and `serverStatus` is still sampled |
| `system.profile` overwrote entries before they were read (it's capped) | The source shows a warning for an hour and is **degraded**: make `system.profile` larger |
| The user can't read the database | Profiler reading is skipped; the checklist already shows the grant |
| Collector restart | Continues from the stored cursor; the in-memory commands start empty, so a shape's plan is captured again once it's profiled again |
| Host, port, database, engine or username changes, or the source is re-enabled | The cursor is cleared along with the PostgreSQL baseline |

**Shapes** (`workload/mongo_shape.py`, pinned by golden files): every value becomes `?`, field
names and operators stay. Lists of values (`$in: [1, 2, 3]`) are one `?`, so list length
doesn't split a shape. Inside expressions (`$expr`, `$group`, `$project`, ...), `"$field"`
paths are kept; in plain filters a `$`-string is a value. Sort patterns, hints and index or
collection names are kept as structure; `limit` and `skip` become `?`; session metadata
(`lsid`, `$clusterTime`, `$db`, ...) is dropped. Each aggregation stage is normalised in its
own context (`$match` as a filter, `$sort` literally, `$lookup`'s sub-pipeline as a pipeline).
Key order is kept.

**Plans:** after storing a cycle's observations, the collector plans the source's busiest
operations of the last hour by total time (`QP_COLLECTOR_PLAN_TOP_N`, 20) with
`EXPLAIN (GENERIC_PLAN, FORMAT JSON)`: all of them every `QP_COLLECTOR_PLAN_SECONDS` (300), and in
between only those without a plan yet, so a new hot statement gets its plan within one cycle.
Only for sources whose "Query plans" capability is available (PostgreSQL 16+). A plan identical
to the latest one just has its "last checked" time moved; a changed plan is a new row (plans
are evidence that Phase 5's findings will cite). Statements that can't be planned get a row with
the reason. Planning gets at most a third of the cycle time limit; what doesn't fit waits for
the next cycle, and a planning failure never fails collection.

For MongoDB, the same schedule and budget apply to the busiest shapes that have a command in
memory: `explain` at queryPlanner verbosity (nothing executes, 5-second `maxTimeMS`), rebuilt
from the command's query fields only (no session metadata replayed). Only reads are explained
(find, aggregate, count, distinct; not pipelines ending in `$out`/`$merge`); a write gets a
plan row saying why. The output is **redacted** before storing: filters normalised, index
bounds kept only when they're full ranges, limit amounts and the SBE plan text dropped.

**Findings:** after a successful cycle, about every `QP_RULES_INTERVAL_SECONDS` (60; every
cycle in the demo), the rules run for the source over the last `QP_RULES_WINDOW_MINUTES` (60).
They run in their own session after the cycle commits, so a rule failure never affects
collection. The rules:

| Rule | Planted problem | Fires when | Evidence | Confidence |
|---|---|---|---|---|
| `PG-SEQSCAN-HOT` | P1 | An operation with ≥ 10% of the source's execution time and ≥ 10 calls whose latest captured plan has a sequential scan with a filter (not a LIKE) on a table of ≥ 10,000 rows, keeping ≤ 5% of it | Plan | High |
| `PG-SORT-NO-INDEX` | P2 | ≥ 10 calls, and the plan has a Limit above a Sort above a full scan of a table of ≥ 10,000 rows | Plan | High |
| `PG-LEADING-WILDCARD` | P3 | ≥ 10 calls, and the plan answers a LIKE/ILIKE with a scan of a table of ≥ 1,000 rows. Medium confidence when the pattern is a parameter (its value isn't recorded), high when the plan shows a literal starting with `%`; anchored literals (`'abc%'`) don't fire | Plan | Medium / high |
| `PG-UNUSED-INDEX` | P4 | A non-unique index of ≥ 1 MB whose every sample in the last `QP_RULES_UNUSED_INDEX_WINDOW_HOURS` (24) shows zero scans, over at least `QP_RULES_UNUSED_INDEX_MIN_SPAN_SECONDS` (1 hour; 2 minutes in the demo). UNIQUE and PRIMARY KEY indexes never fire (they enforce constraints) | Statistics | Medium |
| `MDB-COLLSCAN` | M1 | A MongoDB shape with ≥ 10 profiled operations whose winning plan has a COLLSCAN with a filter, examining ≥ 100 documents per document returned and ≥ 1,000 per operation | Plan | High |
| `MDB-BLOCKING-SORT` | M2 | ≥ 10 operations, and the winning plan has an in-memory SORT whose input isn't a filtered COLLSCAN | Plan | High |
| `MDB-EXAMINED-RATIO` | M3 | ≥ 10 operations using an index (an IXSCAN, no COLLSCAN or SORT in the plan) that examine ≥ 50 keys or documents per document returned and ≥ 1,000 per operation. High confidence when the shape has a `$regex` and the index bounds cover the whole index (the unanchored-regex case), medium otherwise | Statistics | Medium / high |

The rules are disjoint, so one problem gives one finding; each engine's rules read only their
own engine's plans. Their suggested fix is `CREATE INDEX CONCURRENTLY …` for P1 and P2,
`pg_trgm` and a GIN index for P3, `DROP INDEX CONCURRENTLY …` for P4, a mongosh
`createIndex` on the filtered fields for M1 and in the sort order for M2, and anchoring the
regex (or a text/search index) for M3, each with its trade-offs.

**Finding lifecycle:** one row per (source, rule, target), enforced by a unique constraint.

| Situation | What happens |
|---|---|
| Detected, no finding yet | Created, open |
| Detected, open | Evidence, facts and last seen refreshed |
| Detected, dismissed | Refreshed, **stays dismissed** |
| Detected, resolved | Reopened, but only if the evidence was collected after the resolution (so a just-added index isn't contradicted by the plan on record) |
| Not detected, open | Resolved automatically once not seen for `QP_FINDING_RESOLVE_AFTER_SECONDS` (3600) |

**Other jobs:** table and index statistics (`pg_stat_user_tables`, `pg_stat_user_indexes`:
scans, live/dead rows, sizes, and whether an index is UNIQUE or a PRIMARY KEY) every `QP_COLLECTOR_RELATION_STATS_SECONDS` (300); capability
re-checks every `QP_COLLECTOR_CAPABILITY_CHECK_SECONDS` (900), so enabling an extension later
turns its feature on; and once a day it deletes observations, table samples and plans (not
re-checked within the period) and MongoDB server samples older than `QP_RETENTION_DAYS` (14).

**Failures:** each cycle has a time limit (`QP_COLLECTOR_CYCLE_TIMEOUT_SECONDS`, 30). A failed
cycle marks the source **failing** with the error, and the next attempt waits longer each time
(interval, 2×, 4×, … up to `QP_COLLECTOR_MAX_BACKOFF_SECONDS`, 300). The first success marks it
healthy again: recovery is automatic. Waits are spread by ±10%, and sources start at random
offsets, so they don't all poll at once. Added, removed and disabled sources are picked up every
`QP_COLLECTOR_REFRESH_SECONDS` (30).

**Source status from collection:** `healthy` (collected, all capabilities available, no recent
evictions or profiler overflow), `degraded` (collected, but a capability is missing, statements
were evicted or `system.profile` overflowed),
`failing` (the last cycle failed; `last_error` says why).

**Demo data** (`python -m query_pulse.demo.seed`, then `python -m query_pulse.demo.workload`; the
`demo-workload` compose service runs both): an e-commerce schema in the demo database (customers,
products, orders; sizes from `QP_DEMO_CUSTOMERS` / `_PRODUCTS` / `_ORDERS`, default 20,000 / 5,000
/ 200,000) with four planted problems, and a workload at `QP_DEMO_RATE` queries per second (20)
running them next to healthy controls:

| ID | Planted problem | Healthy control |
|---|---|---|
| P1 | Orders looked up by `customer_id`, which has no index (sequential scan) | Customer by primary key |
| P2 | Latest 20 orders sorted by `created_at`, which has no index | Order by primary key |
| P3 | Product search with `LIKE '%term%'` (a leading wildcard can't use a b-tree) | Products by indexed category (40 categories, about 2.5% each, so the planner uses the index) |
| P4 | `idx_orders_legacy_lookup`: a large index nothing uses | Orders by primary-key range |

The seed only fills empty tables, so it's safe to rerun. On the running demo, P1 ranks first by
total time and P2 second.

In MongoDB (`QP_DEMO_MONGO_URL`), the seed fills `shop.orders` (`QP_DEMO_MONGO_ORDERS`, 50,000)
and `shop.products` (`QP_DEMO_MONGO_PRODUCTS`, 20,000), creates the control indexes, makes
`system.profile` 64 MB and turns profiling on for `shop` (level 1, slowms 0, so the controls
are visible too; a real deployment keeps slowms at 100 ms or more). The workload runs at
`QP_DEMO_MONGO_RATE` operations per second (10):

| ID | Planted problem | Healthy controls |
|---|---|---|
| M1 | Orders by `customerId`, which has no index (COLLSCAN) | Order by `_id` (IDHACK); order by indexed `orderNo` |
| M2 | Latest 20 orders sorted by `placedAt`, which has no index (in-memory SORT) | Latest 20 by `_id` (index order, no sort) |
| M3 | Products by an unanchored `$regex` on the indexed `sku` (the whole index is scanned) | Products by a `^SKU-0…` prefix (tight bounds), limit 10 |

---

## 7. Configuration and operation

### 7.1 Environment variables (backend)

All settings are validated when the API starts. A missing or invalid value stops it with a clear
error.

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `QP_ENVIRONMENT` | no | `dev` | `dev`, `test` or `prod` |
| `QP_DATABASE_URL` | no | `postgresql+psycopg://querypulse:querypulse@localhost:5432/querypulse` | The app's own database |
| `QP_SESSION_SECRET` | **yes** | — | Signs session cookies; at least 32 characters |
| `QP_SESSION_HTTPS_ONLY` | no (**yes** when `prod`) | `false` | Adds the `Secure` cookie flag. The API refuses to start with `QP_ENVIRONMENT=prod` unless this is `true`. |
| `QP_SESSION_MAX_AGE_SECONDS` | no | `43200` (12 h) | Session lifetime |
| `QP_FERNET_KEY` | **yes** | — | Encryption key(s) for stored source credentials. One Fernet key, or several comma-separated for rotation (the first encrypts, all decrypt). Losing it makes stored passwords unreadable. |
| `QP_SOURCE_CONNECT_TIMEOUT_SECONDS` | no | `5` | How long a connection test or check waits for a monitored database (up to 60) |
| `QP_COLLECTOR_INTERVAL_SECONDS` | no | `60` | Collection interval per source (15 in the compose demo) |
| `QP_COLLECTOR_CYCLE_TIMEOUT_SECONDS` | no | `30` | Time limit for one collection cycle |
| `QP_COLLECTOR_MAX_BACKOFF_SECONDS` | no | `300` | Longest wait between attempts after failures |
| `QP_COLLECTOR_RELATION_STATS_SECONDS` | no | `300` | How often table and index statistics are sampled |
| `QP_COLLECTOR_CAPABILITY_CHECK_SECONDS` | no | `900` | How often capabilities are re-checked |
| `QP_COLLECTOR_REFRESH_SECONDS` | no | `30` | How often the collector picks up added or removed sources (and writes its heartbeat) |
| `QP_COLLECTOR_PLAN_SECONDS` | no | `300` | How often each source's busiest operations are all re-planned |
| `QP_COLLECTOR_PLAN_TOP_N` | no | `20` | How many of each source's busiest operations are planned (1–200) |
| `QP_COLLECTOR_PROFILE_MAX_ENTRIES` | no | `10000` | MongoDB: profiler entries read per cycle at most (the rest wait for the next) |
| `QP_COLLECTOR_SAMPLE_CACHE_SIZE` | no | `500` | MongoDB: read shapes per source whose newest command is held in collector memory for explain (never stored) |
| `QP_RETENTION_DAYS` | no | `14` | Observations, table and server samples, and stale plans older than this are deleted daily |
| `QP_OPENAI_BASE_URL` | no | `https://api.openai.com/v1` | Where OpenAI requests go; an OpenAI-compatible gateway works (see AI assistant), and compose passes it through |
| `QP_AI_TIMEOUT_SECONDS` | no | `60` | How long an AI request may take (up to 300) |
| `QP_AI_ALLOW_FAKE_PROVIDER` | no | `false` | Offers the fake provider (tests only); refused when `QP_ENVIRONMENT=prod` |
| `QP_LOG_FORMAT` | no | `json` | `json` (one object per line) or `text` |
| `QP_LOG_LEVEL` | no | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR` (PyMongo's loggers stay at WARNING) |
| `QP_ALLOWED_ORIGINS` | no | — | Comma-separated extra origins allowed to make state-changing requests |
| `QP_LOGIN_MAX_FAILURES` | no | `10` | Failed sign-ins per client before sign-in is refused for a while |
| `QP_LOGIN_WINDOW_SECONDS` | no | `300` | The window those failures are counted in |
| `QP_RULES_INTERVAL_SECONDS` | no | `60` | How often each source's rules run (15 in the compose demo) |
| `QP_RULES_WINDOW_MINUTES` | no | `60` | The window the rules look at |
| `QP_RULES_UNUSED_INDEX_MIN_SPAN_SECONDS` | no | `3600` | How long an index must show zero scans to count as unused (120 in the demo) |
| `QP_RULES_UNUSED_INDEX_WINDOW_HOURS` | no | `24` | How far back index samples are read |
| `QP_FINDING_RESOLVE_AFTER_SECONDS` | no | `3600` | An open finding not seen for this long resolves itself |
| `QP_ADMIN_USERNAME` | no | `admin` | Admin username |
| `QP_ADMIN_PASSWORD` | **yes** | — | Admin password; at least 8 characters |
| `QP_DEMO_ENABLED` | no | `false` | Creates the demo user and enables "Try the demo" |
| `QP_DEMO_USERNAME` | no | `demo` | Must differ from the admin username |
| `QP_DEMO_PASSWORD` | if demo enabled | — | Demo user's password |

Values can come from the environment or a local `.env` file (see `.env.example`).

### 7.2 Commands (backend)

| Command | Does |
|---|---|
| `alembic upgrade head` | Creates or updates the app database schema |
| `python -m query_pulse.accounts.bootstrap` | Makes the accounts match the environment and prints `created`, `updated`, `unchanged` or `removed` for each. Safe to run repeatedly. |
| `uvicorn query_pulse.main:create_app --factory` | Runs the API |
| `python scripts/export_openapi.py [--check]` | Writes `openapi.json`, or fails if it is out of date |
| `python -m query_pulse.collector` | Runs the collector (one instance only) |
| `python -m query_pulse.demo.seed` / `.workload` | Seeds the demo database / runs the demo workload |
| `python scripts/ai_eval.py [--fake]` (`make ai-eval`) | Scores the AI on the seven planted problems; needs `QP_AI_EVAL_API_KEY` (or `OPENAI_API_KEY`) and `QP_AI_EVAL_MODEL`, or `--fake`; writes `evals/results.md` |
| `python scripts/save_eval_bundles.py` | Saves the demo's evidence bundles as the evaluation set |
| `python scripts/soak.py --minutes 60` (`make soak`) | Soak test against the running stack: fails on memory growth, slow or failed cycles |
| `pytest tests/test_fuzz.py` (`make fuzz`) | Schemathesis over every API operation (`QP_FUZZ_EXAMPLES`) |

The API container runs the first three in order on every start.

### 7.3 Local stack (`make demo` in the backend repo)

| Service | Host port | Purpose | Local demo credentials |
|---|---|---|---|
| `app` | 8000 | The API | `admin` / `admin-demo-password`, `demo` / `demo-password` |
| `app-db` | 5432 | PostgreSQL 16, the app's own database | `querypulse` / `querypulse` |
| `demo-postgres` | 5433 | PostgreSQL 16 to be monitored, with `pg_stat_statements` and the read-only role `querypulse_monitor` (`pg_monitor`) | `postgres` / `postgres`; monitor: `querypulse_monitor` / `monitor` |
| `collector` | — | The collector, polling every 15 s in the demo, with rules every cycle and table/index samples every minute; healthy while its heartbeat is fresh | — |
| `demo-workload` | — | Seeds both demo databases once (and turns on MongoDB's profiler for `shop`), then runs the P1–P4 workload at 20 queries/s and the M1–M3 workload at 10 operations/s, and attaches sample AI answers to the planted findings (it reads the app database for that, `QP_DEMO_APP_DATABASE_URL`) | `shop_app` / `shop-app` (demo role) |
| `mongo` | 27017 | MongoDB 7, single-node replica set `rs0`, to be monitored. From the host use `mongodb://localhost:27017/?directConnection=true`; from the stack, host `mongo`, database `shop`, no username. | none |

All ports listen on `127.0.0.1` only, so the databases, with their default credentials and
MongoDB without auth, can't be reached from other machines on your network. The API starts only
after all three databases report healthy. Data persists in named Docker volumes;
`docker compose down -v` deletes it.

### 7.4 Frontend configuration

| Variable | Default | Purpose |
|---|---|---|
| `QP_BACKEND_URL` | `http://localhost:8000` | Where the dev server proxies `/api` |
| `QP_OPENAPI_SOURCE` | `../query-pulse-backend/openapi.json` | Spec used by `npm run gen:api` (a path or URL) |
| `E2E_ADMIN_USERNAME`, `E2E_ADMIN_PASSWORD` | `admin`, `admin-demo-password` | Credentials for the Playwright tests |

Backend acceptance tests (`tests/acceptance/`): `QP_ACCEPTANCE_URL` (unset: skipped),
`QP_ACCEPTANCE_ADMIN_PASSWORD` (`admin-demo-password`), `QP_ACCEPTANCE_WAIT_SECONDS` (240).

---

## 8. The API contract between the repos

1. The backend generates `openapi.json` from its code and commits it. Its CI fails if the file is
   out of date.
2. The frontend generates TypeScript types from that file (`npm run gen:api`) and sends every
   request through one typed client, so a backend change the frontend hasn't absorbed is a
   compile error.
3. The frontend's CI regenerates the types from the backend's `main` branch and fails if the
   committed types differ.

Push backend API changes first, then regenerate and push the frontend.

---

## 9. Verification

| Acceptance criterion | Status | Proven by |
|---|---|---|
| QP-AC-01: `make demo` starts all services healthy and the demo workload is running | Done | Backend CI `stack` job (services healthy, demo data seeded, collector heartbeat); verified locally |
| QP-AC-02: admin signs in and out; protected endpoints return 401 without a session | Done | Backend `test_auth.py` and `test_access.py` (every endpoint from the spec); frontend Playwright `sign-in.spec.ts` |
| QP-AC-03: demo user gets 403 on every write endpoint in `openapi.json` | Done | Backend `test_access.py`, generated from the spec |
| QP-AC-04: CI fails when `openapi.json` or the frontend types are stale | Done | Backend `export_openapi.py --check`; frontend `gen:api:check` |
| QP-AC-05: the demo PostgreSQL and MongoDB sources can be added after a successful test connection | Done | Backend `test_sources.py`; frontend `sources.test.tsx`; Playwright `sources.spec.ts` against the Docker stack |
| QP-AC-06: missing extension, missing role and profiler-off each show as unavailable with the correct fix | Done | Backend `tests/integration/test_adapters.py` (real PostgreSQL and MongoDB via testcontainers): extension not loaded, not created, missing `pg_monitor`, missing `clusterMonitor`, profiler off and on |
| QP-AC-07: an unreachable host or wrong password fails within the timeout with a clear error, and nothing is saved | Done | Integration tests (wrong password, unknown database, closed port, unresolvable host, timed); `test_sources.py` (nothing saved); Playwright wrong-password journey |
| QP-AC-08: a marker password never appears in any response body or log line | Done | `test_sources.py::test_marker_password_never_leaks` (every source endpoint, success and failure, plus all log records) |

| QP-AC-09: a query run exactly 500 times between cycles shows Δcalls = 500 and a mean matching a direct `pg_stat_statements` diff within 1% | Done | `tests/integration/test_collector.py::test_500_calls_between_cycles_cross_checked` |
| QP-AC-10: after `pg_stat_statements_reset()`, no negative or inflated delta is stored and collection continues | Done | `test_reset_discards_the_interval_and_collection_continues`; delta-engine unit tests |
| QP-AC-11: restarting the collector keeps the baseline | Done | `test_restarted_collector_continues_from_the_persisted_baseline`; serialisation unit test |
| QP-AC-12: rising `dealloc` produces an eviction warning on the source | Done | Unit tests + `test_rising_dealloc_warns_on_the_source` (`pg_stat_statements.max=100`) |
| QP-AC-13: a database going down marks the source failing; it recovers on its own | Done | `test_outage_marks_failing_then_recovers` (pauses the container); backoff and supervisor unit tests |

| QP-AC-14: under the demo workload, P1 ranks first by total time over the last 15 minutes | Done | Backend `tests/acceptance/test_demo_workload.py::test_p1_ranks_first_by_total_time` (full stack, CI `stack` job); ranking unit tests in `test_explorer.py` |
| QP-AC-15: P1's generic plan contains a Seq Scan on `orders`; planning never executes the query | Done | `tests/integration/test_plans.py::test_p1_plan_shows_the_seq_scan_without_executing_it` (call count unchanged after 10 direct EXPLAINs and two more planning cycles); the same check end to end in the acceptance test |
| QP-AC-16: a copied explorer URL reopens the same filters and time range | Done | Playwright `explorer.spec.ts` (opened in a second browser context); Vitest `explorer.test.tsx` |
| QP-AC-17: journey home → P1 → plan viewer shows the sequential scan | Done | Playwright `explorer.spec.ts` |

| QP-AC-18: P1, P2, P3 and P4 each produce exactly one open finding from their expected rule | Done | Backend acceptance `test_each_planted_problem_produces_exactly_one_finding_and_controls_none` (full stack); every rule with fixture evidence in `test_findings.py` |
| QP-AC-19: healthy control queries produce no findings | Done | The same acceptance test (exactly four open findings); control fixtures for every rule in `test_findings.py` |
| QP-AC-20: every finding's evidence links resolve to existing observations or plans | Done | `test_findings.py::test_findings_api_lists_filters_and_resolves_every_evidence_link`; acceptance `test_every_finding_evidence_link_resolves` |
| QP-AC-21: dismissing a finding keeps it dismissed across later collection cycles | Done | `tests/integration/test_findings_cycles.py` (real PostgreSQL, three more cycles); engine unit test |

| QP-AC-22: two queries differing only in values share one shape; different operators or fields don't | Done | Golden files `tests/golden/mongo_shapes/` (22, every command) and the pair tests in `test_mongodb.py`; thirty real lookups with different values become one shape in `tests/integration/test_mongo_collector.py` |
| QP-AC-23: M1, M2 and M3 each produce their expected finding; controls produce none | Done | Backend acceptance `test_mongo_planted_problems_produce_exactly_one_finding_each_and_controls_none` (full stack); each rule on real MongoDB 7 plans in `test_mongodb.py`, including the all-problems test in `test_findings.py`; M1 end to end on a real MongoDB in `test_mongo_collector.py` |
| QP-AC-24: with profiling off, the UI shows the capability gap and fix, not an empty list | Done | `tests/integration/test_mongo_collector.py::test_profiling_off_reports_the_gap_and_collects_nothing`; `test_mongo_cycle.py` (switching it off is noticed and listed as a gap); Vitest `mongodb.test.tsx` ("profiling off") |
| QP-AC-25: PostgreSQL and MongoDB operations appear in one explorer, each opening its own plan viewer | Done | Playwright `mongodb.spec.ts` (full stack); backend acceptance `test_both_engines_share_one_explorer_with_their_own_plans`; Vitest `mongodb.test.tsx` |

| QP-AC-26: with AI disabled, the whole Playwright suite passes and no provider call is made | Done | Frontend CI runs every Playwright journey against the stack with AI off; `e2e/ai-and-a11y.spec.ts` checks the AI features say so and `provider_calls` is 0; backend `test_ai.py::test_ai_is_off_by_default_and_makes_no_provider_call` |
| QP-AC-27: a response citing an evidence id not in the bundle is rejected and shown as an error | Done | `test_ai.py` (the checker and the API, with the fake provider citing `plan:9.9.9`); Vitest `ai.test.tsx` (shown as an error, not advice) |
| QP-AC-28: evidence bundles never contain literals, credentials or connection strings | Done | `test_ai.py`: bundles for P1–P4 and M1–M3 built from real plans with planted literals; the API bundle for every finding on a source with a marker password, host and username |
| QP-AC-29: `make ai-eval` runs over all seven bundles and reports scores; scores are in the README | Harness done; scores pending | `scripts/ai_eval.py` over `evals/bundles/` (saved from the demo); `test_ai.py::test_the_eval_runs_over_all_seven_saved_bundles` with the fake provider. The real run needs a key and is manual |
| QP-AC-30: a new developer reaches a working demo from the README alone | Pending (manual) | The fresh-machine release check, run by hand before tagging v1.0.0 |

Phase 1 close-out (Origin check, login rate limit, JSON logs with request IDs) is covered by
`test_hardening.py`. Phase 7 hardening: `test_fuzz.py` (Schemathesis over all 34 operations;
it found ids beyond the `integer` column causing 500s and 422s with the wrong shape, both
fixed, with regression tests in `test_hardening.py`), the version matrix (CI `versions` job),
the nightly soak test (`.github/workflows/nightly.yml`), and an axe accessibility check of every
main screen in light and dark (`e2e/ai-and-a11y.spec.ts`; it found low contrast on muted text,
fixed in the design tokens). No stored MongoDB values (ADR 0010) is covered by the marker-value check
in `test_mongo_collector.py`, redaction tests on every captured plan in `test_mongodb.py`, and
`test_mongo_cycle.py`.

Test counts: 412 backend unit (one of them fuzzes all 34 operations), 28 backend integration and 6
backend acceptance (pytest), 109 frontend (Vitest), 15 end-to-end (Playwright).

---

## 10. Not built yet

| Planned | Phase | Release |
|---|---|---|
| Release deliverables (manual): the real `make ai-eval` run and its scores in the README, the fresh-machine check, tags v0.1.0–v1.0.0, the demo video and README GIF, blog posts | 7 | v1.0.0 |
