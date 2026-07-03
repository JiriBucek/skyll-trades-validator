"""Recalc worklist — the "closes to zero" contracts: have SKIPPED fills AND, counting ALL fills
(assigned + the skipped ones), net ~flat.

These are the recalc-able batch. The contract's full ledger balances to ~0, but some fills were
never aggregated into a trade (empty trade_ids, with a later assigned fill — a genuine middle-skip,
not a pending tail), so the trades/PnL are wrong. Re-aggregating the whole contract (recalc_trader)
re-walks every fill — including the skipped ones — into proper flat-to-flat trades, and because the
ledger already nets flat, recalc_trader's net=0 preflight PASSES and the contract lands flat with the
trades corrected. This is the validator's `closes to zero` set (UI: `N skipped · ±L → closes to 0`).

NOT in here (by design): contracts whose net is non-zero with EVERYTHING counted — those are genuine
opens (or unrecoverable pre-retention carries) and recalc_trader ABORTS on net!=0. They need backfill
or open-tail handling, not a recalc.

Pulls DIRECTLY over the cohort (not via the cached overview, which drops dormant contracts), so the
list is complete. Re-run as you clean to see what's left.

  python -m app.worklist            # markdown worklist (default)
  python -m app.worklist --json --min-skips 5 --active-days 60
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone

from . import db
from .config import Config
from .engine import COHORT_SQL, ELIGIBLE_PRED, NET_ALL_SQL, SKIPPED_SQL

# recalc_trader's preflight only counts these fills; its net must be 0 or it aborts. Usually equals
# the full net — differs only when a contract carries synthetic (price<=0) / non-Outright / ALGO fills.
ELIG_NET_SQL = f"""
SELECT account, contract,
       SUM(CASE WHEN side = 1 THEN quantity ELSE -quantity END) AS net
FROM fills
WHERE account = ANY(%(accounts)s)
  AND {ELIGIBLE_PRED}
