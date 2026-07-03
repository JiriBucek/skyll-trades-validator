# skyll-trades-validator

**Read-only dashboard that checks the integrity of the Skyll fills → trades → profit pipeline, per group / trader / contract, over a rolling window.** It surfaces the failure classes that have bitten us repeatedly — **dropped fills**, **skipped fills**, **phantom opens** — and tells you, at a glance, where the ledger doesn't add up.

The model it checks is the whole Skyll model: **fills → trades → profit**. A `(account, contract)` is healthy when its fills net to zero (flat) at end of day, and every fill is aggregated into a trade. When that breaks, the position never returns to zero, the trades mis-bound, and the trader's P&L is wrong — usually found only when a client complains. This makes it visible first.

> **Strictly read-only.** Connects only to `PROD_DATABASE_CONNECTION_STRING_READONLY` with the session forced read-only. It never writes. All recovery (reingest, recalc) happens in `aws-mwaa-local-runner`, never here.

---

## The day-by-day model

A collapsible **Group → Trader → contract** tree. Each contract has a **timeline** of the last 30 days (the window). Every day is one of four states:

| Color | State | Meaning |
|---|---|---|
| 🟢 **green** | `flat` | EOD net ≈ 0 — the position closed to zero that day. The healthy case. |
| 🟡 **yellow** | `open` | Non-flat at EOD — an open position. |
| 🟣 **purple** | `skipped` | **Aggregation-eligible** fills that day that are in the ledger but were **never aggregated into a trade** (empty `trade_ids`). Fills the aggregator excludes *by design* (spread legs / ''-typed awaiting the gate-open, ALGO-market echo artifacts, price ≤ 0) are labeled `excluded` instead — visible in the day data and `/api/fills`, never purple; ALGO echoes also don't count in any net. |
| 🔴 **red** | `mismatch` | A completed day where our fills' **gross volume ≠ the FIX feed** for that contract — the feed has fills we lack → a **dropped fill**. Like-for-like: only `fill_type='Outright'` fills are compared (legs/order-mgmt fills never reach the feed), option series under the future's symbol+maturity are excluded from the feed side, and a surplus explained by recovery backfills shows `backfilled` instead of red. |

Priority when a day is several at once: `mismatch` (red) > `skipped` (purple) > `open` (yellow) > `flat` (green).

### Rows: squares vs a line of numbers

- A contract is a **problem** when the position is open at EOD for the **last 3+ trailing days** (a *sustained* open, not a fresh overnight). Tune with `PROBLEM_OPEN_DAYS`.
- **Fine** rows (≤2 trailing opens, or any open that later closed back to green) render as **colored squares**.
- **Problem** rows render as a **line of EOD-net lot numbers** (e.g. `−2 +2 +4`), colored by the same states — so you can read the position day by day and spot where it diverged.

### Only real activity is shown

A cell renders only on a day that had **fills**, a **drop**, or a **skipped fill**. A position carried forward with no activity (e.g. `+30` held untouched for two weeks) shows **blank** on those days instead of smearing `+30` across them. The clutter of carried positions is gone; what's left is real trading days plus the red/purple problem days.

### The end-of-row note

- `open Nd` — the **true age** of the current open run (looks back up to `OPEN_LOOKBACK_DAYS`, default 365), not capped at the 30-day window. `45d`, `203d`, `366+d`.
- `feed mismatch` — at least one red day (likely dropped fill).
- `· no FIX` — a sustained open the feed can't confirm (option / give-up / alias account).
- `N skipped · ±L` — **whole-history** count of skipped fills and **signed** lots (buy +, sell −) = how much the trades are off. Appends **`→ closes to 0`** (and turns green) when, counting **all** fills incl. the skipped ones, the contract nets ~flat (`closes_to_zero`) — re-aggregating re-walks the skips into trades and it lands flat (the recalc-able batch). If it's still non-zero with everything counted it's a genuine open and the chip stays purple.
- `B − S = N` — **only when the row is a problem** (sustained open, skipped, or mismatch): the whole-history **buy lots** (green) − **sell lots** (red) = net position. Shows the buy/sell volume behind the net.
- `spread` — a detected spread/curve leg (see below).

