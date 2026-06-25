"""TT REST client (read-only) for the two cross-checks:

  1. position-now  -> enrich(state): for every currently-open TT contract, compare our DB net
                      against TT's live netPosition. Classifies open_confirmed vs suspected_drop.
  2. fills_diff    -> on-demand per (account, contract): paginate TT's fills ledger and diff
                      against our DB fills to pinpoint the exact missing fill(s).

Read-only: only GET endpoints + the token POST. Never mutates TT.
Account names and instrument aliases map 1:1 to our DB `account` / `contract`.
"""
from __future__ import annotations

import json
import os
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from . import db
from .config import Config

CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache"
CACHE_DIR.mkdir(exist_ok=True)

# (env, app_secret, user_id) for each TT environment we check
ENVS = [
    ("ext_prod_live", lambda: Config.TT_APP_SECRET, lambda: Config.TT_PROD_USER_ID),
    ("ext_prod_sim", lambda: Config.TT_SIM_APP_SECRET, lambda: Config.TT_SIM_USER_ID),
]


def _norm_account(name: str | None) -> str:
    return (name or "").lstrip("&").strip()


class TTClient:
    def __init__(self, env: str, app_secret: str, user_id: str):
        self.env = env
        self.app_secret = app_secret
        self.user_id = user_id
        self.app_key = app_secret.split(":")[0]
        self._token = None
        self._instr_cache = self._load_cache(f"instruments_{env}.json")

    # --- low level ---
    def _load_cache(self, fname: str) -> dict:
        p = CACHE_DIR / fname
        if p.exists():
            try:
                return json.loads(p.read_text())
            except Exception:
                return {}
        return {}

    def _save_cache(self, fname: str, data: dict):
        try:
            (CACHE_DIR / fname).write_text(json.dumps(data))
        except Exception:
            pass

    def token(self) -> str:
        if self._token:
            return self._token
        r = requests.post(
            f"{Config.TT_URL_BASE}/ttid/{self.env}/token",
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Accept": "application/json", "x-api-key": self.app_key},
            data={"grant_type": "user_app", "app_key": self.app_secret},
            timeout=30,
        )
        r.raise_for_status()
        j = r.json()
        self._token = f"{j['token_type'].capitalize()} {j['access_token']}"
        return self._token

    def get(self, path: str, params: dict | None = None) -> dict:
        params = dict(params or {})
        params["requestId"] = f"{Config.REQUEST_ID_BASE}--{uuid.uuid4()}"
        r = requests.get(
            f"{Config.TT_URL_BASE}/{path}",
            headers={"x-api-key": self.app_key, "Authorization": self.token()},
            params=params, timeout=60,
        )
        if r.status_code != 200:
            raise RuntimeError(f"TT {path} -> HTTP {r.status_code}: {r.text[:200]}")
        return r.json()

    # --- resolvers ---
    def accounts_map(self) -> dict[int, str]:
        """accountId -> normalized account name (one bulk call)."""
        data = self.get(f"ttuser/{self.env}/user/{self.user_id}/accounts")
        return {a["accountId"]: _norm_account(a.get("accountName"))
                for a in data.get("accounts", [])}

    def name_to_id(self) -> dict[str, int]:
        return {v: k for k, v in self.accounts_map().items()}

    def instrument_alias(self, instrument_id: str) -> str | None:
        key = str(instrument_id)
        if key in self._instr_cache:
            return self._instr_cache[key]
        try:
            data = self.get(f"ttpds/{self.env}/instrument/{instrument_id}")
            alias = data.get("instrument", [{}])[0].get("alias")
        except Exception:
            alias = None
        if alias:
            self._instr_cache[key] = alias
            self._save_cache(f"instruments_{self.env}.json", self._instr_cache)
        return alias

    # --- positions ---
    def positions(self) -> list[dict]:
        out, params = [], {}
        while True:
            data = self.get(f"ttmonitor/{self.env}/position", params)
            out.extend(data.get("positions", []))
            if data.get("lastPage") or not data.get("nextPageKey"):
                break
            params = {"pageKey": data["nextPageKey"]}
        return out

    def scan_positions(self, accounts: dict[int, str]) -> tuple[dict[tuple, float], set[str]]:
        """Returns ({(account, contract): netPosition} for NON-ZERO positions,
        {account names that appear in the position response at all}).
        An account in the second set is verifiable (TT reports its positions, even if flat)."""
        open_pos: dict[tuple, float] = {}
        reported: set[str] = set()
        for p in self.positions():
            name = accounts.get(p.get("accountId"))
            if not name:
                continue
            reported.add(name)
            net = float(p.get("netPosition") or 0.0)
            if abs(net) <= Config.FLAT_EPS:
                continue
            alias = self.instrument_alias(p.get("instrumentId"))
            if not alias:
                continue
            open_pos[(name, alias)] = open_pos.get((name, alias), 0.0) + net
        return open_pos, reported

    # --- fills (drill-down) ---
    def fills(self, account_id: int, start_ns: int, end_ns: int) -> list[dict]:
        """Paginated TT fills for an account in [start_ns, end_ns]. Caps at 500/call."""
        out, min_ts = [], start_ns
        while True:
            data = self.get(f"ttledger/{self.env}/fills", {
                "accountId": account_id, "minTimestamp": min_ts, "maxTimestamp": end_ns,
            })
            batch = data.get("fills", [])
            out.extend(batch)
            if len(batch) < 500:
                break
            min_ts = int(batch[-1]["timeStamp"]) + 1
        return out


