# Design — skyll-trades-validator

Full rationale behind the read-only integrity dashboard. See [../README.md](../README.md) for the quick tour.

## Problem

Skyll ingests **fills** from TT (via the TT REST API) and Stellar (via FIX into `raw_fills_fix`), aggregates them into **trades**, then computes **intraday** MTM candles and **daily** candles. The chain is only as good as the fills. When a fill is silently dropped, the derived position for a `(account, contract)` never returns to zero, the trade walk mis-bounds into a fake "mega-trade", and the trader's P&L is wrong — usually discovered only when a client complains.

Known fill-loss mechanisms (all observed in production):
- **Microsecond PK collision** — the `fills` primary key is the 6-col natural key `(trader_id, platform_id, contract, price, quantity, timestamp)`; TT nanosecond timestamps are rounded to microsecond on ingest, so two genuinely-distinct same-price/qty fills in the same µs collide and the second is dropped. Hits high-volume traders hardest (Demetris).
- **Un-paginated TT reads** — `ttledger/fills` caps at 500/call; the ingestion's `_retrieve_fills` doesn't paginate, so busy windows silently truncate.
- **Skipped clearing-alias fills** — Stellar fills under an unresolved clearing alias are left unprocessed.
- **Cash-settled expiry** — *rarely*, a position genuinely held to a cash-settled future's expiry has no closing fill, so it stays open. Note this is NOT the system "closing/settling" the contract — there is no expiry logic anywhere; we only ever aggregate the fills ledger. The far more common reason a non-flat old contract exists is simply a **lost fill** we haven't chased.

This tool makes all of that visible at a glance, and lets you confirm a suspected drop against TT in two clicks. The model it checks is the whole Skyll model: **fills → trades → profit**, and the health test is whether the ledger **aggregates to flat** (see `aws-mwaa-local-runner/dags/misc/recovery/PRINCIPLES.md`).

## What "healthy" means

For each `(platform_account, contract)`:
1. **Net position = 0** at end of day (signed cumulative fills). Flat is the common healthy state.
2. **Every fill assigned to a trade** (`fills.trade_ids` non-empty) on completed days.
3. **Daily candle close ≈ realized P&L** of trades closed that day (secondary; fuzzy by design).

## Day model

Day boundaries are **UTC calendar days**, chosen to match the rest of Skyll:
- the daily rollup keys on `func.date(intraday.datetime)` evaluated in UTC and bounds days with `pendulum...start_of('day')` in UTC;
- the `daily/weekly/monthly_product_profit` continuous aggregates use `time_bucket('1 day', open_time)` (UTC).

This means EOD net for day `D` is the cumulative signed fill quantity through `D 23:59:59 UTC`. A futures session that crosses UTC midnight can make a same-session hold look like an overnight carry, but matching the existing convention keeps the dashboard consistent with every other Skyll P&L surface.

## Engine

Three read-only queries over the grouped-trader account set (`group_members → traders → trader_platforms`):
1. **`net_all`** — all-history signed-sum + first/last fill per `(account, contract)`. Gives current net and lets us classify expired residuals. (~10s; cached.)
2. **`window`** — per `(account, contract, UTC-day)` signed delta, fill count, and unassigned-fill count, for the window. (<1s.)
3. **`realized` / `candles`** — per `(account, day)` realized P&L (with a cross-day-trade count) and the daily candle close, for reconciliation.

EOD net per day is reconstructed as `current_net − Σ(later window deltas)` walked backwards, so the expensive all-history scan runs once and the per-day work is cheap.

**Active vs residual (display triage only).** A `(account, contract)` enters the timeline if it traded in the window, or is currently non-flat and not past expiry. A non-flat contract that is **past expiry** with no recent fills is bucketed into a collapsed "residual" section so the ~1,800 dead 2024 contracts don't drown the signal. This is **purely a display filter** — calling them "settled" is shorthand for "old, non-flat, not currently being chased"; they are still just non-flat ledgers (almost always pre-retention lost fills), not anything the system settled.

**Switch-on day.** The first day of the current trailing non-zero run = "the last day this was flat, plus one". If every window day is non-flat, it reads `before_window`.

**Orphans** count only on completed days (`day < today UTC`); a fill ingested today may simply not have been aggregated yet.

## FIX-feed cross-check (`fixfeed.py`) — the authoritative drop detector

