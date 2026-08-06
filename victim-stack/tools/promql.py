"""Minimal Prometheus HTTP API client."""

import json
import os
import urllib.error
import urllib.parse
import urllib.request

PROM_URL = os.environ.get("PROM_URL", "http://localhost:9090")


class PrometheusError(RuntimeError):
    pass


def _get(path: str, params: dict[str, str]) -> dict:
    url = f"{PROM_URL}{path}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise PrometheusError(f"prometheus unreachable at {PROM_URL}: {exc}") from exc
    if body.get("status") != "success":
        raise PrometheusError(f"query failed: {body.get('error')}")
    return body["data"]


def query(expr: str) -> list[tuple[dict, float]]:
    """Instant query. Returns [(labels, value), ...]."""
    data = _get("/api/v1/query", {"query": expr})
    out = []
    for result in data.get("result", []):
        raw = result["value"][1]
        value = float("nan") if raw in ("NaN", "+Inf", "-Inf") else float(raw)
        out.append((result["metric"], value))
    return out


def scalar(expr: str, default: float = 0.0) -> float:
    """Instant query collapsed to a single number. NaN and empty both -> default."""
    results = query(expr)
    if not results:
        return default
    value = results[0][1]
    return default if value != value else value  # NaN check


def targets_up() -> dict[str, float]:
    """job -> up value (1/0). Missing job means Prometheus has never scraped it."""
    return {labels.get("job", "?"): value for labels, value in query("up")}
