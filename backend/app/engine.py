"""Validation engine (v3 — the simplified day-by-day model).

For every (account, contract) we walk the display window day by day and record the END-OF-DAY net
position. Two states only:
  flat — |EOD net| ~ 0            (green square)
  open — non-flat at EOD          (yellow square)

A row is a PROBLEM when the position stays open at EOD for the last PROBLEM_OPEN_DAYS+ TRAILING days
(a sustained open — not just a fresh overnight, and not a middle open that later closed back to
flat). For problem rows the day strip becomes a line of EOD-net NUMBERS, and each completed day's
GROSS traded volume in `fills` is cross-checked against the FIX drop-copy `raw_fills_fix`
(fixfeed.cross_check):
  gross agrees  -> the open is real            (yellow number)
  gross differs -> a fill is probably missing  (red number)

Known spread / curve books (Config.SPREAD_PRODUCTS) are NEVER problems — their legs carry net != 0
by design; they stay faint and out of the counts.

All work is read-only. Day boundaries are UTC to match the daily-candle rollup.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import re

from . import db
from .config import Config
from .contracts import is_expired
from .fixfeed import CANON_SQL, canon, parse_contract

# A contract is FIX-mappable iff it's a clean future "SYM MmmYY" (the grain the FIX cross-check
# maps). Option strikes / synthetic markers can still show green/yellow, but they can't go red
# (no per-day FIX comparison) — see fixfeed.parse_contract.
_FUTURE_RE = re.compile(r"^[A-Za-z0-9]+\s+[A-Za-z]{3}\d{2}$")


def in_scope(contract: str | None) -> bool:
    return bool(_FUTURE_RE.fullmatch((contract or "").strip()))


def symbol_of(contract: str | None) -> str:
    """First token of the contract — 'I Sep26' -> 'I', 'SO3 Dec26' -> 'SO3'."""
    return (contract or "").strip().split(" ", 1)[0]


def detect_spread_keys(net_all, acct2trader: dict, today: date, eps: float,
                       min_balance: float) -> set:
    """Find the (trader_id, product-symbol) books that are CALENDAR SPREADS / curves, straight from
    the position data.

    Signature: across a product's OPEN, NON-EXPIRED, FUTURES maturities, the trader is net LONG one
    month and net SHORT another (opposing signs) — e.g. James Pitron FGBM +50 / -50. Magnitudes
    needn't match, but the smaller side must be >= `min_balance` of the larger (so a directional
    book with a 1-lot residual in another month isn't a 'spread'). Detection is per TRADER (legs net
    across all the trader's accounts). Excluded:
      * OPTIONS / strikes — `in_scope` keeps only clean "SYM MmmYY" futures (an option and a future
        sharing the first token, e.g. "I Sep26" vs "I Sep26 C97.5 American", are NOT a calendar leg).
      * EXPIRED maturities — ancient offsetting residuals are not a held spread (the FGBS trap)."""
    net_by: dict[tuple, float] = defaultdict(float)        # (trader_id, contract) -> net
    for nr in net_all:
        c = nr["contract"]
        if not in_scope(c) or is_expired(c, today) is True:
            continue
        tid = acct2trader.get(nr["account"])
        if tid is not None:
            net_by[(tid, c)] += float(nr["net"] or 0.0)
    sides: dict[tuple, list] = defaultdict(lambda: [0.0, 0.0])  # (trader, symbol) -> [pos, neg]
    for (tid, contract), net in net_by.items():
        if abs(net) <= eps:
            continue
        s = sides[(tid, symbol_of(contract))]
        if net > 0:
            s[0] += net
        else:
            s[1] += -net
    return {key for key, (pos, neg) in sides.items()
            if pos > eps and neg > eps and min(pos, neg) / max(pos, neg) >= min_balance}


# day-strip / roll-up state ordering (worse wins a roll-up cell)
RANK = {"flat": 0, "open": 1, "skipped": 2, "mismatch": 3}


def _rank(s: str) -> int:
    return RANK.get(s, 0)


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
       SUM(CASE WHEN side = 1 THEN quantity ELSE 0 END)  AS buys,
       SUM(CASE WHEN side <> 1 THEN quantity ELSE 0 END) AS sells,
       MIN(timestamp) AS first_fill,
       MAX(timestamp) AS last_fill,
       COUNT(*)       AS n_fills
FROM fills
WHERE account = ANY(%(accounts)s)
GROUP BY account, contract, platform_id
"""

