"""FIX-feed cross-check — the authoritative per-account drop / extra / mis-attribution detector.

This REPLACES the old TT *position* cross-check (`tt.enrich`), which used the
`ttmonitor/.../position` endpoint — and that endpoint ignores the `accountId` filter, so it
cross-nets accounts and produces both false positives and false negatives. The real source of
truth is the FIX feed `raw_fills_fix`, an independent in-database copy of every fill:
  * TT accounts  (platform_id=1) → `raw_fills_fix` platform **I_TT**   (a second, push copy of
    the TT REST data; a divergence means the TT-API pull dropped a fill — watermark / µs-collision).
  * Stellar accts (platform_id=2) → `raw_fills_fix` platform **I_STELLAR** (the very source the
    Stellar processor builds `fills` from; a divergence means a processing skip / mis-attribution).

For every non-flat `(account, contract)` we compute, WITHIN the FIX retention window, our net vs
the FIX net and emit one verdict (see PRINCIPLES.md — `net ≠ 0` is a lost fill, an extra/mis-
attributed fill, or a genuine open, NEVER an expiry-settlement):

  our_ret == FIX            → genuine  (CONFIRMED_OPEN if ≠0, else flat)
  FIX has fills we lack     → DROP             (recoverable; surfaces the missing fills + day)
  we have fills FIX lacks   → EXTRA_MISATTR    (duplicate / mis-attributed; e.g. an alias order)
  carried in pre-retention  → PARTIAL_CARRY    (opened before the wall; opening is unrecoverable)
  no FIX rows at all        → UNVERIFIABLE     (give-up account / option-strike / pre-retention)
  diverges but can't pin    → UNRECONCILED     (investigate — block-vs-leg / synthetic markers)

Account match is **label-robust**: the REST feed labels accounts `LFCTEU150_MA`, the FIX feed
uses `LFCTEU150` / `&LFCTEU150` / `LFCTEU150:…`. Sub-account suffixes (`_MA`,`_AL`,`_JPX`,…) belong
to the SAME trader, so we canonicalize both sides to a base account and aggregate at the
`(canonical_account, symbol, maturity, platform)` grain (= the economic position).

Read-only. The actual recovery lives in `aws-mwaa-local-runner/dags/misc/recovery/` (raw_diff_ts.py
→ reingest.py → recalc_trader.py); this module only diagnoses + produces a reingest-ready diff.
The per-second / count-excess / cumulative matchers are ported from `recovery/raw_diff_ts.py`.
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

# raw FIX net per (canonical account, symbol, maturity, feed) — ONE pass over raw_fills_fix,
# AS OF the cutoff (end of the last completed UTC day) so today's in-flight fills don't count.
RAW_NET_SQL = f"""
SELECT {CANON_SQL} AS cb, symbol, maturity_month_year AS mat, platform,
       SUM(CASE WHEN side = 1 THEN quantity ELSE -quantity END) AS net,
       COUNT(*) AS n
FROM raw_fills_fix
WHERE platform IN ('I_TT', 'I_STELLAR') AND timestamp < %(cutoff)s
GROUP BY cb, symbol, maturity_month_year, platform
"""

# our net BEFORE the retention wall, per (account, contract) — the pre-retention carry.
PRE_NET_SQL = """
SELECT account, contract,
       SUM(CASE WHEN side = 1 THEN quantity ELSE -quantity END) AS net
FROM fills
WHERE account = ANY(%(accounts)s) AND timestamp < %(rs)s
GROUP BY account, contract
"""

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
ORDER BY timestamp
"""


# ---------------------------------------------------------------------------
# enrich: resolve a FIX verdict for every non-flat active contract
# ---------------------------------------------------------------------------

