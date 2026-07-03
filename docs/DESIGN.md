# Design — skyll-trades-validator

Rationale and internals behind the read-only integrity dashboard. See [../README.md](../README.md) for the tour and [../AGENTS.md](../AGENTS.md) for the agent entry point.

## Problem

Skyll ingests **fills** from TT (REST) and Stellar (FIX → `raw_fills_fix`), aggregates them into **trades**, then computes intraday MTM and daily candles. The chain is only as good as the fills *and* the aggregation. Two distinct failures both make a `(account, contract)` "not add up":

1. **A fill is missing from `fills`** (dropped on ingest). The position never returns to zero → a phantom open; the trade walk mis-bounds. Mechanisms seen in prod: microsecond PK collision (nanosecond TT timestamps rounded to µs collide on the 6-col natural key), un-paginated TT reads (`ttledger/fills` caps at 500/call), watermark/out-of-order reads, skipped clearing-alias Stellar fills.
2. **A fill is present but never aggregated into a trade** (`trade_ids` empty). The aggregator built trades, **skipped** some fills, and continued. The fills net is right but the trades are short — the P&L is wrong even though no fill was lost.

There is **no expiry/settlement logic anywhere** — we only ever aggregate the fills ledger. A non-flat old contract is a lost or skipped fill, not "it expired" (see `aws-mwaa-local-runner/recovery/PRINCIPLES.md`). This tool makes both failure classes visible at a glance and, for drops, lets you confirm against the FIX feed in one click.

## What "healthy" means

For each `(platform_account, contract)`, per UTC day:
1. **Net position ≈ 0** at end of day (signed cumulative fills). Flat is the common healthy state.
2. **Every fill aggregated into a trade** (`trade_ids` non-empty).
3. Our fills' **gross volume == the FIX feed**'s for that day (no dropped fill).

## Day model

UTC calendar days, to match the rest of Skyll (the daily rollup keys on `func.date(datetime)` in UTC; the continuous aggregates `time_bucket('1 day', open_time)`). EOD net for day `D` = cumulative signed fill qty through `D 23:59:59 UTC`. The net includes **all** fills (assigned *and* skipped), so a skipped-fill open is visible in the timeline.

Four day-states, by priority `mismatch > skipped > open > flat`:
- 🟢 `flat` — |EOD net| ≤ `FLAT_EPS`.
- 🟡 `open` — non-flat at EOD.
- 🟣 `skipped` — ≥1 skipped **aggregation-eligible** fill that day (in the ledger, never aggregated;
  eligibility = `ELIGIBLE_PRED`, mirrors create_trades: `fill_type='Outright' AND exchange<>'ALGO'
  AND price>0`). Ineligible unassigned fills (Leg/'' awaiting the gate-open, ALGO-market echoes,
  price≤0) are **`excluded`** instead — per-day `excluded`/`excluded_lots` on the cell and
  `excluded_count/lots/algo/legs` on the contract — visible, labeled, never purple.
  `exchange='ALGO'` rows (TT synthetic-parent echoes of real child fills, same second/qty/price
  but different `uniqueExecId` — verified 2026-07-03) are NON-ECONOMIC and excluded from every
  net/gross/activity sum too; `/api/fills` lists them flagged `excluded` with Δ=0.
- 🔴 `mismatch` — completed day where our fills gross ≠ `raw_fills_fix` gross (a dropped fill).

## Engine (`engine.py`)

Read-only queries over the grouped-trader account set (`group_members → traders → trader_platforms`):
1. **`NET_ALL`** — all-history signed sum + first/last fill per `(account, contract)`. Current net + spread detection. (Heaviest; scans all fills.)
2. **`WINDOW`** — per `(account, contract, UTC-day)` signed delta, **gross** (Σ qty), fill count, for the window. EOD net per day is reconstructed as `current_net − Σ(later window deltas)` walked backwards, so the all-history scan runs once and per-day work is cheap.
3. **`SKIPPED`** — per `(account, contract, UTC-day)` count + signed lots of skipped fills, whole history (see below).
4. **`RAW_GROSS_DAY`** (`fixfeed`) — `raw_fills_fix` gross per day for the problem rows only.
5. **`OPEN_LOOKBACK`** — daily deltas over `OPEN_LOOKBACK_DAYS` for the *carried-in* rows only, to date the true open-run start.

