"""Agent-readable report layer.

Turns the validation tree into a flat, machine-parseable list of *findings* — each a non-flat
`(account, contract)` whose net diverges from the FIX feed (`raw_fills_fix`) in an actionable way,
plus the concrete recovery pointers (the `raw_diff_ts` discovery command, the on-demand `/api/raw-diff`,
the recover→recalc chain). The whole point is that **🔴 red means a fill is genuinely wrong and you
can act on it** — flat books, genuine opens, pre-retention carries and ancient residuals all
collapse out of the way.

Two renderings: `json` (full) and `md` (compact digest, top-N), both led by the one-line health header.

Run offline (no server):
    secretctl run skyll-mwaa -- ./venv/bin/python -m app.report            # JSON
    secretctl run skyll-mwaa -- ./venv/bin/python -m app.report --md       # markdown digest
    ... --severity drop,extra_misattr,stranded --min-net 2 --group Axia --limit 40 --window 45
Or hit the running server:  GET /api/findings  (?format=md, ?severity=, ?min_net=)
"""
from __future__ import annotations

import argparse
import json
import sys
from urllib.parse import quote

from . import engine, fixfeed
from .config import Config

# default findings = the 🔴 actionable verdicts + the amber investigate ones (NOT plain unverifiable)
PROBLEM_SEVERITIES = ["drop", "extra_misattr", "stranded", "unreconciled", "orphan"]

# Per-verdict: what it means + how to recover (encodes the FIX-feed runbook from RECOVERY.md).
HINTS = {
    "drop": (
        "Net diverges from the FIX feed AND the missing fills are present in `raw_fills_fix` but "
        "absent from our `fills` — a SILENTLY DROPPED FILL (TT-API watermark / µs-collision, or a "
        "Stellar processing skip). The FIX feed is the authoritative per-account source of truth. "
        "Recover with raw_diff_ts → reingest → recalc_trader (the missing fills carry their "
        "uniqueExecId). If the FIX net is non-zero, recovering lands a genuine open position (MIXED)."
    ),
    "extra_misattr": (
        "We hold MORE than the FIX feed for this account — fills in our `fills` that the FIX feed "
        "does not have under this account. Usually a duplicate (Stellar double-insert) or a "
        "MIS-ATTRIBUTED order: an alias-on-alias fill (trader=FCTRisk/AXIA, account=AXIA/LFCTEUM/"
        "GHFC01) defaulted into the wrong book. READ BOTH the account AND trader columns: if the "
        "trader's own login already nets to flat without these fills, they are not theirs — ORPHAN "
        "them off the book (do not delete), then recalc. (This is the Josh Gadenne BRN Jul26 +24.)"
    ),
    "stranded": (
        "Fills on this real cohort account aggregated under trader_id 0 (Unassigned) / 349 "
        "(IgnoredAccounts) and were never linked to a trade — the account wasn't mapped in "
        "trader_platforms when its fills ingested, so trades are invisible to the real trader. "
        "Fix = recalc_trader (linkage-only, re-resolves trader 0→N); NO backfill — the fills are all "
        "present. (This is the Josh Gadenne LFCTEU200 Euribor class.)"
    ),
    "unreconciled": (
        "Net diverges from the FIX feed but neither a drop nor an extra reconciles it cleanly — "
        "usually TT block-vs-leg aggregation, synthetic price≤0 markers, or spread legs. Inspect "
        "with raw_diff_ts / the /api/raw-diff drill-down before acting."
    ),
    "orphan": (
        "Fills exist on a completed day with empty `trade_ids` (not trader_id 0/349) — the "
        "create-trades aggregation skipped them, so trades won't reconcile even if raw fills "
        "balance. Re-run recalc_trader for the account/contract."
    ),
    "unverifiable": (
        "Open with no FIX cross-check available — no `raw_fills_fix` rows for this account+symbol "
        "(pre-retention opening before the ~2026-03-30 wall, an option strike we can't map, or a "
        "give-up / clearing-alias account the feed doesn't carry). Not a confirmed bug; needs an eye."
    ),
    "partial_carry": (
        "Non-flat because a position was carried in from BEFORE the FIX retention wall "
        "(~2026-03-30); the opening fills are unrecoverable. The in-window activity reconciles to "
        "the feed. Not actionable — recalc forward from a flat anchor only if you must."
    ),
    "confirmed_open": "Our net == the FIX feed's net — a genuine open position. Not a bug.",
}

