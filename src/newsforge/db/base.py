"""Declarative base + reusable column *factories* for the NewsForge schema.

Each factory returns a FRESH Column object so columns are never shared/aliased
across tables (a common SQLAlchemy pitfall). Models assign them directly:

    id: Mapped[str] = uuid_pk()
    created_at: Mapped[str] = ts_col()
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Column, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Mapped to a single table per model; UUID primary keys."""

    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ts() -> str:
    return datetime.now().isoformat(timespec="seconds")


# --- column factories -------------------------------------------------------- #

def uuid_pk() -> Column[str, String]:
    """UUID primary key with a generated default."""
    return mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()), index=True)


def ts_col(default: Any = None, **kw) -> Column[str, str]:
    """Timestamp column (ISO string). ``default`` may be the callable :func:`ts`."""
    kw.setdefault("index", True)
    return mapped_column(String(27), default=ts if default is None else default, **kw)


def ts_nullable() -> Column[str | None, str]:
    """Nullable timestamp column (ISO string)."""
    return mapped_column(String(27), nullable=True, index=True)


def json_col(default: Any = None) -> Column[Any, Text]:
    """JSON blob stored as Text with manual (de)serialization helpers."""
    return mapped_column(Text, default=None if default is None else lambda d=default: d, nullable=True)
