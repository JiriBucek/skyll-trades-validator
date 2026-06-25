"""Agent-readable report layer.

Turns the validation tree into a flat, machine-parseable list of *findings* — each with
enough context (account, contract, net, since-when, TT verdict) AND concrete
*investigation pointers* (the tt-diff command, the SQL to pull the fills, which known
failure mode + fix tool applies) for an AI agent to pick up and go dig into the DB.

Two renderings: `json` (full, for `jq`/parsing) and `md` (compact digest, top-N).

Run offline (no server):
    secretctl run skyll-mwaa -- ./venv/bin/python -m app.report            # JSON
    secretctl run skyll-mwaa -- ./venv/bin/python -m app.report --md       # markdown digest
    ... --severity suspected_drop,orphan --min-net 2 --group Axia --limit 40 --window 45
Or hit the running server:  GET /api/findings  (?format=md, ?severity=, ?min_net=)
"""
from __future__ import annotations

import argparse
import json
import sys
from urllib.parse import quote

from . import engine, tt
from .config import Config

PROBLEM_SEVERITIES = ["suspected_drop", "orphan", "open_unverifiable"]

# Per-severity: what it means + how to investigate root cause (encodes the dropped-fill runbook).
HINTS = {
    "suspected_drop": (
        "Open in our DB but TT reports flat (or smaller / opposite) and the position was carried "
        "past today — almost always a SILENTLY DROPPED FILL. Most likely cause on high-volume "
        "accounts: the µs-collision PK (fills natural-key rounds ns→µs, drops same-µs fills); also "
        "un-paginated TT reads (ttledger/fills caps 500/call) or a skipped clearing-alias fill. "
        "Run the tt-diff to pinpoint the exact missing fill(s), recover them, then recalc."
    ),
    "open_unverifiable": (
        "Open with no TT cross-check available (Stellar account, or account not reported by TT, or "
        "opened today = possible ingestion lag). For Stellar, check raw_fills_fix (platform "
        "'I_STELLAR', match on exec_id) for fills our `fills` table is missing; for TT-but-not-"
        "reported, the account may be a give-up/clearing account."
    ),
    "orphan": (
        "Fills exist on a completed day with empty `trade_ids` — the create-trades aggregation "
        "skipped them, so trades won't reconcile even if raw fills balance. Likely causes: "
        "trader_id=0 stranding (fills ingested before the trader_platforms mapping existed), an "
        "unresolved clearing alias, or a fill_type the aggregator filters out (it only takes "
        "'Outright')."
    ),
}

# Top-level playbook handed to the agent once.
PLAYBOOK = {
    "what_this_is": "Integrity findings for the Skyll fills→trades→intraday→daily pipeline. "
                    "Each finding is a (account, contract) whose end-of-day net position is not flat "
                    "in a way that looks wrong. READ-ONLY — investigate, do not fix from here.",
    "known_failure_modes": [
        "µs-collision: fills PK is the 6-col natural key; TT ns timestamps round to µs, so two "
        "same-price/qty fills in the same µs collide and the 2nd is dropped (hits high-volume "
        "traders e.g. Demetris hardest).",
        "un-paginated TT reads: ttledger/fills caps at 500/call; ingestion doesn't paginate, "
        "so busy windows silently truncate.",
        "skipped clearing-alias fill (Stellar): unresolved alias → fill left unprocessed.",
        "cash-settled expiry: a position carried to a cash-settled future's expiry has no closing "
        "fill → looks open forever (expected; bucketed as settled_residual, NOT a finding).",
        "trader_id=0 stranding / orphan: fills ingested before trader_platforms mapping existed, "
        "or aggregator filtered the fill_type.",
    ],
    "how_to_investigate": [
        "1. Reach the missing fill: GET /api/tt-diff?account=&contract=&days=N (widen N past the "
        "open-since day) — lists TT fills absent from our DB.",
        "2. Confirm against source: TT → ttledger/{env}/fills (paginate!); Stellar → raw_fills_fix "
        "WHERE platform='I_STELLAR' AND exec_id matching.",
        "3. Recover (in aws-mwaa-local-runner, NOT here): re-ingest missing fills idempotently, or "
        "inject_correction_fill.py for a flat-nightly residual; then recalc_trader.py --account; "
        "then rebuild intraday/daily.",
    ],
    "fix_tools_repo": "/Users/butcha/Developer/Skyll/aws-mwaa-local-runner",
    "read_only": True,
}


def _last_flat_day(contract: dict):
    if contract["switch_on"] in (None, "before_window"):
        return contract["switch_on"]  # None (flat) or 'before_window'
    flats = [c["date"] for c in contract["days"] if c["flat"]]
    return flats[-1] if flats else "before_window"


def _days_open(contract: dict) -> int:
    n = 0
    for c in reversed(contract["days"]):
        if c["flat"]:
            break
        n += 1
    return n


