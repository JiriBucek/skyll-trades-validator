"""FIX-feed cross-check against the in-database drop-copy `raw_fills_fix`.

Two entry points:

  cross_check(state)        — the OVERVIEW check (v3). For every PROBLEM row (a sustained open) it
                              compares, per COMPLETED day, the GROSS traded volume (Σ qty) in our
                              `fills` against `raw_fills_fix` at the canonical grain. Gross differs
                              -> that day is a `mismatch` (a fill is probably missing -> red).

  account_diff(acct, ctr)   — the on-demand DRILL-DOWN: the exact fills missing-from / extra-in our
                              DB with uniqueExecId, reingest-ready (the per-second / count-excess /
                              cumulative matchers, ported from recovery/raw_diff_ts.py).

`raw_fills_fix` is an independent push-copy of every fill:
  * TT accounts  (platform_id=1) → platform **I_TT**      (a divergence means the TT-API pull
    dropped a fill — watermark / µs-collision).
  * Stellar accts (platform_id=2) → platform **I_STELLAR** (the source the Stellar processor builds
    `fills` from; a divergence means a processing skip / mis-attribution).

Account match is **label-robust**: the REST feed labels accounts `LFCTEU150_MA`, the FIX feed uses
`LFCTEU150` / `&LFCTEU150` / `LFCTEU150:…`. Sub-account suffixes (`_MA`,`_AL`,`_JPX`,…) belong to
the SAME book, so both sides canonicalize to a base account and aggregate at the
`(canonical_account, symbol, maturity, platform)` grain (= the economic position).

Read-only. The actual recovery lives in `aws-mwaa-local-runner/recovery/`.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime, timezone

from . import db
from .config import Config

# FIX retention wall — both feeds start here (verified live 2026-06-29:
# I_TT min 2026-03-30 00:00:00.512, I_STELLAR min 2026-03-30 00:00:00). A position opened before
# this has no recoverable opening; we never paint it red.
RETENTION_START = Config.FIX_RETENTION_START

# platform_id (our `fills`/cohort) -> raw_fills_fix feed
FEED_BY_PLATFORM = {1: "I_TT", 2: "I_STELLAR"}

_MONTHS = {"jan": "01", "feb": "02", "mar": "03", "apr": "04", "may": "05", "jun": "06",
           "jul": "07", "aug": "08", "sep": "09", "oct": "10", "nov": "11", "dec": "12"}

# strip a leading give-up '&', a ':suffix', and a trailing sub-account tag -> canonical base account
_CANON_SUFFIX = re.compile(r"_(MA|AL|ALGO|MANUAL|JPX|MM|MX)$")
# the same transform expressed in SQL, so we can GROUP/match raw_fills_fix.account the same way
CANON_SQL = ("regexp_replace(regexp_replace(ltrim(account,'&'),':.*$',''),"
             "'_(MA|AL|ALGO|MANUAL|JPX|MM|MX)$','')")

EPS = 1e-6


def canon(account: str | None) -> str:
    return _CANON_SUFFIX.sub("", (account or "").lstrip("&").split(":")[0])


def parse_contract(contract: str | None) -> tuple[str | None, str | None]:
    """'ES Jun26' -> ('ES', '202606'). Options (strike token) and odd labels -> (None, None)."""
    m = re.fullmatch(r"([A-Za-z0-9]+)\s+([A-Za-z]{3})(\d{2})", (contract or "").strip())
    if not m:
        return None, None
    mon = _MONTHS.get(m.group(2).lower())
    return (m.group(1), f"20{m.group(3)}{mon}") if mon else (None, None)


def parse_uid(exec_id: str | None) -> str | None:
    """FIX exec_id '20260611-17:30:18.602888:AvVKLRJRCdF9JddWYXnFi1:64929:M:…:F' -> the uid token."""
    if not exec_id:
        return None
    parts = exec_id.split(":")
    return parts[3] if len(parts) > 3 and re.fullmatch(r"[A-Za-z0-9]{18,}", parts[3]) else None


# ---------------------------------------------------------------------------
# matchers (ported verbatim in spirit from recovery/raw_diff_ts.py)
#   Each finds the legs of `other` not covered by `base`, gated on the recovered net == target.
#   Robust to FIX-vs-API timestamp jitter (≈ few hundred µs) and TT block-vs-leg aggregation.
# ---------------------------------------------------------------------------

def _sec(dt) -> int:
    return int(dt.replace(tzinfo=dt.tzinfo or timezone.utc).timestamp())


def _net(rows) -> float:
    """net over [(side, qty, price, row)] tuples."""
    return sum(q if s == 1 else -q for s, q, p, _ in rows)


def _per_second_missing(base, other):
    """base: [(sec, side, qty, price)]; other: [(sec, side, qty, price, row)].
    Skip a second whose net already matches (block-vs-leg identical); only net-divergent seconds
    yield legs, multiset-minus the legs `base` already holds."""
    base_by = defaultdict(list)
    for t, s, q, p in base:
        base_by[t].append((s, q, p))
    other_by = defaultdict(list)
    for t, s, q, p, row in other:
        other_by[t].append((s, q, p, row))
    out = []
    for t, olegs in other_by.items():
        blegs = base_by.get(t, [])
        onet = sum(q if s == 1 else -q for s, q, p, _ in olegs)
        bnet = sum(q if s == 1 else -q for s, q, p in blegs)
        if abs(onet - bnet) < EPS:
            continue
        held = Counter(blegs)
        for s, q, p, row in olegs:
            k = (s, q, p)
            if held.get(k, 0) > 0:
                held[k] -= 1
            else:
                out.append((s, q, p, row))
    return out


def _count_excess_missing(base, other):
    """base: [(day, side, qty, price)]; other: [(day, side, qty, price, row)]. Count-excess per
    (day, side, qty, price) bucket — correct at matching granularity incl. high-frequency."""
    base_c = Counter(base)
    buckets = defaultdict(list)
    for d, s, q, p, row in other:
        buckets[(d, s, q, p)].append((s, q, p, row))
    out = []
    for key, items in buckets.items():
        excess = len(items) - base_c.get(key, 0)
        if excess > 0:
            out.extend(items[:excess])
    return out


def _cumulative_missing(base, other, tol_s=3):
    """base: [(dt, side, qty)]; other: [(dt, side, qty, row)]. Block-aggregation-proof: group by
    (date, side), walk `other` legs chronologically consuming `base` quantity (within tol seconds);
    a leg `base` can't cover is genuinely missing. Survives TT price-averaged block fills."""
    from datetime import timedelta
    b_by, o_by = defaultdict(list), defaultdict(list)
    for dt, s, q in base:
        b_by[(dt.date(), s)].append((dt, q))
    for dt, s, q, row in other:
        o_by[(dt.date(), s)].append((dt, q, row))
    tol = timedelta(seconds=tol_s)
    out = []
    for k, olegs in o_by.items():
        blist = sorted(b_by.get(k, []))
        oi, avail = 0, 0.0
        for ts_o, q_o, row in sorted(olegs, key=lambda x: x[0]):
            while oi < len(blist) and blist[oi][0] <= ts_o + tol:
                avail += blist[oi][1]
                oi += 1
            if avail >= q_o:
                avail -= q_o
            else:
                out.append((k[1], q_o - avail, None, row))
                avail = 0.0
    return out


