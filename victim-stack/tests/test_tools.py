"""Unit tests for the log/metric parsers. No Docker required."""

import json

import pytest

from tools import logs, promql


def write_log(tmp_path, service, records):
    path = tmp_path / f"{service}.log"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def record(ts, event="worker.memory_sampled", **extra):
    base = {
        "timestamp": ts,
        "level": "DEBUG",
        "service": "worker",
        "event": event,
        "message": "m",
    }
    base.update(extra)
    return base


def test_parse_timestamp_is_utc():
    assert logs.parse_timestamp("1970-01-01T00:00:01.500Z") == pytest.approx(1.5)


def test_read_skips_malformed_and_incomplete_lines(tmp_path, monkeypatch):
    path = tmp_path / "worker.log"
    good = json.dumps(record("2026-08-04T10:00:00.000Z"))
    path.write_text(good + "\nnot json\n" + json.dumps({"event": "x"}) + "\n\n", encoding="utf-8")
    monkeypatch.setattr(logs, "LOG_DIR", str(tmp_path))
    assert len(logs.read("worker")) == 1


def test_read_filters_by_since(tmp_path, monkeypatch):
    write_log(
        tmp_path,
        "worker",
        [record("2026-08-04T10:00:00.000Z"), record("2026-08-04T10:00:10.000Z")],
    )
    monkeypatch.setattr(logs, "LOG_DIR", str(tmp_path))
    since = logs.parse_timestamp("2026-08-04T10:00:05.000Z")
    assert len(logs.read("worker", since=since)) == 1


def test_max_gap_detects_restart_boundary(tmp_path, monkeypatch):
    write_log(
        tmp_path,
        "worker",
        [
            record("2026-08-04T10:00:00.000Z"),
            record("2026-08-04T10:00:02.000Z"),
            # restart: heartbeat stops for 9s
            record("2026-08-04T10:00:11.000Z"),
            record("2026-08-04T10:00:13.000Z"),
        ],
    )
    monkeypatch.setattr(logs, "LOG_DIR", str(tmp_path))
    assert logs.max_gap_seconds(logs.read("worker"), "worker.memory_sampled") == pytest.approx(9.0)


def test_max_gap_needs_two_samples(tmp_path, monkeypatch):
    write_log(tmp_path, "worker", [record("2026-08-04T10:00:00.000Z")])
    monkeypatch.setattr(logs, "LOG_DIR", str(tmp_path))
    assert logs.max_gap_seconds(logs.read("worker"), "worker.memory_sampled") == 0.0


def test_count_events(tmp_path, monkeypatch):
    write_log(
        tmp_path,
        "worker",
        [
            record("2026-08-04T10:00:00.000Z"),
            record("2026-08-04T10:00:01.000Z", event="job.failed"),
        ],
    )
    monkeypatch.setattr(logs, "LOG_DIR", str(tmp_path))
    records = logs.read("worker")
    assert logs.count_events(records, "job.failed") == 1


def test_inject_cli_builds_every_subcommand():
    """Regression: a handler without a docstring used to crash argparse setup.

    verify.py calls the handlers directly, so nothing else exercises the CLI.
    """
    import inject

    with pytest.raises(SystemExit) as exit_info:
        inject.main(["--help"])
    assert exit_info.value.code == 0


def test_verify_cli_builds():
    import verify

    with pytest.raises(SystemExit) as exit_info:
        verify.main(["--help"])
    assert exit_info.value.code == 0


def test_every_scenario_has_an_expectation_row():
    import inject
    import verify

    assert set(verify.EXPECTED) == {"healthy", *inject.SCENARIOS}
    for name, expectations in verify.EXPECTED.items():
        assert set(expectations) == set(verify.SIGNAL_ORDER), name


def test_scalar_treats_nan_as_default(monkeypatch):
    monkeypatch.setattr(promql, "query", lambda expr: [({}, float("nan"))])
    assert promql.scalar("whatever", default=7.0) == 7.0


def test_scalar_returns_default_on_empty_result(monkeypatch):
    monkeypatch.setattr(promql, "query", lambda expr: [])
    assert promql.scalar("whatever", default=3.0) == 3.0
