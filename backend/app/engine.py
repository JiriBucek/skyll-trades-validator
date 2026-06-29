"""Validation engine: read-only computation of fills -> trades -> daily-candle integrity.

Pipeline:
  compute_state(window_days)  -> heavy read-only DB work; raw state + provisional verdicts (no FIX)
  fixfeed.enrich(state)       -> the authoritative per-account FIX-feed cross-check (DROP / EXTRA /
                                 CONFIRMED_OPEN / UNVERIFIABLE / PARTIAL_CARRY), replacing the old
                                 TT-position guess. (network-free; reads raw_fills_fix.)
  assemble_tree(state)        -> group/trader/account JSON tree + per-day roll-ups + health header

Taxonomy (every non-flat cell is exactly one of these — see docs/IMPROVEMENT-PLAN.md / PRINCIPLES.md):
  flat · confirmed_open · partial_carry · unverifiable · orphan · unreconciled · extra_misattr ·
  stranded · drop · settled_residual(display-triage). Only DROP / EXTRA_MISATTR / STRANDED are
  🔴 actionable; everything expected (flat, genuine opens, pre-retention carries, ancient
  residuals) collapses out of the way.

All work is read-only. Day boundaries are UTC to match the daily-candle rollup.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import re

from . import db
from .config import Config
from .contracts import is_expired

# A contract is IN SCOPE for the integrity model iff it's a clean future "SYM MmmYY" — the same
# grain the FIX cross-check maps. Option strikes (a P/C-strike token) and synthetic markers are
# out of scope across every detector (FIX, stranding, orphan): the model is fills→trades→profit on
# FUTURES, and stranded/unlinked option dust is not actionable (operator leaves it).
_FUTURE_RE = re.compile(r"^[A-Za-z0-9]+\s+[A-Za-z]{3}\d{2}$")


def in_scope(contract: str | None) -> bool:
    return bool(_FUTURE_RE.fullmatch((contract or "").strip()))

# severity ordering for roll-ups (higher = worse, wins a cell/day). Only the last three are 🔴.
SEVERITY = {
    "flat": 0,
    "settled_residual": 1,
    "partial_carry": 2,
    "confirmed_open": 3,
    "pending_fix": 4,        # transient: a non-flat contract before the FIX check resolves it
    "unverifiable": 4,
    "orphan": 5,
    "unreconciled": 6,
    "extra_misattr": 7,
    "stranded": 8,
    "drop": 9,
}
ACTIONABLE = ("drop", "extra_misattr", "stranded")
# verdicts whose trailing open run paints its colour across the cells
OPEN_RUN_VERDICTS = ("drop", "extra_misattr", "unreconciled", "confirmed_open",
                     "unverifiable", "partial_carry", "pending_fix")

# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

COHORT_SQL = """
SELECT t.id AS trader_id, t.name AS trader_name,
       g.id AS group_id, g.name AS group_name,
       tp.platform_id, p.name AS platform_name,
       tp.platform_account AS account,
       COALESCE(tp.is_sim_account, false) AS is_sim,
       COALESCE(tp.opt_out, false)        AS opt_out
FROM group_members gm
JOIN groups   g ON g.id = gm.group_id
JOIN traders  t ON t.id = gm.trader_id
JOIN trader_platforms tp ON tp.trader_id = t.id
JOIN platforms p ON p.id = tp.platform_id
WHERE COALESCE(g.is_archived, false) = false
  AND COALESCE(t.is_archived, false) = false
  AND tp.platform_account IS NOT NULL AND tp.platform_account <> ''
ORDER BY g.name, t.name, p.name, tp.platform_account
"""

NET_ALL_SQL = """
SELECT account, contract, platform_id,
       SUM(CASE WHEN side = 1 THEN quantity ELSE -quantity END) AS net,
       MIN(timestamp) AS first_fill,
       MAX(timestamp) AS last_fill,
       COUNT(*)       AS n_fills
