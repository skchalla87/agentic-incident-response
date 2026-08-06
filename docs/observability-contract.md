# Observability Contract

This is a **contract**, not documentation of incidental output. Downstream
tooling and test fixtures read these field names, event names, and metric names
directly. Treat every string in this document as a public API.

Implementation lives in `victim-stack/common/obs.py`. Changing a name there
without changing this document is a bug.

## Stability rules

1. Field names, event names, and metric names are **append-only**. Renaming or
   removing one is a breaking change.
2. `message` is human prose. Tools must never parse it. Anything a tool needs
   gets its own field.
3. Every log line is exactly one JSON object on one line. No multi-line
   tracebacks — they go into `error_detail`, newlines stripped, truncated to 400
   characters.
4. The `event` vocabulary is **closed**. `obs.log()` asserts membership, so an
   undeclared event fails loudly at write time rather than silently polluting a
   fixture.
5. Metric label values use route **templates** (`/widgets/{widget_id}`), never
   concrete paths. Concrete paths would explode cardinality and break quantile
   queries.

---

## 1. Logs

**Location:** `/var/log/victim/{service}.log` inside containers, bind-mounted to
`victim-stack/logs/` on the host. Also mirrored to stdout for
`docker compose logs`; **the files are authoritative.**

**Format:** newline-delimited JSON (one object per line), UTF-8, compact
separators. No rotation — this is throwaway software; `make destroy` deletes
the files.

Uvicorn's own access log is disabled (`--no-access-log`) so the file contains
only contract records.

### 1.1 Fields

| Field | Type | Required | Notes |
|---|---|---|---|
| `timestamp` | string | yes | ISO 8601, UTC, millisecond precision, `Z` suffix — `2026-08-04T21:14:03.221Z` |
| `level` | string | yes | `DEBUG` \| `INFO` \| `WARN` \| `ERROR` \| `CRITICAL` |
| `service` | string | yes | `api` \| `worker` |
| `event` | string | yes | closed vocabulary, §1.2 |
| `message` | string | yes | human prose, never parsed |
| `request_id` | string | no | uuid4 hex; set on every record emitted inside an HTTP request scope |
| `duration_ms` | number | no | float milliseconds |
| `error_type` | string | no | exception class name — `PoolTimeout`, `ConnectionRefusedError`, `TimeoutError` |
| `error_detail` | string | no | single-line, truncated exception detail |
| `status_code` | integer | no | `http.request.completed` only |
| `job_id` | string | no | worker jobs and `job.enqueued` |

`request_id` propagates via a `contextvars.ContextVar`, so it reaches records
emitted from the threadpool (endpoints are sync `def`).

### 1.2 Event vocabulary

| Event | Service | Level | Emitted when |
|---|---|---|---|
| `service.started` | both | INFO | first line of a process lifetime |
| `service.config_loaded` | both | INFO | immediately after `service.started`; `message` carries resolved config as JSON |
| `http.request.started` | api | INFO | request entry |
| `http.request.completed` | api | INFO | response sent; carries `status_code`, `duration_ms` |
| `http.request.failed` | api | ERROR | unhandled exception escaped the handler |
| `db.query.completed` | api | INFO | carries `duration_ms` |
| `db.pool.acquired` | api | DEBUG | carries `duration_ms` = time spent waiting for a slot |
| `db.pool.timeout` | api | ERROR | **pool-exhaustion signal**; `error_type=PoolTimeout` |
| `db.connect.failed` | api | ERROR | **bad-config signal**; `error_type=OperationalError` |
| `cache.hit` | api | DEBUG | |
| `cache.miss` | api | DEBUG | |
| `cache.timeout` | api | ERROR | **cache-latency signal**; `error_type=TimeoutError` |
| `job.enqueued` | api | INFO | `POST /jobs` accepted |
| `job.processed` | worker | INFO | carries `job_id`, `duration_ms` |
| `job.failed` | worker | ERROR | carries `error_type` |
| `worker.memory_sampled` | worker | DEBUG | every `HEARTBEAT_SECONDS` (default 1s); `message` carries RSS |

**Why `worker.memory_sampled` exists:** without a periodic heartbeat, an idle
worker and a dead worker produce identical (empty) log output. The heartbeat
makes restart boundaries visible as *gaps* in a regular stream. The worker also
sleeps `WARMUP_SECONDS` (default 6s) before its loop, so a restart gap runs
~8–9s against a ~2s observed cadence — comfortably above noise, not a
sub-scrape-interval blip. Both values were tuned after measurement: at a 2s
cadence and a 3s warmup, restart gaps came out at 4.9s against a 3.0s healthy
baseline, which is too thin a margin to assert on.

---

## 2. Metrics

Exposed at `api:8000/metrics` and `worker:9100/metrics`. Prometheus scrapes both
every **5 seconds** (short on purpose: verification finishes in ~1 minute per
scenario, not ~10).

