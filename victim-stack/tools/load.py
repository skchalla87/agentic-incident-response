"""Modest, steady background traffic.

Concurrency is deliberately low (default 6). If the load generator alone could
saturate a 5-connection pool, every scenario would look like pool exhaustion
and the discrimination matrix would be worthless.
"""

import random
import threading
import time
import urllib.error
import urllib.request

from tools.compose import API_URL


class LoadGenerator:
    def __init__(self, concurrency: int = 6, pause: float = 0.4, timeout: float = 10.0) -> None:
        self.concurrency = concurrency
        self.pause = pause
        self.timeout = timeout
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.counts = {"ok": 0, "error": 0}
        self._lock = threading.Lock()

    def _record(self, key: str) -> None:
        with self._lock:
            self.counts[key] += 1

    def _hit(self, method: str, path: str) -> None:
        request = urllib.request.Request(f"{API_URL}{path}", method=method)
        if method == "POST":
            request.data = b"{}"
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                response.read()
            self._record("ok")
        except Exception:
            self._record("error")

    def _worker(self) -> None:
        while not self._stop.is_set():
            widget_id = random.randint(1, 20)
            roll = random.random()
            if roll < 0.4:
                self._hit("GET", f"/widgets/{widget_id}")
            elif roll < 0.85:
                self._hit("GET", f"/widgets/{widget_id}/cached")
            else:
                self._hit("POST", "/jobs")
            time.sleep(self.pause)

    def start(self) -> "LoadGenerator":
        self._threads = [
            threading.Thread(target=self._worker, daemon=True) for _ in range(self.concurrency)
        ]
        for thread in self._threads:
            thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=15)

    def __enter__(self) -> "LoadGenerator":
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Generate background traffic against the api.")
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--concurrency", type=int, default=6)
    args = parser.parse_args()

    generator = LoadGenerator(concurrency=args.concurrency)
    print(f"load: {args.concurrency} threads for {args.seconds}s against {API_URL}")
    with generator:
        time.sleep(args.seconds)
    print(f"load: {generator.counts}")


if __name__ == "__main__":
    main()
