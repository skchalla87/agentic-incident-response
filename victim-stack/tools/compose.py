"""docker compose wrappers plus the .env scenario-state file."""

import json
import os
import subprocess
import time
import urllib.error
import urllib.request

STACK_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(STACK_DIR, ".env")
ENV_EXAMPLE_PATH = os.path.join(STACK_DIR, ".env.example")

API_URL = os.environ.get("API_URL", "http://localhost:8000")
TOXIPROXY_URL = os.environ.get("TOXIPROXY_URL", "http://localhost:8474")

HEALTHY_ENV = {"API_DB_PORT": "5432", "WORKER_LEAK_MEMORY": "0"}


def compose(*args: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    cmd = ["docker", "compose", *args]
    return subprocess.run(
        cmd,
        cwd=STACK_DIR,
        check=check,
        text=True,
        capture_output=capture,
    )


def read_env() -> dict[str, str]:
    path = ENV_PATH if os.path.exists(ENV_PATH) else ENV_EXAMPLE_PATH
    values = dict(HEALTHY_ENV)
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def write_env(values: dict[str, str]) -> None:
    lines = ["# Managed by inject.py. Edit inject.py, not this file.\n"]
    for key in sorted(values):
        lines.append(f"{key}={values[key]}\n")
    with open(ENV_PATH, "w", encoding="utf-8") as handle:
        handle.writelines(lines)


def set_env(**overrides: str) -> dict[str, str]:
    values = read_env()
    values.update({key: str(value) for key, value in overrides.items()})
    write_env(values)
    return values


# ---------------------------------------------------------------------------
# Toxiproxy control API
# ---------------------------------------------------------------------------
def _toxiproxy(method: str, path: str, payload: dict | None = None) -> tuple[int, str]:
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{TOXIPROXY_URL}{path}",
        data=body,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def add_cache_latency(latency_ms: int, jitter_ms: int) -> None:
    remove_cache_latency()
    status, body = _toxiproxy(
        "POST",
        "/proxies/cache/toxics",
        {
            "name": "cache_latency",
            "type": "latency",
            "stream": "downstream",
            "toxicity": 1.0,
            "attributes": {"latency": latency_ms, "jitter": jitter_ms},
        },
    )
    if status >= 300:
        raise RuntimeError(f"toxiproxy rejected the toxic ({status}): {body}")


def remove_cache_latency() -> None:
    _toxiproxy("DELETE", "/proxies/cache/toxics/cache_latency")


def active_toxics() -> list[dict]:
    status, body = _toxiproxy("GET", "/proxies/cache/toxics")
    return json.loads(body) if status < 300 else []


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------
def api_healthy(timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(f"{API_URL}/health", timeout=timeout) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def wait_for_api(timeout: float = 90.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if api_healthy():
            return
        time.sleep(1)
    raise TimeoutError(f"api never became healthy within {timeout}s")