FROM fills
WHERE account = ANY(%(accounts)s)
GROUP BY account, contract, platform_id
"""

WINDOW_SQL = """
SELECT account, contract, platform_id,
       (timestamp AT TIME ZONE 'UTC')::date AS d,
       SUM(CASE WHEN side = 1 THEN quantity ELSE -quantity END) AS net_delta,
       COUNT(*) AS n_fills,
       COUNT(*) FILTER (WHERE trade_ids IS NULL OR trade_ids::text IN ('[]', '')) AS n_orphan,
       COUNT(*) FILTER (
         WHERE (trade_ids IS NULL OR trade_ids::text IN ('[]', ''))
           AND trader_id = ANY(%(stranded_ids)s)
       ) AS n_stranded
FROM fills
WHERE account = ANY(%(accounts)s)
  AND timestamp >= %(start)s
GROUP BY account, contract, platform_id, d
"""

# All-history stranding: fills on a real cohort account that aggregated under trader_id 0
# (Unassigned) / 349 (IgnoredAccounts) and were never linked to a trade (trade_ids NULL) — the
# Josh-Gadenne class (account not yet in trader_platforms when its fills ingested). This is an
# aggregation gap (fix = recalc_trader, NO backfill), not a dropped fill, and is independent of the
# display window. Excludes today (fills may still be aggregating).
STRANDED_ALL_SQL = """
SELECT account, contract, platform_id,
       COUNT(*) AS n,
       SUM(CASE WHEN side = 1 THEN quantity ELSE -quantity END) AS stranded_net,
       MAX(timestamp) AS last_stranded
FROM fills
WHERE account = ANY(%(accounts)s)
  AND trader_id = ANY(%(stranded_ids)s)
  AND (trade_ids IS NULL OR trade_ids::text IN ('[]', ''))
  AND timestamp < %(cutoff)s
GROUP BY account, contract, platform_id
"""

REALIZED_SQL = """
SELECT account,
       (close_time AT TIME ZONE 'UTC')::date AS d,
       SUM(profit) AS realized,
       COUNT(*)    AS n_trades,
       COUNT(*) FILTER (
         WHERE (open_time AT TIME ZONE 'UTC')::date <> (close_time AT TIME ZONE 'UTC')::date
       ) AS n_cross_day
FROM trades
WHERE account = ANY(%(accounts)s)
  AND close_time >= %(start)s
GROUP BY account, d
"""

CANDLE_SQL = """
SELECT account, date AS d, close_pnl
FROM intraday_daily_profit_loss
WHERE account = ANY(%(accounts)s)
  AND date >= %(start_date)s
  AND product_id IS NULL
