# Recalc worklist — “closes to zero” contracts (skipped fills, ledger nets flat)

_Generated 2026-06-30 · `make worklist` to regenerate._

**196 contracts · 30 traders · 17823 skipped fills.** These are the validator's **`closes to zero`** contracts: each has fills that were never aggregated into a trade (skipped), but counting **all** fills — including the skipped ones — the contract nets ~flat. Re-aggregating (`recalc_trader`) re-walks every fill into proper trades; because the ledger already balances, the net=0 preflight passes and the contract lands flat with the trades/PnL corrected. **recalc only, no backfill.**

## Per-contract pipeline (one at a time — full detail in `aws-mwaa-local-runner/dags/misc/recovery/RECOVERY.md`)
0. **Gate**: no live ingestion (weekend / pause `Trading-Orchestrate-Fills-Processing`). If the contract traded in the last ~14d, also pause the 2-hourly intraday/daily DAGs.
1. `tags.py backup --account --contract` (skip if the trader is tag-free).
2. `recalc_trader.py --account --contract --dry-run` → `--execute` (rebuilds trades, deletes intraday+daily, relinks fills, auto-backs-up). Net=0 preflight should PASS for these.
3. `tags.py remap --account --contract` (restore tags/descriptions; assert count in == out).
4. `intraday.py intraday --account --contract --execute` (reconciles Σrealized vs Σprofit).
5. `intraday.py daily --account --execute`  →  `caggs.py --start --end`.
6. **Verify**: the contract drops its skipped-fills note in the validator (0 skipped), still flat; append the result to `recovery/ledger.jsonl`. Tick the box here.

