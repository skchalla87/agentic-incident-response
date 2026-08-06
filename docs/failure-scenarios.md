# Failure Scenarios and Their Signatures

Four scenarios. Each must produce a signal vector that **no other scenario
produces**. If two ever collide, the sandbox cannot teach anything to tell them
apart, and `verify.py` fails the run with `COLLISION`.

Inject with `make inject SCENARIO=<name>`, return to healthy with `make reset`.

---

## The signals

Twelve booleans, computed in `verify.py::observe()` from Prometheus and the log
files. Thresholds in parentheses.

All metric queries are scoped by `job` — the api and worker share a namespace,
and an unscoped query can read the wrong service's series.

| Signal | Source | Fires when |
|---|---|---|
| `pool_pegged` | metric | `max_over_time(db_connections_active)` ≥ `db_connections_max` (and max > 0) |
| `pool_timeouts` | metric | `increase(db_pool_timeouts_total)` > 0 |
| `p99_db_high` | metric | p99 of `/widgets/{widget_id}` > 1.0s |
| `p99_cached_high` | metric | p99 of `/widgets/{widget_id}/cached` > 0.75s |
| `cache_hits_collapsed` | metric | cache hit **ratio** < 0.5, given a non-trivial lookup rate |
| `cache_errors` | metric | `increase(cache_errors_total)` > 0 |
| `worker_restarts` | metric | `changes(service_start_time_seconds{service="worker"})` ≥ 2 |
| `api_restarted` | metric | `changes(service_start_time_seconds{service="api"})` ≥ 1 |
| `worker_mem_sawtooth` | metric | max/min of `worker_memory_bytes` > 1.5 |
| `worker_log_gap` | **log** | largest gap between `worker.memory_sampled` records > 5s |
| `error_rate_step` | metric | `increase(http_requests_total{status=~"5.."})` > 5 |
| `api_cpu_flat` | metric | `rate(process_cpu_seconds_total{job="api"})` < 0.5 |

### Two thresholds that are subtler than they look

**`cache_hits_collapsed` is a ratio, not a rate.** Hits per second fall under
*any* scenario that slows the api down — pool exhaustion cuts overall
throughput roughly fivefold, which would drag absolute cache hit rate below a
rate-based threshold and make scenario 1 masquerade as scenario 3. The ratio
`hits / (hits + misses + errors)` only moves when the cache itself stops
serving, which is what the signal is supposed to mean.

**Restart signals use a wider window than rate signals** (soak + 20s). A
container recreated at `t0` may have had its final pre-restart scrape just
before `t0`; if the range vector starts exactly at `t0` it contains only the
new value and `changes()` returns 0. This cost one failed verification run
before it was understood.

---

## The discrimination matrix

`YES` = must fire. `no` = must not fire. `·` = don't care (not asserted).
`healthy` is a control run with no injection — it must fire nothing.

| Signal | healthy | pool-exhaustion | oom-crashloop | cache-latency | bad-config |
|---|:--:|:--:|:--:|:--:|:--:|
| `pool_pegged` | no | **YES** | no | no | no |
| `pool_timeouts` | no | **YES** | no | no | no |
| `p99_db_high` | no | **YES** | no | no | no |
| `p99_cached_high` | no | · | no | **YES** | no |
| `cache_hits_collapsed` | no | no | no | **YES** | no |
| `cache_errors` | no | no | no | **YES** | no |
| `worker_restarts` | no | no | **YES** | no | no |
| `api_restarted` | no | no | no | no | **YES** |
| `worker_mem_sawtooth` | no | no | **YES** | no | no |
| `worker_log_gap` | no | no | **YES** | no | no |
| `error_rate_step` | no | **YES** | no | · | **YES** |
| `api_cpu_flat` | YES | YES | YES | YES | YES |

Each scenario owns at least one signal that fires for it alone:

| Scenario | Unique discriminator |
|---|---|
| pool-exhaustion | `pool_pegged` |
| oom-crashloop | `worker_restarts` |
| cache-latency | `cache_hits_collapsed` |
| bad-config | `api_restarted` |

---

## Scenario detail

### 1. `pool-exhaustion`

