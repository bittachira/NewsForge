"""P5 — public web layer: FastAPI + Jinja2 SSR (no client framework).

Read-only over persisted state. The ONLY articles rendered are those with a COMPLETED
publication row — the publisher already consumed the Decision Engine verdict, so this
layer never re-derives gates, regenerates content or invents facts (§11/§24). Article
bodies come from the persisted GeneratedArtifact when one exists; otherwise the stored
story summary is shown (no new facts).

Routes:
  GET /health            -> minimal public health probe (HTTP 503 on unhealthy, no details)
  GET /articles          -> SSR list of published articles
  GET /articles/{slug}   -> SSR article page with JSON-LD, canonical, OG, Twitter cards
  GET /sitemap.xml       -> sitemap of published article URLs
  GET /feed.xml          -> RSS 2.0 feed of published articles
  GET /analytics         -> INTERNAL BI dashboard (requires NEWSFORGE_ADMIN_TOKEN)

Security (OPS hardening): FastAPI auto-docs (/docs, /redoc, /openapi.json) are disabled;
the analytics/BI route is gated by an admin token and fails closed; /health never echoes
exception text (fixed literals only, correct 503 on failure).
"""
from __future__ import annotations

import hmac
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
from sqlalchemy import text

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates

from newsforge.analytics import content_roi_query, total_ai_cost
from newsforge.config import BrandConfig
from newsforge.core.logger import get_logger
from newsforge.db import get_session, init_db, generated_artifacts, publications, stories
from newsforge.db.models import PublicationStatus, from_jsonable
from newsforge.seo.feeds import render_rss_xml, render_sitemap_xml
from newsforge.seo.meta import (
    build_jsonld,
    canonical_url,
    open_graph_tags,
    render_jsonld_script,
    twitter_card_tags,
)

logger = get_logger("web.app")
_TEMPLATES_DIR = Path(__file__).parent / "templates"

# Single env source of truth for the internal BI gate (see _internal_allowed).
_ADMIN_TOKEN_ENV = "NEWSFORGE_ADMIN_TOKEN"


def _internal_allowed(request: Request) -> bool:
    """Fail-closed gate for internal/BI endpoints (no full RBAC in the MVP).

    A single admin token (env ``NEWSFORGE_ADMIN_TOKEN``) must be provided via the
    ``X-Admin-Token`` header or ``?token=`` query param. When the token is not
    configured the endpoint stays closed. Compared in constant time; the token is never
    echoed into responses or logs."""
    token = os.getenv(_ADMIN_TOKEN_ENV)
    if not token:
        return False
    provided = request.headers.get("X-Admin-Token") or request.query_params.get("token") or ""
    return isinstance(provided, str) and hmac.compare_digest(provided, token)


def _published_entries(session) -> list[dict]:
    """One entry per story that has at least one COMPLETED publication (deduped).

    Deterministic ordering: newest published_at first, then slug."""
    pubs = session.query(publications).filter_by(
        status=PublicationStatus.COMPLETED.value).all()
    best: dict[str, dict] = {}
    for pub in pubs:
        story = session.get(stories, str(pub.story_id))
        if story is None or not story.slug:
            continue
        entry = {
            "story_pk": str(story.id),
            "slug": story.slug,
            "title": story.title or story.slug,
            "summary": story.summary,
            "published_at": pub.published_at,
        }
        cur = best.get(entry["story_pk"])
        if cur is None:
            best[entry["story_pk"]] = entry
            continue
        # Keep the latest published_at for this story across destinations.
        new_t = str(entry["published_at"] or "")
        old_t = str(cur["published_at"] or "")
        if new_t > old_t:
            best[entry["story_pk"]] = entry
    return sorted(best.values(), key=lambda e: (str(e["published_at"] or ""), e["slug"]), reverse=True)


def _article_sections(session, story) -> list[dict]:
    """Body sections from the persisted GeneratedArtifact (READ ONLY).

    Never regenerates: if no artifact exists, only the stored summary is shown."""
    artifact = (session.query(generated_artifacts)
                .filter_by(story_id=str(story.story_id))
                .order_by(generated_artifacts.created_at.desc()).first())
    if artifact is not None:
        body = from_jsonable(artifact.body_json) or {}
        sections = body.get("sections") or []
        if sections:
            return sections
    return [{"type": "note", "text": story.summary or ""}]