"""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _today_utc() -> date:
    return datetime.now(timezone.utc).date()


def _window(window_days: int):
    today = _today_utc()
    start_date = today - timedelta(days=window_days - 1)
    start_dt = datetime(start_date.year, start_date.month, start_date.day, tzinfo=timezone.utc)
    days = [start_date + timedelta(days=i) for i in range((today - start_date).days + 1)]
    return today, start_date, start_dt, days


def _iso(v):
    return v.isoformat() if v is not None else None


# ---------------------------------------------------------------------------
# state computation (no network)
# ---------------------------------------------------------------------------

def compute_state(window_days: int | None = None) -> dict:
    window_days = window_days or Config.WINDOW_DAYS
    today, start_date, start_dt, days = _window(window_days)
    eps = Config.FLAT_EPS

    cohort = db.query(COHORT_SQL)
    accounts = sorted({r["account"] for r in cohort})
    if not accounts:
        raise RuntimeError("No grouped-trader accounts found.")

    cutoff_dt = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
    net_all = db.query(NET_ALL_SQL, {"accounts": accounts})
    window = db.query(WINDOW_SQL, {"accounts": accounts, "start": start_dt,
                                   "stranded_ids": list(Config.STRANDED_TRADER_IDS)})
    stranded_rows = db.query(STRANDED_ALL_SQL, {"accounts": accounts, "cutoff": cutoff_dt,
                                                "stranded_ids": list(Config.STRANDED_TRADER_IDS)})
    realized = db.query(REALIZED_SQL, {"accounts": accounts, "start": start_dt})
    candles = db.query(CANDLE_SQL, {"accounts": accounts, "start_date": start_date})

    # (account, contract) -> all-history stranded fills (forces the contract visible + 🔴 STRANDED).
    # Futures only — option-strike / synthetic stranding is out of scope (see in_scope()).
    stranded_idx: dict[tuple, dict] = {}
    for r in stranded_rows:
        if not in_scope(r["contract"]):
            continue
        stranded_idx[(r["account"], r["contract"])] = {
            "n": int(r["n"]), "net": round(float(r["stranded_net"] or 0.0), 6),
            "last": _iso(r["last_stranded"]),
        }

    # index window deltas: (account, contract) -> {date: row}
    win_idx: dict[tuple, dict] = defaultdict(dict)
    for r in window:
        win_idx[(r["account"], r["contract"])][r["d"]] = r

    # today's signed delta per (account, contract). The FIX cross-check judges EOD of the last
    # COMPLETED day (the flat-test boundary), so today's in-flight fills are excluded from both
    # sides — otherwise the real-time FIX feed leads our batch-processed `fills` on the actively
    # trading front month and lag masquerades as a flood of "drops".
    today_delta: dict[tuple, float] = {}
    for key, bydate in win_idx.items():
        row = bydate.get(today)
        if row:
            today_delta[key] = float(row["net_delta"] or 0.0)

    # reconciliation: account -> {date: {...}}
    recon: dict[str, dict] = defaultdict(dict)
    for r in realized:
        recon[r["account"]].setdefault(r["d"], {})["realized"] = float(r["realized"] or 0.0)
        recon[r["account"]][r["d"]]["n_trades"] = r["n_trades"]
        recon[r["account"]][r["d"]]["n_cross_day"] = r["n_cross_day"]
    for r in candles:
        recon[r["account"]].setdefault(r["d"], {})["candle_close"] = (
            None if r["close_pnl"] is None else float(r["close_pnl"])
        )

    contracts_by_account: dict[str, list] = defaultdict(list)

    for nr in net_all:
        account, contract = nr["account"], nr["contract"]
        platform_id = nr["platform_id"]
        current_net = float(nr["net"] or 0.0)
        deltas = win_idx.get((account, contract), {})
        has_window_fills = sum(d["n_fills"] for d in deltas.values()) > 0
        expired = is_expired(contract, today)
        is_open = abs(current_net) > eps
        # Stranding (futures, unlinked under trader 0/349) is an actionable aggregation gap — the
        # Josh-Gadenne Euribor class — so it forces the contract into the active view even when
        # dormant/expired. (stranded_idx is already futures-only.)
        stranded_info = stranded_idx.get((account, contract))
        include = has_window_fills or (is_open and expired is not True) or bool(stranded_info)
        if not include:
            if is_open and expired is True:
                # dormant residual on an expired contract -> settled (display-triage) bucket
                contracts_by_account[account].append({
                    "account": account, "contract": contract,
                    "platform_id": platform_id,
                    "current_net": round(current_net, 6),
                    "first_fill": _iso(nr["first_fill"]), "last_fill": _iso(nr["last_fill"]),
                    "expired": expired, "category": "stale_residual",
                    "days": [], "switch_on": None, "has_orphans": False, "has_stranded": False,
                    "verdict": "settled_residual", "fix": None, "stranded_info": None,
                })
            # else: fully dormant + flat -> skip entirely
            continue

        # walk the window forward: opening balance = current_net - sum(window deltas)
        total_window_delta = sum(float(d["net_delta"] or 0.0) for d in deltas.values())
        opening = current_net - total_window_delta

        scope = in_scope(contract)  # orphan/stranding signals apply to futures only
        day_cells = []
        running = opening
        has_orphans = False
        has_stranded = bool(stranded_info)
        for d in days:
            row = deltas.get(d)
            running += float(row["net_delta"]) if row else 0.0
            n_orphan = int(row["n_orphan"]) if row else 0
            n_stranded = int(row["n_stranded"]) if row else 0
            # orphans/stranding only count on completed days (today's fills may be pending agg),
            # and only for in-scope futures
            orphan_completed = n_orphan if (d < today and scope) else 0
            stranded_completed = n_stranded if (d < today and scope) else 0
            if orphan_completed:
                has_orphans = True
            if stranded_completed:
                has_stranded = True
            day_cells.append({
                "date": d.isoformat(),
                "eod_net": round(running, 6),
                "flat": abs(running) <= eps,
                "n_fills": int(row["n_fills"]) if row else 0,
                "n_orphan": orphan_completed,
                "n_stranded": stranded_completed,
            })

        # switch-on day = first day of the current trailing non-zero run.
        switch_on = None
        if is_open:
            switch_on = "before_window"
            for cell in reversed(day_cells):
                if cell["flat"]:
                    break
                switch_on = cell["date"]
            else:
                switch_on = "before_window"

        # provisional verdict. Stranding (unaggregated trader_id 0/IgnoredAccounts fills) is the
        # actionable root cause and owns the verdict; otherwise a non-flat contract is left
        # 'pending_fix' for fixfeed.enrich to resolve, and a flat-with-orphans contract is 'orphan'.
        if has_stranded:
            verdict = "stranded"
        elif is_open:
            verdict = "pending_fix"
        elif has_orphans:
            verdict = "orphan"
        else:
            verdict = "flat"

        contracts_by_account[account].append({
            "account": account, "contract": contract, "platform_id": platform_id,
            "current_net": round(current_net, 6),
            "first_fill": _iso(nr["first_fill"]), "last_fill": _iso(nr["last_fill"]),
            "expired": expired, "category": "active",
            "days": day_cells, "switch_on": switch_on,
            "has_orphans": has_orphans, "has_stranded": has_stranded,
            "verdict": verdict, "fix": None, "stranded_info": stranded_info,
        })

    return {
        "cohort": cohort,
        "net_all": net_all,
        "today_delta": today_delta,
        "window": {
            "start_date": start_date.isoformat(),
            "end_date": today.isoformat(),
            "days": [d.isoformat() for d in days],
        },
        "contracts_by_account": contracts_by_account,
        "recon_by_account": recon,
        "drop_rollup": [],
        "fix_checked": False,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# per-day cell severity + roll-ups
# ---------------------------------------------------------------------------

def _cell_state(contract: dict, cell: dict) -> str:
    """Resolve a single day cell's state for a contract (after FIX enrichment)."""
    if not cell["flat"]:
        # part of the current trailing open run? -> paint with the contract's verdict colour
        if contract["switch_on"] and contract["verdict"] in OPEN_RUN_VERDICTS:
            run_start = contract["switch_on"]
            if run_start == "before_window" or cell["date"] >= run_start:
                v = contract["verdict"]
                return "unverifiable" if v == "pending_fix" else v
        return "unverifiable"
    if cell.get("n_stranded", 0) > 0:
        return "stranded"
    if cell["n_orphan"] > 0:
        return "orphan"
    return "flat"