def _find_missing(base_rows, other_rows, target):
    """Find `other` legs not in `base` that net to `target`, using the most-robust matcher that
    reconciles. base/other rows are raw DB rows (timestamp, side, quantity, price[, exec_id]).
    Returns (legs, net, method) where legs = [(side, qty, price, row)]."""
    base_day = [(dt.date(), int(s), float(q), round(float(p), 6)) for dt, s, q, p, *_ in base_rows]
    other_day = [(dt.date(), int(s), float(q), round(float(p), 6), r)
                 for r in other_rows for dt, s, q, p, *_ in [r]]
    base_sec = [(_sec(r[0]), int(r[1]), float(r[2]), round(float(r[3]), 6)) for r in base_rows]
    other_sec = [(_sec(r[0]), int(r[1]), float(r[2]), round(float(r[3]), 6), r) for r in other_rows]
    base_ts = [(r[0], int(r[1]), float(r[2])) for r in base_rows]
    other_ts = [(r[0], int(r[1]), float(r[2]), r) for r in other_rows]
    for cand, name in ((_count_excess_missing(base_day, other_day), "count-excess"),
                       (_per_second_missing(base_sec, other_sec), "per-second"),
                       (_cumulative_missing(base_ts, other_ts), "cumulative")):
        if abs(_net(cand) - target) < EPS:
            return cand, _net(cand), name
    # nothing reconciled — return the per-second best for inspection
    legs = _per_second_missing(base_sec, other_sec)
    return legs, _net(legs), "unreconciled"


# ---------------------------------------------------------------------------
# SQL (read-only; psycopg2 %(name)s params)
# ---------------------------------------------------------------------------

