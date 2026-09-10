"""Central configuration for NewsForge.

All tunables live here so the platform can be reconfigured without code changes:
brand, languages, database path, AI provider settings and every quality / trust /
decision threshold that gates what gets published.

Nothing sensitive is hardcoded in this module — secrets come from environment
variables (see .env.example) and are never committed.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class BrandConfig:
    """Public-facing identity. Fully configurable; rename is a one-line change."""

    name: str = os.getenv("NEWSFORGE_BRAND_NAME", "Lumen")
    tagline: str = os.getenv("NEWSFORGE_TAGLINE", "Verified light on what matters.")
    site_url: str = os.getenv("NEWSFORGE_SITE_URL", "http://localhost:8000").rstrip("/")
    admin_title: str = os.getenv("NEWSFORGE_ADMIN_TITLE", "NewsForge Admin")

    @property
    def slug(self) -> str:
        return self.name.lower()


# Anchors resolved at import time against the source root (NOT the process cwd), so the
# default DB/backup locations are deterministic on any machine/container regardless of
# where uvicorn/pytest is launched from (§9 persistence hardening). Set an absolute path
# via env to override (the container uses NEWSFORGE_DB_PATH=/data/newsforge.db).
_SOURCE_ROOT = Path(__file__).resolve().parents[2]


def _resolve_data_path(raw: str) -> Path:
    p = Path(raw).expanduser()
    if p.is_absolute():
        return p
    return (_SOURCE_ROOT / p).resolve()


def _default_db_path() -> str | Path:
    raw = os.getenv("NEWSFORGE_DB_PATH", "data/newsforge.db")
    if isinstance(raw, str) and raw.startswith(("postgresql", "postgres", "mysql", "mariadb")):
        return raw  # DSN passthrough (dialect swap) MUST NOT be coerced to a Path
    return _resolve_data_path(raw)


def _default_backup_dir() -> Path:
    return _resolve_data_path(os.getenv("NEWSFORGE_BACKUP_DIR", "data/backups"))


@dataclass(frozen=True)
class DatabaseConfig:
    """SQLite-first; swap the SQLAlchemy dialect to reach PostgreSQL later.

    ``path`` is either a filesystem path (resolved deterministically) or a full
    DSN string (``postgresql://…``) passed through to SQLAlchemy verbatim.
    ``backup_dir`` hosts offline SQLite backups created by ``newsforge.db.backup``."""

    path: str | Path = field(default_factory=_default_db_path)
    backup_dir: Path = field(default_factory=_default_backup_dir)
    echo_sql: bool = _env_bool("NEWSFORGE_ECHO_SQL", False)


@dataclass(frozen=True)
class AiConfig:
    """Model Router configuration. Provider-independent by design."""

    # When MOCK is True the router returns deterministic, source-grounded output
    # and never touches the network — used for MVP demos, tests and offline runs.
    mock: bool = _env_bool("NEWSFORGE_MOCK_AI", True)

    # OpenAI-compatible providers (base_url + optional api_key).
    openai_base_url: str = os.getenv("NEWSFORGE_OPENAI_BASE_URL", "https://api.openai.com/v1")
    openai_api_key: str | None = os.getenv("NEWSFORGE_OPENAI_API_KEY")

    # LM Studio local server (OpenAI-compatible, no key by default).
    lm_studio_base_url: str = os.getenv("NEWSFORGE_LM_STUDIO_URL", "http://localhost:1234/v1")
    lm_studio_api_key: str | None = os.getenv("NEWSFORGE_LM_STUDIO_API_KEY")

    # Ollama (native /api/chat format).
    ollama_base_url: str = os.getenv("NEWSFORGE_OLLAMA_URL", "http://localhost:11434")
    ollama_model: str = os.getenv("NEWSFORGE_OLLAMA_MODEL", "llama3.1")

    # Which provider the router selects for a given task size (see ai/router.py).
    default_provider: str = os.getenv("NEWSFORGE_DEFAULT_PROVIDER", "mock")

    # Real-provider cost model (USD per 1k tokens, combined in+out). Only used
    # when ``mock=False``; MOCK tier keeps ``mock_cost_per_1k_tokens``.
    cost_per_1k_tokens: float = float(os.getenv("NEWSFORGE_AI_COST_PER_1K", "0.0") or "0.0")

    # Real-provider timeout (seconds) for a single generate call.
    request_timeout_s: float = float(os.getenv("NEWSFORGE_AI_TIMEOUT_S", "30") or "30")

    # Max completion tokens requested from a real provider.
    max_tokens: int = int(os.getenv("NEWSFORGE_AI_MAX_TOKENS", "500") or "500")

    # Cost / latency tuning per model tier.
    small_model: str = os.getenv("NEWSFORGE_SMALL_MODEL", "gpt-4o-mini")
    medium_model: str = os.getenv("NEWSFORGE_MEDIUM_MODEL", "gpt-4o")
    large_model: str = os.getenv("NEWSFORGE_LARGE_MODEL", "gpt-4o")

    # Token/latency cost model (tokens per 1k output, ms latency) for the MOCK tier.
    mock_cost_per_1k_tokens: float = 0.0
    mock_latency_ms: int = 5


@dataclass(frozen=True)
class TrustConfig:
    """Trust scoring + quality thresholds that gate publishing."""

    # Minimum content trust score (0-100) to be eligible for auto-publish.
    min_trust_to_publish: float = 60.0
    # Minimum quality-gate composite score (0-100).
    quality_min_score: float = 70.0
    # Source tiers that are allowed to seed publishable content on their own.
    # TIER_1/2 can contribute; TIER_3 needs corroboration; TIER_4 never auto-publishes.
    tier_allowed_seeds: tuple[str, ...] = ("TIER_1", "TIER_2")

    # Anti-slop thresholds.
    max_repetition_ratio: float = 0.18      # fraction of near-duplicate sentences
    min_unique_sentences: int = 3           # too short / thin -> reject
    generic_phrase_penalty_threshold: float = 0.40


@dataclass(frozen=True)
class DecisionConfig:
    """CONTENT_DECISION_ENGINE weights (0-1 each, summed)."""

    news_value: float = 0.25
    search_value: float = 0.20
    user_value: float = 0.15
    originality: float = 0.20
    trust: float = 0.15
    commercial_value: float = 0.05
    urgency: float = 0.0

    # RED categories force HUMAN-IN-THE-LOOP review regardless of score.
    red_categories: tuple[str, ...] = (
        "politics", "elections", "accusations", "crime", "offenses",
        "health", "medical", "emergency", "suicide", "self_harm",
        "security", "national_security", "financial_advice", "litigation",
        "identifiable_persons", "unconfirmed_rumor", "unverified",
    )


@dataclass(frozen=True)
class SeoConfig:
    default_charset: str = "utf-8"
    og_type: str = "website"
    twitter_card: str = "summary_large_image"
    robots_txt: str = "User-agent: *\nAllow: /\n"


@dataclass(frozen=True)
class ServerConfig:
    host: str = os.getenv("NEWSFORGE_HOST", "127.0.0.1")
    port: int = int(os.getenv("NEWSFORGE_PORT", "8000"))
    debug: bool = _env_bool("NEWSFORGE_DEBUG", False)


@dataclass(frozen=True)
class LanguagesConfig:
    """Each language gets independent SEO architecture (hreflang, sitemaps)."""

    languages: tuple[str, ...] = ("es", "en", "pt")
    default_language: str = "es"


def get_config(*, sections: tuple[str, ...] | None = None) -> dict[str, object]:
    """Return the requested config objects as a plain dict (for templates/tests)."""
    cfgs = {
        "brand": BrandConfig(),
        "database": DatabaseConfig(),
        "ai": AiConfig(),
        "trust": TrustConfig(),
        "decision": DecisionConfig(),
        "seo": SeoConfig(),
        "server": ServerConfig(),
        "languages": LanguagesConfig(),
    }
    if sections is None:
        return cfgs
    return {k: getattr(cfgs, k) for k in sections}