def _worst(states) -> str:
    best = "flat"
    for s in states:
        if SEVERITY.get(s, 0) > SEVERITY.get(best, 0):
            best = s
    return best


def assemble_tree(state: dict) -> dict:
    cohort = state["cohort"]
    days = state["window"]["days"]
    cba = state["contracts_by_account"]
    recon = state["recon_by_account"]

    groups: dict[int, dict] = {}
    seen = set()

    # Group membership is the single source of truth for who's in scope: the client removes genuine
    # spread traders from the group upstream, so the cohort (group_members) already excludes them —
    # we no longer maintain a hard-coded spread list (the old collapse_pct heuristic mislabelled
    # several non-spread traders anyway).
    for r in cohort:
        gkey, tkey = r["group_id"], r["trader_id"]
        g = groups.setdefault(gkey, {
            "group_id": gkey, "group_name": r["group_name"], "traders": {},
        })
        t = g["traders"].setdefault(tkey, {
            "trader_id": tkey, "trader_name": r["trader_name"] or f"trader {tkey}",
            "accounts": {},
        })
        akey = r["account"]
        if (gkey, tkey, akey) in seen:
            continue
        seen.add((gkey, tkey, akey))

        contracts = cba.get(akey, [])
        active = [c for c in contracts if c["category"] == "active"]
        residual = [c for c in contracts if c["category"] == "stale_residual"]
        t["accounts"][akey] = {
            "account": akey,
            "platform_id": r["platform_id"], "platform_name": r["platform_name"],
            "is_sim": r["is_sim"], "opt_out": r["opt_out"],
            "active": active, "residual": residual,
        }

    out_groups = []
    for g in sorted(groups.values(), key=lambda x: (x["group_name"] or "")):
        g_day = {d: "flat" for d in days}
        g_summary = _empty_summary()
        out_traders = []
        for t in sorted(g["traders"].values(), key=lambda x: (x["trader_name"] or "")):
            t_day = {d: "flat" for d in days}
            t_summary = _empty_summary()
            open_since = None
            t_recon_flags = 0
            accounts_out = []
            for a in sorted(t["accounts"].values(), key=lambda x: x["account"]):
                for c in a["active"]:
                    for cell in c["days"]:
                        s = _cell_state(c, cell)
                        cell["state"] = s
                        if SEVERITY[s] > SEVERITY[t_day[cell["date"]]]:
                            t_day[cell["date"]] = s
                    _tally(t_summary, c)
                    if c["verdict"] in OPEN_RUN_VERDICTS:
                        so = c["switch_on"]
                        if so and so != "before_window":
                            open_since = so if open_since is None else min(open_since, so)
                        elif so == "before_window":
                            open_since = open_since or "before_window"
                for c in a["residual"]:
                    _tally(t_summary, c)
                for d in days:
                    rec = recon.get(a["account"], {}).get(_as_date(d))
                    if rec and _recon_unexplained(rec):
                        t_recon_flags += 1
                accounts_out.append(a)

            t_worst = _worst(t_day.values())
            out_traders.append({
                "trader_id": t["trader_id"], "trader_name": t["trader_name"],
                "accounts": accounts_out,
                "day_status": t_day, "worst": t_worst,
                "open_since": open_since, "recon_flags": t_recon_flags,
                "summary": t_summary,
            })
            for d in days:
                if SEVERITY[t_day[d]] > SEVERITY[g_day[d]]:
                    g_day[d] = t_day[d]
            _merge_summary(g_summary, t_summary)

        out_groups.append({
            "group_id": g["group_id"], "group_name": g["group_name"],
            "traders": out_traders,
            "day_status": g_day, "worst": _worst(g_day.values()),
            "summary": g_summary,
        })

    overall = _empty_summary()
    for g in out_groups:
        _merge_summary(overall, g["summary"])

    return {
        "window": state["window"],
        "generated_at": state["generated_at"],
        "groups": out_groups,
        "overall": overall,
        "drop_rollup": state.get("drop_rollup", []),
        "health": _health(overall, state.get("drop_rollup", [])),
        "fix_checked": state.get("fix_checked", False),
    }


