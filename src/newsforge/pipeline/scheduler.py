"""Pipeline scheduler: periodic execution of the P1-P6 pipeline.

CURRENT_MODEL: one application process -> one scheduler.
NOT MULTI-INSTANCE SAFE BY ITSELF. Execution is protected by the process-wide
lock in pipeline_trigger.py (threading.Lock) and by the 6-layer idempotency
model (source items, stories, claims, artifacts, publications, process lock).

For multi-instance deployments (multiple Render services, multiple containers),
use an external scheduler instead:
  - Render Cron (scheduled HTTP calls to POST /admin/pipeline/run)
  - External cron job
  - Dedicated worker process

The scheduler can be replaced without changing run_pipeline() -- only the
trigger mechanism changes.

The scheduler failure never crashes the application (fail-safe wrapping).
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any

from newsforge.config import SchedulerConfig
from newsforge.core.logger import get_logger, log_event

logger = get_logger("pipeline.scheduler")

_scheduler = None
_scheduler_lock = threading.Lock()
_status: dict[str, Any] = {
    "last_run_at": None,
    "next_run_at": None,
    "run_count": 0,
    "last_error": None,
    "running": False,
}


def _run_pipeline_job() -> None:
    """Execute the pipeline once. Called by the scheduler on each trigger."""
    _status["running"] = True
    _status["last_run_at"] = datetime.now(timezone.utc).isoformat()
    try:
        from newsforge.web.pipeline_trigger import run_pipeline_http
        result = run_pipeline_http(source_id=None)
        _status["last_error"] = None
        log_event(logger, "scheduler_pipeline_run",
                  status="completed",
                  published=result.get("published", 0) if isinstance(result, dict) else 0)
    except Exception as exc:
        _status["last_error"] = str(exc)
        log_event(logger, "scheduler_pipeline_run",
                  status="error",
                  error=str(exc))
    finally:
        _status["running"] = False
        _status["run_count"] += 1


def start_scheduler(cfg: SchedulerConfig) -> Any:
    """Start the APScheduler BackgroundScheduler with the given config.

    Returns the scheduler instance. SINGLE_PROCESS scope only.
    If the scheduler is already running, returns the existing instance."""
    global _scheduler

    with _scheduler_lock:
        if _scheduler is not None:
            return _scheduler

        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from apscheduler.triggers.interval import IntervalTrigger
            from apscheduler.triggers.cron import CronTrigger
        except ImportError:
            logger.warning("APScheduler not installed, scheduler disabled")
            return None

        _scheduler = BackgroundScheduler()

        if cfg.cron_expression:
            parts = cfg.cron_expression.split()
            if len(parts) >= 5:
                trigger = CronTrigger(
                    minute=parts[0], hour=parts[1],
                    day=parts[2], month=parts[3], day_of_week=parts[4],
                )
            else:
                trigger = IntervalTrigger(minutes=cfg.interval_minutes)
        else:
            trigger = IntervalTrigger(minutes=cfg.interval_minutes)

        _scheduler.add_job(
            _run_pipeline_job,
            trigger=trigger,
            id="newsforge_pipeline",
            name="NewsForge Pipeline",
            replace_existing=True,
            max_instances=1,
        )
        _scheduler.start()
        log_event(logger, "scheduler_started",
                  interval_minutes=cfg.interval_minutes,
                  cron_expression=cfg.cron_expression or None)
        return _scheduler


def get_scheduler_status() -> dict[str, Any]:
    """Return the current scheduler status (admin endpoint)."""
    with _scheduler_lock:
        return dict(_status)
