"""The observability contract, in code.

Anything in here is a contract surface: field names, event names, and metric
names are read by tooling and used as test fixtures. Changing a string in this
file is a contract change -- update docs/observability-contract.md in the same
commit.
"""

import json
import os
import sys
import threading
import time
from contextvars import ContextVar
from datetime import UTC, datetime

from prometheus_client import Counter, Gauge, Histogram

SERVICE = os.environ.get("SERVICE_NAME", "unknown")
LOG_DIR = os.environ.get("LOG_DIR", "/var/log/victim")

# ---------------------------------------------------------------------------
# Event vocabulary (closed set -- see docs/observability-contract.md)
# ---------------------------------------------------------------------------
EVENTS = frozenset(
    {
        "service.started",
        "service.config_loaded",
        "http.request.started",
        "http.request.completed",
        "http.request.failed",
        "db.query.completed",
        "db.pool.acquired",
        "db.pool.timeout",
        "db.connect.failed",
        "cache.hit",
        "cache.miss",
        "cache.timeout",
        "job.enqueued",
        "job.processed",
        "job.failed",
        "worker.memory_sampled",
    }
)

LEVELS = frozenset({"DEBUG", "INFO", "WARN", "ERROR", "CRITICAL"})

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

_log_lock = threading.Lock()
_log_file = None


def _open_log() -> object:
    global _log_file
    if _log_file is None:
        os.makedirs(LOG_DIR, exist_ok=True)
        path = os.path.join(LOG_DIR, f"{SERVICE}.log")
        _log_file = open(path, "a", buffering=1, encoding="utf-8")
    return _log_file


def _now() -> str:
    """ISO 8601, UTC, millisecond precision, Z suffix."""
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def log(event: str, message: str, level: str = "INFO", **fields: object) -> None:
    """Emit one JSON object per line to the log file (authoritative) and stdout."""
    assert event in EVENTS, f"undeclared event {event!r}; add it to the contract first"
    assert level in LEVELS, f"undeclared level {level!r}"

    record: dict[str, object] = {
        "timestamp": _now(),
        "level": level,
        "service": SERVICE,
        "event": event,
        "message": message,
    }
    rid = request_id_var.get()
    if rid is not None:
        record["request_id"] = rid
    for key, value in fields.items():
        if value is not None:
            record[key] = value

    line = json.dumps(record, separators=(",", ":"), default=str)
    with _log_lock:
        handle = _open_log()
        handle.write(line + "\n")
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


# ---------------------------------------------------------------------------
# Metrics (stable names -- contract surface)
#
# Registered per-owning-service, NOT unconditionally. A worker that exported
# db_connections_active=0 would be publishing a falsehood, and any query that
# forgot a job filter would silently read the worker's zero instead of the
# api's real value. Touching a metric you do not own is a NameError, loudly.
# ---------------------------------------------------------------------------
_IS_API = SERVICE == "api"
_IS_WORKER = SERVICE == "worker"

# Owned by both: every service has a start time.
SERVICE_START_TIME = Gauge(
    "service_start_time_seconds", "Unix epoch at which this process started", ["service"]
)

if _IS_API:
    DB_CONNECTIONS_ACTIVE = Gauge(
        "db_connections_active", "Connections currently checked out of the api pool"
    )
    DB_CONNECTIONS_MAX = Gauge("db_connections_max", "Configured pool ceiling")
    DB_POOL_TIMEOUTS = Counter(
        "db_pool_timeouts_total", "Pool acquire attempts that exceeded the wait deadline"
    )

    HTTP_REQUEST_DURATION = Histogram(
        "http_request_duration_seconds",
        "End-to-end HTTP request latency",
        ["endpoint", "method"],
        buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    )
    HTTP_REQUESTS = Counter(
        "http_requests_total", "HTTP responses served", ["endpoint", "method", "status"]
    )

    CACHE_HITS = Counter("cache_hits_total", "Cache lookups served from Redis")
    CACHE_MISSES = Counter("cache_misses_total", "Cache lookups that fell through to the db")
    CACHE_ERRORS = Counter("cache_errors_total", "Cache lookups that errored", ["error_type"])

if _IS_WORKER:
    WORKER_JOBS_PROCESSED = Counter("worker_jobs_processed_total", "Jobs consumed successfully")
    WORKER_JOBS_FAILED = Counter("worker_jobs_failed_total", "Jobs that raised")
    WORKER_MEMORY_BYTES = Gauge("worker_memory_bytes", "Worker RSS in bytes")


def mark_started(config: dict[str, object]) -> None:
    """Stamp start time and emit the two startup events every service owes."""
    SERVICE_START_TIME.labels(service=SERVICE).set(time.time())
    log("service.started", f"{SERVICE} process started")
    log(
        "service.config_loaded",
        "resolved config: " + json.dumps(config, sort_keys=True, default=str),
    )


def read_rss_bytes() -> int:
    """RSS from /proc. Linux-only; the stack only ever runs in containers."""
    with open("/proc/self/status", encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    return 0