def _as_date(iso_str: str) -> date:
    return date.fromisoformat(iso_str)


def _empty_summary():
    return {k: 0 for k in SEVERITY} | {"active_contracts": 0}


def _tally(summary, contract):
    summary["active_contracts"] += 1 if contract["category"] == "active" else 0
    v = contract["verdict"]
    if v in summary:
        summary[v] += 1


def _merge_summary(dst, src):
    for k, v in src.items():
        dst[k] = dst.get(k, 0) + v


def _health(overall: dict, drop_rollup: list) -> dict:
    """The one-line top-of-page verdict (1c). Healthy = zero actionable findings."""
    drop_fills = sum(d["fills"] for d in drop_rollup)
    h = {
        "drop_contracts": overall.get("drop", 0),
        "drop_windows": len(drop_rollup),
        "drop_fills": drop_fills,
        "extra_misattr": overall.get("extra_misattr", 0),
        "stranded": overall.get("stranded", 0),
        "unreconciled": overall.get("unreconciled", 0),
        "unverifiable": overall.get("unverifiable", 0),
        "confirmed_open": overall.get("confirmed_open", 0),
        "partial_carry": overall.get("partial_carry", 0),
        "orphan": overall.get("orphan", 0),
        "flat": overall.get("flat", 0),
    }
    h["actionable"] = h["drop_contracts"] + h["extra_misattr"] + h["stranded"]
    h["healthy"] = h["actionable"] == 0
    h["headline"] = (
        f"{h['drop_windows']} drop window(s) ({h['drop_fills']} fills, recoverable) · "
        f"{h['extra_misattr']} mis-attributed · {h['stranded']} stranded · "
        f"{h['unreconciled']} unreconciled · {h['unverifiable']} unverifiable — "
        f"everything else flat/open/carry"
    )
    return h


