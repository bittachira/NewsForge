"""Operator CLI — register a news source through the same ORM path the pipeline uses.

The only supported way to create a real :class:`~newsforge.db.models.sources` row
today is directly through the ORM (the app exposes no source-registry endpoint by
design). This module makes that operator-only and safe:

* Runs inside the container shell (Render Shell / ``docker exec`` / CI):
  ``python -m newsforge.cli register-source --name "BOE" --url https://www.boe.es/rss --type OFFICIAL --tier TIER_1``
* Creates/updates **exactly one** ``sources`` row via the ORM.
* Reuses the same SSRF policy as the fetch engine (:func:`assert_public_target`):
  non-http(s) schemes and loopback/private/metadata hosts are refused at
  registration time (``--no-public-check`` only for offline/dev use).
* Does NOT run migrations (the web app boots them) and does NOT open an HTTP
  route — it is a local, operator-invoked bootstrap tool.
* ``--verify`` fetches + ingests the source immediately through the existing
  SSRF-guarded :func:`newsforge.sources.engine.ingest_source` so the operator
  learns right away whether the feed actually yields items.

Exit codes: 0 success, 1 registration/verify failure, 2 usage error (argparse).
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
import unicodedata
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from newsforge.core.netguard import SSRFError, assert_public_target
from newsforge.db import sources as sources_model
from newsforge.db.models import SourceTier, SourceType
from newsforge.db.session import get_session_factory
from newsforge.sources.engine import ingest_source

SOURCE_ID_DEFAULT = "source-id-auto"
SCHEME_MSG = "refusing non-http(s) scheme: {scheme!r}"


class CliError(RuntimeError):
    """A user-facing registration failure (validation, policy or DB)."""


def _require_schema() -> None:
    """Fail fast with a clear message when the ``sources`` table is missing.

    Migrations are the web app's job (``init_db``/``init_production_db`` at boot);
    this tool must never mutate schema. If the app has never started against this
    database, the operator gets an actionable error instead of a raw SQL trace.
    """
    from sqlalchemy import text

    try:
        with get_session_factory()() as session:
            session.execute(text("SELECT 1 FROM sources LIMIT 1"))
    except OperationalError:
        raise CliError(
            "database schema not ready (no sources table). Start the web service "
            "once so it applies migrations, then retry this command."
        ) from None


def _slugify(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", name)
    ascii_ = "".join(c for c in normalized if not unicodedata.combining(c))
    base = re.sub(r"[^a-z0-9]+", "-", ascii_.lower()).strip("-")
    return base or SOURCE_ID_DEFAULT


# --------------------------------------------------------------------------- #
# Validation (fail fast, offline where possible)
# --------------------------------------------------------------------------- #
def _validate_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
    except ValueError as exc:  # malformed URL
        raise CliError(f"malformed url {url!r}: {exc}") from None
    if parsed.scheme not in ("http", "https"):
        raise CliError(SCHEME_MSG.format(scheme=parsed.scheme))
    if not parsed.hostname:
        raise CliError(f"refusing url without a host: {url!r}")


def _check_public(url: str) -> None:
    try:
        asyncio.run(assert_public_target(url))
    except SSRFError as exc:
        raise CliError(f"refusing public-host check: {exc}") from None


def _resolve_type(value: str) -> str:
    valid = {t.value for t in SourceType}
    if value not in valid:
        raise CliError(f"unknown --type {value!r}; choose from {sorted(valid)}")
    return value


def _resolve_tier(value: str) -> str:
    valid = {t.value for t in SourceTier}
    if value not in valid:
        raise CliError(f"unknown --tier {value!r}; choose from {sorted(valid)}")
    return value


def _resolve_trust_score(value: int) -> int:
    if not 0 <= value <= 100:
        raise CliError("--trust-score must be an integer between 0 and 100")
    return value


# --------------------------------------------------------------------------- #
# Core operations (pure logic, exercised directly by tests)
# --------------------------------------------------------------------------- #
def register_source(
    *,
    name: str,
    url: str,
    source_id: str | None = None,
    type: str = SourceType.RSS.value,
    tier: str = SourceTier.TIER_3.value,
    country: str | None = None,
    language: str = "es",
    trust_score: int = 50,
    check_public: bool = True,
    update: bool = False,
) -> dict:
    """Create (or with ``update``, modify) ONE ``sources`` row.

    Returns ``{"source_id": ..., "created": bool}``. Raises :class:`CliError`
    on validation/policy failure or when the source already exists (no
    ``update``). Touches no other table and opens no HTTP route.
    """
    if not name or not name.strip():
        raise CliError("--name is required")
    trust_score = _resolve_trust_score(trust_score)
    _validate_url(url)
    source_type = _resolve_type(type)
    source_tier = _resolve_tier(tier)
    if check_public:
        _check_public(url)

    sid = (source_id or _slugify(name)).strip()
    if not sid:
        raise CliError("--source-id is required when it cannot be derived from --name")

    _require_schema()
    factory = get_session_factory()
    with factory() as session:
        existing = session.scalars(
            select(sources_model).where(sources_model.source_id == sid)
        ).first()
        if existing is not None and not update:
            raise CliError(
                f"SOURCE_EXISTS source_id={sid} (pass --update to overwrite fields)"
            )
        if existing is not None:
            existing.name = name.strip()
            existing.url = url
            existing.type = source_type
            existing.tier = source_tier
            existing.country = country
            existing.language = language
            existing.trust_score = trust_score
            created = False
        else:
            row = sources_model(
                source_id=sid,
                name=name.strip(),
                url=url,
                type=source_type,
                tier=source_tier,
                country=country,
                language=language,
                trust_score=trust_score,
            )
            session.add(row)
            created = True
        session.commit()
    return {"source_id": sid, "created": created}


def verify_source(source_id: str) -> dict:
    """Fetch + ingest the stored source through the existing engine.

    Returns ``{"added": int, "skipped": int}``; raises :class:`CliError` for an
    unknown source or a failed fetch/parse/DB step.
    """
    _require_schema()
    factory = get_session_factory()
    with factory() as session:
        row = session.scalars(
            select(sources_model).where(sources_model.source_id == source_id)
        ).first()
    if row is None:
        raise CliError(f"unknown source_id={source_id!r}")
    src = {
        "source_id": row.source_id,
        "name": row.name,
        "url": row.url,
        "type": row.type,
        "tier": row.tier,
    }
    result = asyncio.run(ingest_source(src))
    if result.errors:
        raise CliError("; ".join(result.errors))
    return {"added": result.added, "skipped": result.skipped_dupe}


# --------------------------------------------------------------------------- #
# CLI entry point
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="newsforge.cli",
        description="NewsForge operator CLI — register a news source.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    reg = sub.add_parser("register-source", help="create/update one sources row")
    reg.add_argument("--name", required=True, help="human-readable source name")
    reg.add_argument("--url", required=True, help="feed/page URL (http/https only)")
    reg.add_argument(
        "--source-id",
        default=None,
        help="stable source_id (default: slugified --name)",
    )
    reg.add_argument(
        "--type", default=SourceType.RSS.value,
        choices=sorted(t.value for t in SourceType),
        help="source kind",
    )
    reg.add_argument(
        "--tier", default=SourceTier.TIER_3.value,
        choices=sorted(t.value for t in SourceTier),
        help="verification tier",
    )
    reg.add_argument("--country", default=None, help="ISO country code (optional)")
    reg.add_argument("--language", default="es", help="content language (default es)")
    reg.add_argument("--trust-score", type=int, default=50, help="0-100 (default 50)")
    reg.add_argument(
        "--update", action="store_true",
        help="overwrite fields of an existing source_id (default: refuse)",
    )
    reg.add_argument(
        "--verify", action="store_true",
        help="after registering, run the existing ingest engine against the URL",
    )
    reg.add_argument(
        "--no-public-check", action="store_true",
        help="skip the SSRF/public-host DNS check (OFFLINE/DEV ONLY)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        info = register_source(
            name=args.name,
            url=args.url,
            source_id=args.source_id,
            type=args.type,
            tier=args.tier,
            country=args.country,
            language=args.language,
            trust_score=args.trust_score,
            check_public=not args.no_public_check,
            update=args.update,
        )
    except CliError as exc:
        print(f"SOURCE_REGISTER_FAILED {exc}")
        return 1

    verb = "SOURCE_CREATED" if info["created"] else "SOURCE_UPDATED"
    print(
        f"{verb} source_id={info['source_id']} name={args.name} "
        f"type={args.type} tier={args.tier} trust_score={args.trust_score}"
    )

    if args.verify:
        try:
            summary = verify_source(info["source_id"])
        except CliError as exc:
            print(f"SOURCE_VERIFY_FAILED {exc}")
            return 1
        print(
            f"SOURCE_VERIFY_OK added={summary['added']} skipped={summary['skipped']} "
            f"source_id={info['source_id']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())