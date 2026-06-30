"""batch_diff.py — fetch a TT account's fills ONCE, diff EVERY contract against our DB.

app.tt.fills_diff re-paginates the WHOLE account ledger for each (account, contract) — so
checking N contracts on a busy account costs N full account fetches (the expensive part is
TT's 500/call pagination over the window, NOT the per-contract diff). This tool fetches the
account ledger ONCE for the window and diffs every contract in Python, amortizing that cost.

Reuses the validator's TTClient + the exact ns->us multiset matching used by fills_diff, so a
genuine same-microsecond collision drop (TT has 2, we have 1) is counted correctly.

Read-only. Writes per-contract reingest-compatible JSON ({account, contract, missing:[...]}) for
any contract with missing TT fills, into --outdir, so recovery/reingest.py can consume
them directly.

Usage (from skyll-trades-validator/backend):
  secretctl run skyll-mwaa -- ./venv/bin/python batch_diff.py --account LFCTEU16 --days 120 \
      [--contracts "R Sep26,FESX Jun26"] [--only-nonflat] [--outdir /tmp/diffs]
"""
import argparse, json, os, re, time
from bisect import bisect_left
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta

from app.tt import _clients, db   # reuse the validator's TT client factory + DB helper

TOL_US = 5000   # match tolerance in microseconds (jitter is ~1µs; also absorbs minor clock skew)


def us_key(dt) -> int:
    return int(round(dt.timestamp() * 1e6))


def unmatched_tt(our, tt_rows):
    """Jitter-tolerant diff: greedily match each of our fills to its NEAREST unmatched TT fill of
    the same (side, qty) within TOL_US, then return the TT fills left unmatched = genuinely missing.

    Why: our pre-fix fills carry NULL unique_exec_id and DB-µs timestamps that differ from TT's
    ns->µs rounding by ~1µs, so an exact-µs multiset match falsely flags real fills as 'missing'
    (and the net then fails to reconcile). Nearest-match within tolerance pairs each real fill to
    its twin, leaving only true collision drops."""
    our_b = defaultdict(list)
    for ts, side, qty in our:
        our_b[(side, qty)].append(ts)
    for v in our_b.values():
        v.sort()
    tt_b = defaultdict(list)
    for tf in tt_rows:
        ts = int(round(int(tf["timeStamp"]) / 1000.0))   # ns -> µs
        tt_b[(int(tf.get("side")), round(float(tf.get("lastQty")), 4))].append((ts, tf))
    missing = []
    for key, tt_items in tt_b.items():
        tt_items.sort(key=lambda x: x[0])
        tt_ts = [t for t, _ in tt_items]
        used = [False] * len(tt_items)
        for ots in our_b.get(key, []):
            best, bestd = -1, TOL_US + 1
            lo = bisect_left(tt_ts, ots)
            cand = list(range(max(0, lo - 2), min(len(tt_items), lo + 3)))
            if all(used[j] for j in cand):
                cand = range(len(tt_items))            # fallback: scan all unmatched
            for j in cand:
                if not used[j]:
                    d = abs(tt_ts[j] - ots)
                    if d < bestd:
                        bestd, best = d, j
            if best >= 0 and bestd <= TOL_US:
                used[best] = True
        for j, (ts, tf) in enumerate(tt_items):
            if not used[j]:
                missing.append({
                    "timestamp": datetime.fromtimestamp(int(tf["timeStamp"]) / 1e9, tz=timezone.utc).isoformat(),
                    "side": key[0], "qty": key[1], "price": tf.get("lastPx"),
                    "execId": tf.get("execId"), "uniqueExecId": tf.get("uniqueExecId")})
    return missing


def is_option(contract: str) -> bool:
    """Option contracts carry a strike token like 'C6260', 'P740500', 'C112'."""
    return bool(re.search(r"(?:^|\s)[PC]\d{2,}", contract))


def bucket(is_opt: bool, db_net: float, days_since: float, tt_fills: int, missing: int, net_missing: float) -> str:
    """Classify a non-flat contract. Flat-anchor principle (operator): traders flatten overnight and
    DEFINITELY over weekends, so a position untouched for >7 days SHOULD be flat — if it isn't, it's a
    drop/gap/settlement, not a live position."""
    if abs(db_net) < 1e-6:
        return "flat"
    if days_since is not None and days_since <= 7:
        return "OPEN_RECENT"               # traded within the week -> net!=0 may be a live position
    # should-be-flat (untouched past a weekend) but isn't:
    if is_opt:
        return "OPTION_SETTLEMENT"          # expired option net!=0 = exercise/assignment, not a dropped fill
    if missing and abs(db_net + net_missing) < 0.5:
        return "CLEAN_DROP"                 # backfilling the missing TT fills flattens it -> safe to recover
    if tt_fills == 0:
        return "UNTESTABLE_OLD"             # TT ledger aged out (>~120d) -> can't confirm via TT
    return "INGESTION_GAP"                  # should be flat, TT has fills, but backfill doesn't reconcile


NET_SQL = """
SELECT contract,
       sum(CASE WHEN side = 1 THEN quantity ELSE -quantity END) AS net,
       count(*) AS c,
       max(timestamp) AS last_fill
FROM fills
WHERE account = %(account)s AND timestamp >= %(start)s
GROUP BY contract
"""

