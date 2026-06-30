"""Agent-readable findings — the SAME picture the UI shows, as structured data (JSON / markdown).

An AI agent pulls this instead of screen-reading the heatmap: it sees exactly what the operator sees,
then acts. Read-only. Computes the tree (engine.compute_state → fixfeed.cross_check →
engine.assemble_tree) and flattens every problem `(account, contract)` into a finding, most
actionable first.

Finding categories (the day-by-day model — see README.md / docs/DESIGN.md):
  mismatch     — a COMPLETED day where our fills GROSS volume (Σ qty) ≠ the FIX drop-copy
                 `raw_fills_fix` for that (account, symbol, maturity). The feed has fills we lack →
                 a DROPPED fill (red in the UI). Actionable: recover + recalc.
  skipped      — fills sitting in the ledger with empty `trade_ids` that the aggregator passed over
                 (there is a LATER fill on the same contract that IS in a trade). Never aggregated →
                 the trades don't add up (purple in the UI). `closes_to_zero` = has skips AND,
                 counting ALL fills (incl. the skipped ones), the contract nets ~flat → recalc_trader
                 re-walks the skips into trades and it lands flat (the easy recalc batch). If it is
                 still non-zero with everything counted, it is a genuine open (recalc aborts).
  unverifiable — a sustained open the FIX feed can't confirm (option strike / give-up /
                 clearing-alias account, or pre-retention). Grey in the UI.
  open         — a sustained open the FIX feed confirms (feeds agree) — most likely a genuine hold.

SPREADS (a trader net long one futures month of a product and net short another — detected from the
position data, engine.detect_spread_keys) are EXCLUDED from findings and listed separately, so the
agent knows what was set aside. A spread leg that ALSO has skipped fills still surfaces under
`skipped` (a skipped fill is a real integrity bug regardless of spread status).

CLI:
  python -m app.report                 # all findings, JSON
  python -m app.report --md            # markdown digest
  python -m app.report --category mismatch,skipped --min-net 5 --group Axia --limit 40
"""
from __future__ import annotations

import argparse
import json

from . import engine, fixfeed
from .config import Config

CATEGORIES = ("mismatch", "skipped", "unverifiable", "open")
SEV = {"mismatch": 4, "skipped": 3, "unverifiable": 2, "open": 1}

INVESTIGATE = {
    "mismatch": "The FIX feed has fills our `fills` lacks on the flagged day(s) — a dropped fill. "
                "Pull the exact fills with aws-mwaa-local-runner/dags/misc/recovery/raw_diff_ts.py "
                "(or GET /api/raw-diff?account=&contract=), then reingest → recalc_trader. "
                "Recovery runs in aws-mwaa-local-runner — this tool is strictly read-only.",
    "skipped":  "Fills sit in the ledger with empty trade_ids but were never aggregated into a trade. "
                "If closes_to_zero is true (counting ALL fills, incl. the skipped ones, the contract "
                "nets ~flat) recalc_trader re-walks the skips into trades and it lands flat — the easy "
                "recalc batch. If it is still non-zero with everything counted it is a GENUINE OPEN "
                "(recalc_trader aborts on net≠0). Read-only here.",
    "unverifiable": "Sustained open but the FIX feed has no rows for this (account, symbol, maturity) "
                "— option strike / give-up / clearing-alias account, or pre-retention. Can't confirm "
                "from the feed; check the fill history (GET /api/fills) or the source platform.",
    "open":     "Sustained open the FIX feed confirms (feeds agree) — most likely a genuine hold. "
                "Watch; no action unless it shouldn't be open.",
}

PLAYBOOK = [
    "Pull findings (this report). Most-actionable first: mismatch (dropped fill) > skipped > "
    "unverifiable > open.",
    "mismatch → GET /api/raw-diff?account=&contract= (or raw_diff_ts.py) for the exact missing fills "
    "with uniqueExecId → reingest → recalc_trader, in aws-mwaa-local-runner.",
    "skipped  → the fills are present but unaggregated; recalc_trader re-walks them into trades. "
    "closes_to_zero ⇒ everything counted nets ~flat → recalc lands it flat (do these first). "
    "Not closes_to_zero ⇒ genuine open, recalc aborts (needs backfill or open-tail handling).",
    "Everything here is READ-ONLY. All writes/recovery happen in aws-mwaa-local-runner.",
]


