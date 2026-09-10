"""Deployment/build visibility metadata (OPS_HARDENING_OBSERVABILITY §7).

Originates from environment/build metadata (``NEWSFORGE_VERSION``,
``NEWSFORGE_GIT_COMMIT``, ``NEWSFORGE_BUILD_TIME``) and falls back to ``unknown`` —
a SHA or build time is NEVER invented or guessed. Exposed only via the authenticated
``/metrics`` endpoint (never on public ``/health``).
"""
from __future__ import annotations

import os
import sys


def get_build_info() -> dict:
    """Return build/deployment metadata (deterministic, no secrets)."""
    from newsforge.db.schema import SCHEMA_VERSION

    return {
        "version": os.getenv("NEWSFORGE_VERSION") or "unknown",
        "git_commit": os.getenv("NEWSFORGE_GIT_COMMIT") or "unknown",
        "build_time": os.getenv("NEWSFORGE_BUILD_TIME") or "unknown",
        "python_version": (
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        ),
        "schema_version": SCHEMA_VERSION,
    }