# OPTION rows ride under the UNDERLYING FUTURE's (symbol, maturity) in raw_fills_fix and must
# never count toward a futures contract's gross. An OPTION carries an explicit STRIKE after a
# C/P marker; a FUTURE never does (verified 2026-07-22 against every distinct desc shape in the
# feed — 49 option shapes all match, 306 future shapes none):
#   CME 'Q3AN6 C28950' / 'E1AK6 P7180'      -> ' [CP][0-9]'
#   ICE 'I FMU0026_OMCA<strike>'            -> '_OM[CP]'
#   Eurex 'OGBL SI 20260504 PS AM P 125.00' -> ' AM [CP] '
# ⚠️ CORRECTION (2026-07-22): the previous pattern ' SI [0-9]{8} [CP]S$' matched the Eurex/ICE
# futures SETTLEMENT suffix (CS = Cash-Settled indices, PS = Physically-Settled bonds) — those
# are FUTURES ('FESX SI 20260619 CS' @ ~6300 = the Euro STOXX 50 future, no strike), and the old
# regex wrongly excluded ~203k genuine futures rows (~61k Stellar + ~142k TT) from the FIX
# cross-check, masking real drops on FESX/FGBL/FBTP/FGBS/FDAX/FOAT/... contracts. Likewise bare
# '_OM' is narrowed to '_OM[CP]' (strike-bearing options only).
# Keep in sync with OPTION_DESC_RE in aws-mwaa-local-runner process_stellar_fills_dag.py.
NON_FUTURES_DESC_RE = r"( [CP][0-9]|_OM[CP]| AM [CP] )"

# raw FIX GROSS traded volume (Σ qty) per (canonical account, symbol, maturity, feed, UTC day),
# over the display window AND BEFORE the cutoff (end of the last completed UTC day) so today's
# in-flight fills don't count. Bounded to the symbols/canons that actually have a problem row.
RAW_GROSS_DAY_SQL = f"""
SELECT {CANON_SQL} AS cb, symbol, maturity_month_year AS mat, platform,
       (timestamp AT TIME ZONE 'UTC')::date AS d,
       SUM(quantity) AS gross
FROM raw_fills_fix
WHERE platform IN ('I_TT', 'I_STELLAR')
  AND timestamp >= %(start)s AND timestamp < %(cutoff)s
  AND symbol = ANY(%(syms)s) AND {CANON_SQL} = ANY(%(canons)s)
  AND security_desc !~ '{NON_FUTURES_DESC_RE}'
  {{mlrt}}
GROUP BY cb, symbol, maturity_month_year, platform, d
"""

# Since 2026-07-17 the gateways archive spread legs (442='2') and combos ('3') in
# raw_fills_fix. The fills side of every cross-check here is Outright-only, so
# like-for-like requires the raw side to count PLAIN fills only (442 absent or '1').
# Column-aware: before the migration lands the predicate is empty, so the validator
# keeps working against a pre-rollout database.
_MLRT_PRED_CACHE = None

def _mlrt_pred(db) -> str:
    global _MLRT_PRED_CACHE
    if _MLRT_PRED_CACHE is None:
        row = db.query("""SELECT 1 FROM information_schema.columns
                          WHERE table_name='raw_fills_fix'
                            AND column_name='multi_leg_reporting_type'""", {})
        _MLRT_PRED_CACHE = (
            "AND (multi_leg_reporting_type IS NULL OR multi_leg_reporting_type = '1')"
            if row else ""
        )
    return _MLRT_PRED_CACHE

# per-divergent-contract row pulls (small set), bounded to [retention, cutoff)
OUR_ROWS_SQL = """
SELECT timestamp, side, quantity, price
FROM fills
WHERE account = ANY(%(accounts)s) AND contract = %(contract)s
  AND timestamp >= %(rs)s AND timestamp < %(cutoff)s
  AND price > 0 AND fill_type = 'Outright'
ORDER BY timestamp
"""

RAW_ROWS_SQL = f"""
SELECT timestamp, side, quantity, price, exec_id
FROM raw_fills_fix
WHERE platform = %(feed)s AND symbol = %(sym)s AND maturity_month_year = %(mat)s
  AND {CANON_SQL} = %(cb)s AND timestamp < %(cutoff)s
  {{mlrt}}
  AND security_desc !~ '{NON_FUTURES_DESC_RE}'
ORDER BY timestamp
"""


