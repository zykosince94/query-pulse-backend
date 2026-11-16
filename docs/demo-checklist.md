# Demo checklist

The scripted demo. Run it by hand, step by step, before tagging every release, on a machine
that starts from a fresh clone. Tick each step; if one fails, file an issue and don't tag.

*Covers releases up to v1.0.0 (Phases 1–7).*

## 0. Fresh start

- [ ] `git clone` both repos side by side (`query-pulse-backend`, `query-pulse-frontend`).
- [ ] Backend: `docker compose down -v` if a previous stack exists (the demo must work from empty
      volumes), then `make demo` (or `docker compose up -d --build --wait`). It finishes with
      every service healthy.
- [ ] `curl http://127.0.0.1:8000/ready` returns `{"status":"ready","database":"ok"}`.
- [ ] `docker compose logs app` shows one JSON object per line.
- [ ] Frontend: `npm ci && npm run dev`, then open http://localhost:5173.

## 1. Sign-in (QP-AC-02, QP-AC-03)

- [ ] Visiting `/` while signed out goes to the sign-in screen.
- [ ] A wrong password says "Invalid username or password" and stays on the screen.
- [ ] Sign in as `admin` / `admin-demo-password`: the Workload page opens.
- [ ] Sign out, then **Try the demo**: a "Read-only demo" badge shows; add/edit buttons say the
      demo is read-only. Sign out and back in as admin.

## 2. Data sources (QP-AC-05, QP-AC-06, QP-AC-07)

- [ ] Data sources → Add source → PostgreSQL. Name `Demo PostgreSQL`, host `demo-postgres`,
      port `5432`, database `shop`, user `querypulse_monitor`, password `monitor`.
- [ ] **Save** is disabled until **Test connection** succeeds; the test says "Connected to
      version 16…" and every capability is available. Save: the source is Healthy and the
      password shows as "Saved (encrypted)".
- [ ] Change the password to `wrong` and test: a clear authentication error appears within a few
      seconds, and nothing is saved.
- [ ] Add MongoDB with database `inventory` (host `mongo`, port `27017`, no credentials), and
      test only: the checklist shows the profiler as unavailable on `inventory`, with the exact
      command to enable it. (The demo seed turned profiling on for `shop` only.)
- [ ] Add MongoDB for real: name `Demo MongoDB`, host `mongo`, port `27017`, database `shop`, no
      credentials. Every capability is available (server metrics, plans, profiler); save: Healthy.

## 3. Collection (Phase 3)

- [ ] Within a minute, the home page's health strip shows Demo PostgreSQL with calls and
      execution time for the last hour and a moving sparkline; "Collector: running".
- [ ] The source's detail page shows "Last collected …" and both per-minute charts; **Show
      data** lists each minute's values.

## 4. Workload explorer and plans (QP-AC-14 to QP-AC-17)

Wait two or three minutes after adding the source, so several cycles have been collected.

- [ ] Under **Operations**, ranked by total time over the last hour, row 1 is
      `SELECT id, status, total FROM orders WHERE customer_id = $1` (P1) with roughly half the
      total time, and row 2 is the "latest orders" sort (P2).
- [ ] Change the range to **Last 15 minutes**: P1 is still first.
- [ ] Rank by **Calls**: the primary-key lookups (controls C1/C2) move to the top.
- [ ] Type `orders` in "Statement or shape contains" and press Enter: only operations on
      `orders` remain.
- [ ] **Lock range**, then **Copy link**. Open the link in a private window and sign in: the same
      range (as dates), ranking, filter and rows appear.
- [ ] Back on the home page, click P1. The detail page shows its statement, total time with its
      share, calls, mean time, rows, cache hit ratio, and two charts.
- [ ] The **Query plan** shows a **Seq Scan on orders** highlighted "Reads the whole table", with
      cost `0.00..~4,400`, about 10 rows and `Filter: (customer_id = $1)`. Raw plan opens the
      EXPLAIN JSON.
- [ ] Open P2 (latest orders): its plan has a Sort on `created_at DESC` under a Limit.
- [ ] As the demo user, the plan is visible but there are no capture buttons.

## 5. Findings (QP-AC-18 to QP-AC-21)

About three minutes after adding the source (P4 needs two minutes of index samples):

- [ ] The sidebar's **Findings** link counts every open finding (4 for PostgreSQL, 7 once
      MongoDB's three arrive).
- [ ] Findings → Open, source **Demo PostgreSQL**, lists exactly four, one per planted problem:
      **PG-SEQSCAN-HOT** on the `customer_id` lookup (P1), **PG-SORT-NO-INDEX** on "latest
      orders" (P2), **PG-LEADING-WILDCARD** on the product search (P3, medium confidence, saying
      the pattern itself isn't recorded) and **PG-UNUSED-INDEX** on
      `public.idx_orders_legacy_lookup` (P4, "Statistics heuristic"). None for the controls.
- [ ] Each card shows its facts with real numbers, the suggested SQL with Copy, "Query Pulse
      never runs it for you", and its evidence links.
- [ ] On the P1 card, click the **Plan #…** evidence link: the operation page shows "Showing plan
      #…, the one a finding cites" with the Seq Scan outlined as "Cited by the finding". **Show
      the latest plan** returns to the current plan.
- [ ] Click the P1 card's observation-window link: the operation opens over that exact range.
- [ ] The P1 operation page lists its finding above the plan.
- [ ] **Dismiss** the P3 finding: it moves to the Dismissed tab and the count drops to 3. Wait a
      minute (several rule runs): it's still dismissed. **Reopen** it.
- [ ] Rule filter **PG-UNUSED-INDEX** shows one finding, and the URL keeps the filter.
- [ ] As the demo user: findings are visible, with no status buttons.

