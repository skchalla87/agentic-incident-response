#!/usr/bin/env python3
"""Failure injection for the victim stack.

One subcommand per scenario, plus `reset` (return to healthy) and `status`.
Each scenario is designed to produce a signature that no other scenario
produces -- see docs/failure-scenarios.md for the discrimination matrix, and
verify.py for the machine-checkable version of it.

Usage:
    ./inject.py pool-exhaustion
    ./inject.py reset
"""

import argparse
import sys

from tools import compose

SCENARIOS = ("pool-exhaustion", "oom-crashloop", "cache-latency", "bad-config")


def inject_pool_exhaustion() -> None:
    """Rogue client pins every db pool connection.

    Signature: db_connections_active pegs at db_connections_max, db.pool.timeout
    events appear in api.log, api CPU stays flat, nothing restarts.
    """
    print("injecting pool-exhaustion: starting rogue client")
    # --no-deps is load-bearing: leaker depends_on api, and without it compose
    # recreates the api container, which would fire the api-restart signal that
    # is supposed to belong to bad-config alone.
    compose.compose("--profile", "inject", "up", "-d", "--no-deps", "leaker")


def inject_oom_crashloop() -> None:
    """Worker leaks memory until the 128m cgroup limit kills it, forever.

    Signature: worker_memory_bytes sawtooth, service_start_time_seconds{worker}
    changes repeatedly, worker.log heartbeat gaps at restart boundaries.
    """
    print("injecting oom-crashloop: enabling LEAK_MEMORY on worker")
    compose.set_env(WORKER_LEAK_MEMORY="1")
    compose.compose("up", "-d", "--force-recreate", "worker")


def inject_cache_latency() -> None:
    """Toxiproxy delays every cache response by 500ms-5s.

    Signature: p99 on /widgets/{id}/cached spikes, cache_hits_total flatlines,
    cache_errors_total climbs, db pool stays healthy.
    """
    print("injecting cache-latency: adding 500ms-5s latency toxic to cache proxy")
    compose.add_cache_latency(latency_ms=2750, jitter_ms=2250)


def inject_bad_config() -> None:
    """Api points at a port nothing listens on, then restarts.

    A wrong port (not a wrong hostname) on purpose: ECONNREFUSED is instant, so
    the signature is errors-up-with-latency-DOWN. A bogus hostname would hang on
    DNS and look like pool exhaustion.
    """
    print("injecting bad-config: pointing api at db:5433 and restarting")
    compose.set_env(API_DB_PORT="5433")
    compose.compose("up", "-d", "--force-recreate", "api")


def reset() -> None:
    """Return the stack to healthy. Safe to run at any time, including twice."""
    print("reset: stopping rogue client")
    compose.compose("--profile", "inject", "rm", "-sf", "leaker", check=False)

    print("reset: removing cache toxics")
    try:
        compose.remove_cache_latency()
    except Exception as exc:  # toxiproxy may be down; not fatal for a reset
        print(f"  (toxiproxy unreachable: {exc})")

    print("reset: restoring healthy env and recreating api + worker")
    compose.set_env(**compose.HEALTHY_ENV)
    compose.compose("up", "-d", "--force-recreate", "api", "worker")
    compose.wait_for_api()
    print("reset: api healthy")


def status() -> None:
    """Report what is currently injected, and whether the api is answering."""
    env = compose.read_env()
    toxics = compose.active_toxics()
    running = compose.compose("ps", "--services", "--filter", "status=running", capture=True)
    services = sorted(filter(None, (running.stdout or "").splitlines()))
    print(f"env          : {env}")
    print(f"cache toxics : {[t.get('name') for t in toxics] or 'none'}")
    print(f"running      : {', '.join(services) or 'nothing'}")
    print(f"api healthy  : {compose.api_healthy()}")


HANDLERS = {
    "pool-exhaustion": inject_pool_exhaustion,
    "oom-crashloop": inject_oom_crashloop,
    "cache-latency": inject_cache_latency,
    "bad-config": inject_bad_config,
    "reset": reset,
    "status": status,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, handler in HANDLERS.items():
        summary = ((handler.__doc__ or "").strip().splitlines() or [name])[0]
        subparsers.add_parser(name, help=summary)

    args = parser.parse_args(argv)
    HANDLERS[args.command]()
    return 0


if __name__ == "__main__":
    sys.exit(main())
