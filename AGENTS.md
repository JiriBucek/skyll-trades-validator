# AGENTS.md — skyll-trades-validator

Read-only integrity dashboard for the Skyll fills→trades→intraday→daily pipeline. See [README.md](README.md) and [docs/DESIGN.md](docs/DESIGN.md).

## Golden rule: READ-ONLY
This tool must **never** write to the production database. It connects with `PROD_DATABASE_CONNECTION_STRING_READONLY` and forces `SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY` / `conn.set_session(readonly=True)`. Do not add write paths, do not use the `_WRITE` connection string, do not call mutating TT endpoints. If you need to fix data, that belongs in the `aws-mwaa-local-runner` tools (`recalc_trader.py`, `inject_correction_fill.py`), not here.

## Secrets
Secrets come from the **`skyll-mwaa`** keychain repo via `secretctl` (see the hive `secrets-management-keychain` workflow). Launch everything through it:

```bash
secretctl run skyll-mwaa -- ./backend/venv/bin/uvicorn app.api:app --port 8799
```

Required env (all keychain-backed in `skyll-mwaa`):
- `PROD_DATABASE_CONNECTION_STRING_READONLY` — Timescale Cloud, `sslmode=require`.
- `APP_SECRET` (TT live, env `ext_prod_live`) and `SIM_APP_SECRET` (TT sim, env `ext_prod_sim`).

The keychain must be unlocked first (`secretctl unlock`) — interactive, run by the user; agents cannot unlock.

## If you're an agent investigating a problem — start here
Don't read the UI. Pull structured findings:

```bash
make report-md                              # health header + drop-day rollup + everything wrong
make report ARGS="--severity drop,extra_misattr,stranded --min-net 5"   # JSON, filtered
# or, if the server is up:
curl -s 'http://127.0.0.1:8799/api/findings?format=md'
curl -s 'http://127.0.0.1:8799/api/findings' | jq '.findings[] | select(.severity=="drop")'
```

The report leads with a one-line **health header** (`N drop windows · A mis-attributed · S stranded`)
and the **drops-by-ingestion-day** rollup. Each finding is one `(account, contract)` with a `severity`
(`drop` | `extra_misattr` | `stranded` | `unreconciled` | `orphan`), a **`fix`** block (FIX-feed nets +
the missing/extra fills with `uniqueExecId`), and an `investigate` block (ready-to-run `/api/raw-diff`
+ `raw_diff_ts` commands, the recover→recalc chain, a `hint`). Workflow: pull findings → run the
contract's **`/api/raw-diff`** (the authoritative FIX-feed diff) → recover + recalc **in
`aws-mwaa-local-runner`, never here** (drop = reingest→recalc; stranded/orphan = recalc only; extra =
orphan it off the book). Implemented in `backend/app/report.py` + `fixfeed.py`.

## Conventions matched from the ecosystem
- Day boundary is **UTC**, matching the daily-candle rollup and continuous aggregates. The FIX
  cross-check judges **as of the last completed UTC day** (today's in-flight fills excluded).
- platforms: `1=TT`, `2=Stellar`. Cohort = traders with a `group_members` row (group membership is
  the only filter — the client removes genuine spread traders from the group upstream; no hard-coded list).
- Cross-check = the FIX feed `raw_fills_fix` (`I_TT` for TT, `I_STELLAR` for Stellar), the authoritative
  per-account copy. The TT `ttmonitor/position` endpoint is **not used** (it ignores `accountId`).
- Account match is label-robust: REST `LFCTEU150_MA` ↔ FIX `LFCTEU150` / `&` / `:` forms (canonicalised).
- TT `ttledger/{env}/fills` (secondary drill-down) caps at 500/call — paginate on `minTimestamp = last+1`.
- `/api/fills?account=&contract=` = the fill-history drill-down (newest-first, with a chronological
  running position + linked/trader flags); the UI reaches it via the hash route `#/fills?...` (clicking
  a contract name). Backend = `engine.fills_history`.
- The read-only DSN is a **hot standby**; heavy reads retry on `conflict with recovery`.