def enrich(state: dict) -> dict:
    """Resolve `c["verdict"]` (new taxonomy) + attach `c["fix"]` for every non-flat active contract.
    Mutates state in place. Also builds `state["drop_rollup"]` (drops clustered by ingestion day)."""
    eps = Config.FLAT_EPS
    tol = Config.FIX_NET_TOL
    cohort = state["cohort"]
    accounts = sorted({r["account"] for r in cohort})
    today_delta = state.get("today_delta", {})
    # cutoff = end of the last completed UTC day (today 00:00 UTC); judge EOD, exclude in-flight.
    cutoff = datetime.fromisoformat(state["window"]["end_date"]).replace(tzinfo=timezone.utc)

    # canon -> the set of real fills-accounts that share it (for per-contract row pulls)
    canon_accounts: dict[str, set] = defaultdict(set)
    for a in accounts:
        canon_accounts[canon(a)].add(a)

    # --- batched heavy reads (cached upstream; ~12s cold) ---
    raw_idx: dict[tuple, tuple] = {}
    for r in db.query(RAW_NET_SQL, {"cutoff": cutoff}):
        pid = 1 if r["platform"] == "I_TT" else 2
        raw_idx[(r["cb"], r["symbol"], r["mat"], pid)] = (float(r["net"] or 0.0), int(r["n"]))

    pre_idx: dict[tuple, float] = {}
    for r in db.query(PRE_NET_SQL, {"accounts": accounts, "rs": RETENTION_START}):
        pre_idx[(r["account"], r["contract"])] = float(r["net"] or 0.0)

    # --- canonical aggregation of OUR side over ALL contributors (sub-accounts net together),
    #     AS OF the cutoff: current all-history net minus today's in-flight delta. ---
    # key = (canon, symbol, maturity, platform_id)
    our_net: dict[tuple, float] = defaultdict(float)
    our_pre: dict[tuple, float] = defaultdict(float)
    key_contract: dict[tuple, str] = {}
    for nr in state["net_all"]:
        sym, mat = parse_contract(nr["contract"])
        if not sym:
            continue
        key = (canon(nr["account"]), sym, mat, nr["platform_id"])
        cutoff_net = float(nr["net"] or 0.0) - today_delta.get((nr["account"], nr["contract"]), 0.0)
        our_net[key] += cutoff_net
        our_pre[key] += pre_idx.get((nr["account"], nr["contract"]), 0.0)
        key_contract.setdefault(key, nr["contract"])

    # --- collect the non-flat active contracts that need a verdict, grouped by canonical key ---
    contracts_by_key: dict[tuple, list] = defaultdict(list)
    for acct, clist in state["contracts_by_account"].items():
        for c in clist:
            if c["category"] != "active":
                continue
            if abs(c["current_net"]) <= eps:
                continue
            if c.get("verdict") == "stranded":  # aggregation problem owns the verdict; skip net check
                continue
            sym, mat = parse_contract(c["contract"])
            if not sym or c["platform_id"] not in FEED_BY_PLATFORM:
                _set(c, "unverifiable", {"reason": "unmappable (option/strike or non-FIX platform)"})
                continue
            key = (canon(acct), sym, mat, c["platform_id"])
            contracts_by_key[key].append(c)

    drop_fills: list[dict] = []      # for the by-ingestion-day rollup

    for key, members in contracts_by_key.items():
        cb, sym, mat, pid = key
        feed = FEED_BY_PLATFORM[pid]
        raw_net, raw_n = raw_idx.get(key, (0.0, 0))
        net = our_net.get(key, sum(m["current_net"] for m in members))
        pre = our_pre.get(key, 0.0)
        our_ret = net - pre

        if raw_n == 0:
            verdict, fix = "unverifiable", {
                "feed": feed, "raw_net": 0.0, "raw_n": 0, "our_net": round(net, 4),
                "reason": "no FIX rows (pre-retention / give-up / clearing-alias account)"}
        elif abs(our_ret - raw_net) < tol:
            if abs(raw_net) < tol:
                # retention matches and FIX flat -> any residual net is pre-retention carry
                verdict = "partial_carry" if abs(pre) > tol else "flat"
                fix = {"feed": feed, "raw_net": round(raw_net, 4), "raw_n": raw_n,
                       "our_net": round(net, 4), "pre_retention": round(pre, 4),
                       "reason": "retention reconciles; residual carried in before the FIX wall"}
            else:
                verdict = "confirmed_open"
                fix = {"feed": feed, "raw_net": round(raw_net, 4), "raw_n": raw_n,
                       "our_net": round(net, 4), "pre_retention": round(pre, 4),
                       "reason": "our net == FIX net — a genuine open position"}
        elif abs(pre) > tol:
            # diverges AND carried a pre-retention position -> can't cleanly auto-classify
            verdict = "partial_carry"
            fix = {"feed": feed, "raw_net": round(raw_net, 4), "raw_n": raw_n,
                   "our_net": round(net, 4), "pre_retention": round(pre, 4),
                   "reason": "pre-retention carry + possible in-window drop (manual review)"}
        else:
            verdict, fix = _discriminate(key, members, our_ret, raw_net, raw_n,
                                         canon_accounts.get(cb, {cb}), key_contract.get(key), cutoff)
            # a known spread/curve leg keeps its verdict (faint cell) but stays OUT of the systemic
            # drop-by-day rollup — per-leg divergence is expected and not something we chase.
            if verdict == "drop" and not any(m.get("is_spread") for m in members):
                drop_fills.extend(fix.get("missing", []))

        for c in members:
            _set(c, verdict, fix)

    state["drop_rollup"] = _rollup_by_day(drop_fills)
    state["fix_checked"] = True
    return state


