#!/usr/bin/env python3
"""Prove the sandbox is not defective.

For each scenario: reset, generate steady load, take a baseline, inject, soak,
then assert that the expected telemetry signature is *actually visible* -- and
that the other scenarios' signatures are *actually absent*.

The last check is the important one. If two scenarios produce the same signal
vector, the sandbox cannot teach an agent to tell them apart, and this script
exits non-zero saying so.

Usage:
    ./verify.py                       # control run + all four scenarios
    ./verify.py --scenario cache-latency
    ./verify.py --soak 90 --baseline 30
"""

import argparse
import sys
import time

import inject
from tools import compose, logs, promql

DB_ENDPOINT = "/widgets/{widget_id}"
CACHED_ENDPOINT = "/widgets/{widget_id}/cached"

# Restart signals look at soak + this many seconds, so the last scrape of the
# pre-restart process is inside the range vector. See observe().
RESTART_WINDOW_PAD = 20

# Quiet period after reset, before the baseline starts. Keeps the restarts that
# reset() itself causes outside every measurement window.
SETTLE_SECONDS = 25

# Signal name -> (description, threshold-bearing evaluator)
# Every evaluator returns a (bool, observed_value) pair.
SIGNAL_ORDER = [
    "pool_pegged",
    "pool_timeouts",
    "p99_db_high",
    "p99_cached_high",
    "cache_hits_collapsed",
    "cache_errors",
    "worker_restarts",
    "api_restarted",
    "worker_mem_sawtooth",
    "worker_log_gap",
    "error_rate_step",
    "api_cpu_flat",
]

# None == don't care. See docs/failure-scenarios.md for the prose version.
EXPECTED = {
    "healthy": {
        "pool_pegged": False,
        "pool_timeouts": False,
        "p99_db_high": False,
        "p99_cached_high": False,
        "cache_hits_collapsed": False,
        "cache_errors": False,
        "worker_restarts": False,
        "api_restarted": False,
        "worker_mem_sawtooth": False,
        "worker_log_gap": False,
        "error_rate_step": False,
        "api_cpu_flat": True,
    },
    "pool-exhaustion": {
        "pool_pegged": True,
        "pool_timeouts": True,
        "p99_db_high": True,
        "p99_cached_high": None,  # cached path can inherit pool waits on a miss
        "cache_hits_collapsed": False,
        "cache_errors": False,
        "worker_restarts": False,
        "api_restarted": False,
        "worker_mem_sawtooth": False,
        "worker_log_gap": False,
        "error_rate_step": True,
        "api_cpu_flat": True,
    },
    "oom-crashloop": {
        "pool_pegged": False,
        "pool_timeouts": False,
        "p99_db_high": False,
        "p99_cached_high": False,
        "cache_hits_collapsed": False,
        "cache_errors": False,
        "worker_restarts": True,
        "api_restarted": False,
        "worker_mem_sawtooth": True,
        "worker_log_gap": True,
        "error_rate_step": False,
        "api_cpu_flat": True,
    },
    "cache-latency": {
        "pool_pegged": False,
        "pool_timeouts": False,
        "p99_db_high": False,
        "p99_cached_high": True,
        "cache_hits_collapsed": True,
        "cache_errors": True,
        "worker_restarts": False,
        "api_restarted": False,
        "worker_mem_sawtooth": False,
        "worker_log_gap": False,
        "error_rate_step": None,  # enqueue also rides the proxied client
        "api_cpu_flat": True,
    },
    "bad-config": {
        "pool_pegged": False,
        "pool_timeouts": False,
        "p99_db_high": False,  # ECONNREFUSED is fast: errors up, latency DOWN
        "p99_cached_high": False,
        "cache_hits_collapsed": False,
        "cache_errors": False,
        "worker_restarts": False,
        "api_restarted": True,
        "worker_mem_sawtooth": False,
        "worker_log_gap": False,
        "error_rate_step": True,
        "api_cpu_flat": True,
    },
}


