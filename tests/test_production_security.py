"""PRODUCTION_SECRETS_AND_DEPLOYMENT — production gates (all offline).

Covers the fail-fast production configuration gate, secret requirements/redaction
(logs, exceptions, error records, metrics, HTTP/artefacts), the database gate
(SQLite forbidden, PostgreSQL only), the AI gate (explicit provider, required key,
MOCK guard), the migration gate (head parity, outdated/unknown revisions),
rollback compatibility (newer DB blocks older app) and the deployment-target
abstraction. No real network, no real API calls, no live PostgreSQL needed.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from newsforge.config import (
    PRODUCTION_DEPLOYMENT_TARGET,
    DatabaseConfig,
    assert_production_safe,
    assert_mock_not_active_in_production,
    get_secret,
    missing_production_secrets,
    production_deployment_target,
    secret_source,
    validate_production_config,
)
from newsforge.core.error_tracker import persist_error, sanitize_message
from newsforge.core.logger import get_logger, log_event, redact_text, redact_value
from newsforge.core.metrics import metrics, reset_metrics
from newsforge.db import build_engine, init_db, migrate_database
from newsforge.db.schema import (
    SCHEMA_VERSION,
    SchemaIncompatibleError,
    ensure_schema_compatible,
)
from newsforge.db.session import (
    MigrationIncompatibilityError,
    assert_schema_migrated,
    init_production_db,
    migration_head_revision,
    on_disk_migration_revision,
)

_PG_DSN = "postgresql+pg8000://newsforge:sup3r-secret-pass@localhost:5432/newsforge"


def _set_prod_env(monkeypatch, **over):
    """Point the environment at a VALID production config, then apply overrides."""
    base = {
        "NEWSFORGE_ENVIRONMENT": "production",
        "NEWSFORGE_DATABASE_URL": _PG_DSN,
        "NEWSFORGE_ADMIN_TOKEN": "prod-admin-token-000",
        "NEWSFORGE_MOCK_AI": "false",
        "NEWSFORGE_DEFAULT_PROVIDER": "openai",
        "NEWSFORGE_OPENAI_API_KEY": "sk-prod00000000000000000000",
        "NEWSFORGE_SITE_URL": "https://newsforge.example",
        "NEWSFORGE_DEBUG": "false",
    }
    for k, v in over.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    for k, v in base.items():
        if k not in over:
            monkeypatch.setenv(k, v)


# --------------------------------------------------------------------------- #
# Production configuration gate
# --------------------------------------------------------------------------- #
def test_dev_environment_never_gated(monkeypatch):
    monkeypatch.setenv("NEWSFORGE_ENVIRONMENT", "development")
    assert validate_production_config(DatabaseConfig(path="/tmp/newsforge.db")) == []


def test_prod_rejects_sqlite_file_path(monkeypatch):
    _set_prod_env(monkeypatch)
    problems = validate_production_config(DatabaseConfig(path="/tmp/newsforge.db"))
    assert any("NEWSFORGE_DATABASE_URL" in p for p in problems)


def test_prod_rejects_missing_database_url(monkeypatch):
    _set_prod_env(monkeypatch, NEWSFORGE_DATABASE_URL=None)
    problems = validate_production_config(DatabaseConfig())
    assert any("NEWSFORGE_DATABASE_URL" in p for p in problems)


def test_prod_rejects_missing_admin_token(monkeypatch):
    _set_prod_env(monkeypatch, NEWSFORGE_ADMIN_TOKEN=None)
    problems = validate_production_config(DatabaseConfig(path=_PG_DSN))
    assert any("NEWSFORGE_ADMIN_TOKEN" in p for p in problems)


def test_prod_rejects_mock_ai(monkeypatch):
    _set_prod_env(monkeypatch, NEWSFORGE_MOCK_AI="true")
    problems = validate_production_config(DatabaseConfig(path=_PG_DSN))
    assert any("NEWSFORGE_MOCK_AI=false" in p for p in problems)


def test_prod_rejects_debug(monkeypatch):
    _set_prod_env(monkeypatch, NEWSFORGE_DEBUG="true")
    problems = validate_production_config(DatabaseConfig(path=_PG_DSN))
    assert any("NEWSFORGE_DEBUG=true" in p for p in problems)


def test_prod_rejects_localhost_site_url(monkeypatch):
    _set_prod_env(monkeypatch, NEWSFORGE_SITE_URL="http://localhost:8000")
    problems = validate_production_config(DatabaseConfig(path=_PG_DSN))
    assert any("NEWSFORGE_SITE_URL" in p for p in problems)


def test_prod_rejects_missing_real_provider(monkeypatch):
    # MOCK off but no explicit provider -> 'mock' default must be rejected.
    _set_prod_env(monkeypatch, NEWSFORGE_DEFAULT_PROVIDER=None)
    problems = validate_production_config(DatabaseConfig(path=_PG_DSN))
    assert any("provider" in p.lower() for p in problems)


def test_prod_rejects_openai_without_api_key(monkeypatch):
    _set_prod_env(monkeypatch, NEWSFORGE_OPENAI_API_KEY=None)
    problems = validate_production_config(DatabaseConfig(path=_PG_DSN))
    assert any("NEWSFORGE_OPENAI_API_KEY" in p for p in problems)


def test_prod_rejects_invalid_provider(monkeypatch):
    _set_prod_env(monkeypatch, NEWSFORGE_DEFAULT_PROVIDER="magic")
    problems = validate_production_config(DatabaseConfig(path=_PG_DSN))
    assert any("supported AI provider" in p for p in problems)


def test_prod_rejects_nonpositive_timeout_and_max_tokens(monkeypatch):
    _set_prod_env(monkeypatch, NEWSFORGE_AI_TIMEOUT_S="0", NEWSFORGE_AI_MAX_TOKENS="-1")
    problems = validate_production_config(DatabaseConfig(path=_PG_DSN))
    assert any("NEWSFORGE_AI_TIMEOUT_S > 0" in p for p in problems)
    assert any("NEWSFORGE_AI_MAX_TOKENS > 0" in p for p in problems)


def test_prod_accepts_full_valid_config(monkeypatch):
    _set_prod_env(monkeypatch)
    assert validate_production_config(DatabaseConfig(path=_PG_DSN)) == []


def test_prod_accepts_ollama_without_api_key(monkeypatch):
    _set_prod_env(monkeypatch, NEWSFORGE_DEFAULT_PROVIDER="ollama", NEWSFORGE_OPENAI_API_KEY=None)
    assert validate_production_config(DatabaseConfig(path=_PG_DSN)) == []


def test_assert_production_safe_raises_with_problems(monkeypatch):
    _set_prod_env(monkeypatch, NEWSFORGE_ADMIN_TOKEN=None)
    with pytest.raises(RuntimeError) as ei:
        assert_production_safe(DatabaseConfig(path=_PG_DSN))
    assert "NEWSFORGE_ADMIN_TOKEN" in str(ei.value)
    assert "Production configuration rejected" in str(ei.value)


# --------------------------------------------------------------------------- #
# Secrets interface + requirements
# --------------------------------------------------------------------------- #
def test_get_secret_reads_environment(monkeypatch):
    monkeypatch.setenv("NEWSFORGE_ADMIN_TOKEN", "token-from-env")
    assert get_secret("NEWSFORGE_ADMIN_TOKEN") == "token-from-env"
    assert get_secret("UNSET_SECRET_XYZ") is None


def test_secret_source_is_environment():
    assert secret_source() == "env"


def test_missing_production_secrets_reports_gaps(monkeypatch):
    _set_prod_env(monkeypatch)
    assert missing_production_secrets() == []
    _set_prod_env(monkeypatch, NEWSFORGE_ADMIN_TOKEN=None)
    assert "NEWSFORGE_ADMIN_TOKEN" in missing_production_secrets()
    _set_prod_env(monkeypatch, NEWSFORGE_OPENAI_API_KEY=None)
    assert "NEWSFORGE_OPENAI_API_KEY" in missing_production_secrets()


def test_missing_production_secrets_ignores_mock_provider(monkeypatch):
    _set_prod_env(monkeypatch, NEWSFORGE_MOCK_AI="true", NEWSFORGE_OPENAI_API_KEY=None)
    assert "NEWSFORGE_OPENAI_API_KEY" not in missing_production_secrets()


def test_mock_guard_rejects_production_mock(monkeypatch):
    _set_prod_env(monkeypatch, NEWSFORGE_MOCK_AI="true")
    with pytest.raises(RuntimeError) as ei:
        assert_mock_not_active_in_production(True)
    assert "MOCK_AI" in str(ei.value)


def test_mock_guard_allows_dev_mock_and_prod_real():
    assert_mock_not_active_in_production(False)  # production-real is fine
    assert_mock_not_active_in_production(True)   # dev default env: no raise


def test_ai_router_refuses_mock_in_production(monkeypatch):
    from newsforge.ai import AiRouter
    from newsforge.config import AiConfig

    _set_prod_env(monkeypatch, NEWSFORGE_MOCK_AI="true")
    with pytest.raises(RuntimeError):
        AiRouter(config=AiConfig(mock=True))
    _set_prod_env(monkeypatch, NEWSFORGE_MOCK_AI="false")
    router = AiRouter(config=AiConfig(mock=False, default_provider="ollama"))
    assert router is not None


# --------------------------------------------------------------------------- #
# Secret redaction (logs / exceptions / error records / metrics)
# --------------------------------------------------------------------------- #
def test_redact_text_masks_configured_secret_value(monkeypatch):
    monkeypatch.setenv("NEWSFORGE_ADMIN_TOKEN", "supertokensecret123")
    out = redact_text("auth header supertokensecret123 leaked")
    assert "supertokensecret123" not in out
    assert "[REDACTED]" in out


def test_redact_text_masks_password_cookie_auth_forms():
    samples = [
        "Password=abc123",
        "password=abc123",
        "pwd: hunter2",
        "Authorization: Bearer token123",
        "api_key: xyz987",
        "apikey=xyz987",
    ]
    for sample in samples:
        out = redact_text(sample)
        assert "abc123" not in out and "token123" not in out and "xyz987" not in out, sample
        assert "[REDACTED]" in out


def test_redact_text_masks_cookie_header():
    out = redact_text("Cookie: sid=AAAA; sess=bBBB")
    assert "AAAA" not in out and "bBBB" not in out
    assert "[REDACTED]" in out


def test_redact_text_masks_dsn_password_in_freetext():
    out = redact_text("connect via postgresql://u:realpw123@host:5432/db")
    assert "realpw123" not in out
    assert "****" in out or "[REDACTED]" in out


def test_redact_value_masks_sensitive_keys_and_keeps_others():
    redacted = redact_value({"authorization": "x", "cookie": "y",
                             "api_key": "k", "title": "ok", "n": 1})
    assert redacted == {"authorization": "[REDACTED]", "cookie": "[REDACTED]",
                        "api_key": "[REDACTED]", "title": "ok", "n": 1}


def test_secret_never_in_structured_logs(monkeypatch, capsys):
    secret = "log-secret-value-42"
    monkeypatch.setenv("NEWSFORGE_ADMIN_TOKEN", secret)
    logger = get_logger("unittest.prod.logs")
    log_event(logger, "prod_probe", message=f"surfaced {secret} in message",
              error_type="ValueError", error_message=f"and {secret} in error")
    out = capsys.readouterr().out
    assert secret not in out
    assert json.loads(out.splitlines()[-1])["event"] == "prod_probe"


def test_secret_never_in_exceptions_or_error_records(monkeypatch, tmp_path):
    secret = "record-secret-value-77"
    monkeypatch.setenv("NEWSFORGE_ADMIN_TOKEN", secret)
    exc = RuntimeError(f"boom {secret} tail")
    assert sanitize_message(str(exc)) == f"boom [REDACTED] tail"

    from newsforge.db.session import use_isolated_database_ctx

    with use_isolated_database_ctx(str(tmp_path / "prod_errors.db")):
        persist_error(module="unittest", error_type="RuntimeError",
                      message=str(exc),
                      context={"story_id": "s-1", "x": secret})
        from newsforge.db import get_session
        from newsforge.db.models import errors

        with get_session() as s:
            row = s.query(errors).order_by(errors.id.desc()).first()
        assert row is not None
        assert secret not in (row.message or "")
        assert secret not in (row.context_json or "")


def test_secret_never_in_metrics_snapshot(monkeypatch):
    secret = "metric-secret-value-9"
    monkeypatch.setenv("NEWSFORGE_ADMIN_TOKEN", secret)
    reset_metrics()
    metrics().inc("leak_probe", tags={"where": secret})
    body = json.dumps(metrics().snapshot())
    assert secret not in body
    reset_metrics()


# --------------------------------------------------------------------------- #
# Migration gate + production database startup
# --------------------------------------------------------------------------- #
@pytest.fixture()
def sqlite_engine(tmp_path):
    engine = build_engine(DatabaseConfig(path=str(tmp_path / "mig.db")))
    yield engine
    engine.dispose()


def test_migration_gate_passes_at_head(sqlite_engine):
    init_db(sqlite_engine)
    assert on_disk_migration_revision(sqlite_engine) == migration_head_revision(sqlite_engine)
    assert assert_schema_migrated(sqlite_engine) == SCHEMA_VERSION


def test_migration_gate_upgrades_outdated_schema(sqlite_engine):
    # Simulate a database left at the 0001 baseline; migrate_database brings it
    # to head and the gate accepts it.
    init_db(sqlite_engine)
    from alembic import command

    from newsforge.db.session import _alembic_config_for

    cfg = _alembic_config_for(sqlite_engine)
    command.stamp(cfg, "0001")
    assert on_disk_migration_revision(sqlite_engine) == "0001"

    migrate_database(sqlite_engine)
    assert on_disk_migration_revision(sqlite_engine) == migration_head_revision(sqlite_engine)
    assert assert_schema_migrated(sqlite_engine) == SCHEMA_VERSION


def test_migration_gate_fails_fast_when_behind_head(sqlite_engine):
    # DB at an older VALID revision but not migrated -> fail fast, never serve.
    init_db(sqlite_engine)
    with sqlite_engine.begin() as conn:
        conn.exec_driver_sql("UPDATE alembic_version SET version_num='0001'")
    with pytest.raises(MigrationIncompatibilityError) as ei:
        assert_schema_migrated(sqlite_engine)
    assert "does not match" in str(ei.value)


def test_migration_gate_unknown_revision_fails_fast(sqlite_engine):
    init_db(sqlite_engine)
    with sqlite_engine.begin() as conn:
        conn.exec_driver_sql("UPDATE alembic_version SET version_num='9999'")
    with pytest.raises(Exception):  # alembic cannot locate the revision
        migrate_database(sqlite_engine)


def test_init_production_db_rejects_sqlite_even_with_valid_env(monkeypatch, sqlite_engine):
    _set_prod_env(monkeypatch)
    with pytest.raises(MigrationIncompatibilityError) as ei:
        init_production_db(sqlite_engine)
    assert "postgresql" in str(ei.value)


def test_newer_schema_version_blocks_older_app(sqlite_engine):
    init_db(sqlite_engine)
    with sqlite_engine.begin() as conn:
        conn.exec_driver_sql(
            "UPDATE _newsforge_meta SET value='2' WHERE key='schema_version'"
        )
    with pytest.raises(SchemaIncompatibleError) as ei:
        ensure_schema_compatible(sqlite_engine)  # this build supports version 1
    assert "NEWER" in str(ei.value)


# --------------------------------------------------------------------------- #
# Deployment target + smoke design + docs
# --------------------------------------------------------------------------- #
def test_deployment_target_is_unselected():
    assert PRODUCTION_DEPLOYMENT_TARGET == "UNSELECTED"
    target = production_deployment_target()
    for field in ("target", "provider", "region", "compute", "postgresql",
                  "storage", "secret_mechanism", "domain", "tls", "rollback"):
        assert field in target
    assert target["target"] == "UNSELECTED"
    assert target["provider"] is None          # nothing invented/pre-allocated
    assert target["secret_mechanism"] == "env"  # the actual (only) mechanism


def test_deployment_smoke_script_covers_production_endpoints():
    script = Path("scripts/deploy_smoke.py").read_text(encoding="utf-8")
    for endpoint in ("/live", "/ready", "/health", "/articles",
                     "/sitemap.xml", "/feed.xml", "/metrics"):
        assert endpoint in script.replace(" ", ""), endpoint


def test_production_documentation_exists():
    ready = Path("PRODUCTION_READINESS.md").read_text(encoding="utf-8")
    deployment = Path("DEPLOYMENT.md").read_text(encoding="utf-8")
    for keyword in ("secrets", "migration", "backup", "rollback", "smoke",
                    "promotion", "http", "persistent", "ephemeral"):
        assert keyword.lower() in ready.lower(), keyword
    assert "PRODUCTION_READINESS.md" in deployment or "preview" in deployment.lower()