GROUP BY account, contract
"""


def gather(min_skips: int = 1):
    cohort = db.query(COHORT_SQL)
    acct2tg = {}
    for r in cohort:
        acct2tg.setdefault(r["account"], (r["trader_name"] or f"trader {r['trader_id']}",
                                          r["group_name"], r["platform_id"]))
    accounts = sorted(acct2tg)

    net = {(r["account"], r["contract"]): float(r["net"] or 0.0)
           for r in db.query(NET_ALL_SQL, {"accounts": accounts})}
    elig = {(r["account"], r["contract"]): float(r["net"] or 0.0)
            for r in db.query(ELIG_NET_SQL, {"accounts": accounts})}

    skip = defaultdict(lambda: {"n": 0, "lots": 0.0, "last": None})
    for r in db.query(SKIPPED_SQL, {"accounts": accounts}):
        s = skip[(r["account"], r["contract"])]
        s["n"] += int(r["n"])
        s["lots"] += float(r["lots"] or 0.0)
        d = r["d"].isoformat()
        s["last"] = d if (s["last"] is None or d > s["last"]) else s["last"]

    tol = Config.CLOSES_TO_ZERO_TOL
    items = []
    for key, s in skip.items():
        if s["n"] < min_skips:
            continue
        n = net.get(key, 0.0)
        if abs(n) > tol:           # not flat counting ALL fills -> genuine open; recalc aborts -> skip
            continue
        e = elig.get(key, n)
        acct, contract = key
        tn, gn, pid = acct2tg.get(acct, ("?", "?", None))
        items.append({
            "group": gn, "trader": tn, "account": acct, "contract": contract,
            "platform": "TT" if pid == 1 else "Stellar" if pid == 2 else str(pid),
            "skipped_fills": s["n"],
            "skipped_lots": round(s["lots"], 4),
            "current_net": round(n, 4),         # ~0 — closes to zero with EVERYTHING counted
            "elig_net": round(e, 4),            # recalc_trader preflight net; !=0 ⇒ dry-run will abort
            "recalc_clean": abs(e) <= tol,
            "last_skip_day": s["last"],
        })
    # clean ones first (preflight passes), each block biggest cleanup (most skipped fills) first.
    items.sort(key=lambda x: (x["recalc_clean"], x["skipped_fills"]), reverse=True)
    return items


def render_md(items: list, active_days: int | None) -> str:
    today = datetime.now(timezone.utc).date()
    by_trader: dict[tuple, list] = defaultdict(list)
    for it in items:
        by_trader[(it["group"], it["trader"])].append(it)
    traders = sorted(by_trader, key=lambda k: (
        -sum(i["skipped_fills"] for i in by_trader[k]), k[1]))

    total_fills = sum(i["skipped_fills"] for i in items)
    flagged = [i for i in items if not i["recalc_clean"]]
    L = [
        "# Recalc worklist — “closes to zero” contracts (skipped fills, ledger nets flat)",
        "",
        f"_Generated {today.isoformat()} · `make worklist` to regenerate._",
        "",
        f"**{len(items)} contracts · {len(traders)} traders · {total_fills} skipped fills.** "
        "These are the validator's **`closes to zero`** contracts: each has fills that were never "
        "aggregated into a trade (skipped), but counting **all** fills — including the skipped ones — "
        "the contract nets ~flat. Re-aggregating (`recalc_trader`) re-walks every fill into proper "
        "trades; because the ledger already balances, the net=0 preflight passes and the contract "
        "lands flat with the trades/PnL corrected. **recalc only, no backfill.**",
        "",
    ]
    if flagged:
        L += [
            f"> ⚠ {len(flagged)} row(s) are flagged `recalc-net ≠ 0` — the full ledger nets flat but "
            "the recalc-eligible subset (price>0, Outright, non-ALGO) does not, so `recalc_trader`'s "
            "preflight may still abort (synthetic / option / ALGO fills). Dry-run first; if it aborts, "
            "treat as a genuine open. They sort to the bottom of each trader.",
            "",
        ]
    L += [
        "## Per-contract pipeline (one at a time — full detail in "
        "`aws-mwaa-local-runner/recovery/RECOVERY.md`)",
        "0. **Gate**: no live ingestion (weekend / pause `Trading-Orchestrate-Fills-Processing`). "
        "If the contract traded in the last ~14d, also pause the 2-hourly intraday/daily DAGs.",
        "1. `tags.py backup --account --contract` (skip if the trader is tag-free).",
        "2. `recalc_trader.py --account --contract --dry-run` → `--execute` (rebuilds trades, deletes "
        "intraday+daily, relinks fills, auto-backs-up). Net=0 preflight should PASS for these.",
        "3. `tags.py remap --account --contract` (restore tags/descriptions; assert count in == out).",
        "4. `intraday.py intraday --account --contract --execute` (reconciles Σrealized vs Σprofit).",
        "5. `intraday.py daily --account --execute`  →  `caggs.py --start --end`.",
        "6. **Verify**: the contract drops its skipped-fills note in the validator (0 skipped), still "
        "flat; append the result to `recovery/ledger.jsonl`. Tick the box here.",
        "",
        "> `net` = the full-ledger net (~0 — why it's recalc-able). `recalc-net` = the eligible-fills "
        "net `recalc_trader` preflights on (should also be ~0). `skipped lots` = how far the trades "
        "are currently off. `last skip` flags recency — a recent contract may self-heal via the "
        "2-hourly DAGs; pause them to avoid a delete/insert race.",
        "",
    ]
    for gk, tk in traders:
        rows = by_trader[(gk, tk)]   # already globally sorted (clean first, then by skip count)
        L.append(f"### {tk}  ·  {gk}  ({len(rows)})")
        L.append("")
        L.append("| ✓ | account | contract | platform | skipped fills | skipped lots | net | recalc-net | last skip |")
        L.append("|---|---|---|---|--:|--:|--:|--:|---|")
        for r in rows:
            recent = ""
            if active_days and r["last_skip_day"]:
                age = (today - datetime.fromisoformat(r["last_skip_day"]).date()).days
                recent = " ⚡" if age <= active_days else ""
            warn = "" if r["recalc_clean"] else " ⚠"
            L.append(f"| [ ] | {r['account']} | {r['contract']} | {r['platform']} | "
                     f"{r['skipped_fills']} | {r['skipped_lots']:+g} | {r['current_net']:+g} | "
                     f"{r['elig_net']:+g}{warn} | {r['last_skip_day'] or '—'}{recent} |")
        L.append("")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(
        description="Recalc worklist: 'closes to zero' contracts (skipped fills, full ledger nets flat).")
    ap.add_argument("--json", action="store_true", help="JSON instead of markdown")
    ap.add_argument("--min-skips", type=int, default=1, help="only contracts with >= this many skips")
    ap.add_argument("--active-days", type=int, default=14,
                    help="flag (⚡) contracts whose last skip is within this many days")
    args = ap.parse_args()
    items = gather(min_skips=args.min_skips)
    print(json.dumps(items, indent=2) if args.json else render_md(items, args.active_days))


if __name__ == "__main__":
    main()
