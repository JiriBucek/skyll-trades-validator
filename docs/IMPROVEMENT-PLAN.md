# Validator v2 — improvement plan

**Goal:** turn the validator into a *fast, trustworthy overview* of whether the Skyll fills→trades
pipeline is healthy and exactly where fills were dropped, missed, duplicated or mis-attributed.

**North star:** make **🔴 red mean "a fill is genuinely wrong and you can act on it."** Everything
that is *expected* — flat books, genuine open positions, spread traders, ancient pre-retention
contracts — must collapse out of view so a systemic problem shows up as **one row, not fifty**.

This plan is the product of a long recovery/forensics session (2026-06-26→29). Read the context
first; it's what makes the rest obvious.

---

## 0. Read before starting (context)

- `docs/DESIGN.md`, `README.md` — what the validator does today.
- **`../aws-mwaa-local-runner/dags/misc/recovery/PRINCIPLES.md`** — THE model: fills ledger →
  aggregate to trades → profit; the *flat test*; "non-flat = lost fill / extra / mis-attributed /
  genuine open, never expiry-settlement"; always read **both** the `account` and `trader` columns.
- `../aws-mwaa-local-runner/dags/misc/recovery/RECOVERY.md` + `raw_diff_ts.py` — the per-account
  FIX-feed diff (block-aggregation-robust, per-second net) we want to fold INTO the validator.
- `hive/history/2026-06-29-skyll-fills-to-trades-recovery.md` — the session that produced this plan.
- Agent memory: `skyll-fills-to-trades-model`, `skyll-raw-fills-fix-recovery`,
  `skyll-trades-validator`, `skyll-spread-traders-vs-drops`, `skyll-stellar-fill-pipeline`.

**DB access:** `cd Skyll/aws-mwaa-local-runner && secretctl run skyll-mwaa -- ./venv/bin/python`
(MWAA venv has sqlalchemy + `PROD_DATABASE_CONNECTION_STRING_READONLY`). The validator's own venv
uses `from app.db import query` (psycopg2, `%(name)s` params — note `%` in ILIKE must be a param).
The validator is **read-only**; never write from it.

**Validator code map:** `app/engine.py` (`compute_state`, the `net_all`/`window`/`realized`
queries), `app/report.py` (buckets + rendering), `app/tt.py` (`TTClient`, `fills_diff`, the
position API), `app/contracts.py` (expiry parse — *display triage only*), `app/spread_traders.py`
(hard-coded list — to be replaced by live `collapse_pct`), `app/db.py`, `app/api.py`, `app/config.py`.

**Live test cases to validate against** (must each end up in the right bucket):
- **Josh Gadenne / LFCTEU200** — 82 contracts were *stranded* (28k fills under `trader_id=0` /
  `IgnoredAccounts`); recovered. Stranding must be its own bucket, not "drop".
- **Josh / 6J Sep26 +18** — a **genuine open** (clearing statement confirmed 18 long); our net ==
  FIX net. Must classify CONFIRMED-OPEN, not 🔴.
- **Josh / BRN Jul26 +24** — a stray `trader=FCTRisk, account=AXIA` (alias+alias) order defaulted
  into his book; his own book is flat without it. Must classify MIS-ATTRIBUTED, not "his open".
- **Demetris / Andreas / Adam SR3 etc.** — real TT drops, FIX has more than us → DROP.

---

## The taxonomy (every cell is exactly one of these)

`FLAT · CONFIRMED-OPEN · SPREAD · DROP · EXTRA/MIS-ATTRIBUTED · STRANDED · UNVERIFIABLE`

Only **DROP**, **EXTRA/MIS-ATTRIBUTED**, **STRANDED** are 🔴 actionable. FLAT/CONFIRMED-OPEN/SPREAD
are green/calm; UNVERIFIABLE is ⚪ amber.

---

## Phase 1 — the spine — ✅ DONE (2026-06-29)