| Metric | Owner | Type | Labels | Meaning |
|---|---|---|---|---|
| `db_connections_active` | api | gauge | — | connections currently checked out of the api pool |
| `db_connections_max` | api | gauge | — | configured pool ceiling; constant `5` |
| `db_pool_timeouts_total` | api | counter | — | acquire attempts that exceeded `DB_POOL_ACQUIRE_TIMEOUT` |
| `http_request_duration_seconds` | api | histogram | `endpoint`, `method` | buckets: `.005 .01 .025 .05 .1 .25 .5 1 2.5 5 10 +Inf` |
| `http_requests_total` | api | counter | `endpoint`, `method`, `status` | `status` is the numeric code as a string |
| `cache_hits_total` | api | counter | — | lookups served from Redis |
| `cache_misses_total` | api | counter | — | lookups that fell through to the db |
| `cache_errors_total` | api | counter | `error_type` | cache errors. **Does not** increment hits or misses |
| `worker_jobs_processed_total` | worker | counter | — | resets to 0 on restart; the reset is itself a crashloop signal |
| `worker_jobs_failed_total` | worker | counter | — | |
| `worker_memory_bytes` | worker | gauge | — | RSS from `/proc/self/status`, sampled every 1s |
| `service_start_time_seconds` | both | gauge | `service` | unix epoch of process start — the **restart detector** |
| `process_cpu_seconds_total` | both | counter | (per target) | from `prometheus_client`'s default collector; backs "CPU stayed healthy" |
| `up` | — | gauge | `job` | Prometheus-generated; 0 while a target is down |

### 2.0 Ownership is enforced, and queries must be job-scoped

`obs.py` registers each metric **only in the service that owns it**. A worker
that exported `db_connections_active = 0` would be publishing a falsehood.

This matters because both services share a metric namespace in Prometheus. An
unscoped query like `db_connections_max` returns *two* series once a metric is
registered in both, and whichever comes back first wins. Every query in
`verify.py` therefore carries `{job="api"}` or `{job="worker"}`, and any tool
reading these metrics later must do the same.

This was not a hypothetical: the first verification run had the worker
shadowing the api's zeroes, which made `pool_pegged` fire for every scenario
including the healthy control, and made the cache-hit signal permanently dead.

`/metrics` is excluded from `http_requests_total` and
`http_request_duration_seconds` so scrapes don't contaminate request rates.

### 2.1 Why the three extra metrics exist

- **`db_pool_timeouts_total`** — makes pool exhaustion *countable*, not merely
  inferable from p99 latency.
- **`cache_errors_total`** — separates "cache said no" from "cache did not
  answer". Without it, a timeout would look like a miss.
- **`service_start_time_seconds`** — makes restart correlation a one-line
  PromQL query (`changes(...)`) instead of log archaeology. Both the
  oom-crashloop and bad-config signatures depend on it.

---

## 3. HTTP surface

| Method | Path | Route template used as `endpoint` label | Dependencies |
|---|---|---|---|
| `GET` | `/widgets/{widget_id}` | `/widgets/{widget_id}` | db only; holds a pool connection for `DB_QUERY_HOLD_SECONDS` (0.15s) via server-side `pg_sleep` |
| `GET` | `/widgets/{widget_id}/cached` | `/widgets/{widget_id}/cached` | cache first, db on miss or cache error |
| `POST` | `/jobs` | `/jobs` | `LPUSH jobs` to Redis, returns 202 |
| `GET` | `/health` | `/health` | none |
| `GET` | `/metrics` | (excluded) | none |

### 3.1 Deliberate footgun

`GET /widgets/{widget_id}?hold=<seconds>` holds the pooled connection open for
up to 30 seconds. Nothing sane ships this. It exists so the rogue client in the
pool-exhaustion scenario can pin connections through a real application code
path rather than by connecting to Postgres behind the app's back — which would
never move `db_connections_active`, since that gauge counts *pool checkouts*.

### 3.2 Deliberate wiring asymmetry

- `api` reaches Redis **through Toxiproxy** (`toxiproxy:26379`).
- `worker` reaches Redis **directly** (`cache:6379`).

This keeps the cache-latency scenario scoped to API paths. If the worker also
went through the proxy, its throughput would collapse too and cache-latency
would start to resemble oom-crashloop. The asymmetry is load-bearing.

### 3.3 Error semantics

Cache failures **degrade** (fall through to the db, still return 200) rather
than erroring, on read paths. This keeps 5xx rate meaningful as a *distinct*
signal rather than something every scenario trips. `POST /jobs` is the
exception: it has nowhere to fall back to, so a cache outage returns 503.

---

## 4. Contract self-check

`verify.py` validates the log files against this document on every run:
required fields present, declared levels only, `service` field matching the
file it was found in. `obs.log()` asserts the event vocabulary at write time.
Between them, a drifted contract fails a check rather than quietly corrupting
future fixtures.