# ---------------------------------------------------------------------------
# cross_check: per-day GROSS-volume compare for sustained-open (problem) rows
# ---------------------------------------------------------------------------

def cross_check(state: dict) -> dict:
    """For every PROBLEM row (a sustained open), compare each COMPLETED day's GROSS traded volume
    (Σ qty) in `fills` against `raw_fills_fix`, at the canonical (account, symbol, maturity, feed)
    grain. A day whose gross differs by more than Config.GROSS_TOL is marked `mismatch` (a fill is
    probably missing → red). Sets `cell["mismatch"]` per day and `c["has_mismatch"]`. Read-only.

    Like-for-like: the fills side counts fill_type='Outright' only, and the raw side counts plain
    fills only (442 absent/'1' — legs and combos are ARCHIVED in raw_fills_fix since 2026-07-17
    but excluded here via _mlrt_pred, which is empty until the column migration lands) and
    excludes option series riding under the future's (symbol, maturity). A fills-over-FIX surplus fully explained
    by late-inserted fills (recovery backfills) marks the day `backfilled` (informational), not red.

    Today is never flagged — its fills are still aggregating, so the real-time FIX feed would lead
    our batch-processed `fills` and the lag would masquerade as a drop."""
    tol = Config.GROSS_TOL
    today = state["today"]                                   # iso 'YYYY-MM-DD'
    start_dt = datetime.fromisoformat(state["window"]["start_date"]).replace(tzinfo=timezone.utc)
    cutoff = datetime.fromisoformat(today).replace(tzinfo=timezone.utc)  # 00:00 UTC today
    fills_gross = state.get("fills_gross_by_key_day", {})
    late_gross = state.get("fills_late_gross_by_key_day", {})

    # collect the FIX-mappable SUSTAINED-OPEN rows, grouped by canonical key (sub-accounts net
    # together). Spread legs are included on purpose: a feed mismatch (likely a dropped fill) is a
    # real integrity bug even on a book we otherwise exclude, so we must still compare it — engine
    # ._tally then counts it. (sustained_open == problem rows ∪ held spread legs.)
    members_by_key: dict[tuple, list] = defaultdict(list)
    for acct, clist in state["contracts_by_account"].items():
        for c in clist:
            if not c.get("sustained_open"):
                continue
            sym, mat = parse_contract(c["contract"])
            if not sym or c["platform_id"] not in FEED_BY_PLATFORM:
                c["unverifiable"] = True   # option strike / non-FIX platform → can't compare
                continue
            members_by_key[(canon(acct), sym, mat, c["platform_id"])].append(c)

    state["fix_checked"] = True
    if not members_by_key:
        return state

    syms = sorted({k[1] for k in members_by_key})
    canons = sorted({k[0] for k in members_by_key})
    raw_gross: dict[tuple, dict] = defaultdict(dict)         # key -> {iso_day: gross}
    for r in db.query(RAW_GROSS_DAY_SQL.replace('{mlrt}', _mlrt_pred(db)),
                      {"start": start_dt, "cutoff": cutoff, "syms": syms, "canons": canons}):
        pid = 1 if r["platform"] == "I_TT" else 2
        key = (r["cb"], r["symbol"], r["mat"], pid)
        if key in members_by_key:
            raw_gross[key][r["d"].isoformat()] = float(r["gross"] or 0.0)

    for key, members in members_by_key.items():
        fg = fills_gross.get(key, {})
        lg = late_gross.get(key, {})
        # fills_gross day keys are date objects (built in engine.compute_state) → normalise to iso
        fg_iso = {(d.isoformat() if hasattr(d, "isoformat") else d): v for d, v in fg.items()}
        lg_iso = {(d.isoformat() if hasattr(d, "isoformat") else d): v for d, v in lg.items()}
        rg = raw_gross.get(key, {})
        # If the FIX feed carries NO rows for this key over the window, we can't verify it (a give-up
        # / clearing-alias account, or a product that clears under another symbol) — it is NOT a
        # dropped fill. Leave it yellow/unverifiable rather than painting a false red.
        verifiable = sum(rg.values()) > tol
        for c in members:
            if not verifiable:
                c["unverifiable"] = True
                c["has_mismatch"] = False
                continue
            any_mm = False
            for cell in c["days"]:
                d = cell["date"]
                if d >= today:                              # today / future: in-flight, never flag
                    continue
                rv = rg.get(d, 0.0)
                fv = fg_iso.get(d, 0.0)
                cell["raw_gross"] = round(rv, 6)            # FIX-feed gross that day (for the tooltip)
                cell["cmp_gross"] = round(fv, 6)            # our FIX-comparable gross (Outright only)
                diff = fv - rv
                if abs(diff) <= tol:
                    continue
                if diff > tol and abs(diff - lg_iso.get(d, 0.0)) <= tol:
                    # surplus fully explained by late-inserted (recovery-backfilled) fills — the
                    # feed can never contain those; informational, not a drop.
                    cell["backfilled"] = True
                    continue
                cell["mismatch"] = True
                any_mm = True
            c["has_mismatch"] = any_mm
    return state


