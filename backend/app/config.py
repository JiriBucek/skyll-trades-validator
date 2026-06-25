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

    @classmethod
    def require_db(cls):
        if not cls.DB_DSN:
            raise RuntimeError(
                "PROD_DATABASE_CONNECTION_STRING_READONLY is empty. "
                "Launch via `secretctl run skyll-mwaa -- ...` and ensure the keychain is unlocked "
                "(`secretctl unlock`)."
            )
        return cls.DB_DSN
