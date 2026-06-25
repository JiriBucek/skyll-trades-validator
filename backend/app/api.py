"""FastAPI app: read-only validation dashboard API.

  GET /api/overview?window=30&tt=1   -> full group/trader heatmap tree (cached)
  GET /api/tt-diff?account=&contract=&days=30 -> on-demand TT fills-vs-DB diff
  POST /api/refresh                  -> bust the cache
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

from . import engine, report, tt
from .config import Config

app = FastAPI(title="Skyll Trades Validator", version="1.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

_cache: dict[tuple, tuple[float, dict]] = {}
_lock = threading.Lock()


def _overview(window: int, with_tt: bool) -> dict:
    key = (window, with_tt)
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < Config.CACHE_TTL:
            return hit[1]
        state = engine.compute_state(window)
        if with_tt:
            try:
                tt.enrich(state)
            except Exception as e:  # never let TT failure break the overview
                state["tt_checked"] = False
                state["tt_error"] = str(e)
        tree = engine.assemble_tree(state)
        tree["tt_error"] = state.get("tt_error")
        tree["cached_at"] = now
        _cache[key] = (now, tree)
        return tree


@app.get("/api/overview")
def overview(
    window: int = Query(default=Config.WINDOW_DAYS, ge=1, le=120),
    tt: int = Query(default=1),
    refresh: int = Query(default=0),
):
    if refresh:
        with _lock:
            _cache.pop((window, bool(tt)), None)
    return _overview(window, bool(tt))


@app.post("/api/refresh")
def refresh():
    with _lock:
        _cache.clear()
    return {"ok": True}


@app.get("/api/tt-diff")
def tt_diff(
    account: str = Query(...),
    contract: str = Query(...),
    days: int = Query(default=Config.WINDOW_DAYS, ge=1, le=180),
):
    return tt.fills_diff(account, contract, days)


@app.get("/api/findings")
def findings(
    window: int = Query(default=Config.WINDOW_DAYS, ge=1, le=120),
    severity: str = Query(default=",".join(report.PROBLEM_SEVERITIES)),
    min_net: float = Query(default=0.0),
    group: str | None = Query(default=None),
    trader: str | None = Query(default=None),
    limit: int | None = Query(default=None),
    format: str = Query(default="json"),
    refresh: int = Query(default=0),
):
    """Agent-readable flat list of problem findings + investigation pointers.
    Reuses the cached overview computation."""
    if refresh:
        with _lock:
            _cache.pop((window, True), None)
    tree = _overview(window, True)
    rep = report.build_report(
        tree, None,
        severities=[s.strip() for s in severity.split(",") if s.strip()],
        min_net=min_net, group=group, trader=trader, limit=limit,
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
            "api": ["/api/overview", "/api/tt-diff", "/api/health"],
        })
