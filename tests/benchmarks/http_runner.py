"""Tests for the HTTP benchmark runner internals."""

from __future__ import annotations

from benchmarks.http.runner import _parse_oha


def test_parse_oha_normal():
    raw = {
        "summary": {"requestsPerSec": 1234.5, "successRate": 1.0},
        "latencyPercentiles": {"p50": 0.001, "p90": 0.002, "p99": 0.005},
        "statusCodeDistribution": {"200": 100, "500": 2},
    }
    parsed = _parse_oha(raw)
    assert parsed["rps"] == 1234.5
    assert parsed["p50_ms"] == 1.0
    assert parsed["p99_ms"] == 5.0
    assert parsed["total_requests"] == 102
    assert parsed["status_2xx"] == 100
    assert parsed["status_other"] == 2


def test_parse_oha_null_percentiles():
    # oha emits JSON null when there were ~0 responses to bucket. dict.get's
    # default does NOT kick in for present-but-null keys.
    raw = {
        "summary": {"requestsPerSec": None, "successRate": None},
        "latencyPercentiles": {"p50": None, "p90": None, "p99": None},
        "statusCodeDistribution": {},
    }
    parsed = _parse_oha(raw)
    assert parsed == {
        "rps": 0.0,
        "p50_ms": 0.0,
        "p90_ms": 0.0,
        "p99_ms": 0.0,
        "total_requests": 0,
        "success_rate": 0.0,
        "status_2xx": 0,
        "status_other": 0,
    }


def test_parse_oha_missing_keys():
    parsed = _parse_oha({})
    assert parsed["rps"] == 0.0
    assert parsed["p50_ms"] == 0.0
    assert parsed["total_requests"] == 0
