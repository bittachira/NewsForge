"""Operator CLI source-registration tests (newsforge.cli, OPS bootstrap).

Covers the safety contract of the operator-only source bootstrap: offline scheme
validation, the SSRF/public-host policy reuse (private literal refused), type/tier
enum bounds, idempotency (--update), exactly-one-row semantics, and the --verify
ingest plumbing. All DB work runs against an isolated SQlite database.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

import newsforge.db as db
from newsforge import cli as cli_mod
from newsforge.cli import (
    CliError,
    build_parser,
    main,
    register_source,
    verify_source,
)
from newsforge.db import get_session, source_items, sources


@pytest.fixture(autouse=True)
def isolated_db():
    db_dir = Path.cwd() / ".pytest_tmp"
    shutil.rmtree(db_dir, ignore_errors=True)
    db_dir.mkdir(exist_ok=True)
    path = db_dir / "cli-source.db"
    with db.use_isolated_database_ctx(path):
        yield


def _find(sid: str):
    with get_session() as s:
        return s.query(sources).filter_by(source_id=sid).first()


# --------------------------------------------------------------------------- #
# Offline validation (no DNS, no DB)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", ["file:///C:/secret.txt", "ftp://evil.example/x",
                                 "", "not-a-url at all"])
def test_validate_url_rejects_non_http_schemes(bad):
    with pytest.raises(CliError):
        register_source(name="X", url=bad, check_public=False, source_id="x")


def test_validate_url_rejects_unknown_type_tier():
    with pytest.raises(CliError, match="unknown --type"):
        register_source(name="X", url="https://example.com/rss",
                        type="SCAM", check_public=False)
    with pytest.raises(CliError, match="unknown --tier"):
        register_source(name="X", url="https://example.com/rss",
                        tier="TIER_9", check_public=False)


def test_validate_trust_score_bounds():
    with pytest.raises(CliError, match="trust-score"):
        register_source(name="X", url="https://example.com/rss",
                        trust_score=101, check_public=False)


@pytest.mark.parametrize("bad", ["has space", "../up", "semi;colon",
                                 "quote'x", "back\\slash", "$var", "a" * 200])
def test_register_rejects_invalid_source_id(bad):
    with pytest.raises(CliError, match="source_id"):
        register_source(name="X", url="https://example.com/rss",
                        source_id=bad, check_public=False)


def test_main_refuses_no_public_check_in_production(monkeypatch, capsys):
    monkeypatch.setenv("NEWSFORGE_ENVIRONMENT", "production")
    code = main([
        "register-source", "--name", "X", "--url", "https://example.com/rss",
        "--no-public-check",
    ])
    assert code == 1
    out = capsys.readouterr().out
    assert "SOURCE_REGISTER_FAILED" in out
    assert "can never be skipped in production" in out


def test_main_allows_no_public_check_outside_production(monkeypatch, capsys):
    monkeypatch.setenv("NEWSFORGE_ENVIRONMENT", "development")
    code = main([
        "register-source", "--name", "X", "--url", "https://example.com/rss",
        "--no-public-check",
    ])
    assert code == 0
    assert "SOURCE_CREATED" in capsys.readouterr().out


def test_register_requires_name():
    with pytest.raises(CliError, match="--name is required"):
        register_source(name="   ", url="https://example.com/rss", check_public=False)


def test_schema_not_ready_has_actionable_error():
    """Missing schema (app never booted) yields a clear message, not a raw SQL trace."""
    from sqlalchemy import text

    with get_session() as s:
        s.execute(text("DROP TABLE sources"))
        s.commit()
    with pytest.raises(CliError, match="schema not ready"):
        register_source(name="X", url="https://example.com/rss",
                        check_public=False)
    with pytest.raises(CliError, match="schema not ready"):
        verify_source("any")


def test_public_check_blocks_private_literal_offline():
    """The SSRF policy is reused: a literal loopback host is refused without DNS."""
    with pytest.raises(CliError, match="non-public address"):
        register_source(name="X", url="http://127.0.0.1/feed.xml",
                        check_public=True)


# --------------------------------------------------------------------------- #
# DB create/update semantics (exactly one row, idempotency)
# --------------------------------------------------------------------------- #
def test_register_source_creates_one_row_with_defaults():
    info = register_source(
        name="Boletín Oficial", url="https://www.boe.es/rss",
        source_id="boe", type="OFFICIAL", tier="TIER_1",
        country="ES", language="es", trust_score=95,
        check_public=False,
    )
    assert info == {"source_id": "boe", "created": True}
    row = _find("boe")
    assert row is not None
    assert row.name == "Boletín Oficial"
    assert row.url == "https://www.boe.es/rss"
    assert row.type == "OFFICIAL"
    assert row.tier == "TIER_1"
    assert row.country == "ES"
    assert row.trust_score == 95
    # Only one row was created (defaults intact for untouched fields).
    with get_session() as s:
        assert s.query(sources).count() == 1


def test_register_source_derives_source_id_from_name():
    info = register_source(name="El País", url="https://elpais.com/rss",
                           check_public=False)
    assert info["source_id"] == "el-pais"
    assert info["created"] is True


def test_register_source_refuses_duplicate_without_update():
    register_source(name="X", url="https://example.com/rss",
                    source_id="dup", check_public=False)
    with pytest.raises(CliError, match="SOURCE_EXISTS"):
        register_source(name="Y", url="https://example.com/other",
                        source_id="dup", check_public=False)
    with get_session() as s:
        assert s.query(sources).count() == 1


def test_register_source_update_overwrites_fields():
    register_source(name="X", url="https://example.com/rss", source_id="up",
                    type="RSS", tier="TIER_3", trust_score=50, check_public=False)
    info = register_source(name="X Renamed", url="https://example.com/new",
                           source_id="up", type="OFFICIAL", tier="TIER_1",
                           trust_score=90, check_public=False, update=True)
    assert info == {"source_id": "up", "created": False}
    row = _find("up")
    assert row.name == "X Renamed"
    assert row.url == "https://example.com/new"
    assert row.type == "OFFICIAL"
    assert row.tier == "TIER_1"
    assert row.trust_score == 90
    with get_session() as s:
        assert s.query(sources).count() == 1  # never creates a second row


# --------------------------------------------------------------------------- #
# --verify plumbing (ingest engine, not re-implemented)
# --------------------------------------------------------------------------- #
def test_verify_source_unknown_source_id():
    with pytest.raises(CliError, match="unknown source_id"):
        verify_source("missing")


def test_verify_source_runs_existing_ingest_engine(monkeypatch):
    register_source(name="X", url="https://example.com/rss",
                    source_id="v", check_public=False)
    calls: list[dict] = []

    from newsforge.sources.engine import IngestResult

    async def _fake_ingest(src):
        calls.append(src)
        return IngestResult(added=2, skipped_dupe=1)

    monkeypatch.setattr(cli_mod, "ingest_source", _fake_ingest)
    summary = verify_source("v")
    assert summary == {"added": 2, "skipped": 1}
    assert calls[0]["source_id"] == "v"
    assert calls[0]["url"] == "https://example.com/rss"


def test_verify_source_surfaces_ingest_errors(monkeypatch):
    register_source(name="X", url="https://example.com/rss",
                    source_id="v2", check_public=False)

    from newsforge.sources.engine import IngestResult

    async def _bad_ingest(src):
        return IngestResult(errors=["fetch/parse error for v2: boom"])

    monkeypatch.setattr(cli_mod, "ingest_source", _bad_ingest)
    with pytest.raises(CliError, match="boom"):
        verify_source("v2")


# --------------------------------------------------------------------------- #
# main() integration (argparse -> registration -> output/exit codes)
# --------------------------------------------------------------------------- #
def test_main_register_source_success(capsys):
    code = main([
        "register-source", "--name", "Test VO", "--url", "https://example.com/rss",
        "--source-id", "vo-test", "--type", "OFFICIAL", "--tier", "TIER_1",
        "--no-public-check",
    ])
    assert code == 0
    out = capsys.readouterr().out
    assert "SOURCE_CREATED source_id=vo-test" in out
    assert _find("vo-test") is not None


def test_main_register_source_validation_failure(capsys):
    code = main([
        "register-source", "--name", "X", "--url", "ftp://evil.example/x",
    ])
    assert code == 1
    out = capsys.readouterr().out
    assert "SOURCE_REGISTER_FAILED" in out
    assert "refusing non-http(s) scheme" in out


def test_main_verify_flag_plumbs_ingest(capsys, monkeypatch):
    from newsforge.sources.engine import IngestResult

    async def _real_ingest(src):
        return IngestResult(added=3, skipped_dupe=0)

    monkeypatch.setattr(cli_mod, "ingest_source", _real_ingest)
    code = main([
        "register-source", "--name", "X", "--url", "https://example.com/rss",
        "--no-public-check", "--verify",
    ])
    assert code == 0
    out = capsys.readouterr().out
    assert "SOURCE_CREATED" in out
    assert "SOURCE_VERIFY_OK added=3 skipped=0" in out
    with get_session() as s:
        assert s.query(source_items).count() == 0  # no rows unless ingest really ran


def test_main_verify_failure_returns_nonzero(capsys, monkeypatch):
    from newsforge.sources.engine import IngestResult

    async def _bad_ingest(src):
        return IngestResult(errors=["network down"])

    monkeypatch.setattr(cli_mod, "ingest_source", _bad_ingest)
    code = main([
        "register-source", "--name", "X", "--url", "https://example.com/rss",
        "--no-public-check", "--verify",
    ])
    assert code == 1
    assert "SOURCE_VERIFY_FAILED network down" in capsys.readouterr().out


def test_build_parser_lists_enum_choices():
    parser = build_parser()
    args = parser.parse_args(["register-source", "--name", "N", "--url", "https://x"])
    assert args.type in {"RSS", "API", "OFFICIAL", "SCIENTIFIC", "PRESS_RELEASE", "WEBSITE"}
    assert args.tier in {"TIER_1", "TIER_2", "TIER_3", "TIER_4"}
    assert args.trust_score == 50
    assert args.language == "es"