## 6. MongoDB (QP-AC-22 to QP-AC-25)

A minute or two after adding Demo MongoDB:

- [ ] The health strip's Demo MongoDB card counts **profiled operations** and has a sparkline.
- [ ] The explorer (all sources) mixes both engines, each row with its engine badge. Filter to
      Demo MongoDB: seven shapes, each compact JSON with every value as `?` (QP-AC-22: the
      thousands of `customerId` lookups, each with a different value, are one row), with the
      collection under it. `{"find": "orders", "filter": {"customerId": "?"}}` (M1) is first.
- [ ] Open M1: titled **Query shape**, the shape indented, "find on shop.orders", operations,
      documents returned and **examined per returned** (about 20,000). The plan viewer shows a
      **COLLSCAN** flagged "Reads the whole collection" with `{"customerId": {"$eq": "?"}}`,
      and no capture button. Back to the explorer, open P1: its own PostgreSQL plan viewer
      (QP-AC-25).
- [ ] Findings, source Demo MongoDB: exactly three (QP-AC-23). **MDB-COLLSCAN** on M1 and
      **MDB-BLOCKING-SORT** on the `placedAt` sort (both "Plan evidence"), and
      **MDB-EXAMINED-RATIO** on the `sku` `$regex` ("Statistics heuristic", high confidence,
      saying the regex isn't anchored). None for the four controls. The sidebar badge is **7**.
- [ ] The M1 and M2 cards suggest a mongosh `createIndex` command ("Copy the mongosh command").
- [ ] On the M3 card, the **Plan #… IXSCAN using sku_1** link opens the plan with the IXSCAN
      outlined, flagged "Scans the whole index", bounds `["", {}), ?`.
- [ ] The Demo MongoDB source page shows the **Server** panel: uptime, primary, connections,
      cache, and operations per second.
- [ ] Profiling off (QP-AC-24): add a source for database `inventory` (or run
      `db.getSiblingDB("shop").setProfilingLevel(0)` in `docker compose exec mongo mongosh`).
      Filter the explorer to it: a notice says its operations aren't collected, why, and the
      fix with Copy, instead of an empty list. Turn profiling back on with
      `setProfilingLevel(1, { slowms: 0 })` if you turned it off; delete the extra source.
- [ ] No values stored (ADR 0010): open any MongoDB plan's "Stored plan (redacted explain
      JSON)": filters and bounds show `?`, never a customer id or sku.

## 7. AI assistant (QP-AC-26 to QP-AC-29)

With AI off (a fresh stack):

- [ ] Settings shows the AI assistant **Off**, "Requests made by this server since it started:
      0", and the collection settings (15 s interval, 14 days) read-only.
- [ ] On the P1 finding, open **AI assistant**: "AI is off" with a link to Settings, no
      Explain or Draft buttons. **View what is sent** shows the bundle: `rule`, `fact:…`,
      `plan:0` with `(customer_id = $1)`, `table:public.orders`; no host, user or password.

With your OpenAI key (each request is billed to your account):

- [ ] Settings → provider OpenAI, a model id, your key, tick "Turn the AI assistant on", Save.
      A wrong key says OpenAI rejected it and nothing is saved; the right one shows **On** and
      the key only as `…` plus its last four characters.
- [ ] P1 → AI assistant → **Explain this finding**: a short summary and claims, each with the
      evidence ids it cites, labelled advisory, with the provider and model. Explain again:
      the same answer comes back at once (cached; the request counter doesn't move).
- [ ] **Draft a fix**: SQL to copy (expect `CREATE INDEX CONCURRENTLY … (customer_id)`), "A
      draft for review. Query Pulse never runs it."
- [ ] Do the same on M1: the draft is a mongosh `createIndex` command.
- [ ] As the demo user: the answers are visible, with no Explain or Draft buttons.
- [ ] `QP_AI_EVAL_API_KEY=… QP_AI_EVAL_MODEL=… make ai-eval`: a score table for P1–P4 and
      M1–M3; copy the overall scores into the README's "AI evaluation" section (QP-AC-29).
- [ ] Settings → **Delete the key**: AI turns off.

## 8. Accessibility

- [ ] Sign in and reach Findings, a finding's AI panel and Settings with the keyboard only;
      focus is always visible.
- [ ] Switch the OS to dark mode: every screen stays readable.

## 9. Hardening

- [ ] A cross-site write is refused:
      `curl -i -X POST http://127.0.0.1:8000/api/auth/login -H 'Origin: https://evil.example' -H 'content-type: application/json' -d '{"username":"admin","password":"admin-demo-password"}'`
      returns `403`.
- [ ] Eleven wrong passwords in a row from the same machine: the eleventh returns `429` with a
      `Retry-After` header, and the right password is refused until it passes.
- [ ] Every API response has an `X-Request-ID` header, and the app logs show that ID on the
      request's access line.

## 10. Resilience (spot checks)

- [ ] `docker compose stop demo-postgres`: within a couple of cycles the source turns Failing
      with a clear error. `docker compose start demo-postgres`: it recovers on its own.
- [ ] `docker compose restart collector`: collection continues with no gap or spike in the charts.
      MongoDB shapes keep their plans; a plan is captured again only once its shape is profiled
      again (the commands lived in memory).

## 11. Release (v1.0.0)

- [ ] Fresh-machine check (QP-AC-30): clone both repos into a clean VM or Codespace and reach
      a working demo using only the READMEs.
- [ ] Record the demo video and the README GIF of the main journey.
- [ ] Tag v0.1.0 to v1.0.0 in both repos, with release notes.

## 12. Wrap up

- [ ] Screenshots of new screens at desktop width and 375 px, in light and dark mode.
- [ ] `docker compose down -v`.
