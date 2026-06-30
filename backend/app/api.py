"""FastAPI app: read-only validation dashboard API.

  GET /api/overview?window=30&fix=1   -> full group/trader day-by-day tree + health header (cached)
  GET /api/findings?format=md         -> agent-readable findings (same picture, no UI) — see report.py
  GET /api/fills?account=&contract=   -> fill history + running position (the click-through detail)
  GET /api/raw-diff?account=&contract=-> on-demand FIX-feed diff (reingest-ready missing/extra fills)
  POST /api/refresh                   -> bust the cache
  GET /api/health
Serves the built frontend (frontend/dist) at / when present.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import engine, fixfeed, report
from .config import Config

app = FastAPI(title="Skyll Trades Validator", version="2.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

_cache: dict[tuple, tuple[float, dict]] = {}
_lock = threading.Lock()


def _overview(window: int, with_fix: bool) -> dict:
    key = (window, with_fix)
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < Config.CACHE_TTL:
            return hit[1]
        state = engine.compute_state(window)
        if with_fix:
            try:
                fixfeed.cross_check(state)
            except Exception as e:  # never let the FIX check break the overview
                state["fix_checked"] = False
                state["fix_error"] = str(e)
        tree = engine.assemble_tree(state)
        tree["fix_error"] = state.get("fix_error")
        tree["cached_at"] = now
        _cache[key] = (now, tree)
        return tree


@app.get("/api/overview")
def overview(
    window: int = Query(default=Config.WINDOW_DAYS, ge=1, le=120),
    fix: int = Query(default=1),
    tt: int = Query(default=None),   # back-compat alias for `fix`
    refresh: int = Query(default=0),
):
    with_fix = bool(fix if tt is None else tt)
    if refresh:
        with _lock:
            _cache.pop((window, with_fix), None)
    return _overview(window, with_fix)


@app.post("/api/refresh")
def refresh():
    with _lock:
        _cache.clear()
    return {"ok": True}


@app.get("/api/raw-diff")
def raw_diff(account: str = Query(...), contract: str = Query(...)):
    """The authoritative per-account FIX-feed diff: the exact fills missing from / extra in our DB,
    with uniqueExecId — reingest-ready. This is what makes a 🔴 actionable."""
    return fixfeed.account_diff(account, contract)


@app.get("/api/fills")
def fills_history(
    account: str = Query(...),
    contract: str = Query(...),
    limit: int = Query(default=5000, ge=1, le=20000),
):
    """Fill history for one (account, contract) with a chronological running position — the
    'what did he do' drill-down behind clicking a contract name."""
    return engine.fills_history(account, contract, limit)


@app.get("/api/findings")
def findings(
    window: int = Query(default=Config.WINDOW_DAYS, ge=1, le=120),
    category: str = Query(default=",".join(report.CATEGORIES)),
    group: str | None = Query(default=None),
    trader: str | None = Query(default=None),
    account: str | None = Query(default=None),
    min_net: float = Query(default=0.0),
    limit: int | None = Query(default=None),
    format: str = Query(default="json"),
    refresh: int = Query(default=0),
):
    """Agent-readable findings — the same picture the UI shows, as structured data. Reuses the cached
    overview tree. See backend/app/report.py for the categories + investigate hints."""
    if refresh:
        with _lock:
            _cache.pop((window, True), None)
    tree = _overview(window, True)
    rep = report.build_report(
        tree,
        categories=[c.strip() for c in category.split(",") if c.strip()],
        group=group, trader=trader, account=account, min_net=min_net, limit=limit,
    )
    if format == "md":
        return PlainTextResponse(report.render_md(rep))
    return rep


@app.get("/api/health")
def health():
    return {"ok": bool(Config.DB_DSN), "window_days": Config.WINDOW_DAYS}


# --- serve the built frontend if present ---
_dist = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
if _dist.exists():
    app.mount("/", StaticFiles(directory=str(_dist), html=True), name="frontend")
else:
    @app.get("/")
    def root():
        return JSONResponse({
            "service": "skyll-trades-validator",
            "note": "frontend not built; run `cd frontend && yarn build`, or use `yarn dev`.",
            "api": ["/api/overview", "/api/findings", "/api/fills", "/api/raw-diff", "/api/health"],
        })