def observe(window: int, baseline: dict, since: float) -> dict[str, tuple[bool, float]]:
    """Compute every signal over the last `window` seconds.

    Every query is scoped by job. The api and worker share a metric namespace,
    and an unscoped query would silently pick whichever series Prometheus
    returned first.

    Restart signals use a wider window than rate signals: a container recreated
    at t0 may have had its last pre-restart scrape just before t0, and
    changes() needs both the old and the new value inside the range.
    """
    w = f"{window}s"
    wr = f"{window + RESTART_WINDOW_PAD}s"
    out: dict[str, tuple[bool, float]] = {}

    pool_peak = promql.scalar(f'max_over_time(db_connections_active{{job="api"}}[{w}])')
    pool_max = promql.scalar('db_connections_max{job="api"}', default=0.0)
    out["pool_pegged"] = (pool_max > 0 and pool_peak >= pool_max, pool_peak)

    timeouts = promql.scalar(f'increase(db_pool_timeouts_total{{job="api"}}[{w}])')
    out["pool_timeouts"] = (timeouts > 0.5, timeouts)

    p99_db = promql.scalar(
        "histogram_quantile(0.99, sum by (le) (rate(http_request_duration_seconds_bucket"
        f'{{job="api",endpoint="{DB_ENDPOINT}"}}[{w}])))'
    )
    out["p99_db_high"] = (p99_db > 1.0, round(p99_db, 3))

    p99_cached = promql.scalar(
        "histogram_quantile(0.99, sum by (le) (rate(http_request_duration_seconds_bucket"
        f'{{job="api",endpoint="{CACHED_ENDPOINT}"}}[{w}])))'
    )
    out["p99_cached_high"] = (p99_cached > 0.75, round(p99_cached, 3))

    # Hit RATIO, not hit rate. Any scenario that slows the api down also drops
    # absolute cache throughput, so a rate-based signal would fire for pool
    # exhaustion too. The ratio only moves when the cache itself stops serving.
    ratio = promql.scalar(f"""
        sum(rate(cache_hits_total{{job="api"}}[{w}]))
        /
        clamp_min(
            sum(rate(cache_hits_total{{job="api"}}[{w}]))
          + sum(rate(cache_misses_total{{job="api"}}[{w}]))
          + sum(rate(cache_errors_total{{job="api"}}[{w}])), 0.001)
    """)
    lookups = promql.scalar(f"""
        sum(rate(cache_hits_total{{job="api"}}[{w}]))
      + sum(rate(cache_misses_total{{job="api"}}[{w}]))
      + sum(rate(cache_errors_total{{job="api"}}[{w}]))
    """)
    out["cache_hits_collapsed"] = (lookups > 0.2 and ratio < 0.5, round(ratio, 2))

    cache_errors = promql.scalar(f'sum(increase(cache_errors_total{{job="api"}}[{w}]))')
    out["cache_errors"] = (cache_errors > 0.5, round(cache_errors, 1))

    worker_changes = promql.scalar(
        f'changes(service_start_time_seconds{{job="worker",service="worker"}}[{wr}])'
    )
    out["worker_restarts"] = (worker_changes >= 2, worker_changes)

    api_changes = promql.scalar(
        f'changes(service_start_time_seconds{{job="api",service="api"}}[{wr}])'
    )
    out["api_restarted"] = (api_changes >= 1, api_changes)

    # The `> 0` filter is belt-and-braces: the worker sets this gauge before it
    # does anything else, but a scrape landing in the first few milliseconds of
    # a process would otherwise put a zero under min_over_time and flatten the
    # sawtooth to nothing.
    mem_max = promql.scalar(f'max_over_time(worker_memory_bytes{{job="worker"}}[{w}])')
    mem_min = promql.scalar(
        f'min_over_time((worker_memory_bytes{{job="worker"}} > 0)[{w}:5s])', default=1.0
    )
    ratio = mem_max / mem_min if mem_min > 0 else 0.0
    out["worker_mem_sawtooth"] = (ratio > 1.5, round(ratio, 2))

    worker_records = logs.read("worker", since=since)
    gap = logs.max_gap_seconds(worker_records, "worker.memory_sampled")
    out["worker_log_gap"] = (gap > 5.0, round(gap, 1))

    errors = promql.scalar(f'sum(increase(http_requests_total{{job="api",status=~"5.."}}[{w}]))')
    out["error_rate_step"] = (errors > 5.0, round(errors, 1))

    cpu = promql.scalar(f'rate(process_cpu_seconds_total{{job="api"}}[{w}])')
    out["api_cpu_flat"] = (cpu < 0.5, round(cpu, 3))

    return out