**Sustained-open / problem.** The trailing open run = consecutive open EOD days from the most recent backwards. `sustained_open = trailing ≥ PROBLEM_OPEN_DAYS` (drives the number line, for any row incl. spreads). `problem = sustained_open and not spread` (the subset that counts toward health).

**Carried-in + true age.** `opened_before_window = (open every window day) and (opening balance ≠ 0)` — the run began before the window. For those, `_resolve_open_days` looks back `OPEN_LOOKBACK_DAYS` (a single bounded query for the small carried set), cumulates EOD net backward from `current_net`, and finds the last flat day → the true `open_days` (e.g. 203, or `366+` if older than the look-back). Non-carried rows use `trailing` directly.

**Skipped fills.** `SKIPPED_SQL`: a fill with empty `trade_ids` (`NULL` / `'[]'` / `''`) that has a **later** assigned fill on the same `(account, contract)` — i.e. the aggregator passed over it (a trailing unassigned fill with nothing assigned after is just *pending*, not skipped). Grouped by UTC day, whole history. Per contract: total `skipped_count` / `skipped_lots` (the end-of-row note), per-day for the purple window cells, **`net_ex_skips = current_net − skipped_lots`** = the assigned-fills (trades) net, and **`closes_to_zero`** = has skips AND `current_net` (all fills counted) is ~flat ⇒ re-aggregating re-walks the skips into trades and it lands flat (the recalc-able batch; `recalc_trader`'s net=0 preflight passes). `net_ex_skips ≈ 0` while `current_net` is non-zero ⇒ a genuine open the skips don't explain (they're already in `current_net`; recalc aborts). **Window gating (strict):** a contract shows only if it had fills in the selected window (`has_window_fills`). Anything that didn't trade in the window is excluded — even a still-open position or a dormant `closes_to_zero` recalc target. The **only closes to zero** toggle filters this windowed set down to `closes_to_zero`; it never pulls in dormant contracts. The whole-history recalc backlog is the worklist (`make worklist`), worked independently of the UI window.

## Spread detection (`detect_spread_keys`)

Spreads are detected from the position data, not curated. A `(trader, product-symbol)` is a spread when, across its **open, non-expired, futures** maturities, the trader is net **long one month and short another**:
- aggregate `NET_ALL` by `(trader, contract)` (sub-accounts net together), futures only (`in_scope`), drop expired maturities;
- per `(trader, symbol)`, sum the positive legs and the negative legs;
- spread iff both sides are non-zero **and** `min(pos,neg)/max(pos,neg) ≥ SPREAD_MIN_BALANCE` (the balance guard rejects a directional book with a 1-lot residual in another month).

Why each guard: **futures-only** stops an option and a future sharing the first token (`I Sep26` vs `I Sep26 C97.5`) from looking like a calendar leg (this previously masked a real drop); **expired-ignored** stops ancient offsetting residuals (the FGBS trap); **balance** stops `−227` vs `+1`; **per-trader** catches legs split across a trader's accounts. `Config.SPREAD_PRODUCTS` unions an optional manual override. Spread legs carry net ≠ 0 by design → faded, excluded from the aggregated timeline and the counts. *Caveat:* detection uses `current_net`, which includes skipped fills, so a leg whose net is mostly skips can read as a spread — re-aggregating may collapse it. (Net ≠ 0, so such a leg is **not** `closes_to_zero`.)

## FIX cross-check (`fixfeed.cross_check`) — the dropped-fill detector

The TT *position* endpoint the first version misused is no longer part of THIS check (it ignores `accountId`, so naive per-account querying cross-netted accounts; it returned properly as the bulk-snapshot `ttpos.py` check below). The dropped-fill source of truth is `raw_fills_fix` (`I_TT` for TT, `I_STELLAR` for Stellar). For every **problem** row, per **completed** day, compare our fills' **gross** volume vs the feed at the canonical `(account, symbol, maturity, platform)` grain:
- **gross, not net** — robust to TT block-vs-leg aggregation (a 60-lot block vs `54+6` legs is gross 60 either way), so it won't false-red on feed shape.
- **like-for-like (2026-07-02):** the fills side counts `fill_type='Outright'` only — since the
  leg-ingestion change, `fills` also holds `'Leg'` and `''`-typed (order-management) fills, which
  the drop-copy can never contain (our QuickFIX filter drops 442∈{2,3}; BornTech excludes TT algo
  fills). The raw side excludes **option series riding under the future's (symbol, maturity)**:
  `security_desc` matching `… SI <yyyymmdd> CS|PS` (Eurex option series, e.g. `FGBL SI 20260608
  PS`) or containing `_OM` (ICE options, e.g. `I FMU0026_OMCA…`) — verified to carry the underlying
  future's symbol+maturity and previously false-redding STIR/Eurex books.
- differ by > `GROSS_TOL` ⇒ `mismatch` (red) on that day, and `has_mismatch` on the contract —
  **unless** the fills-over-FIX surplus is fully explained by late-inserted fills (`created_at −
  timestamp > 3 days` = recovery backfills): then the day is marked `backfilled` (informational,
  never red — the feed cannot contain what recovery inserted after the fact).
- **no FIX rows at all** for the key ⇒ `unverifiable` (the account/product isn't in the feed under that name — give-up / clearing-alias / option / pre-retention) — never red.
- **today excluded** — the feed is real-time push, our `fills` is batch-ingested, so today's lag would masquerade as a drop.

Account match is label-robust (canonicalise `&` / `:suffix` / `_MA`/`_AL`/… → base account); sub-accounts net together. Drill-down `account_diff` (`/api/raw-diff`) pulls the exact missing/extra fills with `uniqueExecId` via the `raw_diff_ts` matchers (count-excess / per-second / cumulative), reingest-ready.

## TT position check (`ttpos.py`) — the platform's own book as a third feed

The FIX cross-check compares two of OUR copies of the fills; the TT check brings in the one number we don't produce ourselves — TT's live `netPosition` per (account, instrument). On demand (`GET /api/ttpos`, the **TT check** button), every open validator line is joined against ONE bulk `/ttmonitor/{env}/position` pull per env and classified: `match` / `diff` (with `tt_sod` start-of-day net as the ingest-lag-insensitive comparison) / `tt_flat` (no TT row ⇒ the platform thinks flat — the phantom-open detector: missed close on our side, sim position reset, double-booked ledger) / `expired` (TT drops delisted instruments) / `no_api` (Stellar). The reverse join (`tt_only`) surfaces TT opens with no open validator line — possible drops on our side.

Design constraints that shaped it: the position endpoint **ignores `accountId`** — bulk is the only possible access pattern, which makes the whole check ~2 API calls per refresh (snapshot cached `TTPOS_CACHE_TTL`); name resolution runs **id→name** via `ttaccount/account/{id}` (the reliable direction — it sees the give-up/clearing accounts the accounts-list omits), persisted in `backend/.ttpos_cache.json`; **absence = flat** is trustworthy because the endpoint lists idle opens (verified 2026-07-03, BPC_PLEKOVIC). Verified on day one against known ground truth: it reproduced the hand-derived leg-drop reconciliation exactly (LFCTEU154 ZB Sep26 DB −13 vs TT +1; ZN +18; ZF +17 as a TT-only open) and correctly showed nothing for the corrected BPC_ACHEN.

## Aggregated timeline (`assemble_tree`)

The collapsed trader/group strip = worst **non-spread** state per day:
- `mismatch` and `skipped` always color it.
- `open` colors a day only if it's in the contract's **current trailing run** (an open that later closed → resolved → green). A carried-in open that **traded in the window** and is still open colors it (a real current open); one with **no window fills at all** is gated out entirely (strict window gating) and never shows or colors.
- spreads never color it.

A trader's day = worst of its contracts; a group's = worst of its traders, by `flat < open < skipped < mismatch`.

## Findings (`report.py`)

`build_report(tree)` flattens every problem `(account, contract)` into a finding (`mismatch` | `skipped` | `unverifiable` | `open`), most-actionable first, with `net_ex_skips` / `closes_to_zero`, the per-day `mismatch_days`, and an `investigate` hint. Same data as the UI; JSON or markdown; filterable. This is the AI's view — see [../AGENTS.md](../AGENTS.md).

## Non-goals / caveats

- **Not real-time.** Manual refresh, cached 5 min. Cold compute ~30–60s (all-history net + skipped + FIX scans).
- **Judged EOD, not intraday.** Today's in-flight fills are excluded from the cross-checks; a position opened today resolves tomorrow.
- **Retention wall.** `raw_fills_fix` starts ~2026-03-30; no-feed-rows ⇒ `unverifiable`, not a bug.
- **Read-only standby.** Heavy reads retry on `conflict with recovery`. All recovery happens in `aws-mwaa-local-runner`.
- **Skipped-vs-spread coupling.** Because `current_net` includes skipped fills, cleaning skips (re-aggregating) can change what counts as a spread or an open — re-run after a recalc.
