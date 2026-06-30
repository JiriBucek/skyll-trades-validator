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

    # --- known spread / curve books (curated) ---
    # (account, product-symbol) pairs a trader runs as calendar spreads / butterflies across many
    # maturities. A per-(account, contract) net != 0 here is EXPECTED — it's a spread LEG, not a
    # lost fill or a genuine directional open. These are LABELLED 'spread', faded in the UI, and
    # EXCLUDED from the health counts / drop rollup / trader-worst, so a known spreader's legs never
    # read as a problem. (You still see the faint cells if you expand the trader.) Symbol = the first
    # token of the contract ("I Sep26" -> "I", "SO3 Dec26" -> "SO3").
    # Membership is PROVEN by an actual spread instrument trading in the TT ledger (e.g.
    # "I Sep26-Dec26 Calendar", "I Sep26 3mo Butterfly") — NOT merely "many maturities" or "closes
    # to zero" (an outright trader who flattens each maturity also nets to zero). Verify a candidate
    # with: the TT pull's spread-instrument contracts for that (account, product).
    # Identified 2026-06-30. RIGOROUS bar (after the CRA + FGBS false positives):
    #   "TT-calendar" = an INTRA-product term-structure instrument (Calendar/Butterfly) traded with
    #       real volume (>=8 fills) in the TT ledger. NOT inter-product/crack (those touch two
    #       products and shouldn't brand an outright energy book).
    #   "open-legs"   = 3+ NON-EXPIRED maturities held open and offsetting to ~0 (the held-curve
    #       signature) — for Stellar/give-up accounts with no TT instrument feed.
    # EXPIRED maturities are ignored (the FGBS Sep24/Dec24 trap — ancient offsetting residuals are
    # NOT a held spread). "Closes to zero" is NOT a spread (an outright trader who flattens also nets
    # to zero). Jake Nippers LFCTEU109 "I" is LEFT OFF — it has a genuine +200 watermark drop.
    SPREAD_PRODUCTS = {
        # Louis Binns (LCE30102) — Euribor/SONIA calendars (112/126 fills) + SOFR curve
        ("LCE30102", "I"), ("LCE30102", "SO3"), ("LCE30102", "SR3"),
        # Jake Nippers (LFCTEU109) — crude/brent/SONIA calendars (TT, 58/72/21 fills). "I" left off.
        ("LFCTEU109", "BRN"), ("LFCTEU109", "CL"), ("LFCTEU109", "SO3"),
        # Alberto Lopez (LCE30316) — gasoil/brent calendars (TT, 31/17 fills)
        ("LCE30316", "BRN"), ("LCE30316", "G"),
        # Jamie Brewster (LCE30309_CFE) — VIX calendars (TT, 26 fills)
        ("LCE30309_CFE", "VXM"),
        # Jay Vowell (LCE30178, LJ4AX005) — STIR curve (5–6 open offsetting legs)
        ("LCE30178", "SO3"), ("LCE30178", "SR3"), ("LCE30178", "ZQ"), ("LJ4AX005", "I"),
        # Luke Farrier (LCE30251) — STIR curve (open legs)
        ("LCE30251", "I"), ("LCE30251", "SR3"),
        # Steve Hunter (LCE30124) — Euribor curve (open legs)
        ("LCE30124", "I"),
        # Ryan Cohen (LJ4AX008, give-up) — SONIA curve (open legs)
        ("LJ4AX008", "SO3"),
    }

    @classmethod
    def require_db(cls):
        if not cls.DB_DSN:
            raise RuntimeError(
                "PROD_DATABASE_CONNECTION_STRING_READONLY is empty. "
                "Launch via `secretctl run skyll-mwaa -- ...` and ensure the keychain is unlocked "
                "(`secretctl unlock`)."
            )
        return cls.DB_DSN
