# CLAUDE.md

Working config for this repo. Read this before touching anything.

## What this repo is

An agentic platform reference implementation. Incident response is the
reference workload. Right now **only the victim stack exists** — the disposable
sandbox that gets deliberately broken so that agents can later be pointed at it.

## Scope boundary (read this twice)

`/victim-stack` is the **workload under observation**. It must contain:

- the fragile services, their failure injectors, and their verification
- nothing else

**Agent code, orchestration, LLM clients, prompt templates, MCP servers, tool
definitions, and workflow engines do not belong in `/victim-stack`.** They go in
`/platform`, which is empty on purpose. If you find yourself adding an LLM SDK
to `victim-stack/api/requirements.txt`, stop — you are in the wrong directory.

The victim stack must remain runnable, breakable, and diagnosable **with no
agent in the loop**. That property is what makes it a usable test fixture.

## Layout

```
victim-stack/          the sandbox
  docker-compose.yml   6 services + 1 on-demand rogue client
  common/obs.py        THE observability contract, in code
  api/                 FastAPI, 3 endpoints, hand-rolled 5-conn pool
  worker/              Redis-list consumer with a memory-leak flag
  leaker/              rogue client for the pool-exhaustion scenario
  db/init.sql          one table, 50 rows, no migrations framework
  prometheus/          5s scrape config
  toxiproxy/           cache proxy, no toxics at rest
  logs/                bind-mounted JSON log files (authoritative)
  tools/               stdlib-only host helpers (promql, logs, compose, load)
  inject.py            one subcommand per scenario + reset + status
  verify.py            proves each scenario has a distinct, visible signature
  tests/               unit tests for the host helpers

platform/              EMPTY. Agents and orchestration go here, later.
docs/
  observability-contract.md   contract surface — read before changing obs.py
  failure-scenarios.md        signatures + discrimination matrix
  adr/                        template only; ADRs are hand-authored
  decomposition-narrative.md  hand-authored stub, do not fill in
```

## Running it

```
make bootstrap                          # uv venv (3.11) for host tooling, once
make up                                 # build + start, waits for /health
make health                             # hit every endpoint once
make inject SCENARIO=pool-exhaustion    # pool-exhaustion|oom-crashloop|cache-latency|bad-config
make status                             # what is currently broken
make reset                              # back to healthy
make verify                             # ~10 min, proves the sandbox is not defective
make down                               # stop, keep volumes and logs
make destroy                            # stop, delete volumes, images and logs
```

Ports: api `8000`, prometheus `9090`, toxiproxy control `8474`, worker metrics
`9100`, postgres `5432`.

Host tooling runs on the repo venv (`.venv`, Python 3.11 via uv) and is
**stdlib-only** — no DB or Redis driver on the host. Anything needing a driver
runs in a container.

## Code conventions

- Python 3.11+. `ruff` with `line-length = 100`. `make check` before you claim
  something works.
- **The victim stack is throwaway software. Make it breakable and observable,
  not good.** No auth, no migrations framework, no repository/service layers,
  no dependency injection, no config framework. If a change makes the stack
  more robust, it is probably the wrong change.
- Dependencies are pinned exactly and kept minimal. Adding one needs a reason.
- Endpoints are sync `def` on purpose (FastAPI runs them in the threadpool) —
  that is what makes a 5-connection pool observably contended. Do not "fix"
  them into `async def`.
- Deliberate weaknesses carry a comment saying they are deliberate and why.
  `?hold=` on `GET /widgets/{widget_id}` is the main one.
- `docker compose` (v2 syntax), never `docker-compose`.
- Scenario state lives in `victim-stack/.env`, written by `inject.py`. Edit
  `inject.py`, never the `.env` by hand.

## Observability contract (summary)

Full text: `docs/observability-contract.md`. Implementation:
`victim-stack/common/obs.py`. **These names are a public API — tools and test
fixtures read them.** Append-only; renaming is breaking.

**Logs** — newline-delimited JSON at `victim-stack/logs/{api,worker}.log`
(files authoritative, stdout is a mirror). Required fields: `timestamp` (ISO
8601 UTC, ms, `Z`), `level`, `service`, `event`, `message`. Optional:
`request_id`, `duration_ms`, `error_type`, `error_detail`, `status_code`,
`job_id`. `message` is prose and is never parsed — if a tool needs it, it gets
its own field.

**Events** are a closed set asserted at write time in `obs.log()`. Adding one
means editing `obs.EVENTS` **and** the contract doc in the same commit.

**Metrics** — `db_connections_active`, `db_connections_max`,
`db_pool_timeouts_total`, `http_request_duration_seconds` (histogram, labelled
`endpoint`/`method`), `http_requests_total` (labelled `endpoint`/`method`/`status`),
`cache_hits_total`, `cache_misses_total`, `cache_errors_total`,
`worker_jobs_processed_total`, `worker_jobs_failed_total`, `worker_memory_bytes`,
`service_start_time_seconds`. Scrape interval 5s. The `endpoint` label is
always the route **template** — never a concrete path.

## Load-bearing details — do not "clean up"

These look like flaws. They are load-bearing; changing them silently breaks a
scenario signature.

| Detail | Why |
|---|---|
| `DB_POOL_MAX=5` | scenario 1 depends on a pool small enough to peg |
| `?hold=` query param on `/widgets/{id}` | the rogue client's only honest route to the pool |
| api → Redis **via Toxiproxy**, worker → Redis **direct** | keeps cache-latency scoped to the API |
| metrics registered **only** in their owning service | a worker exporting `db_connections_active=0` shadows the api's real value in unscoped queries |
| every PromQL query carries `{job="api"}` / `{job="worker"}` | same reason; the two services share a metric namespace |
| worker `WARMUP_SECONDS=6`, `HEARTBEAT_SECONDS=1` | restart gap must clear healthy heartbeat noise by a wide margin |
| worker `LEAK_MB_PER_TICK=8` | ~12s crash cycles, so a 75s soak yields several restarts, not two |
| worker leaks `b"x" * n`, not `bytearray(n)` | zeroed bytearrays can stay lazily mapped and never move RSS |
| `inject.py` starts the leaker with `--no-deps` | otherwise compose recreates the api and scenario 1 steals scenario 4's signature |
| cache signal is a hit **ratio**, not a hit rate | any slowdown cuts absolute hit rate; only a cache fault moves the ratio |
| restart signals use a wider window than rate signals | `changes()` needs the last pre-restart sample inside the range |
| cache errors degrade to db (200), not 503, on reads | keeps 5xx rate a *distinct* signal |
| bad-config uses a wrong **port**, not a wrong host | ECONNREFUSED is instant; DNS would hang and mimic scenario 1 |
| load generator is only 6 threads | stronger load would saturate the pool and make everything look like scenario 1 |

Everything in this table was either designed deliberately or learned from a
failed `make verify` run. See `LEARNINGS.md`.

## When you change the stack

1. Changing `obs.py` names → update `docs/observability-contract.md` in the
   same commit.
2. Changing anything in the "load-bearing" table → run `make verify` and expect
   to have to fix something.
3. Adding a scenario → add it to `inject.py`, to `EXPECTED` in `verify.py`, and
   to `docs/failure-scenarios.md`. A scenario without a row in the
   discrimination matrix does not exist.
4. Non-obvious findings and debugging scars go in `LEARNINGS.md`, dated.

## Not in this repo (yet, or ever)

No cloud, no Kubernetes, no TLS, no CI beyond lint + unit tests. No Temporal,
no MCP, no LLM client. Local Docker Compose only.
