"""Live TT open-position cross-check (READ-ONLY against the TT REST API).

The validator's nets come from OUR fills ledger. This module asks TT what IT currently thinks
the open positions are, so every open line on the dashboard can be checked against the
platform's own book — the detector for the whole "phantom open" family (missed fill on our
side, expiry carry, sim position reset, TT double-booking).

Design — bulk snapshot, NOT per-line queries:
  - `GET /ttmonitor/{env}/position` IGNORES the accountId param and returns EVERY account's
    rows (verified repeatedly; see hive/apis/skyll.md). Per-line querying is impossible anyway —
    and that makes the whole check cheap: ONE paginated pull per env (live + sim) covers every
    line at once, a handful of API calls per refresh.
  - Account-name resolution runs in the RELIABLE direction. name→accountId is the historically
    brittle path (give-up/clearing accounts are missing from the accounts list;
    platform_trader_id is the stale TT *user* id). We invert it: each accountId that HOLDS a
    position resolves via `GET /ttaccount/{env}/account/{id}` — which does see give-up accounts —
    and the id→name / instrumentId→alias maps are cached persistently (backend/.ttpos_cache.json),
    so the warm-up cost is paid once.
  - ABSENCE = FLAT. The endpoint lists idle open positions too (verified 2026-07-03:
    BPC_PLEKOVIC net −3 visible with no fills for days), so "no TT row" genuinely means TT
    thinks the position is flat — that is the signal, not a blind spot.

Interpretation caveat (surfaced in every payload): the TT number is LIVE while our fills are
batch-ingested (~15 min lag), so a contract trading right now may legitimately differ. The
start-of-day net (sodNetPos) is included as the lag-insensitive comparison point.

Only TT-platform accounts (platform_id == 1) can be checked; Stellar has no TT API.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests

from .config import Config

_CACHE_PATH = Path(__file__).resolve().parent.parent / ".ttpos_cache.json"

_lock = threading.Lock()
_tokens: dict[str, tuple[str, float]] = {}          # env -> (Authorization, expires_at)
_snapshot_cache: dict[str, tuple[float, dict]] = {}  # env -> (fetched_at, {name: {alias: row}})
_id_maps: dict | None = None                         # persisted {"names": {env: {id: name}}, "aliases": {env: {id: alias}}}


class TTError(Exception):
    pass


# --------------------------------------------------------------------------- persistent id maps
def _load_maps() -> dict:
    global _id_maps
    if _id_maps is None:
        try:
            _id_maps = json.loads(_CACHE_PATH.read_text())
        except Exception:
            _id_maps = {}
        _id_maps.setdefault("names", {})
        _id_maps.setdefault("aliases", {})
    return _id_maps


def _save_maps() -> None:
    try:
        fd, tmp = tempfile.mkstemp(dir=str(_CACHE_PATH.parent), prefix=".ttpos_cache.")
        with os.fdopen(fd, "w") as f:
            json.dump(_id_maps, f)
        os.replace(tmp, _CACHE_PATH)
    except Exception:
        pass  # the cache is an optimization; never let it break the check


# --------------------------------------------------------------------------- TT REST client
def _secret(env: str) -> str:
    s = Config.TT_SIM_APP_SECRET if env == Config.TT_ENV_SIM else Config.TT_APP_SECRET
    if not s:
        raise TTError(f"TT credentials for {env} not set — launch via `secretctl run skyll-mwaa -- …` "
                      f"(keychain unlocked?).")
    return s


def _token(env: str) -> str:
    now = time.time()
    tok = _tokens.get(env)
    if tok and tok[1] > now:
        return tok[0]
    secret = _secret(env)
    r = requests.post(
        f"{Config.TT_URL_BASE}/ttid/{env}/token",
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json",
                 "x-api-key": secret.split(":")[0]},
        data={"grant_type": "user_app", "app_key": secret},
        params={"requestId": f"{Config.REQUEST_ID_BASE}--{uuid.uuid4()}"},
        timeout=Config.TTPOS_TIMEOUT,
    )
    if r.status_code != 200:
        raise TTError(f"token {env}: HTTP {r.status_code} {r.text[:200]}")
    j = r.json()
    auth = f"{j['token_type'].capitalize()} {j['access_token']}"
    ttl = float(j.get("seconds_until_expiry", 3600))
    _tokens[env] = (auth, now + max(60.0, ttl - 300.0))
    return auth


def _get(env: str, path: str, params: dict | None = None, retries: int = 5) -> dict:
    """GET with the TT auth headers; polite retry on 429/5xx (the mwaa client just raises)."""
    url = f"{Config.TT_URL_BASE}/{path.lstrip('/')}"
    for attempt in range(retries):
        hdr = {"x-api-key": _secret(env).split(":")[0], "Authorization": _token(env)}
        p = dict(params or {})
        p["requestId"] = f"{Config.REQUEST_ID_BASE}--{uuid.uuid4()}"
        r = requests.get(url, headers=hdr, params=p, timeout=Config.TTPOS_TIMEOUT)
        if r.status_code == 200:
            return r.json()
        if r.status_code in (429, 500, 502, 503, 504) and attempt < retries - 1:
            wait = float(r.headers.get("Retry-After") or (1.5 * (attempt + 1)))
            time.sleep(min(wait, 15.0))
            continue
        if r.status_code == 401 and attempt < retries - 1:
            _tokens.pop(env, None)   # expired token — refresh and retry
            continue
        raise TTError(f"GET {path} [{env}]: HTTP {r.status_code} {r.text[:200]}")
    raise TTError(f"GET {path} [{env}]: retries exhausted")


def _all_positions(env: str, max_pages: int = 60) -> list[dict]:
    out, npk = [], None
    for _ in range(max_pages):
        params = {} if npk is None else {"nextPageKey": npk}
        r = _get(env, f"ttmonitor/{env}/position", params)
        out.extend(r.get("positions", []))
        if str(r.get("lastPage")).lower() == "true":
            break
        npk = r.get("nextPageKey")
        if npk is None:
            break
    return out


def _account_name(env: str, account_id) -> str:
    m = _load_maps()["names"].setdefault(env, {})
    key = str(account_id)
    if key not in m:
        try:
            rec = _get(env, f"ttaccount/{env}/account/{key}")["account"][0]
            m[key] = (rec.get("name") or "").strip()
        except Exception:
            return ""   # unresolvable id — don't poison the persistent cache
    return m[key]


def _alias(env: str, instrument_id) -> str:
    m = _load_maps()["aliases"].setdefault(env, {})
    key = str(instrument_id)
    if key not in m:
        try:
            rec = _get(env, f"ttpds/{env}/instrument/{key}")["instrument"][0]
            m[key] = (rec.get("alias") or "").strip() or key
        except Exception:
            return key   # expired/delisted instrument — fall back to the raw id, don't cache
    return m[key]


# --------------------------------------------------------------------------- snapshot per env
def _env_snapshot(env: str, wanted_names: set[str], refresh: bool) -> dict:
    """{account_name: {contract: row}} for WANTED accounts' NONZERO TT positions in one env.
    Cached for TTPOS_CACHE_TTL seconds. The id→name/alias maps persist across restarts."""
    now = time.time()
    hit = _snapshot_cache.get(env)
    if hit and not refresh and now - hit[0] < Config.TTPOS_CACHE_TTL:
        return hit[1]

    raw = _all_positions(env)
    nonzero = [p for p in raw if abs(float(p.get("netPosition", 0) or 0)) > 1e-9]

    by_name: dict[str, dict] = {}
    for p in nonzero:
        name = _account_name(env, p.get("accountId"))
        if name not in wanted_names:
            continue
        contract = _alias(env, p.get("instrumentId"))
        row = by_name.setdefault(name, {}).setdefault(contract, {
            "net": 0.0, "sod": 0.0, "pnl": 0.0, "realized": 0.0})
        row["net"] += float(p.get("netPosition", 0) or 0)
        row["sod"] += float(p.get("sodNetPos", 0) or 0)
        row["pnl"] += float(p.get("pnl", 0) or 0)
        row["realized"] += float(p.get("realizedPnl", 0) or 0)

    _save_maps()
    snap = {"by_name": by_name, "rows_scanned": len(raw), "rows_nonzero": len(nonzero)}
    _snapshot_cache[env] = (now, snap)
    return snap


# --------------------------------------------------------------------------- the check
def check(tree: dict, refresh: bool = False) -> dict:
    """Annotate the overview tree's OPEN contract rows with TT's live position book.

    Returns {rows, tt_only, errors, ...}: every open (|current_net| > eps) row gets a status —
      match    TT agrees (|tt_net − db_net| <= TTPOS_NET_TOL)
      diff     TT shows a DIFFERENT nonzero position
      tt_flat  TT has NO row ⇒ TT thinks flat (phantom-open family: missed fill our side /
               position reset / double-booked ledger — see the runbook)
      expired  contract already expired — TT drops delisted instruments, comparison meaningless
      no_api   not a TT-platform account (Stellar) — nothing to ask
      error    that env's snapshot failed (creds/network) — see errors{}
    Plus tt_only: TT-open positions for cohort accounts with NO open validator line (flat in
    our DB or out of window) — the reverse detector (drop on OUR side).
    """
    with _lock:
        return _check_locked(tree, refresh)


def _check_locked(tree: dict, refresh: bool) -> dict:
    eps = Config.FLAT_EPS
    tol = Config.TTPOS_NET_TOL

    # collect the open rows + account meta from the assembled tree (no extra DB work)
    open_rows: list[dict] = []          # {account, contract, db_net, env|None, expired, spread}
    acct_env: dict[str, str | None] = {}  # account -> env (None = not TT platform)
    tree_net: dict[tuple, float] = {}     # (account, contract) -> db net, for tt_only annotation
    for g in tree.get("groups", []):
        for t in g.get("traders", []):
            for a in t.get("accounts", []):
                env = ((Config.TT_ENV_SIM if a.get("is_sim") else Config.TT_ENV_LIVE)
                       if a.get("platform_id") == 1 else None)
                acct_env[a["account"]] = env
                for c in a.get("contracts", []):
                    tree_net[(c["account"], c["contract"])] = c["current_net"]
                    if abs(c["current_net"]) > eps:
                        open_rows.append({
                            "account": c["account"], "contract": c["contract"],
                            "db_net": c["current_net"], "env": env,
                            "expired": bool(c.get("expired")), "spread": bool(c.get("is_spread")),
                        })

    # one bulk snapshot per env that has open TT rows (the accountId filter is ignored by TT
    # anyway, so this is the cheapest possible access pattern)
    wanted_by_env: dict[str, set[str]] = {}
    for acct, env in acct_env.items():
        if env:
            wanted_by_env.setdefault(env, set()).add(acct)
    snaps: dict[str, dict] = {}
    errors: dict[str, str] = {}
    # NB: pull an env even when it has no open validator lines — the reverse detector (tt_only)
    # matters most exactly then (TT open + we show nothing = a drop on OUR side).
    for env, wanted in wanted_by_env.items():
        try:
            snaps[env] = _env_snapshot(env, wanted, refresh)
        except Exception as e:
            errors[env] = str(e)

    # annotate the open rows
    out_rows = []
    for r in open_rows:
        env = r["env"]
        row = dict(r)
        row.pop("env", None)
        row["tt_env"] = env
        if env is None:
            row.update(status="no_api", tt_net=None)
        elif r["expired"]:
            row.update(status="expired", tt_net=None)
        elif env in errors:
            row.update(status="error", tt_net=None)
        elif env not in snaps:
            row.update(status="error", tt_net=None)
        else:
            tt = snaps[env]["by_name"].get(r["account"], {}).get(r["contract"])
            if tt is None:
                row.update(status="tt_flat", tt_net=0.0)
            else:
                row.update(
                    status="match" if abs(tt["net"] - r["db_net"]) <= tol else "diff",
                    tt_net=round(tt["net"], 6), tt_sod=round(tt["sod"], 6),
                    tt_pnl=round(tt["pnl"], 2), tt_realized=round(tt["realized"], 2),
                )
        out_rows.append(row)

    # reverse detector: TT-open rows with no open validator line
    open_keys = {(r["account"], r["contract"]) for r in open_rows}
    tt_only = []
    for env, snap in snaps.items():
        for name, contracts in snap["by_name"].items():
            for contract, tt in contracts.items():
                if (name, contract) in open_keys:
                    continue
                db = tree_net.get((name, contract))
                tt_only.append({
                    "account": name, "contract": contract, "tt_env": env,
                    "tt_net": round(tt["net"], 6), "tt_sod": round(tt["sod"], 6),
                    "tt_pnl": round(tt["pnl"], 2),
                    # null db_net = the contract has no window fills (dormant) — not proven flat
                    "db_net": db,
                })
    tt_only.sort(key=lambda x: -abs(x["tt_net"]))

    counts: dict[str, int] = {}
    for r in out_rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    return {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "note": ("TT netPosition is LIVE; our fills are batch-ingested (~15 min lag) — a diff on a "
                 "contract trading right now is expected. tt_sod (start-of-day net) is the "
                 "lag-insensitive comparison."),
        "envs": {env: {"rows_scanned": s["rows_scanned"], "rows_nonzero": s["rows_nonzero"]}
                 for env, s in snaps.items()},
        "errors": errors,
        "counts": counts,
        "rows": sorted(out_rows, key=lambda r: (r["status"] != "diff", r["status"] != "tt_flat",
                                                -abs(r["db_net"]))),
        "tt_only": tt_only,
    }
