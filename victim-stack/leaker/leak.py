"""Rogue client for the pool-exhaustion scenario.

Holds CONCURRENCY long-running requests open against the api. Each one pins a
db pool connection for HOLD_SECONDS, so with CONCURRENCY > DB_POOL_MAX the pool
never drains and legitimate traffic times out on acquire.

Stdlib only -- this container has no reason to know what a database driver is.
"""

import os
import threading
import time
import urllib.error
import urllib.request

TARGET_URL = os.environ.get("TARGET_URL", "http://api:8000/widgets/1")
CONCURRENCY = int(os.environ.get("CONCURRENCY", "6"))
HOLD_SECONDS = float(os.environ.get("HOLD_SECONDS", "10"))

_stop = threading.Event()


def hog(index: int) -> None:
    url = f"{TARGET_URL}?hold={HOLD_SECONDS}"
    while not _stop.is_set():
        try:
            with urllib.request.urlopen(url, timeout=HOLD_SECONDS + 15) as response:
                response.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            print(f"[leaker {index}] {type(exc).__name__}: {exc}", flush=True)
            time.sleep(0.5)


def main() -> None:
    print(
        f"[leaker] {CONCURRENCY} threads holding {HOLD_SECONDS}s requests against {TARGET_URL}",
        flush=True,
    )
    threads = [threading.Thread(target=hog, args=(i,), daemon=True) for i in range(CONCURRENCY)]
    for thread in threads:
        thread.start()
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
