# skyll-trades-validator

**Read-only operational dashboard that checks the integrity of the Skyll fills → trades → intraday → daily-candle pipeline, per group / trader / contract, over a rolling window.**

It answers one question at a glance: *is the system healthy?* A **one-line health header** says how many fills were genuinely **dropped**, **mis-attributed**, or **stranded** — and **🔴 red means a fill is genuinely wrong and you can act on it.** Everything expected — flat books, genuine open positions, pre-retention carries, ancient residuals — collapses out of the way, so a systemic gap shows up as **one row, not fifty**.

Every non-flat `(account, contract)` is cross-checked **as of the last completed UTC day** against the **FIX feed `raw_fills_fix`** — the authoritative, independent per-account copy of every fill — and gets exactly one verdict: `DROP` (the feed has fills we lack → recoverable), `EXTRA / MIS-ATTRIBUTED` (we hold fills the feed lacks → duplicate / alias-defaulted), `STRANDED` (futures fills stuck under trader 0/349, never aggregated), `CONFIRMED-OPEN` (our net == the feed → a genuine hold), `PARTIAL-CARRY` (opened before the retention wall), or `UNVERIFIABLE` (no feed rows). Drops are then **rolled up by ingestion day**, because they cluster on bad-ingestion events (e.g. `2026-06-11 17:30`) that hit many accounts at once.

This is a monitor for the exact failure class that has bitten us repeatedly: dropped fills (µs-collision PK, watermark out-of-order, skipped clearing-alias fills) leave a phantom open position and mis-bounded trades. The dashboard surfaces those automatically instead of us discovering them one client complaint at a time.

> **Strictly read-only.** Connects only to `PROD_DATABASE_CONNECTION_STRING_READONLY` with the session forced read-only, and to the TT REST API for read-only position/fill lookups. It never writes to the database.

---

## What it shows

A collapsible **Group → Trader** tree with a **day-by-day heatmap timeline** (default: last 30 days). Healthy rows collapse away; problems auto-expand and jump out.

Per trader × day cell state (every non-flat cell is exactly one of these):

| State | 🔴? | Meaning |
|---|---|---|
| 🟢 **Flat** | | End-of-day net = 0 and all fills aggregated. The healthy case. |
| 🔵 **Open — confirmed** | | Net ≠ 0 **and our net == the FIX feed**. A genuine open hold. |
| **Carry (pre-retention)** | | Non-flat from a position opened before the FIX wall (~2026-03-30); the opening is unrecoverable, the in-window activity reconciles. Not a bug. |
| 🟡 **Open — unverifiable** | | Net ≠ 0 but no FIX rows to cross-check (option strike, give-up / clearing-alias account, or pre-retention). Needs an eye. |
| 🟠 **Orphan** | | Futures fills on a completed day with empty `trade_ids` (under the real trader) → aggregation gap. |
| 🟣 **Unreconciled** | | Diverges from the FIX feed but can't be pinned (block-vs-leg / synthetic markers / spread legs) → investigate. |
| 🔴 **Extra / mis-attributed** | 🔴 | We hold **more** than the FIX feed — a duplicate or an alias-on-alias order defaulted into the wrong book (read BOTH account + trader; orphan it off). |
| 🔴 **Stranded** | 🔴 | Futures fills aggregated under trader 0 (Unassigned) / 349 (IgnoredAccounts), never linked to a trade — fix with `recalc_trader` (no backfill). |
| 🔴 **Dropped fill** | 🔴 | The FIX feed has fills our `fills` lacks → a silently dropped fill (recoverable; if the feed net ≠ 0, recovery lands a genuine open). |
| ⚪ **Residual (old, un-chased)** | | Non-flat on a **past-expiry** contract with no recent fills. **Display triage only** — collapsed so ancient ledgers don't flood the view. Not "settled": Skyll has no expiry logic. |

Only **Dropped / Extra-mis-attributed / Stranded** are 🔴 actionable; the rest are calm/amber. A **daily-candle reconciliation** delta (daily `close_pnl` vs `Σ trades.profit`) rides along as a secondary badge, known explainable causes suppressed (cross-day trades, thin-contract intraday drops).

### Cross-check: FIX feed (primary) + TT ledger (secondary)
1. **FIX-feed diff** (`raw_fills_fix`, run automatically) — for every non-flat `(account, contract)`, compares our net vs the feed's net **as of the last completed UTC day** (today's in-flight fills excluded, so the real-time feed leading our batch ingestion never reads as a flood of false drops). Account match is **label-robust** (REST `LFCTEU150_MA` ↔ FIX `LFCTEU150` / `&` / `:` forms) and aggregates sub-accounts. Net-divergent contracts then get a per-fill diff (the `raw_diff_ts` matchers) to separate DROP from EXTRA and extract the exact fills + ingestion day. **The TT *position* endpoint that the old version used was removed — it ignores the `accountId` filter and cross-nets accounts.**
2. **FIX drill-down** (`/api/raw-diff`, on demand) — click a flagged contract to see the exact missing-from-us / extra-in-us fills, with `uniqueExecId` (reingest-ready).
3. **TT ledger diff** (`/api/tt-diff`, secondary) — paginates the TT `ttledger/fills` API; carries uniqueExecId for TT accounts and reaches a hair past the FIX wall. How Adam Burt's and Demetris's drops were recovered.

### Fill history (click a contract name)
Clicking a **contract name** opens a fill-history page (`/api/fills`, hash route `#/fills?account=&contract=`): every fill newest-first, each with a **running position** (signed cumulative qty — watch the book build and unwind), the per-fill Δ, whether it's aggregated into a trade (✓), the `trader_id` (0/349 = stranded, flagged), and thin day / weekend separators. Cmd-click opens it in a new tab.

