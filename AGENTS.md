# AGENTS.md — skyll-trades-validator

Read-only integrity dashboard for the Skyll fills→trades→profit pipeline. See [README.md](README.md) and [docs/DESIGN.md](docs/DESIGN.md) for the full model.

## Golden rule: READ-ONLY
This tool must **never** write to the production database. It connects with `PROD_DATABASE_CONNECTION_STRING_READONLY` and forces the session read-only. Do not add write paths, do not use the `_WRITE` string, do not call mutating endpoints. If data needs fixing, that belongs in `aws-mwaa-local-runner` (`recalc_trader.py`, reingest, `inject_correction_fill.py`) — never here.

## Secrets
From the **`skyll-mwaa`** keychain via `secretctl`. Launch everything through it:
```bash
secretctl run skyll-mwaa -- ./backend/venv/bin/uvicorn app.api:app --port 8799
```
Required (all keychain-backed): `PROD_DATABASE_CONNECTION_STRING_READONLY` (Timescale Cloud, `sslmode=require`), `APP_SECRET` / `SIM_APP_SECRET` (TT live / sim). The keychain must be unlocked first (`secretctl unlock`) — interactive, by the user; agents cannot unlock.

## If you're an agent investigating — start here
Don't screen-read the UI. Pull structured findings (the same picture the operator sees):
```bash
make report-md                                                  # health + spreads + everything wrong
make report ARGS="--category mismatch,skipped --min-net 5"      # JSON, filtered
# or, server up:
curl -s 'http://127.0.0.1:8799/api/findings?format=md'
curl -s 'http://127.0.0.1:8799/api/findings' | jq '.findings[] | select(.category=="skipped")'
```

The report leads with a one-line **health header** and the **spread books** (excluded). Each finding is one `(account, contract)` with a `category`, most-actionable first:

| category | color | what it means | fix (in aws-mwaa-local-runner) |
|---|---|---|---|
| `mismatch` | 🔴 red | our fills gross ≠ `raw_fills_fix` on a completed day → a **dropped fill**. Compared like-for-like: fills side counts `fill_type='Outright'` only (Leg/''-typed fills are FIX-invisible by design); raw side excludes option series riding under the future's symbol+maturity; a fills-over-FIX surplus explained by recovery backfills marks the day `backfilled`, not red | `GET /api/raw-diff` for the exact fills (uniqueExecId) → reingest → `recalc_trader` |
| `skipped` | 🟣 purple | aggregation-ELIGIBLE fills with empty `trade_ids`, never aggregated (a later fill *is* in a trade). Ineligible unassigned fills (Leg/'' awaiting gate-open, ALGO-market echo artifacts, price≤0) are labeled `excluded_*` instead and never purple; ALGO echoes are also kept out of all nets | `recalc_trader` re-walks them into trades. `closes_to_zero` ⇒ all fills counted net ~flat → recalc lands it flat |
| `unverifiable` | grey | sustained open, no FIX rows (option / give-up / alias / pre-retention) | check `/api/fills` or the source platform; can't confirm from the feed |
| `open` | 🟡 yellow | sustained open the FIX feed confirms (feeds agree) | likely a genuine hold; watch |