FILLS_SQL = """
SELECT contract, timestamp, quantity, side
FROM fills
WHERE account = %(account)s AND timestamp >= %(start)s AND contract = ANY(%(cts)s)
ORDER BY timestamp
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", required=True)
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--contracts", help="comma-separated; default = all contracts seen")
    ap.add_argument("--only-nonflat", action="store_true", help="only report contracts whose DB net != 0")
    ap.add_argument("--outdir", help="write per-contract reingest JSONs here (CLEAN_DROP contracts only)")
    ap.add_argument("--summary-json", help="append one JSON line per contract here (for cross-account aggregation)")
    args = ap.parse_args()

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days)
    start_ns, end_ns = int(start.timestamp() * 1e9), int(end.timestamp() * 1e9)
    want = set(s.strip() for s in args.contracts.split(",")) if args.contracts else None

    # --- 1a. net + count + last-fill per contract via SQL (cheap; avoids pulling the whole ledger) ---
    db_net, db_count, days_since = {}, {}, {}
    for r in db.query(NET_SQL, {"account": args.account, "start": start}):
        db_net[r["contract"]] = round(float(r["net"]), 4)
        db_count[r["contract"]] = int(r["c"])
        lf = r["last_fill"]
        if lf is not None:
            if lf.tzinfo is None:
                lf = lf.replace(tzinfo=timezone.utc)
            days_since[r["contract"]] = (end - lf).total_seconds() / 86400.0

    # target contracts = explicit --contracts, else the NON-FLAT ones (a drop always shifts net).
    # This keeps the per-fill pull (and the diff) scoped — critical on huge accounts where pulling
    # every fill into Python is the real bottleneck (e.g. 450k+ rows).
    targets = set(want) if want else {ct for ct, n in db_net.items() if abs(n) > 1e-6}
    if not targets:
        print("# no non-flat contracts in window — nothing to diff"); return

    # --- 1b. per-fill (ts_us, side, qty) ONLY for the target contracts ---
    our_fills = defaultdict(list)
    for f in db.query(FILLS_SQL, {"account": args.account, "start": start, "cts": list(targets)}):
        our_fills[f["contract"]].append((us_key(f["timestamp"]), int(f["side"]), round(float(f["quantity"]), 4)))

    # --- 2. fetch the TT account ledger ONCE ---
    t0 = time.time()
    client = tt_fills = None
    for c in _clients():
        acct_id = c.name_to_id().get(args.account)
        if acct_id is None:
            continue
        tt_fills = c.fills(acct_id, start_ns, end_ns)
        client = c
        break
    if tt_fills is None:
        print(f"ABORT: account {args.account} not found in any TT env"); return
    fetch_s = time.time() - t0
    print(f"# fetched {len(tt_fills)} TT fills for {args.account} over {args.days}d in {fetch_s:.0f}s (env {client.env})")

    # --- 3. group TT fills by contract (instrument alias), build TT multiset ---
    tt_by_contract = defaultdict(list)
    for tf in tt_fills:
        alias = client.instrument_alias(tf.get("instrumentId"))
        tt_by_contract[alias].append(tf)

    # --- 4. diff each TARGET contract with the jitter-tolerant matcher ---
    contracts = set(targets)
    results = []
    for ct in sorted(contracts):
        tt_list = tt_by_contract.get(ct, [])
        tt_total = len(tt_list)
        missing = unmatched_tt(our_fills.get(ct, []), tt_list)
        net = round(db_net.get(ct, 0.0), 4)
        if args.only_nonflat and abs(net) < 1e-6 and not missing:
            continue
        net_missing = round(sum((m["qty"] if m["side"] == 1 else -m["qty"]) for m in missing), 4)
        ds = days_since.get(ct)
        opt = is_option(ct)
        verdict = bucket(opt, net, ds, tt_total, len(missing), net_missing)
        results.append({"account": args.account, "contract": ct, "db_net": net,
                        "db_fills": db_count.get(ct, 0), "tt_fills": tt_total, "missing": len(missing),
                        "net_missing": net_missing, "days_since": round(ds, 1) if ds is not None else None,
                        "is_option": opt, "verdict": verdict, "_missing": missing})

    results.sort(key=lambda r: (r["verdict"], -abs(r["db_net"])))
    print(f"{'contract':22s} {'db_net':>7s} {'db_f':>6s} {'tt_f':>6s} {'miss':>5s} {'netmiss':>8s} {'dlast':>6s}  bucket")
    print("-" * 94)
    for r in results:
        dl = f"{r['days_since']:.0f}" if r["days_since"] is not None else "?"
        print(f"{r['contract']:22s} {r['db_net']:+7g} {r['db_fills']:6d} {r['tt_fills']:6d} "
              f"{r['missing']:5d} {r['net_missing']:+8g} {dl:>6s}  {r['verdict']}")
        # only emit reingest JSON for CLEAN_DROP (safe to backfill); gaps need the flat-anchor method
        if args.outdir and r["verdict"] == "CLEAN_DROP":
            os.makedirs(args.outdir, exist_ok=True)
            fn = os.path.join(args.outdir, f"{args.account}_{r['contract'].replace(' ', '_').replace('/', '-')}.json")
            json.dump({"account": args.account, "contract": r["contract"], "env": client.env,
                       "days": args.days, "missing_count": r["missing"], "net_missing": r["net_missing"],
                       "missing": r["_missing"]}, open(fn, "w"), indent=1)
    counts = Counter(r["verdict"] for r in results)
    print(f"\n# {args.account}: " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if args.summary_json:
        os.makedirs(os.path.dirname(args.summary_json), exist_ok=True)
        with open(args.summary_json, "a") as f:
            for r in results:
                f.write(json.dumps({k: r[k] for k in r if k != "_missing"}) + "\n")


if __name__ == "__main__":
    main()