def _investigate(account: str, contract: str, verdict: str, platform_id: int) -> dict:
    a_q, c_q = quote(account), quote(contract)
    inv = {
        "hint": HINTS.get(verdict, ""),
        "sql_recent_fills": (
            "SELECT timestamp, side, quantity, price, fill_type, trade_ids "
            f"FROM fills WHERE account = '{account}' AND contract = '{contract}' "
            "AND timestamp >= now() - interval '60 days' ORDER BY timestamp;"
        ),
    }
    if platform_id == 1:  # TT — fills diff is the primary tool
        inv["tt_fills_diff_api"] = (
            f"curl -s 'http://127.0.0.1:8799/api/tt-diff?account={a_q}&contract={c_q}&days=60' | jq"
        )
        inv["recover_then_recalc"] = (
            f"# in aws-mwaa-local-runner, after recovering the fill(s):\n"
            f"secretctl run skyll-mwaa -- ./venv/bin/python dags/misc/recalc_trader.py "
            f"--account {account} --contract '{contract}'   # dry-run; add --execute to write"
        )
    else:  # Stellar
        inv["check_stellar_source"] = (
            "SELECT * FROM raw_fills_fix WHERE platform = 'I_STELLAR' "
            f"AND account ILIKE '%{account}%' ORDER BY exec_timestamp DESC LIMIT 100;"
        )
    return inv


def build_report(tree: dict, state: dict, *, severities=None, min_net=0.0,
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
                    if abs(c["current_net"]) < min_net:
                        continue
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
                        "last_flat_day": _last_flat_day(c),
                        "days_open": _days_open(c),
                        "expired": c["expired"],
                        "first_fill": c["first_fill"],
                        "last_fill": c["last_fill"],
                        "tt": c["tt"],
                        "investigate": _investigate(c["account"], c["contract"],
                                                    c["verdict"], a["platform_id"]),
                    })

    sev_rank = {s: i for i, s in enumerate(["suspected_drop", "orphan", "open_unverifiable"])}
    findings.sort(key=lambda f: (sev_rank.get(f["severity"], 9), -f["abs_net"]))
    total = len(findings)
    if limit:
        findings = findings[:limit]

    summary = {s: tree["overall"].get(s, 0) for s in
               ["suspected_drop", "orphan", "open_unverifiable", "open_confirmed",
                "settled_residual", "flat"]}
    return {
        "meta": {
            "window": tree["window"],
            "generated_at": tree["generated_at"],
            "tt_checked": tree["tt_checked"],
            "tt_error": tree.get("tt_error"),
            "total_findings": total,
            "returned": len(findings),
            "filters": {"severities": severities, "min_net": min_net,
                        "group": group, "trader": trader, "limit": limit},
        },
        "summary": summary,
        "playbook": PLAYBOOK,
        "findings": findings,
    }


def render_md(report: dict) -> str:
    m, s = report["meta"], report["summary"]
    out = [
        f"# Skyll Trades Validator — findings",
        f"window {m['window']['start_date']} → {m['window']['end_date']} · "
        f"generated {m['generated_at']} · TT {'ok' if m['tt_checked'] else 'UNAVAILABLE'}",
        "",
        f"**{s['suspected_drop']}** suspected dropped fills · **{s['orphan']}** orphan · "
        f"**{s['open_unverifiable']}** open-unverifiable · {s['open_confirmed']} confirmed-open · "
        f"{s['flat']} flat · {s['settled_residual']} settled residuals",
        "",
        f"Showing {m['returned']} of {m['total_findings']} findings (worst first).",
        "",
    ]
    for f in report["findings"]:
        tt = f["tt"] or {}
        ttn = f"TT {tt.get('tt_net')}" if tt.get("checked") and tt.get("in_tt") is not False else "TT n/a"
        out.append(
            f"## [{f['severity']}] {f['trader']} — {f['account']} / {f['contract']}  "
            f"net {f['current_net']:+g} ({ttn})"
        )
        out.append(
            f"- group {f['group']} · {f['platform']}{' · SIM' if f['is_sim'] else ''}"
            f"{' · OPT-OUT' if f['opt_out'] else ''} · open {f['days_open']}d, "
            f"since {f['open_since']} (last flat {f['last_flat_day']}) · last fill {f['last_fill']}"
        )
        out.append(f"- {f['investigate']['hint']}")
        if "tt_fills_diff_api" in f["investigate"]:
            out.append(f"- diff: `{f['investigate']['tt_fills_diff_api']}`")
        out.append("")
    out.append("---")
    out.append("Playbook: " + " ".join(report["playbook"]["how_to_investigate"]))
    return "\n".join(out)


def compute_report(window=None, with_tt=True, **filters) -> dict:
    state = engine.compute_state(window or Config.WINDOW_DAYS)
    if with_tt:
        try:
            tt.enrich(state)
        except Exception as e:
            state["tt_checked"] = False
            state["tt_error"] = str(e)
    tree = engine.assemble_tree(state)
    tree["tt_error"] = state.get("tt_error")
    return build_report(tree, state, **filters)


def main():
    p = argparse.ArgumentParser(description="Agent-readable validation findings.")
    p.add_argument("--md", action="store_true", help="markdown digest instead of JSON")
    p.add_argument("--window", type=int, default=Config.WINDOW_DAYS)
    p.add_argument("--no-tt", action="store_true", help="skip the TT cross-check (faster)")
    p.add_argument("--severity", default=",".join(PROBLEM_SEVERITIES),
                   help="comma list: suspected_drop,orphan,open_unverifiable")
    p.add_argument("--min-net", type=float, default=0.0)
    p.add_argument("--group", default=None)
    p.add_argument("--trader", default=None)
    p.add_argument("--limit", type=int, default=None)
    args = p.parse_args()

    report = compute_report(
        window=args.window, with_tt=not args.no_tt,
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
