"""Read-only database access. Every connection is forced read-only as a hard guard."""
import time
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

from .config import Config

_pool: ThreadedConnectionPool | None = None


def _get_pool() -> ThreadedConnectionPool:
    global _pool
    if _pool is None:
        dsn = Config.require_db()
        _pool = ThreadedConnectionPool(minconn=1, maxconn=5, dsn=dsn)
    return _pool


@contextmanager
def cursor():
    """Yield a read-only dict cursor. The transaction is forced READ ONLY and rolled back on exit."""
    pool = _get_pool()
    conn = pool.getconn()
    try:
        # Hard read-only guard: even if the DB role had write rights, this blocks writes.
        conn.set_session(readonly=True, autocommit=False)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur
        conn.rollback()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def query(sql: str, params: dict | None = None, retries: int = 3) -> list[dict]:
    # The read-only DSN points at a hot standby; a long aggregate can be cancelled when WAL replay
    # needs to remove rows it is reading (SerializationFailure "conflict with recovery"). Retry a
    # few times with backoff before surfacing it — the heavy net/raw scans are otherwise solid.
    for attempt in range(retries):
        try:
            with cursor() as cur:
                cur.execute(sql, params or {})
                return [dict(r) for r in cur.fetchall()]
        except (psycopg2.errors.SerializationFailure, psycopg2.OperationalError) as e:
            if attempt < retries - 1 and "recovery" in str(e).lower():
                time.sleep(2 * (attempt + 1))
                continue
            raise
