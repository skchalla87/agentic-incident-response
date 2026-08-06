# Learnings

Debugging scars and non-obvious decisions, newest first. One entry per thing
learned the hard way. If it cost more than ten minutes or contradicted an
assumption, it belongs here.

Not a changelog. Git already knows what changed; this records **what surprised
us** and **what we would have done differently knowing it**.

## Format

```
## YYYY-MM-DD — <short title>

**Context:** what we were doing.
**Surprise:** what actually happened, and what we expected instead.
**Cause:** the real mechanism, once understood.
**Consequence:** what changed in the code, config, or our mental model.
**Cost:** rough time lost, so we can tell cheap lessons from expensive ones.
```

Tags to use freely: `#observability` `#docker` `#scenario-design` `#tooling`

---

## 2026-08-04 — The test harness bypassed the CLI, so the CLI was broken the whole time `#tooling`

**Context:** running `inject.py status` by hand, after `make verify` had
already passed end to end.

**Surprise:** `IndexError` on startup. Every subcommand was affected —
`inject.py pool-exhaustion` had never once worked from the command line.

**Cause:** argparse help text was built from `handler.__doc__.strip().splitlines()[0]`,
and `status()` had no docstring, so the list was empty. `verify.py` calls
`inject.HANDLERS[name]()` directly, so the whole verification suite exercised
the handlers while never touching the argument parser.

**Consequence:** made the help extraction total, and added tests that build
both CLIs (`--help`) plus one asserting every scenario has a row in
`verify.EXPECTED`. Generalised lesson: **when a test harness calls the
implementation directly, the entry point is untested by construction.** Assume
the seam you skipped is broken.

**Cost:** ~5 min to fix, but it survived three full verification runs
undetected, which is the part worth remembering.

---

## 2026-08-04 — A shared metric namespace let the worker shadow the api `#observability`

**Context:** first full `make verify` run, immediately after the stack came up
clean and every endpoint worked by hand.

**Surprise:** 9 of 12 assertions failed. `pool_pegged` fired for *every*
scenario including the healthy control, and the cache-hit signal was
permanently dead at 0.

**Cause:** `obs.py` defined all metrics at module import into the default
registry, so the **worker also exported `db_connections_active`,
`db_connections_max`, `cache_hits_total`** and friends — all sitting at 0.
Prometheus then held two series per metric name. An unscoped query like
`db_connections_max` returns both, and the helper took the first one, which was
the worker's zero. `pool_peak >= pool_max` became `0 >= 0`.

**Consequence:** Metrics are now registered only in the service that owns them
(`if _IS_API:` / `if _IS_WORKER:` in `obs.py`), so touching a metric you don't
own is a loud `NameError`. Independently, every query in `verify.py` is scoped
by `{job=...}`. Both, not either — the ownership split is the honest fix, the
job scoping is the one that survives someone adding a metric to both services
later.

**Cost:** ~30 min, most of it spent doubting the Prometheus scrape config
before actually printing the label sets.

---

## 2026-08-04 — `changes()` misses a restart that happens at the window edge `#observability`

**Context:** the `bad-config` scenario recreates the api container. Its whole
signature rests on `changes(service_start_time_seconds{service="api"})` ≥ 1.
It observed 0, while the same query run by hand against the same stack returned 1.

**Surprise:** the query was correct; the *window* was wrong.

**Cause:** measurement used a range of exactly the soak duration, starting at
the moment of injection. Prometheus scrapes every 5s, so if the last scrape of
the old process landed just before injection, the range vector contained only
the new value — and a series with one distinct value has zero changes.

**Consequence:** restart signals now use `soak + 20s` while rate and quantile
signals keep the tight soak window. Also added a 25s settle period after
`reset()` so that reset's own recreates stay outside every window. Generalised
lesson: **an edge-triggered signal needs a window that provably contains both
sides of the edge.**

**Cost:** ~20 min.

---

## 2026-08-04 — A gauge that publishes 0 before it has a value poisons min_over_time `#observability`

**Context:** last remaining verification failure. `worker_restarts` fired,
`worker_log_gap` fired, the worker was visibly crashlooping — but
`worker_mem_sawtooth` reported a max/min ratio of exactly 0.

**Surprise:** the sawtooth was plainly there in the raw series.

**Cause:** the worker sleeps `WARMUP_SECONDS` before entering its loop, and the
loop is where `worker_memory_bytes` was first set. For those seconds the gauge
sat at its default 0 and Prometheus faithfully recorded it. `min_over_time`
found the 0, and the ratio guard turned that into a 0.

**Consequence:** the worker now sets the gauge *before* the warmup sleep. The
heartbeat *log* still starts only after warmup, and that asymmetry is
deliberate: a gauge reports a **level** and is true from the first instant; the
heartbeat reports **liveness of the processing loop**, which genuinely isn't
live yet. Conflating the two is what caused the bug. `verify.py` additionally
filters `> 0` before taking the minimum.

**Cost:** ~15 min, plus a 12-minute verification cycle to confirm.

---

## 2026-08-04 — Absolute rates make every slow scenario look like every other one `#scenario-design`

**Context:** designing the cache signal for `cache-latency`.

**Surprise:** `rate(cache_hits_total)` collapsing is not evidence that the
cache broke.

**Cause:** pool exhaustion blocks the load generator's threads for 2s per db
request, cutting total throughput roughly fivefold. Cache hits per second fall
with it, even though the cache is perfectly healthy — so a rate-based threshold
would have fired for scenario 1 and collided with scenario 3.

**Consequence:** the signal is now the hit *ratio*,
`hits / (hits + misses + errors)`, which is throughput-independent. Rule of
thumb adopted: **a signal meant to indict one dependency must be normalised
against traffic volume**, or it indicts whatever is slowest.

**Cost:** caught at design time, not in production — but only because the
verification harness forced the question of "would this fire for the *other*
scenarios too".

---

## 2026-08-04 — Repo scaffolded

**Context:** initial build of the victim stack.
**Surprise:** —
**Cause:** —
**Consequence:** Baseline established. Scenarios, contract, and verification
harness in place; `make verify` is the definition of "the sandbox works".
**Cost:** —