def _clients() -> list[TTClient]:
    cs = []
    for env, secret_fn, uid_fn in ENVS:
        secret = secret_fn()
        if secret:
            cs.append(TTClient(env, secret, uid_fn()))
    return cs


# ---------------------------------------------------------------------------
# enrich: resolve verdicts for currently-open TT contracts
# ---------------------------------------------------------------------------

def enrich(state: dict) -> dict:
    """Mutates state['open_tt_contracts'] verdicts in place using live TT positions."""
    pending = state.get("open_tt_contracts", [])
    if not pending:
        state["tt_checked"] = True
        return state

    tt_open: dict[tuple, float] = {}
    tt_reported: set[str] = set()   # accounts whose positions TT actually reports (verifiable)
    errors = []
    for c in _clients():
        try:
            accounts = c.accounts_map()                  # accountId -> name (ALL accounts)
            open_pos, reported = c.scan_positions(accounts)
            tt_open.update(open_pos)
            tt_reported.update(reported)
        except Exception as e:  # degrade gracefully -> contracts stay unverifiable
            errors.append(f"{c.env}: {e}")

    if errors and not tt_reported:
        # could not reach TT at all -> leave everything unverifiable, surface the error
        for c in pending:
            c["verdict"] = "open_unverifiable"
            c["tt"] = {"checked": False, "error": "; ".join(errors)}
        state["tt_checked"] = False
        state["tt_error"] = "; ".join(errors)
        return state

    eps = Config.FLAT_EPS
    today = state["window"]["end_date"]
    for c in pending:
        our = c["current_net"]
        account, contract = c["account"], c["contract"]
        # If TT doesn't report this account's positions at all, we can't confirm flat -> unverifiable.
        if account not in tt_reported:
            c["verdict"] = "open_unverifiable"
            c["tt"] = {"checked": True, "in_tt": False}
            continue
        tt_net = tt_open.get((account, contract), 0.0)
        # Opened only today -> a TT-flat could just be ingestion lag, not a confirmed drop.
        same_day = c["switch_on"] == today
        c["tt"] = {"checked": True, "in_tt": True,
                   "tt_net": round(tt_net, 6), "our_net": our}
        if abs(tt_net) <= eps:                                   # TT shows flat
            if same_day:
                c["verdict"] = "open_unverifiable"; c["tt"]["recent"] = True
            else:
                c["verdict"] = "suspected_drop"                  # carried overnight, TT flat
        elif (tt_net > 0) == (our > 0):                          # same side -> confirmed
            c["verdict"] = "open_confirmed"
            if abs(tt_net) + eps < abs(our):                     # TT smaller: note the gap
                c["tt"]["discrepancy"] = round(our - tt_net, 4)
        else:                                                    # opposite sign
            if same_day:
                c["verdict"] = "open_unverifiable"; c["tt"]["recent"] = True
            else:
                c["verdict"] = "suspected_drop"; c["tt"]["mismatch"] = True
    state["tt_checked"] = True
    return state


# ---------------------------------------------------------------------------
# fills_diff: on-demand drill-down for one (account, contract)
# ---------------------------------------------------------------------------

OUR_FILLS_SQL = """
SELECT id, timestamp, price, quantity, side
FROM fills
WHERE account = %(account)s AND contract = %(contract)s
  AND timestamp >= %(start)s
ORDER BY timestamp
"""


def fills_diff(account: str, contract: str, days: int) -> dict:
    """Compare TT's fills against our DB fills for one (account, contract) over `days`.
    Returns TT fills missing from our DB (the likely dropped fills)."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    start_ns, end_ns = int(start.timestamp() * 1e9), int(end.timestamp() * 1e9)

    # Match by replicating ingestion's exact ns->datetime conversion, as a multiset so that a
    # genuine same-microsecond collision drop (TT has 2, we have 1) is correctly counted.
    def us_key(dt) -> int:
        return int(round(dt.timestamp() * 1e6))

    our = db.query(OUR_FILLS_SQL, {"account": account, "contract": contract, "start": start})
    our_counts: Counter = Counter()
    for f in our:
        our_counts[(us_key(f["timestamp"]), int(f["side"]), round(float(f["quantity"]), 4))] += 1

    missing, tt_total, env_used = [], 0, None
    for c in _clients():
        try:
            name_to_id = c.name_to_id()
            acct_id = name_to_id.get(account)
            if acct_id is None:
                continue
            tt_fills = c.fills(acct_id, start_ns, end_ns)
            env_used = c.env
            for tf in tt_fills:
                alias = c.instrument_alias(tf.get("instrumentId"))
                if alias != contract:
                    continue
                tt_total += 1
                dt = datetime.fromtimestamp(int(tf["timeStamp"]) / 1e9, tz=timezone.utc)
                side = int(tf.get("side"))
                qty = round(float(tf.get("lastQty")), 4)
                key = (us_key(dt), side, qty)
                if our_counts.get(key, 0) > 0:
                    our_counts[key] -= 1          # matched an existing DB fill
                else:
                    missing.append({
                        "timestamp": dt.isoformat(), "side": side, "qty": qty,
                        "price": tf.get("lastPx"),
                        "execId": tf.get("execId"),
                        "uniqueExecId": tf.get("uniqueExecId"),
                    })
            break  # found the env that owns this account
        except Exception as e:
            return {"account": account, "contract": contract, "error": str(e)}

    net_missing = sum((m["qty"] if m["side"] == 1 else -m["qty"]) for m in missing)
    return {
        "account": account, "contract": contract, "env": env_used,
        "days": days, "our_fills": len(our), "tt_fills": tt_total,
        "missing_count": len(missing), "net_missing": round(net_missing, 4),
        "missing": missing,
    }