Key derived field: **`closes_to_zero`** — the contract has skipped fills AND, counting **all** fills (incl. the skipped ones), nets ~flat, so re-aggregating (`recalc_trader`) re-walks the skips into trades and it lands flat (the recalc-able batch). (`net_ex_skips = current_net − skipped_lots` is the assigned-fills net; ~0 while still open ⇒ a genuine open the skips don't explain.)

**Scope:** findings are **window-scoped** — the same set the UI shows (a contract appears only if it traded in the window; default 30d, `--window N` to widen). The whole-history **recalc backlog** — every `closes_to_zero` contract, dormant or not — is the **worklist** (next section), not the report. Use the report to triage what the operator sees; use the worklist to work recoveries.

Workflow: pull findings → `mismatch`: run the contract's **`/api/raw-diff`** (or `raw_diff_ts.py`) for the missing fills → reingest → recalc; `skipped`: `recalc_trader` only — **all writes in `aws-mwaa-local-runner`, never here.** Implemented in `backend/app/report.py`, `engine.py`, `fixfeed.py`.

## TT position check (`/api/ttpos`) — what does the PLATFORM say about our opens?
```bash
curl -s 'http://127.0.0.1:8799/api/ttpos?window=30' | jq '{counts, errors, diff: [.rows[] | select(.status=="diff" or .status=="tt_flat")], tt_only}'
```
For every **open** validator line, `backend/app/ttpos.py` asks TT's live position book (`GET /ttmonitor/{env}/position`) and returns a verdict: `match` (TT agrees) · `diff` (TT shows a different nonzero net) · `tt_flat` (**TT has NO row ⇒ thinks flat** — the phantom-open detector: missed closing fill on our side / sim position reset / double-booked TT ledger) · `expired` (TT drops delisted instruments — comparison meaningless, this is the expiry-carry class) · `no_api` (Stellar). Plus **`tt_only`**: TT opens with no open validator line = possible drop on OUR side. Reads on TT only — still zero DB writes.

Design facts (hard-won, don't re-derive): the position endpoint **ignores `accountId`** → ONE bulk paginated pull per env (live+sim) covers every line, so the whole check is a handful of API calls (cached `TTPOS_CACHE_TTL`, default 120s). Name resolution runs in the **reverse** (reliable) direction — each accountId holding a position → `GET /ttaccount/{env}/account/{id}` (sees give-up accounts the accounts-list omits), persisted in `backend/.ttpos_cache.json` (first-ever run warms it, ~1 min). **Absence = flat** is trustworthy: the endpoint lists idle opens (verified 2026-07-03, BPC_PLEKOVIC −3). Interpretation caveat: TT is **live**, our fills batch-ingest (~15 min lag) — a `diff` on a contract trading right now can be benign; `tt_sod` (start-of-day net) is the lag-insensitive number.

## Cleanup worklist (the "closes to zero" batch)
The recalc-able batch is the UI **`closes to zero`** contracts: a contract with **skipped fills** where, counting **all** fills (assigned + the skipped ones), the ledger nets ~flat. Re-aggregating (`recalc_trader`) re-walks every fill — including the skipped ones — into proper trades; because the ledger already balances, the **net=0 preflight passes** and the contract lands flat with the trades/PnL corrected. Filter to them in the UI with the **only closes to zero** toggle. Generate / regenerate:
```bash
make worklist        # -> docs/worklist-skipped-recalc.md  (per trader, most skipped fills first)
```
Each contract follows the 10-step pipeline in `aws-mwaa-local-runner/.../recovery/RECOVERY.md` (tags backup → `recalc_trader` → tags remap → `intraday.py intraday`/`daily` → `caggs.py` → verify). Tick the box and append to `recovery/ledger.jsonl`. **recalc only, no backfill.** A few rows are flagged `recalc-net ≠ 0` (full ledger flat but the eligible subset — price>0, Outright, non-ALGO — isn't); dry-run first, they may still abort. **NOT in this batch:** contracts non-zero with everything counted — those are genuine opens (or pre-retention carries) and `recalc_trader` aborts on net≠0 (e.g. LJ4AX017 / I Jun27, +4).

## Conventions
- **Day boundary = UTC**, matching the daily-candle rollup. Cross-checks judge **as of the last completed UTC day** (today's in-flight excluded).
- platforms: `1=TT`, `2=Stellar`. Cohort = traders with a `group_members` row.
- **Spreads are detected from the data** by THREE complementary rules, UNIONed (`engine.detect_spread_keys` ∪ `..._by_activity` ∪ `..._held_legs`): (1) **net** — a trader net long one futures month of a product and net short another; (2) **activity overlap** — across the days a trader traded a product, on ≥`SPREAD_OVERLAP_FRACTION` (default 0.5) of them they traded 2+ maturities the same UTC day (≥`SPREAD_MIN_OVERLAP_DAYS`), over `SPREAD_ACTIVITY_LOOKBACK_DAYS`; (3) **held legs** — the trader holds ≥`SPREAD_MIN_OPEN_LEGS` (default 2) distinct contracts of the product **simultaneously sustained-open** (held, never closing). Rule 3 alone counts ALL contract types incl. **options** (e.g. Jake/I Sep26+Dec26, Emanuel's 10 OGBL strikes, a future+option pair) and catches **same-sign** held curves that rules 1+2 (futures-only, opposing-sign / same-day-overlap) miss. Spread legs are **excluded from the aggregated timeline + every health count** (open/mismatch AND skipped fills — a contract we don't support never inflates the headline), and faded. `Config.SPREAD_PRODUCTS` is an optional manual override. NB: the agent **findings report** (`report.py`) still lists mismatch + skipped fills on spread legs (recovery cares about them regardless — so a dropped fill on a spread book is hidden from the UI headline but never lost).
- Cross-check feed = `raw_fills_fix` (`I_TT` for TT, `I_STELLAR` for Stellar). Compared on **gross volume per day** (robust to block-vs-leg); no FIX rows ⇒ `unverifiable`, never red.
- Account match is label-robust: REST `LFCTEU150_MA` ↔ FIX `LFCTEU150` / `&` / `:` forms (canonicalised); sub-accounts net together.
- `/api/fills?account=&contract=` = fill-history drill-down (newest-first, running position, linked/trader flags), matched on the canonical account. `/api/raw-diff` = on-demand FIX diff (reingest-ready).
- The read-only DSN is a **hot standby**; heavy reads retry on `conflict with recovery`. Cold compute is ~30–60s (all-history net + skipped-fills + FIX scans); cached 5 min.
