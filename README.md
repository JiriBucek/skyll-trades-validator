# skyll-trades-validator

**Read-only operational dashboard that checks the integrity of the Skyll fills → trades → intraday → daily-candle pipeline, per group / trader / contract, over a rolling window.**

It answers one question at a glance: *is the system healthy — are all fills aggregated into trades, and is every trader flat (net position = 0) at the end of each day?* — and when it isn't, it shows **where** a position is stuck open, **since which day**, and (for TT) whether the broker agrees it's really open or we silently **lost a fill**.

This is a monitor for the exact failure class that has bitten us repeatedly: dropped fills (µs-collision PK, un-paginated TT reads, skipped clearing-alias fills) leave a phantom open position and mis-bounded trades. The dashboard surfaces those automatically instead of us discovering them one client complaint at a time.

> **Strictly read-only.** Connects only to `PROD_DATABASE_CONNECTION_STRING_READONLY` with the session forced read-only, and to the TT REST API for read-only position/fill lookups. It never writes to the database.

---

## What it shows

A collapsible **Group → Trader** tree with a **day-by-day heatmap timeline** (default: last 30 days). Healthy rows collapse away; problems auto-expand and jump out.

Per trader × day cell state:

| State | Meaning |
|---|---|
| 🟢 **Flat** | End-of-day net position = 0 and all fills assigned to trades. The healthy case. |
| 🔵 **Open — confirmed** | Net ≠ 0 **and** TT position API confirms the same net. A real overnight hold (TT accounts only). |
| 🟡 **Open — unverifiable** | Net ≠ 0 on a Stellar account (no position API to cross-check). Needs an eyeball. |
| 🔴 **Suspected dropped fill** | Net ≠ 0 in our DB but **TT shows flat / smaller**. The alert — we probably lost a fill. |
| 🟠 **Orphan fills** | Fills on a completed day with empty `trade_ids` → aggregation gap (trades wrong even if raw fills balance). |
| ⚪ **Settled / expired residual** | Net ≠ 0 on a contract that is **past expiry** with no recent fills — a cash-settlement artifact. Expected; bucketed separately, collapsed by default. |

A **daily-candle reconciliation** delta (daily `close_pnl` vs `Σ trades.profit` closed that day) rides along as a secondary badge, with the known explainable causes suppressed (cross-day trades book P&L on the *open* day; thin-contract intraday drops).

### Two-level TT cross-check
1. **Position now** (`ttmonitor/{env}/position`) — runs automatically for currently-open TT contracts to classify 🔴 vs 🔵. *Gotcha: this endpoint ignores the `accountId` param, so rows are filtered client-side.* Tells us *now*, not historical EOD.
2. **Fills diff** (`ttledger/{env}/fills` vs our DB) — runs **on demand** when you click a flagged contract; paginates TT (500/call) and pinpoints the exact missing fill. This is how Adam Burt's and Demetris's dropped fills were recovered.

---

## Core model & assumptions

- **Cohort:** only traders with a `group_members` row (assigned to a group). Ungrouped traders are ignored. Includes **all** their accounts — live, sim and opt-out — with UI filters to hide sim/opt-out.
- **Integrity grain:** `(platform_account, contract)`. Contracts are **not** rolled up into products — `MES Jun26` and `MES Sep26` are distinct positions. Computed per account, displayed rolled up to the trader (a trader's cell = its worst child that day).
- **Net position:** signed cumulative fills (buy `+qty`, sell `−qty`). Flat = 0.
- **Day boundary = UTC calendar day**, matching the existing daily-candle rollup (`func.date(datetime)` UTC) and the `time_bucket('1 day', open_time)` continuous aggregates. EOD net for a day is the cumulative net through `day 23:59:59 UTC`.
- **Switch-on day:** the first day of the current trailing non-zero run — i.e. "the last day this was flat" + 1.
- **Orphans** are only flagged on **completed days** (`day < today UTC`); today's unassigned fills are "pending aggregation", not orphans.
- **Active vs residual:** a contract is in the timeline if it has a fill in the window, or is currently non-flat and not past expiry. A non-flat **expired** contract with no recent fills is a settled residual (⚪), bucketed separately so ~1,800 dead 2024 contracts don't flood the view.

See [`docs/DESIGN.md`](docs/DESIGN.md) for the full rationale, schema notes and the known failure-mode catalogue.

---

## Architecture

```
backend/   FastAPI read-only validation engine (Python)
  app/
    config.py    env + tunables
    db.py        read-only psycopg2 pool (session forced read-only)
    contracts.py contract-expiry parsing
    engine.py    cohort + EOD net positions + orphans + reconciliation + classify
    tt.py        TT REST client (token auth, position-now, paginated fills diff)
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
make report ARGS="--severity suspected_drop --min-net 5 --limit 40"
make report-md                                           # compact markdown digest
# or directly, with filters --severity/--min-net/--group/--trader/--limit/--window/--no-tt:
secretctl run skyll-mwaa -- ./venv/bin/python -m app.report --md --group Axia
```

**From the running server** (same data, cached):

```bash
curl -s 'http://127.0.0.1:8799/api/findings' | jq                       # JSON
curl -s 'http://127.0.0.1:8799/api/findings?format=md'                  # markdown
curl -s 'http://127.0.0.1:8799/api/findings?severity=suspected_drop&min_net=5'
```

Each **finding** is one `(account, contract)` whose end-of-day net looks wrong, carrying
`severity` (`suspected_drop` | `orphan` | `open_unverifiable`), `group` / `trader` /
`account` / `contract` / `platform` / `is_sim` / `opt_out`, `current_net`, `open_since`
(switch-on day), `last_flat_day`, `days_open`, `last_fill`, the live `tt` verdict
(`{tt_net, in_tt, …}`), and an **`investigate`** block: the ready-to-run **tt-diff**
command, the **SQL** to pull the contract's recent fills, the **Stellar source** query,
the **recalc** follow-up, and a `hint` naming the most likely root cause. The response
also carries a top-level **`playbook`** (known failure modes + the recover→recalc steps).

The agent loop: pull findings → run a flagged contract's `tt-diff` to get the exact
missing fill(s) → confirm against `ttledger` / `raw_fills_fix` → recover + `recalc_trader.py`
**in `aws-mwaa-local-runner`, never from here** (this tool is strictly read-only).