---

## Core model & assumptions

- **Cohort:** only traders with a `group_members` row (assigned to a group). Ungrouped traders are ignored. Includes **all** their accounts — live, sim and opt-out — with UI filters to hide sim/opt-out.
- **Integrity grain:** `(platform_account, contract)`. Contracts are **not** rolled up into products — `MES Jun26` and `MES Sep26` are distinct positions. Computed per account, displayed rolled up to the trader (a trader's cell = its worst child that day).
- **Net position:** signed cumulative fills (buy `+qty`, sell `−qty`). Flat = 0.
- **Day boundary = UTC calendar day**, matching the existing daily-candle rollup (`func.date(datetime)` UTC) and the `time_bucket('1 day', open_time)` continuous aggregates. EOD net for a day is the cumulative net through `day 23:59:59 UTC`.
- **Switch-on day:** the first day of the current trailing non-zero run — i.e. "the last day this was flat" + 1.
- **Orphans** are only flagged on **completed days** (`day < today UTC`); today's unassigned fills are "pending aggregation", not orphans.
- **Active vs residual (display triage):** a contract is in the timeline if it has a fill in the window, or is currently non-flat and not past expiry. A non-flat **expired** contract with no recent fills is pushed to the collapsed residual bucket (⚪) so ~1,800 dead 2024 contracts don't flood the view. This is purely a view filter — not a settlement. The model is **fills → trades → profit**; a non-flat ledger means a lost fill (or a genuine open), never "it expired". See `aws-mwaa-local-runner/dags/misc/recovery/PRINCIPLES.md`.

See [`docs/DESIGN.md`](docs/DESIGN.md) for the full rationale, schema notes and the known failure-mode catalogue.

---

## Architecture

```
backend/   FastAPI read-only validation engine (Python)
  app/
    config.py    env + tunables (retention wall, net tolerance, stranded trader ids)
    db.py        read-only psycopg2 pool (session forced read-only; retries standby conflicts)
    contracts.py contract-expiry parsing (display triage)
    engine.py    cohort + EOD net positions + orphans + stranding + reconciliation + roll-ups + health
    fixfeed.py   the FIX-feed cross-check: canonical-account net classify + per-fill DROP/EXTRA
                 discrimination (raw_diff_ts matchers) + on-demand account_diff
    tt.py        TT REST client (token auth, paginated ledger fills diff — secondary drill-down)
    api.py       FastAPI app + endpoints, in-process result cache
frontend/  React + Vite + Tailwind SPA (collapsible heatmap timeline)
```

The engine is importable and side-effect-free so it can later be wired into a scheduled DAG that posts to Telegram. Step one is this on-demand local app.

---

## Running

Secrets live in the `skyll-mwaa` keychain repo (`PROD_DATABASE_CONNECTION_STRING_READONLY`, `APP_SECRET`, `SIM_APP_SECRET`). The keychain must be unlocked (`secretctl unlock`, by you — agents can't).

```bash
# backend (from this folder)
cd backend
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
secretctl run skyll-mwaa -- ./venv/bin/uvicorn app.api:app --port 8799

# frontend
cd frontend
yarn install
yarn dev          # talks to backend on :8799
```

Quick engine smoke test (prints a text summary of the current state, no server):

```bash
secretctl run skyll-mwaa -- ./venv/bin/python -m app.engine
```

---

## Agent-readable output (for AI agents)

The heatmap is for humans. When something looks off, an **AI agent** can pull the same
state as structured, problem-focused data — no screen-reading — and act on it.

**Offline (no server):**

```bash
make report                                              # all findings → JSON (stdout)
make report ARGS="--severity drop,extra_misattr,stranded --min-net 5 --limit 40"
make report-md                                           # compact markdown digest
# or directly, with filters --severity/--min-net/--group/--trader/--limit/--window/--no-fix:
secretctl run skyll-mwaa -- ./venv/bin/python -m app.report --md --group Axia
```

**From the running server** (same data, cached):

```bash
curl -s 'http://127.0.0.1:8799/api/findings' | jq                       # JSON
curl -s 'http://127.0.0.1:8799/api/findings?format=md'                  # markdown
curl -s 'http://127.0.0.1:8799/api/findings?severity=drop,extra_misattr,stranded'
```

The report leads with the **health header** and the **drops-by-ingestion-day rollup**. Each
**finding** is one `(account, contract)`, carrying `severity` (`drop` | `extra_misattr` |
`stranded` | `unreconciled` | `orphan`), `group` / `trader` / `account` / `contract` /
`platform` / `is_sim` / `opt_out`, `current_net`, `open_since`, `last_flat_day`, `days_open`,
`last_fill`, the **`fix`** block (`{feed, raw_net, our_net, gap, missing[], extra[], …}` — the
missing/extra fills carry `uniqueExecId`), `stranded_info`, and an **`investigate`** block: the
ready-to-run **`/api/raw-diff`** + **`raw_diff_ts`** discovery commands, the **recover→recalc**
chain, the **SQL** to read both `trader_id` columns, and a `hint`. The response also carries the
top-level **`playbook`**.

The agent loop: pull findings → run a flagged contract's **`/api/raw-diff`** (or `raw_diff_ts`) to
get the exact missing/extra fill(s) with `uniqueExecId` → recover (`reingest` → `recalc_trader` for
a drop; `recalc_trader` only for stranded/orphan; orphan-off-the-book for extra) **in
`aws-mwaa-local-runner`, never from here** (this tool is strictly read-only).