**Shipped.** The TT-position guess is replaced by the `raw_fills_fix` FIX-feed cross-check
(`backend/app/fixfeed.py`): canonical-account, label-robust, EOD-of-last-completed-day cutoff (kills
the front-month ingestion-lag false drops), batched net classify + per-fill DROP/EXTRA discrimination
on the divergent few (the `raw_diff_ts` matchers), reingest-ready `/api/raw-diff` drill-down. Plus the
drop-by-ingestion-day rollup (1b), the one-line health header (1c), and — bonus from Phase 2b — an
all-history STRANDED detector (futures only). Verdicts collapse to a new taxonomy across engine/report/
api/frontend. **Validated live:** ~192–222 noisy "suspected_drop" → **3 genuinely actionable rows**.
The four named test cases all land correctly: Josh 6J Sep26 = CONFIRMED-OPEN, Josh BRN Jul26 = EXTRA/
MIS-ATTRIBUTED, Louis LCE30102 SR3 Mar27 = DROP (1 missing fill, `uniqueExecId`, stamped 06-11 17:30),
Josh LFCTEU200 stranding = correctly 0 (already recalc'd 06-29; detector validated on logic + the live
`LCE30325` case).

**Update (2026-06-29, post-ship): the hard-coded spread-trader filter is REMOVED** (`spread_traders.py`
deleted). The client gave us ground truth on who's a spread trader and **removed them from the Axia
group**, so the cohort (`group_members`) already excludes them — group membership is now the single
source of truth. (Bonus: the old `collapse_pct` heuristic had mislabelled several *non*-spread traders
— Jake Nippers, James Binns, Vicko, Pitron, O'Shea… — so removing it surfaced their real drops, e.g.
Jake `LFCTEU109 I Sep26` −15/FIX +185.) This obviates **Phase 3a** (live `collapse_pct` auto-detection).

Next: Phase 2a (size reconciliation), Phase 3b/3c (alias flag, confirmed-open snooze), Phase 4
(ingestion monitors).

## Phase 1 — the spine (build this first; biggest bang)

### 1a. FIX-feed cross-check replaces the TT-position guess
**Why:** the TT *position* endpoint ignores the `accountId` filter (cross-nets accounts) → false
positives AND false negatives. The authoritative per-account source of truth is the **FIX feed
`raw_fills_fix`** (platform `I_TT` and `I_STELLAR`).
**How:** for every non-flat `(account, contract)`, diff our `fills` vs `raw_fills_fix` for that
account+symbol+maturity (port the `raw_diff_ts` per-second-net logic into `app/`; gate on net).
Account match must be **label-robust**: REST labels accounts `LFCTEU150_MA`, FIX uses `LFCTEU150` /
`&`/`:` forms — match on the canonical base. Emit a verdict:
- our net **== FIX net** → genuine (open/carry) — not our bug.
- **FIX > ours** → **DROP** (recoverable) — surface the missing fills + day.
- **ours > FIX** → **EXTRA / MIS-ATTRIBUTED**.
- **no FIX rows** → UNVERIFIABLE (pre-retention / give-up).
**Done when:** the four test cases above land in the right bucket; the TT-position check is demoted
to a secondary hint (or removed).

### 1b. Drop-by-day rollup (the fast systemic signal)
**Why:** drops cluster on *ingestion events* (e.g. 2026-06-11 17:30, 04-08, 04-20, 04-28, 05-05)
that hit many accounts at once. "47 red contracts" is really "1 bad window".
**How:** group all DROP-verdict missing fills by ingestion window (timestamp rounded; the FIX/TT
exec time). Show "06-11 17:30 → N missing fills across M accounts/contracts" as a single row that
expands.
**Done when:** a clean DB shows zero drop-day clusters; a seeded drop shows one clustered row.

### 1c. Top-line health header
One line, green-or-not: `N drop windows (X fills, recoverable) · A mis-attributed · S stranded
accounts · U unverifiable — everything else flat/open/spread`. Zero on the first three = healthy.

---

## Phase 2 — catch what `net` misses

### 2a. SIZE reconciliation (Σ qty), per `(account, contract)`, vs the FIX feed
**Why:** net can be 0 while fills are wrong (TT block-averages sweeps into one row; Stellar can
double-insert). Total **size** is immune to block-aggregation — it's what caught the June coverage
gap (whole products `ECF`/`NIFTY`/`FEUA` missing) and the micro-dust. (June reconciled to 99.6%.)
**How:** per account, compute `Σqty(ours) vs Σqty(FIX)` per contract; surface a per-account **size
match %**; <100% = missing or extra fills even when net matches. Neutralize known naming-granularity
(sub-account `_MA`, option strikes, EUREX weekly format, symbol aliases `YEBM`/`JFCE`) — see the
June recon notes in memory so these don't show as false gaps.