def _article_view(session, slug: str) -> Optional[dict]:
    """Load one published article view (story + COMPLETED publication + SEO data)."""
    story = session.query(stories).filter_by(slug=slug).first()
    if story is None:
        return None
    pub = (session.query(publications)
           .filter_by(story_id=str(story.id), status=PublicationStatus.COMPLETED.value)
           .order_by(publications.published_at.desc()).first())
    if pub is None:
        return None  # not published -> never rendered publicly
    brand = BrandConfig()
    url = canonical_url(brand.site_url, story.slug)
    jsonld = build_jsonld(
        site_name=brand.name,
        title=story.title or story.slug,
        summary=story.summary,
        url=url,
        published_at=pub.published_at,
    )
    return {
        "slug": story.slug,
        "title": story.title or story.slug,
        "summary": story.summary,
        "published_at": pub.published_at,
        "sections": _article_sections(session, story),
        "canonical_url": url,
        "og_tags": open_graph_tags(site_name=brand.name, title=story.title or story.slug,
                                    summary=story.summary, url=url),
        "twitter_tags": twitter_card_tags(title=story.title or story.slug, summary=story.summary),
        "jsonld_script": render_jsonld_script(jsonld),
    }


def create_app() -> FastAPI:
    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        # Ensure the SQLite schema exists before serving (fresh /data volume).
        init_db()
        yield

    app = FastAPI(title="NewsForge Web", lifespan=_lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    brand = BrandConfig()

    @app.get("/articles", response_class=HTMLResponse)
    def articles_index(request: Request):
        with get_session() as s:
            entries = _published_entries(s)
        return templates.TemplateResponse(
            request, "articles.html", {"brand": brand.name, "entries": entries})

    @app.get("/articles/{slug}", response_class=HTMLResponse)
    def article_page(request: Request, slug: str):
        with get_session() as s:
            view = _article_view(s, slug)
        if view is None:
            raise HTTPException(status_code=404, detail="article not found or not published")
        return templates.TemplateResponse(
            request, "article.html", {"brand": brand.name, **view})

    @app.get("/analytics", response_class=HTMLResponse)
    def analytics_dashboard(request: Request):
        """INTERNAL SSR analytics dashboard: traffic / revenue / cost / ROI.

        BI reads only (P6); requires NEWSFORGE_ADMIN_TOKEN (fail closed). Totals are
        computed for USD (the cost currency)."""
        if not _internal_allowed(request):
            raise HTTPException(status_code=403, detail="forbidden")
        with get_session() as s:
            rows = content_roi_query(s)
            total_cost = total_ai_cost(s)
            usd_rows = [r for r in rows if r["currency"] == "USD"]
        total_views = sum(r["views"] for r in rows)
        total_revenue = round(sum(r["revenue"] for r in usd_rows), 2)
        return templates.TemplateResponse(
            request, "analytics.html",
            {"brand": brand.name, "rows": rows, "total_views": int(total_views),
             "total_revenue": total_revenue, "total_cost": round(total_cost, 4),
             "currency": "USD"})

    @app.get("/sitemap.xml", response_class=Response)
    def sitemap_xml():
        with get_session() as s:
            entries = _published_entries(s)
        xml_text = render_sitemap_xml(entries, brand.site_url)
        return Response(content=xml_text, media_type="application/xml")

    @app.get("/feed.xml", response_class=Response)
    def rss_feed():
        with get_session() as s:
            entries = _published_entries(s)
        xml_text = render_rss_xml(site_name=brand.name, site_url=brand.site_url, entries=entries)
        return Response(content=xml_text, media_type="application/xml")

    @app.get("/health", response_model=dict)
    def health_check():
        """Minimal public health probe.

        Returns fixed literals only; a connectivity failure is reported as HTTP 503 with
        a generic body — the underlying exception text is NEVER exposed (§ health)."""
        try:
            with get_session() as s:
                # Test DB connection (SQLAlchemy 2.x)
                s.execute(text("SELECT 1"))
        except Exception:  # noqa: BLE001 - connectivity errors are surfaced, not leaked
            logger.warning("health check reported unhealthy")
            return JSONResponse(
                status_code=503,
                content={"status": "unhealthy", "db": "disconnected"},
            )
        return {"status": "ok", "db": "connected"}

    return app


app = create_app()
