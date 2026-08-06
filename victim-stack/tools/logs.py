"""Readers for the JSON log files.

The log files are the contract's other half. Everything here parses only the
documented fields -- if a helper needs a field that isn't in
docs/observability-contract.md, the contract is wrong, not the helper.
"""

import json
import os
from datetime import UTC, datetime

LOG_DIR = os.environ.get(
    "VICTIM_LOG_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
)

REQUIRED_FIELDS = ("timestamp", "level", "service", "event", "message")


def parse_timestamp(value: str) -> float:
    """Contract timestamps are ISO 8601 UTC with a Z suffix -> unix seconds."""
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=UTC
    ).timestamp()


def read(service: str, since: float | None = None) -> list[dict]:
    """All well-formed records for a service, optionally at/after a unix time."""
    path = os.path.join(LOG_DIR, f"{service}.log")
    if not os.path.exists(path):
        return []
    records = []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not all(field in record for field in REQUIRED_FIELDS):
                continue
            record["_ts"] = parse_timestamp(record["timestamp"])
            if since is not None and record["_ts"] < since:
                continue
            records.append(record)
    return records


def count_events(records: list[dict], event: str) -> int:
    return sum(1 for record in records if record["event"] == event)


def max_gap_seconds(records: list[dict], event: str) -> float:
    """Largest interval between consecutive occurrences of a heartbeat event.

    A restart shows up here as a gap several times the heartbeat cadence.
    """
    stamps = sorted(record["_ts"] for record in records if record["event"] == event)
    if len(stamps) < 2:
        return 0.0
    return max(b - a for a, b in zip(stamps, stamps[1:], strict=False))


def truncate_all() -> None:
    if not os.path.isdir(LOG_DIR):
        return
    for name in os.listdir(LOG_DIR):
        if name.endswith(".log"):
            with open(os.path.join(LOG_DIR, name), "w", encoding="utf-8"):
                pass