# ---------------------------------------------------------------------------
# fill history for one (account, contract) — the drill-down "what did he do" view
# ---------------------------------------------------------------------------

FILLS_HISTORY_SQL = """
SELECT timestamp, side, quantity, price, trader_id, fill_type, trade_ids
FROM fills
WHERE account = %(account)s AND contract = %(contract)s
ORDER BY timestamp ASC
"""


def fills_history(account: str, contract: str, limit: int = 5000) -> dict:
    """Every fill for one (account, contract), with a chronological RUNNING POSITION (signed
    cumulative qty, buy +, sell −) so you can watch the position build and unwind. Returned
    latest-first; the running position is absolute (computed over all history) even if the list is
    truncated to the most recent `limit`."""
    rows = db.query(FILLS_HISTORY_SQL, {"account": account, "contract": contract})
    running = 0.0
    out = []
    for r in rows:
        side = int(r["side"])
        qty = float(r["quantity"] or 0.0)
        delta = qty if side == 1 else -qty
        running += delta
        tids = r["trade_ids"]
        linked = bool(tids) and tids not in ([], "[]", "")
        out.append({
            "timestamp": _iso(r["timestamp"]),
            "side": side, "qty": qty,
            "price": float(r["price"]) if r["price"] is not None else None,
            "delta": round(delta, 6), "running_position": round(running, 6),
            "trader_id": r["trader_id"], "fill_type": r["fill_type"], "linked": linked,
        })
    total = len(out)
    current_net = round(running, 6)
    out.reverse()  # latest first
    truncated = bool(limit and total > limit)
    if truncated:
        out = out[:limit]
    return {
        "account": account, "contract": contract,
        "total_fills": total, "returned": len(out), "truncated": truncated,
        "current_net": current_net,
        "first_fill": rows[0]["timestamp"].isoformat() if rows else None,
        "last_fill": rows[-1]["timestamp"].isoformat() if rows else None,
        "fills": out,
    }


def _recon_unexplained(rec: dict) -> bool:
    candle = rec.get("candle_close")
    realized = rec.get("realized")
    if candle is None or realized is None:
        return False
    if rec.get("n_cross_day", 0) > 0:
        return False
    return abs(candle - realized) > Config.RECON_TOLERANCE


# ---------------------------------------------------------------------------
# smoke test: text summary, no server
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json
    from . import fixfeed

    st = compute_state()
    fixfeed.enrich(st)
    tree = assemble_tree(st)
    o = tree["overall"]
    print(f"window {tree['window']['start_date']} .. {tree['window']['end_date']} "
          f"({len(tree['window']['days'])} days)")
    print(f"groups: {len(tree['groups'])}")
    print("\nHEALTH:", tree["health"]["headline"])
    print("\noverall:", json.dumps({k: v for k, v in o.items() if v}, indent=2))
    if tree["drop_rollup"]:
        print("\ndrop-by-ingestion-day:")
        for d in tree["drop_rollup"]:
            print(f"  {d['day']}  {d['fills']} fills  net {d['net']:+g}")
    print("\nactionable / flagged active contracts:")
    n = 0
    for g in tree["groups"]:
        for t in g["traders"]:
            for a in t["accounts"]:
                for c in a["active"]:
                    if c["verdict"] in ("drop", "extra_misattr", "stranded", "unreconciled"):
                        fix = c.get("fix") or {}
                        extra = (f"raw={fix.get('raw_net')} our={fix.get('our_net')} "
                                 f"miss={fix.get('missing_count', fix.get('extra_count', ''))}")
                        print(f"  [{g['group_name']}] {t['trader_name']:<20} "
                              f"{c['account']:<12} {c['contract']:<14} net={c['current_net']:+g} "
                              f"-> {c['verdict']:<13} {extra}")
                        n += 1
                        if n >= 60:
                            break
    print(f"\n({n} actionable/flagged shown)")