# per (account, contract, UTC day): signed net delta + GROSS traded volume (Σ qty) + fill count.
WINDOW_SQL = """
SELECT account, contract, platform_id,
       (timestamp AT TIME ZONE 'UTC')::date AS d,
       SUM(CASE WHEN side = 1 THEN quantity ELSE -quantity END) AS net_delta,
       SUM(quantity) AS gross,
       COUNT(*) AS n_fills
FROM fills
WHERE account = ANY(%(accounts)s)
  AND timestamp >= %(start)s
GROUP BY account, contract, platform_id, d
"""

# SKIPPED fills: a fill sitting in the ledger with NO trade (empty trade_ids) that the aggregator
# passed OVER — i.e. there is a LATER fill on the same (account, contract) that IS assigned to a
# trade. (A trailing unassigned fill with nothing assigned after it is just pending, not skipped.)
# Whole-history, grouped by UTC day so we can both total it and colour the window days. Signed lots
# (buy +, sell -) = how much the trades are off by, because those fills were never aggregated.
SKIPPED_SQL = """
WITH assigned AS (
  SELECT account, contract, MAX(timestamp) AS la_ts
  FROM fills
  WHERE account = ANY(%(accounts)s)
    AND trade_ids IS NOT NULL AND trade_ids::text NOT IN ('[]', '')
  GROUP BY account, contract
)
SELECT f.account, f.contract,
       (f.timestamp AT TIME ZONE 'UTC')::date AS d,
       COUNT(*) AS n,
       SUM(CASE WHEN f.side = 1 THEN f.quantity ELSE -f.quantity END) AS lots
FROM fills f
JOIN assigned a ON a.account = f.account AND a.contract = f.contract
WHERE f.account = ANY(%(accounts)s)
  AND (f.trade_ids IS NULL OR f.trade_ids::text IN ('[]', ''))
  AND f.timestamp < a.la_ts
GROUP BY f.account, f.contract, d
"""

