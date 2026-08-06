"""Redis-list consumer with a memory-leak flag.

The heartbeat (`worker.memory_sampled`, every HEARTBEAT_SECONDS) exists so that
a dead worker is distinguishable from an idle one: restart boundaries show up
as gaps in the heartbeat stream.
"""

import json
import os
import time

import redis
from prometheus_client import start_http_server

import obs

LEAK_MEMORY = os.environ.get("LEAK_MEMORY", "0") == "1"
LEAK_MB_PER_TICK = int(os.environ.get("LEAK_MB_PER_TICK", "4"))
WARMUP_SECONDS = float(os.environ.get("WARMUP_SECONDS", "3"))
HEARTBEAT_SECONDS = float(os.environ.get("HEARTBEAT_SECONDS", "2"))
JOB_QUEUE = "jobs"

# Module-level so the leak survives the loop body and is never collected.
# bytes multiplication actually writes every page, so RSS moves -- a plain
# bytearray(n) can stay lazily-zeroed and never show up in the gauge.
_leaked: list[bytes] = []


def process(job: dict) -> None:
    time.sleep(0.05)
    obs.WORKER_JOBS_PROCESSED.inc()
    obs.log(
        "job.processed",
        f"processed job {job.get('job_id')}",
        job_id=job.get("job_id"),
        duration_ms=50.0,
    )


def main() -> None:
    start_http_server(9100)
    obs.mark_started(
        {
            "redis_host": os.environ["REDIS_HOST"],
            "redis_port": os.environ["REDIS_PORT"],
            "leak_memory": LEAK_MEMORY,
            "leak_mb_per_tick": LEAK_MB_PER_TICK,
            "warmup_seconds": WARMUP_SECONDS,
        }
    )

    # Publish RSS before sleeping. A gauge is a level: it must never sit at a
    # fake 0 while the process is plainly using memory, or min_over_time reads
    # that 0 as the floor of the sawtooth.
    obs.WORKER_MEMORY_BYTES.set(obs.read_rss_bytes())

    # Deliberate warmup. Widens the log gap at restart boundaries so the
    # crashloop signature is unambiguous rather than sub-scrape-interval noise.
    # The heartbeat *log* deliberately does not start until after this sleep:
    # it reports liveness of the processing loop, which genuinely is not live
    # yet. The gauge above reports a level, which is true from the first
    # instant. Different kinds of signal, different rules.
    time.sleep(WARMUP_SECONDS)

    client = redis.Redis(
        host=os.environ["REDIS_HOST"],
        port=int(os.environ["REDIS_PORT"]),
        socket_timeout=5.0,
        decode_responses=True,
    )

    last_heartbeat = 0.0
    while True:
        try:
            item = client.brpop(JOB_QUEUE, timeout=1)
            if item is not None:
                process(json.loads(item[1]))
        except redis.exceptions.RedisError as exc:
            obs.WORKER_JOBS_FAILED.inc()
            obs.log(
                "job.failed",
                f"queue read failed: {exc}",
                level="ERROR",
                error_type=type(exc).__name__,
            )
            time.sleep(1)
        except Exception as exc:
            obs.WORKER_JOBS_FAILED.inc()
            obs.log(
                "job.failed",
                f"job raised: {exc}",
                level="ERROR",
                error_type=type(exc).__name__,
            )

        if LEAK_MEMORY:
            _leaked.append(b"x" * (LEAK_MB_PER_TICK * 1024 * 1024))

        now = time.monotonic()
        if now - last_heartbeat >= HEARTBEAT_SECONDS:
            last_heartbeat = now
            rss = obs.read_rss_bytes()
            obs.WORKER_MEMORY_BYTES.set(rss)
            obs.log("worker.memory_sampled", f"rss={rss}", level="DEBUG")


if __name__ == "__main__":
    main()
