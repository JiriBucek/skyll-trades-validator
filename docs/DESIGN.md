# Design — skyll-trades-validator

Full rationale behind the read-only integrity dashboard. See [../README.md](../README.md) for the quick tour.

## Problem

Skyll ingests **fills** from TT (via the TT REST API) and Stellar (via FIX into `raw_fills_fix`), aggregates them into **trades**, then computes **intraday** MTM candles and **daily** candles. The chain is only as good as the fills. When a fill is silently dropped, the derived position for a `(account, contract)` never returns to zero, the trade walk mis-bounds into a fake "mega-trade", and the trader's P&L is wrong — usually discovered only when a client complains.

Known fill-loss mechanisms (all observed in production):
- **Microsecond PK collision** — the `fills` primary key is the 6-col natural key `(trader_id, platform_id, contract, price, quantity, timestamp)`; TT nanosecond timestamps are rounded to microsecond on ingest, so two genuinely-distinct same-price/qty fills in the same µs collide and the second is dropped. Hits high-volume traders hardest (Demetris).
- **Un-paginated TT reads** — `ttledger/fills` caps at 500/call; the ingestion's `_retrieve_fills` doesn't paginate, so busy windows silently truncate.
- **Skipped clearing-alias fills** — Stellar fills under an unresolved clearing alias are left unprocessed.
- **Cash-settled expiry** — a position carried to a cash-settled future's expiry has no closing fill, so it shows open forever (this one is *expected*, not a data loss).

This tool makes all of that visible at a glance, and lets you confirm a suspected drop against TT in two clicks.

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

**Active vs residual.** A `(account, contract)` enters the timeline if it traded in the window, or is currently non-flat and not past expiry. A non-flat contract that is **past expiry** with no recent fills is a settled residual — there are ~1,800 of these (mostly dead 2024 contracts) and they are bucketed into a collapsed "settled" section so they don't drown the signal.

**Switch-on day.** The first day of the current trailing non-zero run = "the last day this was flat, plus one". If every window day is non-flat, it reads `before_window`.

**Orphans** count only on completed days (`day < today UTC`); a fill ingested today may simply not have been aggregated yet.

## TT cross-check

1. **Position now.** `ttmonitor/{env}/position` (live + sim) returns every position the app key can see in one paginated sweep. We resolve `accountId → name` (one `ttuser/.../accounts` call per env) and `instrumentId → alias` (= our `contract`, cached to disk). For each currently-open TT contract:
   - account **not reported** by TT → `open_unverifiable` (we can't claim a drop);
   - TT flat **and** position carried since before today → `suspected_drop` (the alert);
   - TT flat but opened **today** → `open_unverifiable` (likely ingestion lag, not a confirmed drop);
   - TT same side, ≥ our size → `open_confirmed` (a real hold);
   - TT smaller / opposite → `suspected_drop` with the gap noted.

   Stellar accounts have no position API → always `open_unverifiable`.

2. **Fills diff (on demand).** For a flagged TT contract, `ttledger/{env}/fills` is paginated (500/call, `minTimestamp = last + 1`) and diffed against our DB fills on `(µs timestamp, side, qty)`. TT fills missing from our DB are the dropped fills — the same procedure used to recover Adam Burt's and Demetris's fills by hand. We surface `uniqueExecId` (the dedup key the pending PK-migration will adopt).

## Classification → colours

`flat < settled_residual < open_confirmed < open_pending_tt < open_unverifiable < orphan < suspected_drop`

A trader's day cell = the worst of its contracts that day; a group's = the worst of its traders. The current open run of a suspected-drop / confirmed / unverifiable contract paints its trailing cells with that verdict colour, so a red streak from the switch-on day to today is immediately legible.

## Deliberate non-goals / caveats

- **Not real-time.** Manual refresh; results cached 5 min. The all-history scan + TT sweep is ~15–20s on a cold cache.
- **Position TT-check is "now", not historical.** It can confirm/deny a *currently* open contract, not what a position was at some past EOD. Use the fills diff for historical certainty.
- **Reconciliation is informational.** Daily-candle-vs-realized deltas are flagged only when not explained by cross-day trades; other known divergences (thin-contract intraday drops, the historical same-minute daily-close collision) can still show — treat it as a hint, not a verdict.
- **Rollup heatmap includes all accounts;** the sim/opt-out UI filters affect the detail rows, not the rolled-up strip.
