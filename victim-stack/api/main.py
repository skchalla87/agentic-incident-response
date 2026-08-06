"""Three endpoints, one deliberate footgun, no auth, no layers.

Endpoints are sync `def` so FastAPI runs them in the threadpool -- that is what
makes a 5-connection pool observably contended.
"""

import json
import os
import time
import uuid

import redis
from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

import obs
from db import PoolTimeout, build_pool

QUERY_HOLD = float(os.environ.get("DB_QUERY_HOLD_SECONDS", "0.15"))
CACHE_TTL = int(os.environ.get("CACHE_TTL_SECONDS", "30"))
REDIS_TIMEOUT = float(os.environ.get("REDIS_TIMEOUT", "1.0"))
JOB_QUEUE = "jobs"

app = FastAPI(title="victim-api", docs_url=None, redoc_url=None)
pool = build_pool()
cache = redis.Redis(
    host=os.environ["REDIS_HOST"],
    port=int(os.environ["REDIS_PORT"]),
    socket_timeout=REDIS_TIMEOUT,
    socket_connect_timeout=REDIS_TIMEOUT,
    decode_responses=True,
)

obs.mark_started(
    {
        "db_host": os.environ["DB_HOST"],
        "db_port": os.environ["DB_PORT"],
        "db_pool_max": os.environ.get("DB_POOL_MAX"),
        "redis_host": os.environ["REDIS_HOST"],
        "redis_port": os.environ["REDIS_PORT"],
        "query_hold_seconds": QUERY_HOLD,
    }
)


@app.middleware("http")
async def observe_request(request: Request, call_next):
    request_id = uuid.uuid4().hex
    token = obs.request_id_var.set(request_id)
    started = time.monotonic()
    obs.log("http.request.started", f"{request.method} {request.url.path}")
    try:
        response = await call_next(request)
        status = response.status_code
    except Exception as exc:  # pragma: no cover - safety net, not a code path we plan
        status = 500
        obs.log(
            "http.request.failed",
            f"unhandled error on {request.url.path}: {exc}",
            level="ERROR",
            error_type=type(exc).__name__,
        )
        response = JSONResponse({"error": "internal"}, status_code=500)

    duration = time.monotonic() - started
    route = request.scope.get("route")
    endpoint = getattr(route, "path", request.url.path)
    if endpoint != "/metrics":
        obs.HTTP_REQUEST_DURATION.labels(endpoint=endpoint, method=request.method).observe(
            duration
        )
        obs.HTTP_REQUESTS.labels(
            endpoint=endpoint, method=request.method, status=str(status)
        ).inc()
    obs.log(
        "http.request.completed",
        f"{request.method} {endpoint} -> {status}",
        status_code=status,
        duration_ms=round(duration * 1000, 2),
    )
    response.headers["x-request-id"] = request_id
    obs.request_id_var.reset(token)
    return response


def _fetch_widget(widget_id: int, hold_seconds: float) -> dict | None:
    conn = None
    broken = False
    started = time.monotonic()
    try:
        conn = pool.acquire()
        with conn.cursor() as cur:
            # Server-side sleep: the connection is genuinely held, not just the thread.
            cur.execute("SELECT pg_sleep(%s)", (hold_seconds,))
            cur.execute("SELECT id, name, price FROM widgets WHERE id = %s", (widget_id,))
            row = cur.fetchone()
        conn.commit()
    except Exception:
        broken = True
        raise
    finally:
        if conn is not None:
            pool.release(conn, broken=broken)

    obs.log(
        "db.query.completed",
        f"widget lookup id={widget_id}",
        duration_ms=round((time.monotonic() - started) * 1000, 2),
    )
    if row is None:
        return None
    return {"id": row[0], "name": row[1], "price": float(row[2])}


@app.get("/widgets/{widget_id}")
def read_widget(
    widget_id: int,
    # Deliberate footgun. The rogue client in the pool-exhaustion scenario uses
    # this to pin connections. Nothing sane would ship this.
    hold: float | None = Query(default=None, ge=0, le=30),
) -> Response:
    hold_seconds = QUERY_HOLD if hold is None else hold
    try:
        widget = _fetch_widget(widget_id, hold_seconds)
    except PoolTimeout:
        return JSONResponse({"error": "db pool exhausted"}, status_code=503)
    except Exception as exc:
        return JSONResponse(
            {"error": "db unavailable", "error_type": type(exc).__name__}, status_code=503
        )
    if widget is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(widget)


@app.get("/widgets/{widget_id}/cached")
def read_widget_cached(widget_id: int) -> Response:
    key = f"widget:{widget_id}"
    try:
        cached = cache.get(key)
        if cached is not None:
            obs.CACHE_HITS.inc()
            obs.log("cache.hit", f"cache hit for {key}", level="DEBUG")
            return JSONResponse(json.loads(cached))
        obs.CACHE_MISSES.inc()
        obs.log("cache.miss", f"cache miss for {key}", level="DEBUG")
    except redis.exceptions.RedisError as exc:
        obs.CACHE_ERRORS.labels(error_type=type(exc).__name__).inc()
        obs.log(
            "cache.timeout",
            f"cache unreachable for {key}: {exc}",
            level="ERROR",
            error_type=type(exc).__name__,
        )
        # Fall through to the db so latency degrades without a 5xx step change.

    try:
        widget = _fetch_widget(widget_id, QUERY_HOLD)
    except PoolTimeout:
        return JSONResponse({"error": "db pool exhausted"}, status_code=503)
    except Exception as exc:
        return JSONResponse(
            {"error": "db unavailable", "error_type": type(exc).__name__}, status_code=503
        )
    if widget is None:
        return JSONResponse({"error": "not found"}, status_code=404)

    try:
        cache.setex(key, CACHE_TTL, json.dumps(widget))
    except redis.exceptions.RedisError as exc:
        obs.CACHE_ERRORS.labels(error_type=type(exc).__name__).inc()
        obs.log(
            "cache.timeout",
            f"cache write failed for {key}: {exc}",
            level="ERROR",
            error_type=type(exc).__name__,
        )
    return JSONResponse(widget)


@app.post("/jobs")
def enqueue_job(payload: dict | None = None) -> Response:
    job_id = uuid.uuid4().hex
    job = {"job_id": job_id, "payload": payload or {}}
    try:
        # Shares the proxied client on purpose: a cache outage should be visible
        # on the write path too, not just on reads.
        cache.lpush(JOB_QUEUE, json.dumps(job))
    except redis.exceptions.RedisError as exc:
        obs.CACHE_ERRORS.labels(error_type=type(exc).__name__).inc()
        obs.log(
            "cache.timeout",
            f"enqueue failed: {exc}",
            level="ERROR",
            error_type=type(exc).__name__,
        )
        return JSONResponse({"error": "queue unavailable"}, status_code=503)
    obs.log("job.enqueued", f"queued job {job_id}", job_id=job_id)
    return JSONResponse({"job_id": job_id}, status_code=202)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "api"}


@app.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
