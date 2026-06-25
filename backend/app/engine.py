"""Validation engine: read-only computation of fills -> trades -> daily-candle integrity.

Pipeline:
  compute_state(window_days)  -> heavy read-only DB work, returns raw state (NO TT)
  tt.enrich(state)            -> resolves TT verdicts for currently-open TT contracts (network)
  assemble_tree(state)        -> builds the group/trader/account JSON tree + per-day roll-ups

All work is read-only. Day boundaries are UTC to match the daily-candle rollup.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from . import db
from .config import Config
from .contracts import is_expired

# severity ordering for roll-ups (higher = worse, wins a cell/day)
SEVERITY = {
    "flat": 0,
    "settled_residual": 1,
    "open_confirmed": 2,
    "open_unverifiable": 3,
    "orphan": 4,
    "suspected_drop": 5,
}

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
       COUNT(*) FILTER (WHERE trade_ids IS NULL OR trade_ids::text IN ('[]', '')) AS n_orphan
FROM fills
WHERE account = ANY(%(accounts)s)
  AND timestamp >= %(start)s
GROUP BY account, contract, platform_id, d
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

    net_all = db.query(NET_ALL_SQL, {"accounts": accounts})
    window = db.query(WINDOW_SQL, {"accounts": accounts, "start": start_dt})
    realized = db.query(REALIZED_SQL, {"accounts": accounts, "start": start_dt})
    candles = db.query(CANDLE_SQL, {"accounts": accounts, "start_date": start_date})

    # index window deltas: (account, contract) -> {date: row}
    win_idx: dict[tuple, dict] = defaultdict(dict)
    for r in window:
        win_idx[(r["account"], r["contract"])][r["d"]] = r

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
    open_tt: list[dict] = []

    for nr in net_all:
        account, contract = nr["account"], nr["contract"]
        platform_id = nr["platform_id"]
        current_net = float(nr["net"] or 0.0)
        deltas = win_idx.get((account, contract), {})
        has_window_fills = sum(d["n_fills"] for d in deltas.values()) > 0
        expired = is_expired(contract, today)
        is_open = abs(current_net) > eps

        include = has_window_fills or (is_open and expired is not True)
        if not include:
            if is_open and expired is True:
                # dormant residual on an expired contract -> settled bucket
                contracts_by_account[account].append({
                    "account": account, "contract": contract,
                    "platform_id": platform_id,
                    "current_net": round(current_net, 6),
                    "first_fill": _iso(nr["first_fill"]), "last_fill": _iso(nr["last_fill"]),
                    "expired": expired, "category": "stale_residual",
                    "days": [], "switch_on": None, "has_orphans": False,
                    "verdict": "settled_residual", "tt": None,
                })
            # else: fully dormant + flat -> skip entirely
            continue

        # walk the window forward: opening balance = current_net - sum(window deltas)
        total_window_delta = sum(float(d["net_delta"] or 0.0) for d in deltas.values())
        opening = current_net - total_window_delta

        day_cells = []
        running = opening
        has_orphans = False
        for d in days:
            row = deltas.get(d)
            running += float(row["net_delta"]) if row else 0.0
            n_orphan = int(row["n_orphan"]) if row else 0
            # orphans only count on completed days (today's fills may be pending aggregation)
            orphan_completed = n_orphan if d < today else 0
            if orphan_completed:
                has_orphans = True
            day_cells.append({
                "date": d.isoformat(),
                "eod_net": round(running, 6),
                "flat": abs(running) <= eps,
                "n_fills": int(row["n_fills"]) if row else 0,
                "n_orphan": orphan_completed,
            })

        # switch-on day = first day of the current trailing non-zero run.
        # If every window day is non-flat, the position was opened at/before the window start.
        switch_on = None
        if is_open:
            switch_on = "before_window"
            for cell in reversed(day_cells):
                if cell["flat"]:
                    break
                switch_on = cell["date"]
            else:
                switch_on = "before_window"

        # provisional verdict (TT resolves the open TT case)
        if is_open:
            if platform_id == 1:  # TT
                verdict = "open_pending_tt"
            else:
                verdict = "open_unverifiable"
        elif has_orphans:
            verdict = "orphan"
        else:
            verdict = "flat"

        contract_obj = {
            "account": account, "contract": contract, "platform_id": platform_id,
            "current_net": round(current_net, 6),
            "first_fill": _iso(nr["first_fill"]), "last_fill": _iso(nr["last_fill"]),
            "expired": expired, "category": "active",
            "days": day_cells, "switch_on": switch_on,
            "has_orphans": has_orphans, "verdict": verdict, "tt": None,
        }
        contracts_by_account[account].append(contract_obj)
        if verdict == "open_pending_tt":
            open_tt.append(contract_obj)

    return {
        "cohort": cohort,
        "window": {
            "start_date": start_date.isoformat(),
            "end_date": today.isoformat(),
            "days": [d.isoformat() for d in days],
        },
        "contracts_by_account": contracts_by_account,
        "recon_by_account": recon,
        "open_tt_contracts": open_tt,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# per-day cell severity + roll-ups
# ---------------------------------------------------------------------------

def _cell_state(contract: dict, cell: dict) -> str:
    """Resolve a single day cell's state for a contract (after TT enrichment)."""
    if not cell["flat"]:
        # part of the current trailing open run? -> use the contract's verdict colour
        if contract["switch_on"] and contract["verdict"] in (
            "suspected_drop", "open_confirmed", "open_unverifiable", "open_pending_tt"
        ):
            run_start = contract["switch_on"]
            if run_start == "before_window" or cell["date"] >= run_start:
                v = contract["verdict"]
                return "open_unverifiable" if v == "open_pending_tt" else v
        return "open_unverifiable"
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
    today = state["window"]["end_date"]
    cba = state["contracts_by_account"]
    recon = state["recon_by_account"]

    # group -> trader -> account rows
    groups: dict[int, dict] = {}
    # de-dup (group, trader, account) cohort rows
    seen = set()

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

    # finalize: per-day roll-ups + summaries
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
                    if c["verdict"] in ("suspected_drop", "open_confirmed",
                                        "open_unverifiable", "open_pending_tt"):
                        so = c["switch_on"]
                        cand = a["account"] and so
                        if so and so != "before_window":
                            open_since = so if open_since is None else min(open_since, so)
                        elif so == "before_window":
                            open_since = open_since or "before_window"
                for c in a["residual"]:
                    _tally(t_summary, c)
                # reconciliation flags for this account's window days
                for d in days:
                    rec = recon.get(a["account"], {}).get(_as_date(d))
                    if rec and _recon_unexplained(rec):
                        t_recon_flags += 1
                accounts_out.append(a)

            t_worst = _worst(t_day.values())
            trader_obj = {
                "trader_id": t["trader_id"], "trader_name": t["trader_name"],
                "accounts": accounts_out,
                "day_status": t_day, "worst": t_worst,
                "open_since": open_since, "recon_flags": t_recon_flags,
                "summary": t_summary,
            }
            out_traders.append(trader_obj)
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
        "tt_checked": state.get("tt_checked", False),
    }