PLAYBOOK = {
    "what_this_is": "Integrity findings for the Skyll fills→trades→intraday→daily pipeline, cross-"
                    "checked against the FIX feed `raw_fills_fix` (the authoritative per-account "
                    "copy of every fill). Each finding is a non-flat (account, contract) whose net "
                    "diverges from the feed in an actionable way. READ-ONLY — investigate + recover "
                    "in aws-mwaa-local-runner, never from here.",
    "the_model": "Take the FILLS ledger, aggregate to TRADES, trades give PROFIT. The health test "
                 "is whether a (account, contract) ledger aggregates to FLAT where it should. Non-"
                 "flat = a lost fill, an extra/mis-attributed fill, or a genuine open — NEVER an "
                 "expiry settlement (there is no expiry logic). Always read BOTH account AND trader.",
    "verdicts": {
        "drop": "FIX feed has fills we lack → recover (raw_diff_ts → reingest → recalc).",
        "extra_misattr": "we have fills the FIX feed lacks → duplicate / mis-attributed; orphan off the book.",
        "stranded": "fills under trader 0/349 never linked → recalc_trader (linkage-only, no backfill).",
        "unreconciled": "diverges but can't pin → inspect (block-vs-leg / synthetic / spread).",
        "orphan": "unassigned trade_ids on a completed day → recalc.",
        "confirmed_open / partial_carry / unverifiable / flat / settled_residual": "not actionable.",
    },
    "how_to_recover": [
        "1. Discover: /api/raw-diff?account=&contract= (this tool, on demand) OR "
        "`python -m dags.misc.recovery.raw_diff_ts --account A --contract 'C' --platform I_TT|I_STELLAR "
        "--outdir DIR` — lists the exact missing/extra fills with uniqueExecId.",
        "2. Recover (in aws-mwaa-local-runner, NOT here): reingest.py (gate: must print OK FLAT for a "
        "drop-to-flat) → recalc_trader.py --account --contract → intraday.py → daily → caggs.py.",
        "3. For stranded/orphan: recalc_trader only (no backfill). For extra/mis-attributed: confirm "
        "the trader's own login nets flat without the fills, then orphan them off (don't delete).",
    ],
    "retention_wall": f"FIX feed starts ~{Config.FIX_RETENTION_START}; positions opened before it "
                      "have no recoverable opening (UNVERIFIABLE / PARTIAL_CARRY, not bugs).",
    "fix_tools_repo": "/Users/butcha/Developer/Skyll/aws-mwaa-local-runner",
    "read_only": True,
}

FEED_BY_PLATFORM = {1: "I_TT", 2: "I_STELLAR"}


def _last_flat_day(contract: dict):
    if contract["switch_on"] in (None, "before_window"):
        return contract["switch_on"]
    flats = [c["date"] for c in contract["days"] if c["flat"]]
    return flats[-1] if flats else "before_window"


def _days_open(contract: dict) -> int:
    n = 0
    for c in reversed(contract["days"]):
        if c["flat"]:
            break
        n += 1
    return n