> `net` = the full-ledger net (~0 — why it's recalc-able). `recalc-net` = the eligible-fills net `recalc_trader` preflights on (should also be ~0). `skipped lots` = how far the trades are currently off. `last skip` flags recency — a recent contract may self-heal via the 2-hourly DAGs; pause them to avoid a delete/insert race.

### Demetris Mavrommatis  ·  Axia  (13)

| ✓ | account | contract | platform | skipped fills | skipped lots | net | recalc-net | last skip |
|---|---|---|---|--:|--:|--:|--:|---|
| [ ] | LFCTEU16 | ES Mar25 | TT | 6484 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | LFCTEU16 | GC Feb25 | TT | 646 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | LFCTEU16 | ZF Mar25 | TT | 550 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | LFCTEU16 | 6J Mar25 | TT | 510 | +0 | +0 | +0 | 2024-12-19 |
| [ ] | LFCTEU16 | FESX Mar25 | TT | 353 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | LFCTEU16 | 6E Mar25 | TT | 228 | +0 | +0 | +0 | 2024-12-18 |
| [ ] | LFCTEU16 | NIY Mar25 | TT | 113 | +0 | +0 | +0 | 2024-12-19 |
| [ ] | LFCTEU16 | NQ Mar25 | TT | 99 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | LFCTEU16 | 6B Mar25 | TT | 97 | +0 | +0 | +0 | 2024-12-19 |
| [ ] | LFCTEU16 | ZB Mar25 | TT | 69 | +0 | +0 | +0 | 2024-12-19 |
| [ ] | LFCTEU16 | ZN Mar25 | TT | 44 | +0 | +0 | +0 | 2024-12-19 |
| [ ] | LFCTEU16 | RTY Mar25 | TT | 24 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | LFCTEU16 | YM Mar25 | TT | 21 | +0 | +0 | +0 | 2024-12-20 |

### Andreas Georgiou  ·  Axia  (11)

| ✓ | account | contract | platform | skipped fills | skipped lots | net | recalc-net | last skip |
|---|---|---|---|--:|--:|--:|--:|---|
| [ ] | LFCTEU53 | GC Feb25 | TT | 257 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | LFCTEU53 | 6E Mar25 | TT | 246 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | LFCTEU53 | FGBL Mar25 | TT | 229 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | LFCTEU53 | ZF Mar25 | TT | 196 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | LFCTEU53 | ES Mar25 | TT | 156 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | LFCTEU53 | R Mar25 | TT | 77 | +0 | +0 | +0 | 2024-12-19 |
| [ ] | LFCTEU53 | 6B Mar25 | TT | 77 | +0 | +0 | +0 | 2024-12-19 |
| [ ] | LFCTEU53 | SO3 Jun25 | TT | 62 | +0 | +0 | +0 | 2024-12-19 |
| [ ] | LFCTEU53 | 6J Mar25 | TT | 34 | +0 | +0 | +0 | 2024-12-19 |
| [ ] | LFCTEU53 | CL Feb25 | TT | 17 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | LFCTEU53 | FGBX Mar25 | TT | 12 | +0 | +0 | +0 | 2024-12-20 |

### Waqqas Ahmed  ·  Axia  (14)

| ✓ | account | contract | platform | skipped fills | skipped lots | net | recalc-net | last skip |
|---|---|---|---|--:|--:|--:|--:|---|
| [ ] | LFCTEU12 | NQ Mar25 | TT | 496 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | LFCTEU12 | RTY Mar25 | TT | 168 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | LFCTEU12 | YM Mar25 | TT | 143 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | LFCTEU12 | R Mar25 | TT | 93 | +0 | +0 | +0 | 2024-12-19 |
| [ ] | LFCTEU12 | Z Mar25 | TT | 71 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | LFCTEU12 | ZF Mar25 | TT | 64 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | LFCTEU12 | 6E Mar25 | TT | 62 | +0 | +0 | +0 | 2024-12-19 |
| [ ] | LFCTEU12 | FESX Mar25 | TT | 42 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | LFCTEU12 | FGBL Mar25 | TT | 40 | +0 | +0 | +0 | 2024-12-19 |
| [ ] | LFCTEU12 | 6B Mar25 | TT | 40 | +0 | +0 | +0 | 2024-12-19 |
| [ ] | LFCTEU12 | FDAX Mar25 | TT | 22 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | LFCTEU12 | GC Feb25 | TT | 19 | +0 | +0 | +0 | 2024-12-19 |
| [ ] | LFCTEU12 | CL Feb25 | TT | 14 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | LFCTEU12 | 6J Mar25 | TT | 8 | +0 | +0 | +0 | 2024-12-19 |

### Jake Nippers  ·  Axia  (17)

| ✓ | account | contract | platform | skipped fills | skipped lots | net | recalc-net | last skip |
|---|---|---|---|--:|--:|--:|--:|---|
| [ ] | LFCTEU109 | ES Mar25 | TT | 356 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | LFCTEU109 | RTY Mar25 | TT | 228 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | LFCTEU109 | 6E Mar25 | TT | 103 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | LFCTEU109 | 6J Mar25 | TT | 83 | +0 | +0 | +0 | 2024-12-19 |
| [ ] | LFCTEU109 | R Mar25 | TT | 72 | +0 | +0 | +0 | 2024-12-19 |
| [ ] | LFCTEU109 | SO3 Dec25 | TT | 67 | +0 | +0 | +0 | 2024-12-19 |
| [ ] | LFCTEU109 | NQ Mar25 | TT | 50 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | LFCTEU109 | 6B Mar25 | TT | 43 | +0 | +0 | +0 | 2024-12-19 |
| [ ] | LFCTEU109 | YM Mar25 | TT | 34 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | LFCTEU109 | SO3 Jun25 | TT | 31 | +0 | +0 | +0 | 2024-12-19 |
| [ ] | LFCTEU109 | FGBL Mar25 | TT | 19 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | LFCTEU109 | UC Mar25 | TT | 15 | +0 | +0 | +0 | 2024-12-19 |
| [ ] | LFCTEU109 | NIY Mar25 | TT | 15 | +0 | +0 | +0 | 2024-12-19 |
| [ ] | LFCTEU109 | SR3 Jun25 | TT | 11 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | LFCTEU109 | ZN Mar25 | TT | 10 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | LFCTEU109 | FBTP Mar25 | TT | 6 | +0 | +0 | +0 | 2024-12-19 |
| [ ] | LFCTEU109 | UB Mar25 | TT | 4 | +0 | +0 | +0 | 2024-12-19 |

### Henry Harman  ·  Axia  (18)

| ✓ | account | contract | platform | skipped fills | skipped lots | net | recalc-net | last skip |
|---|---|---|---|--:|--:|--:|--:|---|
| [ ] | LFCTEU144 | ES Mar25 | TT | 333 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | LFCTEU144 | RTY Mar25 | TT | 111 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | LFCTEU144 | 6E Mar25 | TT | 76 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | LFCTEU144 | 6J Mar25 | TT | 59 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | LFCTEU144 | SO3 Mar25 | TT | 38 | +0 | +0 | +0 | 2024-12-19 |
| [ ] | LFCTEU144 | GC Feb25 | TT | 24 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | LFCTEU144 | FGBL Mar25 | TT | 23 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | LFCTEU144 | UB Mar25 | TT | 21 | +0 | +0 | +0 | 2024-12-19 |
| [ ] | LFCTEU144 | NIY Mar25 | TT | 19 | +0 | +0 | +0 | 2024-12-19 |
| [ ] | LFCTEU144 | SO3 Dec25 | TT | 18 | +0 | +0 | +0 | 2024-12-19 |
| [ ] | LFCTEU144 | R Mar25 | TT | 17 | +0 | +0 | +0 | 2024-12-19 |
| [ ] | LFCTEU144 | ZN Mar25 | TT | 16 | +0 | +0 | +0 | 2024-12-19 |
| [ ] | LFCTEU144 | NQ Mar25 | TT | 16 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | LFCTEU144 | 6B Mar25 | TT | 12 | +0 | +0 | +0 | 2024-12-19 |
| [ ] | LFCTEU144 | CL Feb25 | TT | 8 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | LFCTEU144 | 6C Mar25 | TT | 7 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | LFCTEU144 | YM Mar25 | TT | 6 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | LFCTEU144 | ZF Mar25 | TT | 2 | +0 | +0 | +0 | 2024-12-18 |

### Simon Calder  ·  Axia  (7)

| ✓ | account | contract | platform | skipped fills | skipped lots | net | recalc-net | last skip |
|---|---|---|---|--:|--:|--:|--:|---|
| [ ] | LFCTEU111 | ES Mar25 | TT | 206 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | LFCTEU111 | CL Feb25 | TT | 154 | +0 | +0 | +0 | 2024-12-19 |
| [ ] | LFCTEU111 | FGBL Mar25 | TT | 79 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | LFCTEU111 | ZF Mar25 | TT | 55 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | LFCTEU111 | R Mar25 | TT | 44 | +0 | +0 | +0 | 2024-12-19 |
| [ ] | LFCTEU111 | ZN Mar25 | TT | 15 | +0 | +0 | +0 | 2024-12-19 |
| [ ] | LFCTEU111 | FESX Mar25 | TT | 11 | +0 | +0 | +0 | 2024-12-20 |

### Jamie Brewster  ·  Axia  (16)

| ✓ | account | contract | platform | skipped fills | skipped lots | net | recalc-net | last skip |
|---|---|---|---|--:|--:|--:|--:|---|
| [ ] | LFCTEU150 | ES Mar25 | TT | 104 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | LFCTEU150 | SR3 Mar25 | TT | 90 | +0 | +0 | +0 | 2024-12-18 |
| [ ] | LFCTEU150 | ZF Mar25 | TT | 57 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | LFCTEU150 | GC Feb25 | TT | 55 | +0 | +0 | +0 | 2024-12-19 |
| [ ] | LFCTEU150 | 6J Mar25 | TT | 49 | +0 | +0 | +0 | 2024-12-19 |
| [ ] | LFCTEU150 | SO3 Mar25 | TT | 41 | +0 | +0 | +0 | 2024-12-19 |
| [ ] | LFCTEU150 | NQ Mar25 | TT | 38 | +0 | +0 | +0 | 2024-12-19 |
| [ ] | LFCTEU150 | ZN Mar25 | TT | 37 | +0 | +0 | +0 | 2024-12-19 |
| [ ] | LFCTEU150 | R Mar25 | TT | 28 | +0 | +0 | +0 | 2024-12-19 |
| [ ] | LFCTEU154 | ZQ May25 | TT | 9 | +20 | +0 | +0 | 2025-02-25 |
| [ ] | LFCTEU150 | FGBL Mar25 | TT | 6 | +0 | +0 | +0 | 2024-12-19 |
| [ ] | LFCTEU154 | ZQ Dec25 | TT | 6 | +13 | +0 | +0 | 2025-06-25 |
| [ ] | LFCTEU154 | ZT Mar25 | TT | 3 | +0 | +0 | +0 | 2024-12-31 |
| [ ] | LFCTEU154 | SR3 Mar27 | TT | 3 | +25 | +0 | +0 | 2026-03-26 |
| [ ] | LFCTEU154 | MHG Dec25 | TT | 2 | +5 | +0 | +0 | 2025-09-25 |
| [ ] | LFCTEU154 | MNQ Mar26 | TT | 2 | +0 | +0 | +0 | 2026-01-05 |

### John Bresnihan  ·  Axia  (7)

| ✓ | account | contract | platform | skipped fills | skipped lots | net | recalc-net | last skip |
|---|---|---|---|--:|--:|--:|--:|---|
| [ ] | LFCTEU123 | ES Mar25 | TT | 375 | +0 | +0 | +0 | 2024-12-23 |
| [ ] | LFCTEU123 | NQ Mar25 | TT | 68 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | LFCTEU123 | FESX Mar25 | TT | 20 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | LFCTEU123 | Z Mar25 | TT | 18 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | LFCTEU123 | YM Mar25 | TT | 7 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | LFCTEU123 | R Mar25 | TT | 4 | +0 | +0 | +0 | 2024-12-19 |
| [ ] | LFCTEU123 | FGBL Mar25 | TT | 2 | +0 | +0 | +0 | 2024-12-18 |

### James Binns  ·  Axia  (14)

| ✓ | account | contract | platform | skipped fills | skipped lots | net | recalc-net | last skip |
|---|---|---|---|--:|--:|--:|--:|---|
| [ ] | LJ4AX018 | SR3 Dec25 | TT | 106 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | LJ4AX018 | SO3 Mar25 | TT | 59 | +0 | +0 | +0 | 2024-12-23 |
| [ ] | LJ4AX018 | NQ Mar25 | TT | 57 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | LJ4AX018 | R Mar25 | TT | 45 | +0 | +0 | +0 | 2024-12-27 |
| [ ] | LJ4AX018 | SR3 Mar26 | TT | 41 | +0 | +0 | +0 | 2024-12-19 |
| [ ] | LJ4AX018 | SO3 Jun25 | TT | 40 | +0 | +0 | +0 | 2024-12-19 |
| [ ] | LJ4AX018 | GC Feb25 | TT | 38 | +0 | +0 | +0 | 2024-12-29 |
| [ ] | LJ4AX018 | CRA Dec25 | TT | 32 | +0 | +0 | +0 | 2025-08-25 |
| [ ] | LJ4AX018 | CRA Dec24 | TT | 26 | +0 | +0 | +0 | 2025-01-21 |
| [ ] | LJ4AX018 | I Jun25 | TT | 14 | +0 | +0 | +0 | 2024-12-23 |
| [ ] | LJ4AX018 | I Mar26 | TT | 12 | +0 | +0 | +0 | 2024-12-23 |
| [ ] | LJ4AX018 | ZN Mar25 | TT | 12 | +0 | +0 | +0 | 2024-12-30 |
| [ ] | LJ4AX018 | ES Mar25 | TT | 6 | +0 | +0 | +0 | 2024-12-22 |
| [ ] | LJ4AX018 | SR3 Sep25 | TT | 2 | +0 | +0 | +0 | 2024-12-20 |

### Mark Norman  ·  Axia  (6)

| ✓ | account | contract | platform | skipped fills | skipped lots | net | recalc-net | last skip |
|---|---|---|---|--:|--:|--:|--:|---|
| [ ] | LFCTEU163 | ES Mar25 | TT | 335 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | LFCTEU163 | R Mar25 | TT | 17 | +0 | +0 | +0 | 2024-12-19 |
| [ ] | LFCTEU163 | ZF Mar25 | TT | 8 | +0 | +0 | +0 | 2024-12-18 |
| [ ] | LFCTEU163 | 6B Mar25 | TT | 5 | +0 | +0 | +0 | 2024-12-19 |
| [ ] | LFCTEU163 | 6J Mar25 | TT | 4 | +0 | +0 | +0 | 2024-12-19 |
| [ ] | LFCTEU163 | ZN Mar25 | TT | 2 | +0 | +0 | +0 | 2024-12-18 |

### Elliot Harland  ·  Axia  (8)

| ✓ | account | contract | platform | skipped fills | skipped lots | net | recalc-net | last skip |
|---|---|---|---|--:|--:|--:|--:|---|
| [ ] | LFCTEU151 | MES Mar25 | TT | 87 | +0 | +0 | +0 | 2024-12-19 |
| [ ] | LFCTEU151 | ES Mar25 | TT | 78 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | LFCTEU151 | 6E Mar25 | TT | 39 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | LFCTEU151 | Z Mar25 | TT | 23 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | LFCTEU151 | R Mar25 | TT | 22 | +0 | +0 | +0 | 2024-12-19 |
| [ ] | LFCTEU151 | FGBL Mar25 | TT | 16 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | LFCTEU151 | 6J Mar25 | TT | 9 | +0 | +0 | +0 | 2024-12-19 |
| [ ] | LFCTEU184 | LO1 W01Mar-26 P6050 | TT | 1 | +10 | +0 | +0 | 2026-02-16 |

### Alex Morris  ·  Axia  (8)

| ✓ | account | contract | platform | skipped fills | skipped lots | net | recalc-net | last skip |
|---|---|---|---|--:|--:|--:|--:|---|
| [ ] | LFCTEU113 | ES Mar25 | TT | 113 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | LFCTEU113 | ZT Mar25 | TT | 30 | +0 | +0 | +0 | 2024-12-18 |
| [ ] | LFCTEU113 | SO3 Dec25 | TT | 29 | +0 | +0 | +0 | 2024-12-19 |
| [ ] | LFCTEU113 | R Mar25 | TT | 24 | +0 | +0 | +0 | 2024-12-19 |
| [ ] | LFCTEU113 | GC Feb25 | TT | 18 | +0 | +0 | +0 | 2024-12-18 |
| [ ] | LFCTEU113 | 6E Mar25 | TT | 16 | +0 | +0 | +0 | 2024-12-18 |
| [ ] | LFCTEU113 | FGBL Mar25 | TT | 8 | +0 | +0 | +0 | 2024-12-19 |
| [ ] | LFCTEU113 | 6J Mar25 | TT | 7 | +0 | +0 | +0 | 2024-12-19 |

### Oliver Thomas  ·  Axia  (4)

| ✓ | account | contract | platform | skipped fills | skipped lots | net | recalc-net | last skip |
|---|---|---|---|--:|--:|--:|--:|---|
| [ ] | LFCTEU09 | ES Mar25 | TT | 78 | +0 | +0 | +0 | 2024-12-18 |
| [ ] | LFCTEU09 | 6J Mar25 | TT | 47 | +0 | +0 | +0 | 2024-12-19 |
| [ ] | LFCTEU09 | GC Feb25 | TT | 46 | +0 | +0 | +0 | 2024-12-18 |
| [ ] | LFCTEU09 | NIY Mar25 | TT | 25 | +0 | +0 | +0 | 2024-12-19 |

### Louis Binns  ·  Axia  (4)

| ✓ | account | contract | platform | skipped fills | skipped lots | net | recalc-net | last skip |
|---|---|---|---|--:|--:|--:|--:|---|
| [ ] | LJ4AX017 | ZQ Jan26 | TT | 106 | +0 | +0 | +0 | 2025-12-10 |
| [ ] | LCE30102 | SR3 Mar26 | TT | 18 | +0 | +0 | +0 | 2026-03-13 |
| [ ] | LCE30102 | SA3 Dec25 | TT | 11 | -30 | +0 | +0 | 2026-01-28 |
| [ ] | LCE30102 | ZQ Apr26 | TT | 7 | +0 | +0 | +0 | 2026-03-18 |

### Ryan Cohen  ·  Axia  (8)

| ✓ | account | contract | platform | skipped fills | skipped lots | net | recalc-net | last skip |
|---|---|---|---|--:|--:|--:|--:|---|
| [ ] | LJ4AX008 | R Mar25 | TT | 38 | +0 | +0 | +0 | 2024-12-27 |
| [ ] | LCE30186 | 6J Jun26 | TT | 31 | +0 | +0 | +0 | 2026-06-10 |
| [ ] | LJ4AX008 | CGB Mar25 | TT | 28 | +0 | +0 | +0 | 2024-12-23 |
| [ ] | LJ4AX008 | ES Mar25 | TT | 10 | +0 | +0 | +0 | 2024-12-23 |
| [ ] | LJ4AX008 | SR3 Jun25 | TT | 8 | +0 | +0 | +0 | 2024-12-18 |
| [ ] | LJ4AX008 | 6J Mar25 | TT | 7 | +0 | +0 | +0 | 2024-12-19 |
| [ ] | LJ4AX008 | SA3 Jun25 | TT | 7 | +0 | +0 | +0 | 2025-03-04 |
| [ ] | LJ4AX008 | 6C Mar25 | TT | 4 | +0 | +0 | +0 | 2024-12-23 |

### Michael Rosen  ·  Stage 2  (4)

| ✓ | account | contract | platform | skipped fills | skipped lots | net | recalc-net | last skip |
|---|---|---|---|--:|--:|--:|--:|---|
| [ ] | MROSEN | MNQ Mar25 | TT | 80 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | MROSEN | MES Mar25 | TT | 27 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | MROSEN | 6E Mar25 | TT | 8 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | MROSEN | 6A Mar25 | TT | 2 | +0 | +0 | +0 | 2024-12-20 |

### Vicko Perasovic  ·  Axia  (2)

| ✓ | account | contract | platform | skipped fills | skipped lots | net | recalc-net | last skip |
|---|---|---|---|--:|--:|--:|--:|---|
| [ ] | LCE30187 | AH | Stellar | 48 | +0 | +0 | +0 | 2026-04-02 |
| [ ] | LCE30187 | CA | Stellar | 27 | +0 | +0 | +0 | 2026-04-01 |

### Anthony Church  ·  Axia  (4)

| ✓ | account | contract | platform | skipped fills | skipped lots | net | recalc-net | last skip |
|---|---|---|---|--:|--:|--:|--:|---|
| [ ] | LFCTEU116 | FGBM Mar25 | TT | 35 | +0 | +0 | +0 | 2025-01-31 |
| [ ] | LFCTEU116 | FDAX Mar25 | TT | 22 | -2 | +0 | +0 | 2025-01-23 |
| [ ] | LFCTEU116 | CGF Dec25 | TT | 10 | +0 | +0 | +0 | 2025-10-28 |
| [ ] | LFCTEU116 | GC Dec25 | TT | 4 | +0 | +0 | +0 | 2025-10-21 |

### Jay Vowell  ·  Axia  (3)

| ✓ | account | contract | platform | skipped fills | skipped lots | net | recalc-net | last skip |
|---|---|---|---|--:|--:|--:|--:|---|
| [ ] | LCE30178 | I Apr26 | Stellar | 31 | +7 | +0 | +0 | 2026-03-25 |
| [ ] | LCE30178 | CRA Mar26 | Stellar | 27 | +0 | +0 | +0 | 2026-03-03 |
| [ ] | LCE30178 | FGBS Jun26 | Stellar | 2 | +0 | +0 | +0 | 2026-04-24 |

### Dominic Smith Sim  ·  Stage 2  (6)

| ✓ | account | contract | platform | skipped fills | skipped lots | net | recalc-net | last skip |
|---|---|---|---|--:|--:|--:|--:|---|
| [ ] | BPC_DSMITH | MCL Feb25 | TT | 14 | +0 | +0 | +0 | 2024-12-18 |
| [ ] | BPC_DSMITH | MHG Mar25 | TT | 12 | +0 | +0 | +0 | 2024-12-18 |
| [ ] | BPC_DSMITH | MGC Feb25 | TT | 10 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | BPC_DSMITH | MNQ Mar25 | TT | 8 | +0 | +0 | +0 | 2024-12-18 |
| [ ] | BPC_DSMITH | 6E Mar25 | TT | 6 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | BPC_DSMITH | 6J Mar25 | TT | 4 | +0 | +0 | +0 | 2024-12-18 |

### Kapil Soni  ·  Axia  (3)

| ✓ | account | contract | platform | skipped fills | skipped lots | net | recalc-net | last skip |
|---|---|---|---|--:|--:|--:|--:|---|
| [ ] | LJ4AX087 | HG Mar25 | TT | 16 | +0 | +0 | +0 | 2025-01-09 |
| [ ] | LJ4AX087 | AH 3M | TT | 14 | +0 | +0 | +0 | 2025-01-24 |
| [ ] | LJ4AX087 | SI Mar25 | TT | 6 | +0 | +0 | +0 | 2024-12-27 |

### Adam Malik  ·  Axia  (2)

| ✓ | account | contract | platform | skipped fills | skipped lots | net | recalc-net | last skip |
|---|---|---|---|--:|--:|--:|--:|---|
| [ ] | LFCTEU13 | ES Mar25 | TT | 24 | +0 | +0 | +0 | 2024-12-18 |
| [ ] | LFCTEU13 | GC Feb25 | TT | 6 | +0 | +0 | +0 | 2024-12-18 |

### Roy Green  ·  Axia  (3)

| ✓ | account | contract | platform | skipped fills | skipped lots | net | recalc-net | last skip |
|---|---|---|---|--:|--:|--:|--:|---|
| [ ] | LFCTEU133 | 6J Mar25 | TT | 18 | +0 | +0 | +0 | 2024-12-18 |
| [ ] | LFCTEU133 | ES Mar25 | TT | 7 | +0 | +0 | +0 | 2024-12-18 |
| [ ] | LFCTEU133 | 6E Mar25 | TT | 4 | +0 | +0 | +0 | 2024-12-18 |

### Theo Snee  ·  Axia  (5)

| ✓ | account | contract | platform | skipped fills | skipped lots | net | recalc-net | last skip |
|---|---|---|---|--:|--:|--:|--:|---|
| [ ] | LJ4AX100 | ES Mar25 | TT | 8 | +0 | +0 | +0 | 2024-12-20 |
| [ ] | LJ4AX100 | MES Mar25 | TT | 7 | +0 | +0 | +0 | 2024-12-18 |
| [ ] | LJ4AX100 | 6B Mar25 | TT | 5 | +0 | +0 | +0 | 2024-12-19 |
| [ ] | LJ4AX100 | 6E Mar25 | TT | 4 | +0 | +0 | +0 | 2024-12-18 |
| [ ] | LJ4AX100 | ZF Mar25 | TT | 2 | +0 | +0 | +0 | 2024-12-18 |

### Andrew Anderson  ·  Axia  (4)

| ✓ | account | contract | platform | skipped fills | skipped lots | net | recalc-net | last skip |
|---|---|---|---|--:|--:|--:|--:|---|
| [ ] | LJ4AX035 | CL Feb25 | TT | 6 | +0 | +0 | +0 | 2024-12-23 |
| [ ] | LJ4AX035 | NQ Mar25 | TT | 6 | +0 | +0 | +0 | 2024-12-27 |
| [ ] | LJ4AX035 | CL Mar25 | TT | 6 | +0 | +0 | +0 | 2024-12-23 |
| [ ] | LJ4AX035 | Z Mar25 | TT | 4 | +0 | +0 | +0 | 2024-12-31 |

### Nish Shah  ·  Axia  (1)

| ✓ | account | contract | platform | skipped fills | skipped lots | net | recalc-net | last skip |
|---|---|---|---|--:|--:|--:|--:|---|
| [ ] | LJ4AX085 | TFM Apr26 | Stellar | 12 | +0 | +0 | +0 | 2026-03-04 |

### Tom White  ·  Axia  (1)

| ✓ | account | contract | platform | skipped fills | skipped lots | net | recalc-net | last skip |
|---|---|---|---|--:|--:|--:|--:|---|
| [ ] | LCE30132 | TFM Apr26 | Stellar | 6 | +0 | +0 | +0 | 2026-03-05 |

### Luke Farrier  ·  Axia  (1)

| ✓ | account | contract | platform | skipped fills | skipped lots | net | recalc-net | last skip |
|---|---|---|---|--:|--:|--:|--:|---|
| [ ] | LJ4AX039 | SO3 Dec25 | Stellar | 4 | +2 | +0 | +0 | 2025-12-19 |

### James Pitron  ·  Axia  (1)

| ✓ | account | contract | platform | skipped fills | skipped lots | net | recalc-net | last skip |
|---|---|---|---|--:|--:|--:|--:|---|
| [ ] | LJ4AX042 | FGBM Mar25 | TT | 3 | +7 | +0 | +0 | 2024-11-26 |

### Thomas Curran  ·  Stage 2  (1)

| ✓ | account | contract | platform | skipped fills | skipped lots | net | recalc-net | last skip |
|---|---|---|---|--:|--:|--:|--:|---|
| [ ] | BPC_TCURRAN | ZN Mar25 | TT | 2 | +0 | +0 | +0 | 2024-12-18 |