# daily net deltas over a LONGER look-back, for the small set of positions carried into the window
# (open since before day 1) — used only to find when the current open run actually started.
OPEN_LOOKBACK_SQL = """
SELECT account, contract,
       (timestamp AT TIME ZONE 'UTC')::date AS d,
       SUM(CASE WHEN side = 1 THEN quantity ELSE -quantity END) AS delta
FROM fills
WHERE account = ANY(%(accounts)s) AND contract = ANY(%(contracts)s)
  AND timestamp >= %(ls)s
GROUP BY account, contract, d
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

    # skipped fills (unassigned, but with a later assigned fill) per (account, contract, day), whole
    # history — total for the end-of-row note, per-day for the purple cells inside the window.
    skip_idx: dict[tuple, dict] = defaultdict(lambda: {"n": 0, "lots": 0.0, "by_day": {}})
    for r in db.query(SKIPPED_SQL, {"accounts": accounts}):
        s = skip_idx[(r["account"], r["contract"])]
        s["n"] += int(r["n"])
        s["lots"] += float(r["lots"] or 0.0)
        s["by_day"][r["d"]] = {"n": int(r["n"]), "lots": float(r["lots"] or 0.0)}

    # data-driven spread/curve detection (replaces the old hand-curated list). A trader who is net
    # long one futures month of a product and net short another is running a calendar spread — faded
    # + excluded from the aggregated timeline / counts. Config.SPREAD_PRODUCTS is a manual override.
    acct2trader = {r["account"]: r["trader_id"] for r in cohort}
    spread_trader_keys = detect_spread_keys(net_all, acct2trader, today, eps,
                                            Config.SPREAD_MIN_BALANCE)

    # index window deltas: (account, contract) -> {date: row}
    win_idx: dict[tuple, dict] = defaultdict(dict)
    for r in window:
        win_idx[(r["account"], r["contract"])][r["d"]] = r

    # canonical per-day GROSS for the FIX cross-check: (cb, sym, mat, platform_id) -> {date: gross}.
    # Built here (we already hold the window rows); fixfeed.cross_check compares it to raw_fills_fix
    # at the same canonical grain (sub-accounts net together, matching the FIX feed).
    fills_gross_by_key_day: dict[tuple, dict] = defaultdict(lambda: defaultdict(float))
    for (acct, contract), bydate in win_idx.items():
        sym, mat = parse_contract(contract)
        if not sym:
            continue
        cb = canon(acct)
        for d, row in bydate.items():
            key = (cb, sym, mat, row["platform_id"])
            fills_gross_by_key_day[key][d] += float(row["gross"] or 0.0)

    contracts_by_account: dict[str, list] = defaultdict(list)

    for nr in net_all:
        account, contract = nr["account"], nr["contract"]
        platform_id = nr["platform_id"]
        current_net = float(nr["net"] or 0.0)
        deltas = win_idx.get((account, contract), {})
        has_window_fills = sum(d["n_fills"] for d in deltas.values()) > 0
        expired = is_expired(contract, today)
        is_open = abs(current_net) > eps
        sk = skip_idx.get((account, contract))   # whole-history skipped (orphaned) fills, if any
        # a "closes to zero" recalc target: has skipped fills AND nets ~flat counting everything.
        is_ctz = (sk is not None and sk["n"] > 0 and abs(current_net) <= Config.CLOSES_TO_ZERO_TOL)
        # WINDOW-GATED, STRICT: a contract shows ONLY if it had fills in the selected window. Anything
        # that did not trade in the window is excluded — even a still-open position or a `closes to
        # zero` recalc target. The "only closes to zero" toggle then just filters THIS windowed set;
        # it never pulls in dormant contracts. The whole-history recalc backlog is the worklist.
        include = has_window_fills
        if not include:
            continue

        # walk the window forward: opening balance = current_net - sum(window deltas)
        total_window_delta = sum(float(d["net_delta"] or 0.0) for d in deltas.values())
        opening = current_net - total_window_delta
        day_cells = []
        running = opening
        for d in days:
            row = deltas.get(d)
            running += float(row["net_delta"]) if row else 0.0
            flat = abs(running) <= eps
            day_skip = sk["by_day"].get(d) if sk else None
            day_cells.append({
                "date": d.isoformat(),
                "eod_net": round(running, 6),
                "flat": flat,
                "open": not flat,
                "gross": round(float(row["gross"]) if row else 0.0, 6),
                "n_fills": int(row["n_fills"]) if row else 0,
                "mismatch": False,   # set by fixfeed.cross_check for problem rows (completed days)
                "skipped": int(day_skip["n"]) if day_skip else 0,
                "skipped_lots": round(day_skip["lots"], 6) if day_skip else 0.0,
            })

        # trailing open run = consecutive open EOD days counting back from the most recent day.
        trailing = 0
        for cell in reversed(day_cells):
            if cell["open"]:
                trailing += 1
            else:
                break

        sym = symbol_of(contract)
        spread = ((acct2trader.get(account), sym) in spread_trader_keys
                  or (canon(account), sym) in Config.SPREAD_PRODUCTS)
        # sustained_open drives the NUMBER line (held position) for ANY contract, spread or not.
        # problem is the subset that counts toward health (spreads are excluded from the counts).
        sustained_open = trailing >= Config.PROBLEM_OPEN_DAYS
        problem = sustained_open and not spread
        # carried into the window: open at the window start (opening != 0) AND open on every window
        # day (the trailing run spans the whole window). Such an open BEGAN before the window — it
        # does NOT colour the aggregated timeline, and its true age is resolved below.
        opened_before_window = (trailing == len(days)) and (abs(opening) > eps)

        contracts_by_account[account].append({
            "account": account, "contract": contract, "platform_id": platform_id,
            "current_net": round(current_net, 6),
            "total_buys": round(float(nr["buys"] or 0.0), 6),    # whole-history gross buy lots
            "total_sells": round(float(nr["sells"] or 0.0), 6),  # whole-history gross sell lots
            "first_fill": _iso(nr["first_fill"]), "last_fill": _iso(nr["last_fill"]),
            "expired": expired,
            "is_spread": spread,
            "days": day_cells,
            "trailing_open": trailing,
            "open_days": trailing,            # true open age; overridden for carried-in opens below
            "open_capped": False,             # True when the run is older than the look-back
            "opened_before_window": opened_before_window,
            "sustained_open": sustained_open,
            "problem": problem,
            "has_mismatch": False,    # set by fixfeed.cross_check
            "unverifiable": False,    # set True by cross_check when the FIX feed can't confirm it
            "skipped_count": sk["n"] if sk else 0,            # whole-history skipped fills (note)
            "skipped_lots": round(sk["lots"], 6) if sk else 0.0,
            # net of the ASSIGNED (non-skipped) fills = current_net − skipped_lots. ~0 while
            # current_net is NON-zero means the open is entirely the skipped fills but the contract
            # itself is NOT flat — a genuine open (recalc_trader would abort). That is NOT "closes to
            # zero"; see the flag below.
            "net_ex_skips": round(current_net - (sk["lots"] if sk else 0.0), 6),
            # "closes to zero": has skipped fills AND, counting ALL fills (incl. the skipped ones),
            # the contract nets ~flat. Re-aggregating (recalc_trader) re-walks the skips into trades
            # and it lands flat — the recalc-able batch. (current_net already counts every fill.)
            "closes_to_zero": is_ctz,
        })

    # resolve the TRUE open age for positions carried in from before the window (a small set)
    _resolve_open_days(contracts_by_account, today, eps)

    return {
        "cohort": cohort,
        "window": {
            "start_date": start_date.isoformat(),
            "end_date": today.isoformat(),
            "days": [d.isoformat() for d in days],
        },
        "today": today.isoformat(),
        "contracts_by_account": contracts_by_account,
        "fills_gross_by_key_day": {k: dict(v) for k, v in fills_gross_by_key_day.items()},
        "fix_checked": False,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# true open age for positions carried in from before the window
# ---------------------------------------------------------------------------

def _resolve_open_days(contracts_by_account: dict, today: date, eps: float) -> None:
    """For each contract opened BEFORE the window (open_days capped at the window length), look back
    OPEN_LOOKBACK_DAYS to find when the current open run actually started (the last day the position
    was flat) and set the TRUE `open_days`. One bounded query for the whole (small) carried set."""
    carried = [c for clist in contracts_by_account.values() for c in clist
               if c.get("opened_before_window")]
    if not carried:
        return
    lb_days = Config.OPEN_LOOKBACK_DAYS
    lb_start_date = today - timedelta(days=lb_days)
    lb_start_dt = datetime(lb_start_date.year, lb_start_date.month, lb_start_date.day,
                           tzinfo=timezone.utc)
    rows = db.query(OPEN_LOOKBACK_SQL, {
        "accounts": sorted({c["account"] for c in carried}),
        "contracts": sorted({c["contract"] for c in carried}),
        "ls": lb_start_dt,
    })
    deltas: dict[tuple, dict] = defaultdict(dict)
    for r in rows:
        deltas[(r["account"], r["contract"])][r["d"]] = float(r["delta"] or 0.0)

    all_days = [lb_start_date + timedelta(days=i) for i in range((today - lb_start_date).days + 1)]
    for c in carried:
        dd = deltas.get((c["account"], c["contract"]), {})
        # EOD net per calendar day, cumulating backward from today's all-history net
        running = c["current_net"]
        eod = {}
        for day in reversed(all_days):
            eod[day] = running
            running -= dd.get(day, 0.0)
        # walk back from today to the first flat day; the run started the day after it
        open_since, prev, crossed = all_days[0], None, False
        for day in reversed(all_days):
            if abs(eod[day]) <= eps:
                open_since = prev if prev is not None else day
                crossed = True
                break
            prev = day
        c["open_days"] = (today - open_since).days + 1
        c["open_capped"] = not crossed   # older than the look-back -> "N+ d"


# ---------------------------------------------------------------------------
# tree assembly + roll-ups
# ---------------------------------------------------------------------------

def assemble_tree(state: dict) -> dict:
    cohort = state["cohort"]
    days = state["window"]["days"]
    cba = state["contracts_by_account"]

    groups: dict[int, dict] = {}
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
        t["accounts"][akey] = {
            "account": akey,
            "platform_id": r["platform_id"], "platform_name": r["platform_name"],
            "is_sim": r["is_sim"], "opt_out": r["opt_out"],
            "contracts": cba.get(akey, []),
        }

    out_groups = []
    for g in sorted(groups.values(), key=lambda x: (x["group_name"] or "")):
        g_day = {d: "flat" for d in days}
        g_summary = _empty_summary()
        out_traders = []
        for t in sorted(g["traders"].values(), key=lambda x: (x["trader_name"] or "")):
            t_day = {d: "flat" for d in days}
            t_summary = _empty_summary()
            accounts_out = []
            for a in sorted(t["accounts"].values(), key=lambda x: x["account"]):
                for c in a["contracts"]:
                    # dates in the CURRENT (unresolved) trailing open run. An open day that later
                    # closed back to flat is "resolved" and must NOT colour the aggregated timeline
                    # yellow — only a position that is STILL open counts. (Drops always count.)
                    trailing_dates = set()
                    for cell in reversed(c["days"]):
                        if cell["open"]:
                            trailing_dates.add(cell["date"])
                        else:
                            break
                    for cell in c["days"]:
                        st = ("mismatch" if cell.get("mismatch")
                              else "skipped" if cell.get("skipped")
                              else "open" if cell["open"]
                              else "flat")
                        cell["state"] = st       # individual row keeps every day's colour
                        if c["is_spread"]:        # spread legs never drive the roll-up
                            continue
                        # roll-up: red for a drop, purple for a skipped fill (both always count);
                        # yellow for a STILL-open day — including a position carried in from before
                        # the window (a non-spread open that never closed is a real open). Only an
                        # open that LATER closed back to flat is "resolved" and stays green.
                        agg = "flat" if (st == "open" and cell["date"] not in trailing_dates) else st
                        if _rank(agg) > _rank(t_day[cell["date"]]):
                            t_day[cell["date"]] = agg
                    _tally(t_summary, c)
                accounts_out.append(a)

            out_traders.append({
                "trader_id": t["trader_id"], "trader_name": t["trader_name"],
                "accounts": accounts_out,
                "day_status": t_day,
                "summary": t_summary,
            })
            for d in days:
                if _rank(t_day[d]) > _rank(g_day[d]):
                    g_day[d] = t_day[d]
            _merge_summary(g_summary, t_summary)

        out_groups.append({
            "group_id": g["group_id"], "group_name": g["group_name"],
            "traders": out_traders,
            "day_status": g_day,
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
        "health": _health(overall),
        "fix_checked": state.get("fix_checked", False),
    }


def _empty_summary():
    # contracts = total rows · ok = fine · open = sustained open the FIX feed CONFIRMS (feeds agree) ·
    # unverifiable = sustained open the FIX feed can't confirm (option / give-up / alias account) ·
    # mismatch = sustained open with a per-day gross divergence (likely dropped fill) ·
    # spread = curated spread/curve leg (excluded). skipped_* = fills never aggregated into a trade
    # (orthogonal — counted on TOP of whatever bucket the contract falls in).
    return {"contracts": 0, "ok": 0, "open": 0, "unverifiable": 0, "mismatch": 0, "spread": 0,
            "skipped_contracts": 0, "skipped_fills": 0, "closes_to_zero": 0}


def _tally(summary, c):
    summary["contracts"] += 1
    if c.get("skipped_count", 0) > 0:           # orthogonal to the bucket below
        summary["skipped_contracts"] += 1
        summary["skipped_fills"] += c["skipped_count"]
    if c.get("closes_to_zero"):                 # the recalc-able subset of the skipped contracts
        summary["closes_to_zero"] += 1
    if c["is_spread"]:
        summary["spread"] += 1
        return
    if not c["problem"]:
        summary["ok"] += 1
    elif c["has_mismatch"]:
        summary["mismatch"] += 1
    elif c.get("unverifiable"):
        summary["unverifiable"] += 1
    else:
        summary["open"] += 1


def _merge_summary(dst, src):
    for k, v in src.items():
        dst[k] = dst.get(k, 0) + v


def _health(overall: dict) -> dict:
    """The one-line top-of-page verdict. Healthy = no feed mismatches (no likely dropped fills)."""
    h = {
        "mismatch": overall.get("mismatch", 0),
        "open": overall.get("open", 0),
        "unverifiable": overall.get("unverifiable", 0),
        "spread": overall.get("spread", 0),
        "skipped_contracts": overall.get("skipped_contracts", 0),
        "skipped_fills": overall.get("skipped_fills", 0),
        "closes_to_zero": overall.get("closes_to_zero", 0),
    }
    h["actionable"] = h["mismatch"] + h["skipped_contracts"]
    h["healthy"] = h["actionable"] == 0
    h["headline"] = (
        f"{h['mismatch']} feed-mismatch (likely dropped fills) · "
        + (f"{h['skipped_fills']} skipped fills in {h['skipped_contracts']} contracts"
           + (f" ({h['closes_to_zero']} close to zero — recalc-able)" if h["closes_to_zero"] else "")
           + " · " if h["skipped_contracts"] else "")
        + f"{h['open']} sustained opens (feeds agree) · "
        f"{h['unverifiable']} unverifiable (no FIX rows)"
        + (f" · {h['spread']} spread legs (excluded)" if h["spread"] else "")
    )
    return h


# ---------------------------------------------------------------------------
# fill history for one (account, contract) — the click-through "what did he do" detail
# ---------------------------------------------------------------------------

FILLS_HISTORY_SQL = f"""
SELECT timestamp, side, quantity, price, trader_id, fill_type, trade_ids
FROM fills
WHERE {CANON_SQL} = %(cb)s AND contract = %(contract)s
ORDER BY timestamp ASC
"""


def fills_history(account: str, contract: str, limit: int = 5000) -> dict:
    """Every fill for one (canonical account, contract), with a chronological RUNNING POSITION
    (signed cumulative qty, buy +, sell −) so you can watch the position build and unwind. Matches
    on the CANONICAL account so sub-account suffixes (_MA/_AL/…) of the same book net together —
    the same grain the day strip uses. Returned latest-first."""
    rows = db.query(FILLS_HISTORY_SQL, {"cb": canon(account), "contract": contract})
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


# ---------------------------------------------------------------------------
# smoke test: text summary, no server
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from . import fixfeed

    st = compute_state()
    fixfeed.cross_check(st)
    tree = assemble_tree(st)
    o = tree["overall"]
    print(f"window {tree['window']['start_date']} .. {tree['window']['end_date']} "
          f"({len(tree['window']['days'])} days)")
    print(f"groups: {len(tree['groups'])}")
    print("HEALTH:", tree["health"]["headline"])
    print("overall:", o)
    print("\nproblem contracts (sustained opens):")
    n = 0
    for g in tree["groups"]:
        for t in g["traders"]:
            for a in t["accounts"]:
                for c in a["contracts"]:
                    if c["problem"]:
                        tag = "MISMATCH" if c["has_mismatch"] else "open"
                        print(f"  [{g['group_name']}] {t['trader_name']:<20} "
                              f"{c['account']:<12} {c['contract']:<16} net={c['current_net']:+g} "
                              f"trailing={c['trailing_open']:<3} {tag}")
                        n += 1
                        if n >= 80:
                            break
    print(f"\n({n} problem rows shown)")