def _as_date(iso_str: str) -> date:
    return date.fromisoformat(iso_str)


def _empty_summary():
    return {"flat": 0, "settled_residual": 0, "open_confirmed": 0,
            "open_unverifiable": 0, "orphan": 0, "suspected_drop": 0,
            "open_pending_tt": 0, "active_contracts": 0}


def _tally(summary, contract):
    summary["active_contracts"] += 1 if contract["category"] == "active" else 0
    v = contract["verdict"]
    if v in summary:
        summary[v] += 1


def _merge_summary(dst, src):
    for k, v in src.items():
        dst[k] = dst.get(k, 0) + v


def _recon_unexplained(rec: dict) -> bool:
    """True if daily candle close and realized diverge beyond tolerance and it's not
    explained by cross-day trades on that day."""
    candle = rec.get("candle_close")
    realized = rec.get("realized")
    if candle is None or realized is None:
        return False
    if rec.get("n_cross_day", 0) > 0:
        return False  # cross-day trades legitimately split P&L across days
    return abs(candle - realized) > Config.RECON_TOLERANCE


# ---------------------------------------------------------------------------
# smoke test: text summary, no TT, no server
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json

    st = compute_state()
    tree = assemble_tree(st)
    o = tree["overall"]
    print(f"window {tree['window']['start_date']} .. {tree['window']['end_date']} "
          f"({len(tree['window']['days'])} days)")
    print(f"groups: {len(tree['groups'])}")
    print("overall:", json.dumps(o, indent=2))
    print("\nopen TT contracts pending verdict:", len(st["open_tt_contracts"]))
    print("\nsample currently-open / flagged contracts:")
    n = 0
    for g in tree["groups"]:
        for t in g["traders"]:
            for a in t["accounts"]:
                for c in a["active"]:
                    if c["verdict"] not in ("flat",):
                        print(f"  [{g['group_name']}] {t['trader_name']:<22} "
                              f"{a['platform_name']:<7} {c['account']:<12} {c['contract']:<16} "
                              f"net={c['current_net']:+g} since={c['switch_on']} -> {c['verdict']}")
                        n += 1
                        if n >= 40:
                            break
    print("\n(showing first 40 non-flat active contracts)")