### 2b. Aggregation-health / stranding bucket
**Why:** Josh's Euribor was invisible because fills aggregated under `trader_id=0` /
`IgnoredAccounts(349)`, not because of a drop.
**How:** per account flag (a) fills with `trade_ids IS NULL` on completed days (orphans, already
partly present); (b) fills on a *real* account tagged `trader_id=0` or trader `IgnoredAccounts`;
(c) a contract whose account-net is 0 but whose **per-trader** aggregation is fragmented across
trader 0/N. "Account flat but 4,197 fills stranded under trader 0" = STRANDED 🔴 (fix = `recalc_trader`,
no backfill).

---

## Phase 3 — attribution + closing the loop

### 3a. Live `collapse_pct` instead of the hard-coded spread list
**Why:** `collapse_pct = 1 − Σ|per-product net| / Σ|per-contract net|`; ≥70% = calendar-spread
trader whose legs cancel at product level (and whose per-contract red is meaningless). The metric
**drifts** (Vicko 100%→88.9% in two days; the borderline three ~72–81% could cross 70%), so the
hard-coded `spread_traders.py` goes stale.
**How:** compute it live per trader; auto-exclude ≥70% (show the %); keep a manual override for the
borderline band. Anchor: Demetris = ~4% (NOT a spreader), spread desk = "Axia" group.

### 3b. Alias / mis-attribution flag
**Why:** the BRN +24 was a `FCTRisk`/`AXIA` (alias+alias) order defaulted into Josh.
**How:** when a contract's imbalance is driven by fills whose raw source was a **pure alias**
(`AXIA`/`AXIANDEX`/`GHF_AXIA`/`GHFinancial`/`LFCTEUM`/`GHFC01`, or login alias `FCTRisk`), tag it
*mis-attribution-risk*. Cross-check: does the trader's *own*-login book aggregate to flat without
those fills? If yes, the extra is not theirs.

### 3c. "Confirmed open" snooze with provenance
**Why:** the 6J +18 was a genuine open that kept alarming red.
**How:** let the operator mark `(account, contract)` "confirmed open per clearing statement <date>"
→ calm bucket until the net changes (then it re-surfaces). Store the note + date so it's auditable.

---

## Phase 4 — going-forward ingestion monitors (top strip)

Cheap signals that ingestion itself is healthy, not just the data:
- **`unique_exec_id` coverage %** per platform/day (TT hit 100% on 2026-06-26; alert if it dips —
  the watermark MAX−N overlap re-scan depends on it).
- **daily ingest-vs-FIX size delta** per platform (catches a new gap the day it happens).
- **watermark lag / last-fill age** per account (stale account = stopped ingesting).

---

## Sequencing & acceptance

1. **Phase 1 first** (1a FIX-feed verdicts + 1b drop-day rollup + 1c header) — this alone moves you
   from "untrustworthy red" to "here are the N real drops and the day they happened."
2. Then Phase 2 (size recon + stranding), Phase 3 (live spread + alias + snooze), Phase 4 (monitors).
3. **Acceptance for every phase:** run it against the live DB and confirm the named test cases land
   in the correct bucket (Josh stranding=STRANDED, 6J=CONFIRMED-OPEN, BRN=MIS-ATTRIBUTED, SR3-class=
   DROP), and that the spread desk + ancient contracts stay collapsed.

## Risks / caveats
- Keep the validator **read-only**. All writes/recoveries stay in `recovery/` tools.
- The FIX feed has a **retention wall** (`raw_fills_fix` ~2026-03-30; TT ledger ~93d) — pre-wall
  contracts are genuinely UNVERIFIABLE, not bugs; don't paint them red.
- Don't reintroduce expiry-as-settlement logic — non-flat is a lost fill or a genuine open (see
  PRINCIPLES.md). The expiry parse stays a *display filter* only.
- Performance: the all-history net scan is ~10–20s cold; the FIX diff is per-account — cache and
  run the heavy diff on demand / for non-flat contracts only, like `fills_diff` does today.