**Mechanism.** A rogue client container (`leaker`) opens `CONCURRENCY=6`
concurrent requests to `GET /widgets/1?hold=10`. It is started with
`--no-deps`: `leaker` declares `depends_on: api`, and without that flag compose
recreates the api container, which would fire `api_restarted` and hand
scenario 1 scenario 4's signature. Each pins a pooled connection
for 10 seconds. With a pool ceiling of 5, the pool never drains; legitimate
traffic waits `DB_POOL_ACQUIRE_TIMEOUT=2s` and then gets `PoolTimeout` → 503.

**Signature.** `db_connections_active` sits at 5. `db_pool_timeouts_total`
climbs. `db.pool.timeout` records appear in `api.log`. **CPU stays flat** —
this is a *waiting* failure, not a *working* failure, and that is the cheapest
thing that separates it from a genuine load problem.

**Why the rogue client goes through the API.** A rogue client connecting
directly to Postgres would exhaust Postgres' `max_connections` but would never
move `db_connections_active`, which counts *pool checkouts*. The stated
signature would simply not appear. See `docs/observability-contract.md` §3.1.

### 2. `oom-crashloop`

**Mechanism.** `WORKER_LEAK_MEMORY=1` makes the worker append 8 MB per loop tick
to a module-level list. The container has `mem_limit: 128m` and
`restart: unless-stopped`, so the cgroup OOM killer reaps it roughly every
~12 seconds, forever. (At 4 MB/tick the cycle was ~25s and a 75s soak produced
only two restarts — not enough separation from a single incidental restart.)

**Signature.** `worker_memory_bytes` sawtooths. `service_start_time_seconds{worker}`
changes repeatedly. `worker.log` shows heartbeat gaps at restart boundaries.
`worker_jobs_processed_total` resets to zero each cycle. The API is untouched.

**Note.** The leak allocates `b"x" * n`, not `bytearray(n)`. A zeroed bytearray
can stay lazily mapped and never appear in RSS — the gauge would flatline while
the container still got OOM-killed, which is exactly the kind of half-visible
failure this sandbox must not have.

### 3. `cache-latency`

**Mechanism.** A Toxiproxy `latency` toxic (2750ms ± 2250ms jitter → 500ms–5s)
on the downstream of the `cache` proxy. The API's Redis client has a 1.0s
socket timeout, so roughly the top ~90% of delays become timeouts.

**Signature.** p99 on `/widgets/{widget_id}/cached` jumps to ~1.2s (1s timeout
plus a db fallback). `cache_hits_total` flatlines while `cache_errors_total`
climbs. `cache.timeout` records appear in `api.log`. The db pool stays healthy —
the fallback traffic is well under the pool ceiling at normal load levels.

**Boundary with pool-exhaustion.** Both make requests slow. They separate on
*which* endpoint degrades and on `db_connections_active`. The load generator is
deliberately low-concurrency (6 threads, 0.4s pause) so it can never saturate a
5-connection pool on its own — if it could, every scenario would look like
scenario 1.

### 4. `bad-config`

**Mechanism.** `API_DB_PORT=5433` in `.env`, then `docker compose up -d
--force-recreate api`. Nothing listens on 5433, so every db call gets an
instant `ECONNREFUSED`.

**Signature.** A 5xx step change that begins exactly at a restart —
`changes(service_start_time_seconds{service="api"})` = 1 — with
`db.connect.failed` in `api.log`. Crucially **latency goes DOWN**, because
failing fast is fast.

**Why a wrong port, not a wrong hostname.** A bogus hostname hangs in DNS
resolution, which would make bad-config look like a slow-dependency failure —
i.e. like pool-exhaustion. A wrong port on a reachable host gets an immediate
TCP reset, which produces the errors-up-latency-down shape that nothing else in
this matrix produces.

---

## Verifying

```
make verify                              # control + 4 scenarios, ~10 min
make verify ARGS="--scenario cache-latency"
make verify ARGS="--soak 90 --baseline 30"
```

`verify.py` resets, baselines, injects, soaks, and asserts **both** directions
of every non-`·` cell — expected signals present *and* unexpected signals
absent. It then compares the full observed signal vectors pairwise and fails on
any two that are identical. Finally it re-checks the log files against the
observability contract.

A `FAIL` here means the sandbox is defective. Fix the stack, not the assertion.