The TT *position* endpoint (`ttmonitor/.../position`) that the first version used is **gone**: it
ignores the `accountId` filter, so it cross-nets accounts and produces both false positives and
false negatives. The source of truth is the **FIX feed `raw_fills_fix`** — an independent in-DB
copy of every fill (platform `I_TT` for TT accounts, `I_STELLAR` for Stellar). `fixfeed.enrich`
resolves a verdict for every non-flat `(account, contract)`:

1. **Cutoff.** The comparison is **as of the last completed UTC day** (today's in-flight fills are
   subtracted from both sides). The FIX feed is real-time push; our `fills` is batch-ingested, so on
   the actively-trading front month the feed legitimately *leads* us intraday — comparing all-history
   nets would read that lag as a flood of false drops. Judging EOD is the same flat-test boundary the
   model already uses.
2. **Label-robust canonical accounts.** REST labels accounts `LFCTEU150_MA`; FIX uses `LFCTEU150` /
   `&LFCTEU150` / `LFCTEU150:…`. We canonicalise both sides (strip `&`, a `:suffix`, and a sub-account
   tag `_MA`/`_AL`/`_JPX`/…) and aggregate at the `(canonical_account, symbol, maturity, platform)`
   grain — sub-accounts belong to the same trader, so they net together.
3. **Net classify (cheap, batched).** Three big GROUP BYs (raw FIX net per canonical key; our
   pre-retention carry; our net) feed a per-key verdict: `our_ret == FIX` → `confirmed_open` (or
   `flat`); pre-retention carry → `partial_carry`; no FIX rows → `unverifiable`; otherwise **divergent**.
4. **Per-fill discriminate (only the divergent few).** For each divergent key, pull the fills and run
   the `raw_diff_ts` matchers (count-excess / per-second / cumulative, gated on the recovered net) in
   **both directions**: FIX-legs-missing-from-us net dominates → `drop` (+ the exact fills & ingestion
   day); our-legs-missing-from-FIX net dominates → `extra_misattr`; neither reconciles → `unreconciled`.

Drops are then rolled up by **ingestion day** (the missing fill's UTC date) so a systemic gap that hit
many accounts at once (`2026-06-11 17:30`, the April/May windows) is one collapsible row.

**Drill-down.** `/api/raw-diff?account=&contract=` runs the same diff on demand (including today) and
returns the exact missing-from-us / extra-in-us fills with `uniqueExecId` — reingest-ready. The TT
*ledger* diff (`/api/tt-diff`) remains as a secondary cross-check that reaches a hair past the FIX wall.

## Stranding (`STRANDED_ALL_SQL`)

Separate from the net check: a real cohort account whose **futures** fills aggregated under
`trader_id` 0 (Unassigned) / 349 (IgnoredAccounts) and were never linked to a trade (`trade_ids` NULL)
— the account wasn't in `trader_platforms` when its fills ingested (the Josh-Gadenne Euribor class).
This is all-history (independent of the window), forces the contract into the active view, and is
🔴 `stranded` (fix = `recalc_trader`, **no backfill**). Option strikes / synthetic markers are out of
scope (the model is futures), so option dust never shows.

## Classification → colours

`flat < settled_residual < partial_carry < confirmed_open < pending_fix ≈ unverifiable < orphan
< unreconciled < extra_misattr < stranded < drop`

Only **drop / extra_misattr / stranded** are 🔴 actionable. A trader's day cell = the worst of its
contracts that day; a group's = the worst of its traders. The current open run of a non-flat contract
paints its trailing cells with the verdict colour, so a red streak from the switch-on day to today is
immediately legible.

## Deliberate non-goals / caveats

- **Not real-time.** Manual refresh; results cached 5 min. The all-history net scan + the FIX-feed GROUP BY are ~20–30s on a cold cache; the divergent per-fill diffs run only on the handful of net-divergent contracts.
- **Judged EOD, not intraday.** The FIX cross-check is as of the last completed UTC day, so a position opened *today* is never judged (today's in-flight is excluded) — it resolves tomorrow once the day is complete.
- **Retention wall.** The FIX feed starts ~2026-03-30; a position opened before it is `unverifiable` / `partial_carry`, not a bug. The read-only DSN is a hot standby, so heavy reads retry on the occasional `conflict with recovery`.
- **Reconciliation is informational.** Daily-candle-vs-realized deltas are flagged only when not explained by cross-day trades; other known divergences (thin-contract intraday drops, the historical same-minute daily-close collision) can still show — treat it as a hint, not a verdict.
- **Rollup heatmap includes all accounts;** the sim/opt-out UI filters affect the detail rows, not the rolled-up strip.