def check_contract() -> list[str]:
    """The log files must obey docs/observability-contract.md. Cheap, so always run."""
    problems = []
    for service in ("api", "worker"):
        records = logs.read(service)
        if not records:
            problems.append(f"{service}.log has no parseable records")
            continue
        for record in records[:5000]:
            if record["service"] != service:
                problems.append(f"{service}.log: wrong service field {record['service']!r}")
                break
            if record["level"] not in {"DEBUG", "INFO", "WARN", "ERROR", "CRITICAL"}:
                problems.append(f"{service}.log: undeclared level {record['level']!r}")
                break
    return problems


def run_scenario(
    name: str, baseline_seconds: int, soak_seconds: int
) -> dict[str, tuple[bool, float]]:
    from tools.load import LoadGenerator

    print(f"\n=== {name} ===")
    inject.reset()

    generator = LoadGenerator().start()
    try:
        print(f"  settling: {SETTLE_SECONDS}s (keeps reset's own restarts out of the window)")
        time.sleep(SETTLE_SECONDS)
        print(f"  baseline: {baseline_seconds}s")
        time.sleep(baseline_seconds)
        baseline = {
            "cache_hit_rate": promql.scalar('sum(rate(cache_hits_total{job="api"}[30s]))')
        }
        print(f"  baseline cache hit rate: {baseline['cache_hit_rate']:.2f}/s")

        since = time.time()
        if name != "healthy":
            inject.HANDLERS[name]()
        print(f"  soaking: {soak_seconds}s")
        time.sleep(soak_seconds)

        observed = observe(soak_seconds, baseline, since)
    finally:
        generator.stop()

    return observed


def print_matrix(results: dict[str, dict[str, tuple[bool, float]]]) -> None:
    names = list(results)
    width = max(len(s) for s in SIGNAL_ORDER) + 2
    header = "signal".ljust(width) + "".join(n[:16].ljust(18) for n in names)
    print("\n" + header)
    print("-" * len(header))
    for signal in SIGNAL_ORDER:
        row = signal.ljust(width)
        for name in names:
            fired, value = results[name][signal]
            row += (("YES " if fired else "no  ") + f"({value})").ljust(18)
        print(row)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--scenario", choices=["healthy", *inject.SCENARIOS], action="append")
    parser.add_argument("--baseline", type=int, default=25, help="baseline seconds per scenario")
    parser.add_argument("--soak", type=int, default=75, help="soak seconds after injection")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(line_buffering=True)  # progress must survive redirection

    compose.wait_for_api()
    scenarios = args.scenario or ["healthy", *inject.SCENARIOS]

    results: dict[str, dict[str, tuple[bool, float]]] = {}
    for name in scenarios:
        results[name] = run_scenario(name, args.baseline, args.soak)

    print("\nrestoring healthy state")
    inject.reset()

    print_matrix(results)

    failures: list[str] = []

    for name, observed in results.items():
        for signal, expectation in EXPECTED[name].items():
            if expectation is None:
                continue
            fired, value = observed[signal]
            if fired != expectation:
                verb = "expected but missing" if expectation else "unexpected"
                failures.append(f"{name}: {signal} {verb} (observed {value})")

    # Distinguishability: no two scenarios may present the same signal vector.
    vectors = {
        name: tuple(observed[s][0] for s in SIGNAL_ORDER) for name, observed in results.items()
    }
    names = list(vectors)
    for i, left in enumerate(names):
        for right in names[i + 1 :]:
            if vectors[left] == vectors[right]:
                failures.append(
                    f"COLLISION: {left} and {right} produced identical telemetry signatures"
                )

    contract_problems = check_contract()
    failures.extend(f"contract: {problem}" for problem in contract_problems)

    print()
    if failures:
        print(f"FAIL ({len(failures)} problem(s)):")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print(f"PASS: {len(results)} scenario(s), all signatures distinct and as documented")
    return 0


if __name__ == "__main__":
    sys.exit(main())
