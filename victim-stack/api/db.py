"""A hand-rolled 5-connection pool.

Hand-rolled on purpose: the pool is the instrument for scenario 1, so its
checkout accounting has to be visible, not buried in a library.
"""

import os
import threading
import time
from collections import deque

import psycopg

import obs


class PoolTimeout(Exception):
    """Raised when a caller waits longer than DB_POOL_ACQUIRE_TIMEOUT for a slot."""


class ConnectionPool:
    def __init__(self, dsn: str, maxsize: int, acquire_timeout: float) -> None:
        self._dsn = dsn
        self._maxsize = maxsize
        self._acquire_timeout = acquire_timeout
        self._slots = threading.Semaphore(maxsize)
        self._idle: deque = deque()
        self._lock = threading.Lock()
        self._active = 0
        obs.DB_CONNECTIONS_MAX.set(maxsize)
        obs.DB_CONNECTIONS_ACTIVE.set(0)

    def _set_active(self, delta: int) -> None:
        with self._lock:
            self._active += delta
            obs.DB_CONNECTIONS_ACTIVE.set(self._active)

    def acquire(self) -> psycopg.Connection:
        started = time.monotonic()
        if not self._slots.acquire(timeout=self._acquire_timeout):
            obs.DB_POOL_TIMEOUTS.inc()
            obs.log(
                "db.pool.timeout",
                f"no pool slot after {self._acquire_timeout}s",
                level="ERROR",
                error_type="PoolTimeout",
                duration_ms=round((time.monotonic() - started) * 1000, 2),
            )
            raise PoolTimeout(f"pool exhausted after {self._acquire_timeout}s")

        try:
            with self._lock:
                conn = self._idle.popleft() if self._idle else None
            if conn is None:
                conn = psycopg.connect(self._dsn, connect_timeout=2)
        except Exception as exc:
            self._slots.release()
            obs.log(
                "db.connect.failed",
                f"could not open a connection: {exc}",
                level="ERROR",
                error_type=type(exc).__name__,
                error_detail=str(exc).replace("\n", " ")[:400],
            )
            raise

        self._set_active(1)
        obs.log(
            "db.pool.acquired",
            "checked out a pool connection",
            level="DEBUG",
            duration_ms=round((time.monotonic() - started) * 1000, 2),
        )
        return conn

    def release(self, conn: psycopg.Connection | None, broken: bool = False) -> None:
        self._set_active(-1)
        if conn is not None:
            if broken or conn.closed:
                try:
                    conn.close()
                except Exception:
                    pass
            else:
                with self._lock:
                    self._idle.append(conn)
        self._slots.release()


def build_pool() -> ConnectionPool:
    dsn = (
        f"host={os.environ['DB_HOST']} port={os.environ['DB_PORT']} "
        f"dbname={os.environ['DB_NAME']} user={os.environ['DB_USER']} "
        f"password={os.environ['DB_PASSWORD']}"
    )
    return ConnectionPool(
        dsn=dsn,
        maxsize=int(os.environ.get("DB_POOL_MAX", "5")),
        acquire_timeout=float(os.environ.get("DB_POOL_ACQUIRE_TIMEOUT", "2.0")),
    )