def _investigate(c: dict, platform_id: int) -> dict:
    account, contract, verdict = c["account"], c["contract"], c["verdict"]
    a_q, c_q = quote(account), quote(contract)
    feed = FEED_BY_PLATFORM.get(platform_id, "I_TT")
    inv = {"hint": HINTS.get(verdict, "")}
    inv["raw_diff_api"] = (
        f"curl -s 'http://127.0.0.1:{_port()}/api/raw-diff?account={a_q}&contract={c_q}' | jq"
    )
    if verdict in ("drop", "unreconciled", "extra_misattr"):
        inv["raw_diff_ts"] = (
            f"secretctl run skyll-mwaa -- ./venv/bin/python -m dags.misc.recovery.raw_diff_ts "
            f"--account {account} --contract '{contract}' --platform {feed} --outdir /tmp/diffs"
        )
    if verdict == "drop":
        inv["recover_then_recalc"] = (
            "# in aws-mwaa-local-runner, after raw_diff_ts writes the reingest JSON:\n"
            f"… reingest.py /tmp/diffs/*.json   # gate: must print OK FLAT (drop-to-flat)\n"
            f"… recalc_trader.py --account {account} --contract '{contract}' --execute  "
            f"# then intraday → daily → caggs"
        )
    if verdict in ("stranded", "orphan"):
        inv["recalc"] = (
            f"secretctl run skyll-mwaa -- ./venv/bin/python dags/misc/recalc_trader.py "
            f"--account {account} --contract '{contract}'   # linkage-only; add --execute to write"
        )
    if verdict == "extra_misattr":
        inv["sql_by_trader"] = (
            "SELECT trader_id, side, sum(quantity), count(*), min(timestamp), max(timestamp) "
            f"FROM fills WHERE account = '{account}' AND contract = '{contract}' "
            "GROUP BY trader_id, side ORDER BY trader_id, side;  "
            "-- read BOTH columns; an alias-defaulted chunk is not the book's"
        )
    inv["sql_recent_fills"] = (
        "SELECT timestamp, side, quantity, price, fill_type, trader_id, trade_ids "
        f"FROM fills WHERE account = '{account}' AND contract = '{contract}' "
        "AND timestamp >= now() - interval '60 days' ORDER BY timestamp;"
    )
    return inv


def _port() -> int:
    import os
    return int(os.environ.get("VALIDATOR_PORT", "8799"))


def build_report(tree: dict, state: dict | None, *, severities=None, min_net=0.0,
                 group=None, trader=None, limit=None) -> dict:
    severities = severities or PROBLEM_SEVERITIES
    findings = []
    for g in tree["groups"]:
        if group and group.lower() not in (g["group_name"] or "").lower():
            continue
        for t in g["traders"]:
            if trader and trader.lower() not in (t["trader_name"] or "").lower():
                continue
            for a in t["accounts"]:
                for c in a["active"]:
                    if c["verdict"] not in severities:
                        continue
                    if abs(c["current_net"]) < min_net and c["verdict"] != "stranded":
                        continue
                    fix = c.get("fix") or {}
                    findings.append({
                        "severity": c["verdict"],
                        "group": g["group_name"],
                        "trader": t["trader_name"],
                        "trader_id": t["trader_id"],
                        "platform": a["platform_name"],
                        "account": c["account"],
                        "contract": c["contract"],
                        "is_sim": a["is_sim"],
                        "opt_out": a["opt_out"],
                        "current_net": c["current_net"],
                        "abs_net": abs(c["current_net"]),
                        "open_since": c["switch_on"],
                        "last_flat_day": _last_flat_day(c) if c["days"] else None,
                        "days_open": _days_open(c) if c["days"] else None,
                        "expired": c["expired"],
                        "first_fill": c["first_fill"],
                        "last_fill": c["last_fill"],
                        "fix": fix or None,
                        "stranded_info": c.get("stranded_info"),
                        "investigate": _investigate(c, a["platform_id"]),
                    })

    sev_rank = {s: i for i, s in enumerate(PROBLEM_SEVERITIES)}
    findings.sort(key=lambda f: (sev_rank.get(f["severity"], 9), -f["abs_net"]))
    total = len(findings)
    if limit:
        findings = findings[:limit]

    summary = {s: tree["overall"].get(s, 0) for s in
               ["drop", "extra_misattr", "stranded", "unreconciled", "orphan", "confirmed_open",
                "partial_carry", "unverifiable", "settled_residual", "flat"]}
    return {
        "meta": {
            "window": tree["window"],
            "generated_at": tree["generated_at"],
            "fix_checked": tree.get("fix_checked", False),
            "total_findings": total,
            "returned": len(findings),
            "filters": {"severities": severities, "min_net": min_net,
                        "group": group, "trader": trader, "limit": limit},
        },
        "health": tree.get("health", {}),
        "drop_rollup": tree.get("drop_rollup", []),
        "summary": summary,
        "playbook": PLAYBOOK,
        "findings": findings,
    }