def compute_tree(window: int | None = None, with_fix: bool = True) -> dict:
    state = engine.compute_state(window)
    if with_fix:
        fixfeed.cross_check(state)
    return engine.assemble_tree(state)


def _category(c: dict) -> str | None:
    if c.get("has_mismatch"):
        return "mismatch"
    if c.get("skipped_count", 0) > 0:
        return "skipped"
    if c.get("problem") and c.get("unverifiable"):
        return "unverifiable"
    if c.get("problem"):
        return "open"
    return None


def _mismatch_days(c: dict) -> list:
    out = []
    for cell in c["days"]:
        if cell.get("mismatch"):
            fg, rg = cell.get("gross", 0.0), cell.get("raw_gross")
            out.append({"day": cell["date"], "fills_gross": fg, "fix_gross": rg,
                        "diff": round(fg - (rg or 0.0), 4)})
    return out


def _skip_days(c: dict) -> list:
    return [{"day": cell["date"], "fills": cell["skipped"], "lots": cell["skipped_lots"]}
            for cell in c["days"] if cell.get("skipped")]


def _closes_to_zero(c: dict) -> bool:
    """Has skipped fills AND, counting ALL fills (incl. the skipped ones), the contract nets ~flat —
    so re-aggregating (recalc_trader) re-walks the skips into trades and it lands flat. The engine
    already computes this; fall back to the definition for older payloads."""
    if "closes_to_zero" in c:
        return bool(c["closes_to_zero"])
    return (c.get("skipped_count", 0) > 0
            and abs(c["current_net"]) <= Config.CLOSES_TO_ZERO_TOL)


def build_report(tree: dict, *, categories=None, group=None, trader=None, account=None,
                 min_net: float = 0.0, limit: int | None = None) -> dict:
    cats = set(categories or CATEGORIES)
    spreads: dict[str, set] = {}
    findings: list[dict] = []

    for g in tree["groups"]:
        for t in g["traders"]:
            for a in t["accounts"]:
                for c in a["contracts"]:
                    if c["is_spread"]:
                        spreads.setdefault(t["trader_name"], set()).add(
                            c["contract"].split(" ", 1)[0])
                    cat = _category(c)
                    if cat is None or cat not in cats:
                        continue
                    if group and g["group_name"] != group:
                        continue
                    if trader and t["trader_name"] != trader:
                        continue
                    if account and c["account"] != account:
                        continue
                    if (abs(c["current_net"]) < min_net
                            and abs(c.get("skipped_lots", 0.0)) < min_net):
                        continue
                    findings.append({
                        "category": cat,
                        "group": g["group_name"], "trader": t["trader_name"],
                        "account": c["account"], "contract": c["contract"],
                        "platform_id": c["platform_id"],
                        "current_net": c["current_net"],
                        "total_buys": c.get("total_buys"),    # whole-history gross buy lots
                        "total_sells": c.get("total_sells"),  # whole-history gross sell lots
                        "open_days": c.get("open_days"),
                        "open_capped": c.get("open_capped", False),
                        "skipped_count": c.get("skipped_count", 0),
                        "skipped_lots": c.get("skipped_lots", 0.0),
                        "net_ex_skips": c.get("net_ex_skips"),
                        "closes_to_zero": _closes_to_zero(c),
                        "is_spread": c["is_spread"],
                        "has_mismatch": c.get("has_mismatch", False),
                        "unverifiable": c.get("unverifiable", False),
                        "mismatch_days": _mismatch_days(c) if c.get("has_mismatch") else [],
                        "skipped_days_in_window": _skip_days(c) if c.get("skipped_count") else [],
                        "investigate": INVESTIGATE[cat],
                    })

    findings.sort(key=lambda f: (SEV[f["category"]], abs(f["current_net"]), f["skipped_count"]),
                  reverse=True)
    if limit:
        findings = findings[:limit]
    return {
        "window": tree["window"],
        "generated_at": tree["generated_at"],
        "health": tree["health"],
        "summary": tree["overall"],
        "spread_traders": {k: sorted(v) for k, v in sorted(spreads.items())},
        "findings": findings,
        "playbook": PLAYBOOK,
    }


