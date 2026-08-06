# agentic-incident-response

An agentic platform reference implementation, using incident response as the
reference workload.

**Current state: victim stack only.** This is the disposable sandbox that gets
deliberately broken so that agents can later be pointed at it and asked to
diagnose it. There is no agent, no orchestration, and no LLM code in this repo
yet — `/platform` is empty on purpose.

## Quick start

Requires Docker Compose v2 and [uv](https://docs.astral.sh/uv/).

```bash
make bootstrap    # one-time: 3.11 venv for host tooling
make up           # build and start the stack
make health       # sanity-check every endpoint
```

Then break something:

```bash
make inject SCENARIO=pool-exhaustion
make status
make reset
```

And prove the sandbox actually works:

```bash
make verify       # ~10 min
```

Tear down with `make down` (keeps volumes) or `make destroy` (deletes
everything, including log files).

## What's running

| Service | Port | Role |
|---|---|---|
| `api` | 8000 | FastAPI. Three endpoints; a deliberately tiny 5-connection db pool |
| `db` | 5432 | Postgres, one seeded table |
| `cache` | — | Redis |
| `toxiproxy` | 8474 | Sits in front of the cache; latency injection |
| `worker` | 9100 | Redis-list consumer with a memory-leak flag, 128m limit |
| `prometheus` | 9090 | Scrapes api + worker every 5s |
| `leaker` | — | Rogue client, started on demand by `inject.py` |

## Failure scenarios

| Scenario | Unique discriminator |
|---|---|
| `pool-exhaustion` | `db_connections_active` pegs at max, CPU stays flat |
| `oom-crashloop` | worker restarts repeatedly, RSS sawtooths, log gaps |
| `cache-latency` | p99 on the cached endpoint spikes, cache hit rate collapses |
| `bad-config` | 5xx step change at a restart, with latency going **down** |

Each is engineered to produce telemetry no other scenario produces.
`make verify` asserts both that each signature appears and that no two
scenarios collide. Details: [docs/failure-scenarios.md](docs/failure-scenarios.md).

## The observability contract

The log fields and metric names are a **contract**, not incidental output —
downstream tooling and test fixtures read them directly. Read
[docs/observability-contract.md](docs/observability-contract.md) before changing
anything in `victim-stack/common/obs.py`.

## Documentation

- [CLAUDE.md](CLAUDE.md) — working config, conventions, scope boundaries
- [docs/observability-contract.md](docs/observability-contract.md) — the contract
- [docs/failure-scenarios.md](docs/failure-scenarios.md) — signatures and matrix
- [docs/decomposition-narrative.md](docs/decomposition-narrative.md) — hand-authored
- [docs/adr/](docs/adr/) — decision records
- [LEARNINGS.md](LEARNINGS.md) — debugging scars

## A warning about the code

The victim stack is throwaway software and is deliberately bad: no auth, no
migrations, no layering, a 5-connection pool, and an endpoint that lets a caller
pin a database connection for 30 seconds. None of that is an oversight. See the
"load-bearing details" table in [CLAUDE.md](CLAUDE.md) before improving
anything.