def render_md(report: dict) -> str:
    m, s, h = report["meta"], report["summary"], report.get("health", {})
    out = [
        "# Skyll Trades Validator — findings",
        f"window {m['window']['start_date']} → {m['window']['end_date']} · "
        f"generated {m['generated_at']} · FIX-feed cross-check "
        f"{'ok' if m['fix_checked'] else 'NOT RUN'}",
        "",
        f"**{'✅ HEALTHY' if h.get('healthy') else '🔴 ' + str(h.get('actionable', 0)) + ' actionable'}** — "
        + h.get("headline", ""),
        "",
    ]
    rollup = report.get("drop_rollup") or []
    if rollup:
        out.append("**Drops by ingestion day** (a systemic gap is one row, not fifty):")
        for d in rollup:
            out.append(f"- `{d['day']}` — {d['fills']} fills, net {d['net']:+g}")
        out.append("")
    out.append(f"Showing {m['returned']} of {m['total_findings']} findings (worst first).")
    out.append("")
    for f in report["findings"]:
        fix = f["fix"] or {}
        feed = fix.get("feed", "")
        detail = (f"FIX {fix.get('raw_net')} vs ours {fix.get('our_net')}"
                  if "raw_net" in fix else "")
        if f["severity"] == "stranded" and f.get("stranded_info"):
            si = f["stranded_info"]
            detail = f"{si['n']} fills under trader 0/349 (net {si['net']:+g}), last {si['last']}"
        out.append(
            f"## [{f['severity']}] {f['trader']} — {f['account']} / {f['contract']}  "
            f"net {f['current_net']:+g}  ({feed} {detail})".rstrip()
        )
        meta_bits = f"group {f['group']} · {f['platform']}"
        if f["is_sim"]:
            meta_bits += " · SIM"
        if f["opt_out"]:
            meta_bits += " · OPT-OUT"
        if f.get("days_open") is not None:
            meta_bits += (f" · open {f['days_open']}d, since {f['open_since']} "
                          f"(last flat {f['last_flat_day']})")
        meta_bits += f" · last fill {f['last_fill']}"
        out.append(f"- {meta_bits}")
        out.append(f"- {f['investigate']['hint']}")
        if "raw_diff_ts" in f["investigate"]:
            out.append(f"- discover: `{f['investigate']['raw_diff_ts']}`")
        out.append("")
    out.append("---")
    out.append("Recover: " + " ".join(report["playbook"]["how_to_recover"]))
    return "\n".join(out)


def compute_report(window=None, with_fix=True, **filters) -> dict:
    state = engine.compute_state(window or Config.WINDOW_DAYS)
    if with_fix:
        try:
            fixfeed.enrich(state)
        except Exception as e:
            state["fix_checked"] = False
            state["fix_error"] = str(e)
    tree = engine.assemble_tree(state)
    tree["fix_error"] = state.get("fix_error")
    return build_report(tree, state, **filters)


def main():
    p = argparse.ArgumentParser(description="Agent-readable validation findings.")
    p.add_argument("--md", action="store_true", help="markdown digest instead of JSON")
    p.add_argument("--window", type=int, default=Config.WINDOW_DAYS)
    p.add_argument("--no-fix", action="store_true", help="skip the FIX-feed cross-check (faster)")
    p.add_argument("--severity", default=",".join(PROBLEM_SEVERITIES),
                   help="comma list: drop,extra_misattr,stranded,unreconciled,orphan,unverifiable")
    p.add_argument("--min-net", type=float, default=0.0)
    p.add_argument("--group", default=None)
    p.add_argument("--trader", default=None)
    p.add_argument("--limit", type=int, default=None)
    args = p.parse_args()

    report = compute_report(
        window=args.window, with_fix=not args.no_fix,
        severities=[s.strip() for s in args.severity.split(",") if s.strip()],
        min_net=args.min_net, group=args.group, trader=args.trader, limit=args.limit,
    )
    if args.md:
        print(render_md(report))
    else:
        json.dump(report, sys.stdout, indent=2, default=str)
        print()


if __name__ == "__main__":
    main()