# ---------------------------------------------------------------------------
# markdown digest
# ---------------------------------------------------------------------------

def _fmt(n) -> str:
    if n is None:
        return "—"
    r = round(float(n), 2)
    return (f"+{r:g}" if r > 0 else f"{r:g}")


def render_md(rep: dict) -> str:
    h = rep["health"]
    head = "✅ clean" if h["healthy"] else f"🔴 {h['actionable']} actionable"
    L = [
        "# Skyll Trades Validator — findings",
        f"_{rep['window']['start_date']} → {rep['window']['end_date']} · "
        f"generated {rep['generated_at']}_",
        "",
        f"**{head}** — {h['headline']}",
        "",
    ]
    if rep["spread_traders"]:
        sp = "; ".join(f"{k} ({', '.join(v)})" for k, v in rep["spread_traders"].items())
        L += [f"**Spread books (excluded — net long one futures month, short another):** {sp}", ""]

    by_cat: dict[str, list] = {}
    for f in rep["findings"]:
        by_cat.setdefault(f["category"], []).append(f)

    if not rep["findings"]:
        L.append("_No findings._")

    for cat in CATEGORIES:
        fs = by_cat.get(cat, [])
        if not fs:
            continue
        L.append(f"## {cat}  ({len(fs)})")
        for f in fs:
            who = f"{f['trader']} / {f['account']} / {f['contract']}"
            if cat == "mismatch":
                days = " · ".join(f"{d['day']} (ours {d['fills_gross']:g} vs FIX "
                                  f"{('—' if d['fix_gross'] is None else format(d['fix_gross'], 'g'))})"
                                  for d in f["mismatch_days"])
                L.append(f"- **{who}** net {_fmt(f['current_net'])}, open {f['open_days']}"
                         f"{'+' if f['open_capped'] else ''}d — drop day(s): {days}")
            elif cat == "skipped":
                tail = " → **closes to 0** (recalc re-walks the skips → flat)" \
                    if f["closes_to_zero"] \
                    else f" → still open {_fmt(f['current_net'])} (genuine open; recalc aborts)"
                sp = " [spread]" if f["is_spread"] else ""
                L.append(f"- **{who}**{sp} net {_fmt(f['current_net'])} · "
                         f"{f['skipped_count']} skipped, {_fmt(f['skipped_lots'])} lots{tail}")
            else:  # unverifiable / open
                L.append(f"- **{who}** net {_fmt(f['current_net'])}, "
                         f"open {f['open_days']}{'+' if f['open_capped'] else ''}d")
        L.append("")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Agent-readable validator findings (JSON / markdown).")
    ap.add_argument("--md", action="store_true", help="markdown digest instead of JSON")
    ap.add_argument("--category", default=",".join(CATEGORIES),
                    help=f"comma list of {CATEGORIES}")
    ap.add_argument("--group", default=None)
    ap.add_argument("--trader", default=None)
    ap.add_argument("--account", default=None)
    ap.add_argument("--min-net", type=float, default=0.0,
                    help="drop findings whose |current_net| and |skipped_lots| are both under this")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--window", type=int, default=None, help="window days (default Config.WINDOW_DAYS)")
    ap.add_argument("--no-fix", action="store_true", help="skip the FIX cross-check (no 'mismatch')")
    args = ap.parse_args()

    tree = compute_tree(args.window, with_fix=not args.no_fix)
    rep = build_report(
        tree,
        categories=[c.strip() for c in args.category.split(",") if c.strip()],
        group=args.group, trader=args.trader, account=args.account,
        min_net=args.min_net, limit=args.limit,
    )
    print(render_md(rep) if args.md else json.dumps(rep, indent=2, default=str))


if __name__ == "__main__":
    main()