---

## The cross-checks

### FIX feed — dropped fills (`mismatch`, red)

`raw_fills_fix` is an independent in-DB copy of every fill (platform `I_TT` for TT, `I_STELLAR` for Stellar). For every **problem** row, per **completed** day, we compare our fills' **gross volume (Σ qty)** against the feed at the canonical `(account, symbol, maturity)` grain. If they differ by more than `GROSS_TOL`, the day is **red** — the feed has fills we don't, a dropped fill. Gross (not net) is robust to TT block-vs-leg aggregation. Today is never judged (in-flight). If the feed has **no rows at all** for the contract (option / give-up / clearing-alias / pre-retention), it's **unverifiable** (grey, never red) — we can't confirm it. Account match is label-robust (REST `LFCTEU150_MA` ↔ FIX `LFCTEU150` / `&` / `:`), sub-accounts net together.

Drill down: **`GET /api/raw-diff?account=&contract=`** returns the exact missing-from-us / extra-in-us fills with `uniqueExecId` (reingest-ready).

### Skipped fills (`skipped`, purple)

A **skipped fill** is a fill sitting in the ledger with empty `trade_ids` that the aggregator **passed over** — there is a *later* fill on the same contract that *is* in a trade (so it's a genuine middle-skip, not a pending tail). The aggregator built trades, skipped some fills, and continued. The trades then don't add up.

- Computed over the contract's **entire history** (the note total), and colored purple on the affected **window** days.
- **`closes_to_zero`** = the contract has skipped fills **and**, counting **all** fills (assigned + skipped), nets ~flat. Re-aggregating walks the skips into trades and it lands flat — the recalc-able batch (UI filter: **only closes to zero**). The related **`net_ex_skips = current_net − skipped_lots`** is the *assigned-fills* net; ≈ 0 while `current_net` is still non-zero means the contract is a **genuine open** — the skips are already counted in `current_net`, so aggregating them does **not** flatten it and `recalc_trader` would abort (e.g. `LJ4AX017 / I Jun27`: `+4` net, `3` skipped totaling `+4` → still `+4` open).
- Fix = re-aggregate the contract (`recalc_trader`) in `aws-mwaa-local-runner`, so the trades pick the skipped fills up. Only valid when `closes_to_zero` (the ledger balances); a genuine open needs backfill or open-tail handling instead.

### TT position check (`TT check` button, `/api/ttpos`)

The nets above come from **our** fills ledger; the **TT check** asks the platform's own live position book what IT thinks. Click the button and every **open** line gets a badge:

- `TT ✓ +6` — TT agrees the position is open (green).
- `TT +4 ≠ +6` — TT shows a **different** nonzero net (red). Caveat: TT is live, our fills batch-ingest (~15 min lag), so a diff on a contract trading *right now* can be benign; the tooltip carries `tt_sod` (start-of-day net), the lag-insensitive number. Persistent diff on a quiet contract = missing/extra fill on our side (e.g. dropped legs).
- `TT: flat` — TT has **no row** ⇒ thinks the position is flat (red). Absence is a real signal (the endpoint lists idle opens), so this is the **phantom-open detector**: missed closing fill on our side, sim position reset, or a double-booked TT ledger.
- `TT n/a · expired` / `no TT API` — expired contract (TT drops delisted instruments; the expiry-carry class) / Stellar account (no TT API).

The panel above the timeline also lists **TT-only opens** — TT shows an open position but the validator has no open line (flat in our DB or out of window): the reverse detector, a possible drop on **our** side.

Cost: TT's position endpoint ignores account filtering, so ONE bulk paginated pull per env (live + sim) covers every line — ~2 API calls per refresh (cached `TTPOS_CACHE_TTL`, default 120 s). The accountId→name and instrumentId→alias lookups persist in `backend/.ttpos_cache.json`; only the first-ever run pays the warm-up (~1 min). Reads TT only — still zero DB writes.

### Fill history (click a contract name)

Clicking a contract name opens `#/fills?account=&contract=` (`GET /api/fills`): every fill newest-first with a **running position** (signed cumulative qty), per-fill Δ, whether it's linked to a trade (✓ / ○), and the `trader_id`. Matched on the **canonical** account so sub-accounts net together.

- **Collapse days** — a header toggle that folds the fills to **one row per day**, showing the day's net Δ and its **end-of-day position** (where the position landed that day). Click a single day to expand just it; the button flips to **Expand days**. Default is expanded (every fill).
- **Back keeps your place** — returning from a fill detail scrolls the contract you clicked back to the top of the overview (the overview stays mounted, so your expanded groups survive) instead of resetting to the top.

---

## Spread / curve books

Spread traders are **detected from the position data**, not hand-curated (`engine.detect_spread_keys`). A `(trader, product)` is a **spread** when, across the product's **open, non-expired, futures** maturities, the trader is **net long one month and net short another** (opposing signs — e.g. James Pitron `FGBM` `+50` / `−50`). Guards:

- **Futures only** — options/strikes are excluded (an option `I Sep26 C97.5` and the future `I Sep26` share the token "I" but aren't a calendar leg).
- **Balance ≥ `SPREAD_MIN_BALANCE`** (default 0.15) — the smaller side must be ≥ 15% of the larger, so a directional book with a 1-lot residual in another month (`−227` vs `+1`) isn't called a spread.
- **Expired maturities ignored** — ancient offsetting residuals aren't a held spread.
- Detection is **per trader** (legs net across all the trader's accounts).

A spread's legs carry net ≠ 0 by design, so they are **faded** and **excluded from the aggregated timeline and the health counts** — shown only as individual rows when you expand. `Config.SPREAD_PRODUCTS` is an optional manual override (force-label a `(account, "SYM")`). A spread leg that *also* has skipped fills still surfaces (a skipped fill is a real bug regardless).

> Note: because detection uses `current_net` (which includes skipped fills), a leg whose net is mostly *skipped* fills can read as a spread — re-aggregating those skips may collapse the apparent spread. (Such a leg is net ≠ 0, so it is **not** `closes_to_zero`.)

---

## The aggregated timeline (collapsed trader / group)

When a trader/group is collapsed you see one **rolled-up** strip — the worst non-spread state per day. Rules:

- 🔴 **mismatch** and 🟣 **skipped** always color the rollup.
- 🟡 **open** colors a day only if the position is **still open** there (part of the current unresolved run). An open that **later closed back to flat is resolved → stays green** (it's not a current risk).
- A still-open position that **traded in the window** colors it yellow (a real open), with its *true* age shown (`open Nd`) even though it opened before the window. A position with **no fills in the window at all** is gated out entirely (strict window gating — see Core model), so it neither shows nor colors.
- 🟢 **spreads never color the rollup** (excluded), so a pure spreader collapses to green.

So collapsed = a clean read of *current* state: green (flat/resolved/spread), yellow (still-open), purple (skipped fill), red (dropped fill).

---

## Core model & assumptions

- **Cohort:** traders with a `group_members` row (assigned to a group). Includes all their accounts — live, sim, opt-out — with header toggles: **only problems**, **hide sim**, **hide opt-out**, and **only closes to zero** (below).
- **Window-gated view (strict):** the selected window (14/30/60/90d) gates the default view and the counts — a contract shows only if it had fills in the window. Anything that didn't trade in the window is out, including still-open positions and dormant recalc targets. The **only closes to zero** toggle filters this windowed set down to the recalc-able contracts; the whole-history recalc backlog is the worklist (`make worklist`).
- **Grain:** `(platform_account, contract)`. Contracts are **not** rolled up into products — `MES Jun26` and `MES Sep26` are distinct. Sub-accounts (`_MA`/`_AL`/…) net together canonically for the cross-checks.
- **Net position:** signed cumulative fills (buy `+qty`, sell `−qty`); flat = 0. Includes *all* fills (assigned + skipped) — so a skipped-fill open shows in the timeline.
- **Day boundary = UTC calendar day**, matching the daily-candle rollup. EOD net = cumulative net through `day 23:59:59 UTC`.
- **Not real-time / judged EOD.** Manual refresh, cached 5 min; today's in-flight fills are excluded from the cross-checks (resolved tomorrow).
- **Retention wall.** `raw_fills_fix` starts ~2026-03-30 (`FIX_RETENTION_START`); a contract with no feed rows is `unverifiable`, not a bug.

### Tunables (`config.py`)

| Knob | Default | Meaning |
|---|---|---|
| `PROBLEM_OPEN_DAYS` | 3 | trailing open days that make a row a problem (number line) |
| `GROSS_TOL` | 0.5 | per-day gross-volume tolerance (lots) for a `mismatch` |
| `SPREAD_MIN_BALANCE` | 0.15 | smaller opposing side ÷ larger, to count as a spread |
| `OPEN_LOOKBACK_DAYS` | 365 | how far back to find an open run's true start (the `open Nd` age) |
| `WINDOW_DAYS` | 30 | display window |
| `FIX_RETENTION_START` | 2026-03-30 | the FIX feed retention wall |
| `SPREAD_PRODUCTS` | `set()` | optional manual spread override `(account, "SYM")` |

---

## Agent-readable output (for AI agents)

The heatmap is for humans. An AI agent pulls the **same picture** as structured data — no screen-reading — via `backend/app/report.py`.

**Offline (no server):**
```bash
make report-md                                                   # markdown digest
make report ARGS="--category mismatch,skipped --min-net 5 --group Axia"   # JSON, filtered
# direct: --md --category --group --trader --account --min-net --limit --window --no-fix
secretctl run skyll-mwaa -- ./venv/bin/python -m app.report --md
```
**From the running server (cached):**
```bash
curl -s 'http://127.0.0.1:8799/api/findings?format=md'
curl -s 'http://127.0.0.1:8799/api/findings' | jq '.findings[] | select(.category=="skipped")'
```

The report leads with the **health header** and the **spread books** (excluded). Each **finding** is one `(account, contract)` with `category` (`mismatch` | `skipped` | `unverifiable` | `open`), `current_net`, `open_days`, `skipped_count` / `skipped_lots` / `net_ex_skips` / `closes_to_zero`, the per-day `mismatch_days` (`{day, fills_gross, fix_gross, diff}`), and an `investigate` hint. Most-actionable first. The agent loop: pull findings → for a `mismatch`, `GET /api/raw-diff` for the exact missing fills → reingest → `recalc_trader`; for `skipped`, `recalc_trader` re-walks the unaggregated fills into trades — **all in `aws-mwaa-local-runner`, never here.**

Findings are **window-scoped** — the same set the UI shows (a contract appears only if it traded in the window). The whole-history **recalc backlog** — every `closes_to_zero` contract, dormant or not — is **`make worklist`** (`docs/worklist-skipped-recalc.md`), the recovery driver. Use the report to see what the operator sees; use the worklist to work recoveries.

---

## Architecture

```
backend/   FastAPI read-only engine (Python)
  app/
    config.py    env + tunables (window, gross tol, spread balance, open look-back, retention)
    db.py        read-only psycopg2 pool (session forced read-only; retries standby conflicts)
    contracts.py contract-expiry parsing (ignore expired maturities in spread detection)
    engine.py    cohort · EOD net per day · sustained-open/problem · true open age · spread detection
                 · skipped fills · roll-ups · health
    fixfeed.py   the FIX cross-check (cross_check: per-day gross vs raw_fills_fix) + on-demand
                 account_diff (raw_diff_ts matchers, reingest-ready)
    report.py    agent-readable findings (JSON / markdown) — the same picture, no UI
    api.py       FastAPI app + endpoints, in-process result cache
frontend/  React + Vite + Tailwind SPA (the heatmap timeline)
```

## Running

Secrets live in the `skyll-mwaa` keychain (`PROD_DATABASE_CONNECTION_STRING_READONLY`, `APP_SECRET`, `SIM_APP_SECRET`); the keychain must be unlocked (`secretctl unlock`, by you — agents can't).

```bash
make install          # backend venv + frontend node_modules
make up               # build UI + start backend detached + open the browser
make down             # stop it ·  make logs  ·  make smoke  (engine text summary, no server)
```

See [`docs/DESIGN.md`](docs/DESIGN.md) for the rationale and the failure-mode catalogue, and [`AGENTS.md`](AGENTS.md) for the agent entry point.