def _fill(leg, feed) -> dict:
    """leg = (side, qty, price, row). row is a raw_fills_fix row (has exec_id) or our fill row."""
    side, qty, price, row = leg
    ts = row[0] if row is not None else None
    exec_id = row[4] if (row is not None and len(row) > 4) else None
    out = {"timestamp": ts.isoformat() if ts else None, "day": ts.date().isoformat() if ts else None,
           "side": int(side), "qty": float(qty),
           "price": float(price) if price is not None else None}
    if feed and exec_id:
        out["uniqueExecId"] = parse_uid(exec_id)
    return out


# ---------------------------------------------------------------------------
# account_diff: on-demand single-contract FIX diff (the drill-down / reingest-ready output)
# ---------------------------------------------------------------------------

def account_diff(account: str, contract: str) -> dict:
    """Diff one (account, contract) against the FIX feed and return the missing/extra fills with
    uniqueExecId — the same comparison the overview does, on demand and reingest-ready."""
    sym, mat = parse_contract(contract)
    if not sym:
        return {"account": account, "contract": contract,
                "error": "contract not FIX-mappable (option strike or non-future label)"}
    cb = canon(account)
    # determine the feed from the account's platform
    prow = db.query("SELECT platform_id FROM trader_platforms WHERE platform_account = %(a)s LIMIT 1",
                    {"a": account})
    pid = prow[0]["platform_id"] if prow else 1
    feed = FEED_BY_PLATFORM.get(pid, "I_TT")
    accts = sorted({r["account"] for r in db.query(
        f"SELECT DISTINCT account FROM fills WHERE {CANON_SQL} = %(cb)s AND platform_id = %(pid)s",
        {"cb": cb, "pid": pid})} | {account})
    # drill-down includes everything up to now (the operator wants the full picture)
    cutoff = datetime.now(timezone.utc)

    raw_agg = db.query(RAW_ROWS_SQL.replace('{mlrt}', _mlrt_pred(db)), {"feed": feed, "sym": sym, "mat": mat, "cb": cb, "cutoff": cutoff})
    if not raw_agg:
        return {"account": account, "contract": contract, "feed": feed, "raw_fills": 0,
                "verdict": "unverifiable", "note": "no FIX rows (pre-retention / give-up account)"}

    our_rows = db.query(OUR_ROWS_SQL, {"accounts": accts, "contract": contract,
                                       "rs": RETENTION_START, "cutoff": cutoff})
    our_t = [(r["timestamp"], r["side"], r["quantity"], r["price"]) for r in our_rows]
    raw_t = [(r["timestamp"], r["side"], r["quantity"], r["price"], r["exec_id"]) for r in raw_agg]
    our_ret = _net([(int(s), float(q), None, None) for _, s, q, _ in our_t])
    raw_net = _net([(int(s), float(q), None, None) for _, s, q, _, _ in raw_t])
    pre = float(db.query("""SELECT COALESCE(SUM(CASE WHEN side=1 THEN quantity ELSE -quantity END),0)
                            FROM fills WHERE account = ANY(%(a)s) AND contract = %(c)s AND timestamp < %(rs)s""",
                         {"a": accts, "c": contract, "rs": RETENTION_START})[0]["coalesce"])

    miss, miss_net, miss_m = _find_missing(our_t, raw_t, raw_net - our_ret)
    extra, extra_net, extra_m = _find_missing(raw_t, our_t, our_ret - raw_net)
    return {
        "account": account, "contract": contract, "feed": feed, "canonical_account": cb,
        "our_net_retention": round(our_ret, 4), "fix_net": round(raw_net, 4),
        "pre_retention_carry": round(pre, 4),
        "our_fills": len(our_t), "raw_fills": len(raw_t),
        "missing_from_us": [_fill(l, feed) for l in miss], "missing_net": round(miss_net, 4),
        "extra_in_us": [_fill(l, feed) for l in extra], "extra_net": round(extra_net, 4),
        "method": miss_m,
    }
