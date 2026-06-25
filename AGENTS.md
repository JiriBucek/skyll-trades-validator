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

## Conventions matched from the ecosystem
- Day boundary is **UTC**, matching the daily-candle rollup and continuous aggregates.
- platforms: `1=TT`, `2=Stellar`. Cohort = traders with a `group_members` row.
- TT `ttmonitor/{env}/position` ignores `accountId` — filter rows client-side.
- TT `ttledger/{env}/fills` caps at 500/call — paginate on `minTimestamp = last.timeStamp + 1`.
