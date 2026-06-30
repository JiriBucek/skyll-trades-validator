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
| `mismatch` | 🔴 red | our fills gross ≠ `raw_fills_fix` on a completed day → a **dropped fill** | `GET /api/raw-diff` for the exact fills (uniqueExecId) → reingest → `recalc_trader` |
| `skipped` | 🟣 purple | fills in the ledger with empty `trade_ids`, never aggregated (a later fill *is* in a trade) | `recalc_trader` re-walks them into trades. `closes_to_zero` ⇒ all fills counted net ~flat → recalc lands it flat |
| `unverifiable` | grey | sustained open, no FIX rows (option / give-up / alias / pre-retention) | check `/api/fills` or the source platform; can't confirm from the feed |
| `open` | 🟡 yellow | sustained open the FIX feed confirms (feeds agree) | likely a genuine hold; watch |

Key derived field: **`closes_to_zero`** — the contract has skipped fills AND, counting **all** fills (incl. the skipped ones), nets ~flat, so re-aggregating (`recalc_trader`) re-walks the skips into trades and it lands flat (the recalc-able batch). (`net_ex_skips = current_net − skipped_lots` is the assigned-fills net; ~0 while still open ⇒ a genuine open the skips don't explain.)

Workflow: pull findings → `mismatch`: run the contract's **`/api/raw-diff`** (or `raw_diff_ts.py`) for the missing fills → reingest → recalc; `skipped`: `recalc_trader` only — **all writes in `aws-mwaa-local-runner`, never here.** Implemented in `backend/app/report.py`, `engine.py`, `fixfeed.py`.

## Cleanup worklist (the "closes to zero" batch)
The recalc-able batch is the UI **`closes to zero`** contracts: a contract with **skipped fills** where, counting **all** fills (assigned + the skipped ones), the ledger nets ~flat. Re-aggregating (`recalc_trader`) re-walks every fill — including the skipped ones — into proper trades; because the ledger already balances, the **net=0 preflight passes** and the contract lands flat with the trades/PnL corrected. Filter to them in the UI with the **only closes to zero** toggle. Generate / regenerate:
```bash
make worklist        # -> docs/worklist-skipped-recalc.md  (per trader, most skipped fills first)
```
Each contract follows the 10-step pipeline in `aws-mwaa-local-runner/.../recovery/RECOVERY.md` (tags backup → `recalc_trader` → tags remap → `intraday.py intraday`/`daily` → `caggs.py` → verify). Tick the box and append to `recovery/ledger.jsonl`. **recalc only, no backfill.** A few rows are flagged `recalc-net ≠ 0` (full ledger flat but the eligible subset — price>0, Outright, non-ALGO — isn't); dry-run first, they may still abort. **NOT in this batch:** contracts non-zero with everything counted — those are genuine opens (or pre-retention carries) and `recalc_trader` aborts on net≠0 (e.g. LJ4AX017 / I Jun27, +4).

## Conventions
- **Day boundary = UTC**, matching the daily-candle rollup. Cross-checks judge **as of the last completed UTC day** (today's in-flight excluded).
- platforms: `1=TT`, `2=Stellar`. Cohort = traders with a `group_members` row.
- **Spreads are detected from the data** (`engine.detect_spread_keys`): a trader net long one futures month of a product and net short another. Excluded from the aggregated timeline + counts (faded). `Config.SPREAD_PRODUCTS` is an optional manual override. There is **no** TT `ttmonitor/position` use (it ignores `accountId`).
- Cross-check feed = `raw_fills_fix` (`I_TT` for TT, `I_STELLAR` for Stellar). Compared on **gross volume per day** (robust to block-vs-leg); no FIX rows ⇒ `unverifiable`, never red.
- Account match is label-robust: REST `LFCTEU150_MA` ↔ FIX `LFCTEU150` / `&` / `:` forms (canonicalised); sub-accounts net together.
- `/api/fills?account=&contract=` = fill-history drill-down (newest-first, running position, linked/trader flags), matched on the canonical account. `/api/raw-diff` = on-demand FIX diff (reingest-ready).
- The read-only DSN is a **hot standby**; heavy reads retry on `conflict with recovery`. Cold compute is ~30–60s (all-history net + skipped-fills + FIX scans); cached 5 min.
