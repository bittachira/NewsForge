"""Admin-gated HTTP registration of a news source (production OPS, Render Free).

Controlled surface for creating/updating a ``sources`` row over HTTP so an E2E
test source can be registered on Render Free (no Web Shell). It is NOT a general
admin API: the payload is restricted to the exact fields of a source, everything
else (commands, file paths, arbitrary SQL, foreign-entity params) is rejected.

Security contract
-----------------
* POST only (FastAPI answers 405 for every other method).
* Fail-closed ``NEWSFORGE_ADMIN_TOKEN`` gate, identical to ``/admin/pipeline/run``
  (enforced in ``newsforge.web.app`` BEFORE anything in this module runs).
* The body is a REQUIRED, deliberately narrow JSON object. Only
  ``name/url/source_id/type/tier/country/update/verify`` are accepted; any extra
  key (e.g. ``no_public_check``) is rejected with HTTP 400.
* Validation is delegated to :mod:`newsforge.cli` / the sources engine — the same
  code the container CLI uses: SSRF/public-target check (ALWAYS on), allowed
  types/tiers, source_id format, duplicate protection. There is no HTTP path to
  skip the public-target check.
* ``verify=true`` runs the existing SSRF-guarded :func:`ingest_source` through
  ``newsforge.cli.verify_source`` and reports items added/skipped.

Responses
---------
* ``200 {"status": "created"|"updated", "source_id", "name", "type", "tier",
    "verify": null | {"status":"ok","added":N,"skipped":M} | {"status":"error","detail":...}}``
* ``400`` validation/duplicate/SSRF rejection (``{"status":"error","detail":...}``)
* ``503`` database schema not ready (web app has not booted migrations)
* ``500`` unexpected failure / verify-time database failure
"""
from __future__ import annotations

from fastapi.responses import JSONResponse

from newsforge.cli import CliError, register_source, verify_source
from newsforge.core.logger import get_logger, log_event
from newsforge.db.models import SourceTier, SourceType

logger = get_logger("web.source_registration")

_REGISTER_FIELDS = frozenset(
    {"name", "url", "source_id", "type", "tier", "country", "update", "verify"}
)
_OPTIONAL_TEXT_FIELDS = frozenset({"source_id", "type", "tier", "country"})
_BOOL_FIELDS = frozenset({"update", "verify"})


def validate_payload(payload) -> dict:
    """Normalize+validate the REQUIRED registration body; raise ValueError otherwise.

    Returns ``{"name","url","source_id","type","tier","country","update","verify"}``
    with strings stripped and booleans resolved to ``False`` when absent.
    """
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    if not payload:
        raise ValueError("payload must not be empty: name and url are required")

    extra = set(payload) - _REGISTER_FIELDS
    if extra:
        raise ValueError(f"unsupported payload fields: {', '.join(sorted(extra))}")

    out: dict = {}

    for key in ("name", "url"):
        val = payload.get(key)
        if not isinstance(val, str) or not val.strip():
            raise ValueError(f"{key} must be a non-empty string")
        out[key] = val.strip()

    for key in _OPTIONAL_TEXT_FIELDS:
        val = payload.get(key)
        if val is None:
            out[key] = None
        elif isinstance(val, str):
            out[key] = val.strip() or None
        else:
            raise ValueError(f"{key} must be a string")

    for key in _BOOL_FIELDS:
        val = payload.get(key)
        if val is None:
            out[key] = False
        elif isinstance(val, bool):
            out[key] = val
        else:
            raise ValueError(f"{key} must be a boolean")

    return out


def register_source_http(payload: dict) -> JSONResponse:
    """Create/update ONE source (and optionally verify it); returns JSON.

    ``register_source`` is always called with the SSRF/public-target check ON
    (there is no request field that can disable it) and with the narrow fields
    the payload validation already enforced.
    """
    try:
        fields = validate_payload(payload)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"status": "error", "detail": str(exc)})

    try:
        info = register_source(
            name=fields["name"],
            url=fields["url"],
            source_id=fields["source_id"],
            type=fields["type"] or SourceType.RSS.value,
            tier=fields["tier"] or SourceTier.TIER_3.value,
            country=fields["country"],
            check_public=True,
            update=fields["update"],
        )
    except CliError as exc:
        detail = str(exc)
        log_event(
            logger, "source_register_rejected",
            error_message=detail[:500],
            source_id=fields["source_id"],
        )
        if "schema not ready" in detail:
            return JSONResponse(
                status_code=503,
                content={
                    "status": "error",
                    "detail": detail,
                    "hint": "start the web service once so it applies migrations, then retry",
                },
            )
        return JSONResponse(status_code=400, content={"status": "error", "detail": detail})

    body = {
        "status": "created" if info["created"] else "updated",
        "source_id": info["source_id"],
        "name": fields["name"],
        "type": fields["type"] or SourceType.RSS.value,
        "tier": fields["tier"] or SourceTier.TIER_3.value,
        "verify": None,
    }
    log_event(
        logger, "source_register_done",
        source_id=info["source_id"], created=info["created"],
    )

    if fields["verify"]:
        try:
            summary = verify_source(info["source_id"])
        except CliError as exc:
            body["verify"] = {"status": "error", "detail": str(exc)}
        else:
            body["verify"] = {
                "status": "ok",
                "added": summary["added"],
                "skipped": summary["skipped"],
            }
        log_event(
            logger, "source_register_verify", source_id=info["source_id"],
            verify_status=body["verify"]["status"],
        )
    return JSONResponse(content=body)