def _discriminate(key, members, our_ret, raw_net, raw_n, accounts, contract, cutoff):
    """Net diverges with a flat anchor — pull the fills and decide DROP vs EXTRA_MISATTR.
    DROP  = FIX has fills we lack (recover them).  EXTRA_MISATTR = we have fills FIX lacks."""
    cb, sym, mat, pid = key
    feed = FEED_BY_PLATFORM[pid]
    our_rows = db.query(OUR_ROWS_SQL, {"accounts": sorted(accounts), "contract": contract,
                                       "rs": RETENTION_START, "cutoff": cutoff})
    raw_rows = db.query(RAW_ROWS_SQL, {"feed": feed, "sym": sym, "mat": mat, "cb": cb,
                                       "cutoff": cutoff})
    our_t = [(r["timestamp"], r["side"], r["quantity"], r["price"]) for r in our_rows]
    raw_t = [(r["timestamp"], r["side"], r["quantity"], r["price"], r["exec_id"]) for r in raw_rows]

    miss, miss_net, miss_m = _find_missing(our_t, raw_t, raw_net - our_ret)    # FIX legs we lack
    extra, extra_net, extra_m = _find_missing(raw_t, our_t, our_ret - raw_net)  # our legs FIX lacks

    base = {"feed": feed, "raw_net": round(raw_net, 4), "raw_n": raw_n,
            "our_net": round(our_ret, 4), "gap": round(raw_net - our_ret, 4)}

    drop_ok = miss_m != "unreconciled" and abs((our_ret + miss_net) - raw_net) < Config.FIX_NET_TOL
    extra_ok = extra_m != "unreconciled" and abs((our_ret - extra_net) - raw_net) < Config.FIX_NET_TOL

    if drop_ok and abs(miss_net) >= abs(extra_net):
        return "drop", {**base, "method": miss_m, "missing_count": len(miss),
                        "recoverable_net": round(miss_net, 4),
                        "missing": [_fill(l, feed) for l in miss]}
    if extra_ok:
        return "extra_misattr", {**base, "method": extra_m, "extra_count": len(extra),
                                 "extra_net": round(extra_net, 4),
                                 "extra": [_fill(l, feed) for l in extra]}
    return "unreconciled", {**base, "reason": "net diverges but neither direction reconciles "
                            "(block-vs-leg / synthetic price markers / spread legs) — investigate",
                            "miss_net": round(miss_net, 4), "extra_net": round(extra_net, 4)}


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


def _rollup_by_day(drop_fills: list[dict]) -> list[dict]:
    """Drops cluster on ingestion events (e.g. 2026-06-11 17:30). Collapse them so a systemic gap
    is ONE row, not fifty: group missing fills by UTC ingestion day."""
    by_day: dict[str, dict] = {}
    for f in drop_fills:
        d = f.get("day")
        if not d:
            continue
        slot = by_day.setdefault(d, {"day": d, "fills": 0, "net": 0.0})
        slot["fills"] += 1
        slot["net"] += f["qty"] if f["side"] == 1 else -f["qty"]
    out = sorted(by_day.values(), key=lambda x: x["day"], reverse=True)
    for o in out:
        o["net"] = round(o["net"], 4)
    return out


def _set(contract: dict, verdict: str, fix: dict | None):
    contract["verdict"] = verdict
    contract["fix"] = fix


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

    raw_agg = db.query(RAW_ROWS_SQL, {"feed": feed, "sym": sym, "mat": mat, "cb": cb, "cutoff": cutoff})
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
