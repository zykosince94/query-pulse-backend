# Demo checklist

The scripted demo. Run it by hand, step by step, before tagging every release, on a machine
that starts from a fresh clone. Tick each step; if one fails, file an issue and don't tag.

*Covers releases up to v0.5.0 (Phases 1–5).*

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
- [ ] Add MongoDB: host `mongo`, port `27017`, database `shop`, no credentials. The checklist
      shows the profiler as unavailable, with the exact command to enable it.

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
- [ ] Type `orders` in "Statement contains" and press Enter: only statements on `orders` remain.
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

- [ ] The sidebar's **Findings** link shows **4**.
- [ ] Findings → Open lists exactly four, one per planted problem:
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

## 6. Hardening

- [ ] A cross-site write is refused:
      `curl -i -X POST http://127.0.0.1:8000/api/auth/login -H 'Origin: https://evil.example' -H 'content-type: application/json' -d '{"username":"admin","password":"admin-demo-password"}'`
      returns `403`.
- [ ] Eleven wrong passwords in a row from the same machine: the eleventh returns `429` with a
      `Retry-After` header, and the right password is refused until it passes.
- [ ] Every API response has an `X-Request-ID` header, and the app logs show that ID on the
      request's access line.

## 7. Resilience (spot checks)

- [ ] `docker compose stop demo-postgres`: within a couple of cycles the source turns Failing
      with a clear error. `docker compose start demo-postgres`: it recovers on its own.
- [ ] `docker compose restart collector`: collection continues with no gap or spike in the charts.

## 8. Wrap up

- [ ] Screenshots of new screens at desktop width and 375 px, in light and dark mode.
- [ ] `docker compose down -v`.
