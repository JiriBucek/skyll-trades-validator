"""Configuration + tunables. All secrets come from the environment (secretctl run skyll-mwaa)."""
import os


class Config:
    # --- database (READ-ONLY only; the _WRITE string is intentionally never read) ---
    DB_DSN = os.environ.get("PROD_DATABASE_CONNECTION_STRING_READONLY", "")

    # --- TT REST API ---
    TT_URL_BASE = "https://ttrestapi.trade.tt"
    TT_ENV_LIVE = "ext_prod_live"
    TT_ENV_SIM = "ext_prod_sim"
    TT_APP_SECRET = os.environ.get("APP_SECRET", "")        # live trading creds
    TT_SIM_APP_SECRET = os.environ.get("SIM_APP_SECRET", "")  # simulation creds
    # non-secret TT user ids (plaintext in the mwaa .env); needed for the accounts list
    TT_PROD_USER_ID = os.environ.get("TT_PROD_USER_ID", "1095487")
    TT_SIM_USER_ID = os.environ.get("TT_SIM_USER_ID", "1157838")
    REQUEST_ID_BASE = "Trade-Axia"

    # --- validation window / behaviour ---
    WINDOW_DAYS = int(os.environ.get("VALIDATOR_WINDOW_DAYS", "30"))
    # net positions smaller than this are treated as flat (float fill quantities)
    FLAT_EPS = 1e-9
    # daily-candle vs realized reconciliation tolerance (absolute, base currency)
    RECON_TOLERANCE = float(os.environ.get("VALIDATOR_RECON_TOLERANCE", "1.0"))
    # cache TTL for the heavy overview computation (seconds)
    CACHE_TTL = int(os.environ.get("VALIDATOR_CACHE_TTL", "300"))

    # --- FIX-feed cross-check (raw_fills_fix) ---
    # Both feeds (I_TT, I_STELLAR) start at this retention wall; positions opened before it have no
    # recoverable opening (verified live 2026-06-29). Override only if the feed history extends.
    FIX_RETENTION_START = os.environ.get("FIX_RETENTION_START", "2026-03-30")
    # net comparison tolerance (lots) — a net within this of the FIX feed counts as reconciled
    FIX_NET_TOL = float(os.environ.get("VALIDATOR_FIX_NET_TOL", "0.5"))
    # IgnoredAccounts catch-all trader id + the unmapped/orphan trader id (stranding detector)
    STRANDED_TRADER_IDS = (0, int(os.environ.get("IGNORED_ACCOUNTS_TRADER_ID", "349")))

    # --- day-by-day model (v3) ---
    # A position open at END OF DAY for this many TRAILING days is a sustained open we surface as a
    # line of EOD-net numbers (not just a fresh overnight). <= this many trailing opens is "fine".
    PROBLEM_OPEN_DAYS = int(os.environ.get("VALIDATOR_PROBLEM_OPEN_DAYS", "3"))
    # for a position carried into the window (open since before day 1), look back this far to find
    # when the current open run actually started — so the "open N days" note shows the TRUE age (e.g.
    # 45d), not the 30-day window cap. Older than this -> shown as "N+ d".
    OPEN_LOOKBACK_DAYS = int(os.environ.get("VALIDATOR_OPEN_LOOKBACK_DAYS", "365"))
    # per-day GROSS-volume (Σ|qty|) tolerance (lots) when comparing fills vs raw_fills_fix for a
    # problem row. Quantities are integer lots, so 0.5 absorbs only float noise — a real missing
    # fill exceeds it and paints the day red.
    GROSS_TOL = float(os.environ.get("VALIDATOR_GROSS_TOL", "0.5"))

    # smaller side of an opposing-leg book must be >= this fraction of the larger side to count as a
    # spread — stops a directional book with a 1-lot residual in another month (e.g. -227 vs +1)
    # from being labelled a spread. Set to 0 for pure opposing-signs (any imbalance counts).
    SPREAD_MIN_BALANCE = float(os.environ.get("VALIDATOR_SPREAD_MIN_BALANCE", "0.15"))

    # --- spread / curve books ---
    # Spreads are now DETECTED from the position data (engine.detect_spread_keys), NOT hand-curated:
    # a (canonical account, product-symbol) whose OPEN, NON-EXPIRED maturities hold OPPOSING net
    # signs (net long one month, net short another — e.g. James Pitron FGBM +50 / -50) is a calendar
    # spread. Its legs carry net != 0 by design, so they're faded and EXCLUDED from the aggregated
    # trader/group timeline + health counts (shown only as individual rows when you expand).
    # The old hand-curated list was retired (it mislabelled several books — see git history).
    #
    # This set is an optional MANUAL OVERRIDE: add (account, "SYM") pairs to force-label a book the
    # position data can't reveal (e.g. a give-up account whose offsetting leg clears off-platform).
    # It unions with the detected set. Symbol = first token of the contract ("I Sep26" -> "I").
    SPREAD_PRODUCTS: set = set()

    @classmethod
    def require_db(cls):
        if not cls.DB_DSN:
            raise RuntimeError(
                "PROD_DATABASE_CONNECTION_STRING_READONLY is empty. "
                "Launch via `secretctl run skyll-mwaa -- ...` and ensure the keychain is unlocked "
                "(`secretctl unlock`)."
            )
        return cls.DB_DSN
