"""Process-local, best-effort operational metrics (OPS_HARDENING_OBSERVABILITY §5).

These metrics are explicitly ``PROCESS_LOCAL`` + ``BEST_EFFORT``: a bounded in-memory
collector, NOT a distributed/durable metrics system and NOT a Prometheus push. They give
operators and the protected ``/metrics`` endpoint an at-a-glance view of request traffic,
pipeline/AI/publish behaviour and DB errors for the current process.

Cardinality is controlled by construction:

* tags are validated against a block-list so high-cardinality identifiers
  (``request_id``, ``story_id``, ``artifact_id``, ``job_id``, ``run_id``, free text,
  full URLs, ...) can NEVER become tag labels;
* tag values are length-bounded and the number of tags per series is bounded;
* histogram samples are capped (recent window only) so memory stays flat.

Snapshot output is deterministic (sorted series + stable summaries).
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any

MAX_TAGS = 6
MAX_TAG_VALUE_LEN = 64
MAX_HISTORY_SAMPLES = 200

_FORBIDDEN_TAG_KEYS = frozenset({
    "request_id", "story_id", "artifact_id", "job_id", "run_id", "trace_id", "span_id",
    "url", "full_url", "path_raw", "query", "query_string", "body", "text", "message",
    "slug", "title", "content", "user_agent", "email", "ip",
})


def _validate_tags(tags: dict | None) -> tuple[tuple[str, str], ...]:
    """Normalize + validate tags; raises ValueError on cardinality violations."""
    if not tags:
        return ()
    if len(tags) > MAX_TAGS:
        raise ValueError(f"too many tags ({len(tags)} > {MAX_TAGS}) for metric")
    for key, value in tags.items():
        k = str(key).strip().lower()
        if k in _FORBIDDEN_TAG_KEYS:
            raise ValueError(f"tag {key!r} is forbidden (high cardinality / identifiers)")
        if value is None:
            raise ValueError(f"tag {key!r} cannot be None")
        if not isinstance(value, (str, int, float, bool)):
            raise ValueError(f"tag {key!r} value must be a scalar, got {type(value).__name__}")
        if len(str(value)) > MAX_TAG_VALUE_LEN:
            raise ValueError(f"tag {key!r} value exceeds {MAX_TAG_VALUE_LEN} chars")
    return tuple(sorted((str(k), str(v)) for k, v in tags.items()))


def _label(series: tuple[str, tuple[tuple[str, str], ...]]) -> str:
    name, tags = series
    if not tags:
        return name
    body = ",".join(f"{k}={v}" for k, v in tags)
    return f"{name}{{{body}}}"


def _summary(samples: list[float]) -> dict:
    if not samples:
        return {}
    total = sum(samples)
    return {
        "count": len(samples),
        "sum": round(total, 3),
        "avg": round(total / len(samples), 3),
        "min": round(min(samples), 3),
        "max": round(max(samples), 3),
    }


class MetricsCollector:
    """Thread-safe in-memory counters + histogram (recent-window) observations."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._histograms: dict[tuple[str, tuple[tuple[str, str], ...]], list[float]] = {}

    def inc(self, name: str, *, tags: dict | None = None, value: float = 1.0) -> None:
        """Increment a counter. ``value`` must be non-negative."""
        if value < 0:
            raise ValueError("counter increment cannot be negative")
        key = (name, _validate_tags(tags))
        with self._lock:
            self._counters[key] = self._counters.get(key, 0.0) + value

    def observe(self, name: str, *, value: float, tags: dict | None = None) -> None:
        """Record one observation; keeps a bounded recent window per series."""
        key = (name, _validate_tags(tags))
        with self._lock:
            bucket = self._histograms.setdefault(key, [])
            bucket.append(float(value))
            if len(bucket) > MAX_HISTORY_SAMPLES:
                del bucket[: len(bucket) - MAX_HISTORY_SAMPLES]

    def snapshot(self) -> dict:
        """Deterministic snapshot: sorted series -> counter values / histogram summaries."""
        with self._lock:
            counters = OrderedDict(
                (k, v) for k, v in sorted(self._counters.items())
            )
            histograms = OrderedDict(
                (k, _summary(v)) for k, v in sorted(self._histograms.items())
            )
        return {
            "counters": {_label(k): v for k, v in counters.items()},
            "histograms": {_label(k): v for k, v in histograms.items()},
        }

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._histograms.clear()


_metrics = MetricsCollector()


def metrics() -> MetricsCollector:
    """Return the process-local singleton collector."""
    return _metrics


def reset_metrics() -> None:
    """Reset the singleton collector (tests)."""
    _metrics.reset()