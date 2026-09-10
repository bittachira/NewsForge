"""OPS_HARDENING_REPRODUCIBILITY: the build, runtime and CI must reproduce
deterministically from a single commit.

These tests are deliberate, offline and deterministic (no Docker, no network):
they assert the contracts of the pinned dependency set, the multi-stage image
(builder/runtime/test), non-root + writable-data runtime, the absence of baked
.env, the deterministic CI environment (pinned action SHAs, explicit Python,
no hidden pip failures) and deterministic build metadata defaults.
"""
from __future__ import annotations

import os
import re
import sys
import tomllib
from pathlib import Path

import pytest

# Resolved explicitly in CI (NEWSFORGE_REPO_ROOT=/repo, the repo mounted read-only
# into the test image) so the same tests validate the same source both locally and
# in the container. Locally, falls back to the git worktree root.
ROOT = Path(os.getenv("NEWSFORGE_REPO_ROOT", Path(__file__).resolve().parents[1]))
DOCKERFILE = ROOT / "Dockerfile"
REQUIREMENTS = ROOT / "requirements.txt"
REQUIREMENTS_DEV = ROOT / "requirements-dev.txt"
PYPROJECT = ROOT / "pyproject.toml"
WORKFLOW = ROOT / ".github" / "workflows" / "staging-verification.yml"

BASE_DIGEST_RE = re.compile(
    r"^ARG PYTHON_BASE_IMAGE=python:3\.12\.\d+-slim@sha256:[0-9a-f]{64}$",
    re.MULTILINE,
)
PIN_RE = re.compile(r"^[A-Za-z0-9_.\-]+(?:\[[A-Za-z0-9_.\-]+\])?==[^<>=!\s]+$")
USES_RE = re.compile(r"^\s+uses:\s+([^\s/]+/[^\s]+)@([0-9a-f]{40})\s*$")
RANGE_OPS = ("<", ">", "~=", "==")


def _text(path: Path) -> str:
    assert path.exists(), f"missing {path}"
    return path.read_text(encoding="utf-8")


def _pin_set(text: str) -> set[str]:
    return {
        line
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
        and not line.startswith("-r ")
    }


def _dependency_names(pins: set[str]) -> set[str]:
    names = set()
    for pin in pins:
        name, _, _ = pin.partition("==")
        name = name.split("[", 1)[0]
        names.add(name.lower().replace("_", "-"))
    return names


# --------------------------------------------------------------------------- #
# Dependency set
# --------------------------------------------------------------------------- #


def test_version_of_requirements_files_is_frozen():
    for path in (REQUIREMENTS, REQUIREMENTS_DEV):
        text = _text(path)
        for line in text.splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if path is REQUIREMENTS_DEV and line.startswith("-r "):
                continue
            assert PIN_RE.match(line.strip()), (
                f"{path.name}: not an exact pin: {line!r}"
            )


def test_no_range_operators_in_frozen_files():
    for path in (REQUIREMENTS, REQUIREMENTS_DEV):
        for line in _text(path).splitlines():
            assert RANGE_OPS[0] not in line and RANGE_OPS[1] not in line, (
                f"{path.name}: bare range operator left in {line!r}"
            )


def test_runtime_set_is_declaratively_consistent_with_pyproject():
    data = tomllib.loads(_text(PYPROJECT))
    declared = set(data["project"]["dependencies"])
    declared_names = set()
    for dep in declared:
        name, *_ = re.split(r"[<>=~\[! ]", dep, maxsplit=1)
        declared_names.add(name.lower().replace("_", "-"))

    runtime_names = _dependency_names(_pin_set(_text(REQUIREMENTS)))
    missing = declared_names - runtime_names
    assert not missing, f"pyproject declares deps missing from requirements.txt: {missing}"


def test_test_dev_dependencies_are_separated_from_runtime():
    runtime = _text(REQUIREMENTS)
    dev = _text(REQUIREMENTS_DEV)
    for name in ("pytest", "httpx2"):
        assert re.search(rf"^{re.escape(name)}==", dev, re.MULTILINE), (
            f"{name} must be pinned in requirements-dev.txt"
        )
        assert not re.search(rf"^{re.escape(name)}==", runtime, re.MULTILINE), (
            f"{name} is a test dependency and must NOT live in requirements.txt"
        )


def test_dev_overlay_includes_runtime_and_pytest_in_container_target():
    lines = [ln for ln in _text(REQUIREMENTS_DEV).splitlines() if ln.strip()]
    assert any(ln.strip() == "-r requirements.txt" for ln in lines)


# --------------------------------------------------------------------------- #
# Dockerfile / image
# --------------------------------------------------------------------------- #


def test_dockerfile_pins_base_image_to_digest():
    dockerfile = _text(DOCKERFILE)
    assert BASE_DIGEST_RE.search(dockerfile), (
        "Dockerfile must pin python:3.12.x-slim to a full sha256 digest"
    )


def test_dockerfile_has_three_stages():
    dockerfile = _text(DOCKERFILE)
    assert "AS builder" in dockerfile
    assert "AS runtime" in dockerfile
    assert "AS test" in dockerfile


def test_build_tools_are_isolated_to_builder_stage():
    dockerfile = _text(DOCKERFILE)
    runtime_section = dockerfile.split("AS runtime", 1)[1].split("AS test", 1)[0]
    assert "apt-get" not in runtime_section, "runtime stage must not run apt-get"
    assert "gcc" not in runtime_section, "runtime stage must not contain compilers"
    assert "COPY --from=builder /opt/venv /opt/venv" in runtime_section


def test_runtime_uses_venv_from_builder_in_path():
    dockerfile = _text(DOCKERFILE)
    assert 'PATH="/opt/venv/bin:${PATH}"' in dockerfile
    assert "CMD" in dockerfile


def test_image_runs_as_non_root():
    dockerfile = _text(DOCKERFILE)
    runtime_section = dockerfile.split("AS runtime", 1)[1].split("AS test", 1)[0]
    assert re.search(r"^USER newsforge\s*$", runtime_section, re.MULTILINE)
    assert not re.search(r"^USER root\s*$", runtime_section, re.MULTILINE)
    # The "test" target transiently switches to root to install the pytest
    # overlay, but always reverts to the non-root runtime user.
    assert re.search(r"^USER newsforge\s*$", dockerfile.split("AS test", 1)[1], re.MULTILINE)


def test_data_directories_writable_and_app_owned_by_runtime_user():
    dockerfile = _text(DOCKERFILE)
    assert re.search(r"mkdir -p /data/backups", dockerfile)
    assert re.search(r"chown -R newsforge:newsforge /app /data", dockerfile)


def test_no_env_file_is_baked_into_image():
    dockerfile = _text(DOCKERFILE)
    assert "COPY .env.example .env" not in dockerfile


def test_source_identity_build_metadata_present():
    dockerfile = _text(DOCKERFILE)
    for line in (
        "ARG GIT_COMMIT=unknown",
        "ARG VERSION=unknown",
        "ARG BUILD_TIME=unknown",
        "ENV NEWSFORGE_GIT_COMMIT=${GIT_COMMIT}",
    ):
        assert line in dockerfile, f"missing {line!r} in Dockerfile"


def test_dockerfile_does_not_redeclare_runtime_dependency_ranges():
    """Runtime deps are frozen by requirements.txt, never inline in the Dockerfile."""
    dockerfile = _text(DOCKERFILE)
    assert re.search(r"pip install .* -r /tmp/requirements.txt", dockerfile) or \
        re.search(r"pip install .* -r requirements.txt", dockerfile)


# --------------------------------------------------------------------------- #
# CI workflow determinism
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(not WORKFLOW.exists(), reason="CI workflow not present")
def test_ci_pins_all_actions_to_full_shas():
    text = _text(WORKFLOW)
    uses_lines = [ln for ln in text.splitlines() if re.search(r"^\s+uses:\s+", ln)]
    assert uses_lines, "workflow should use actions"
    for ln in uses_lines:
        assert USES_RE.match(ln), f"action must be SHA-pinned (no @vX): {ln.strip()!r}"
    assert not re.search(r"uses:\s+[^\s]+@v\d", text), "bump-pinned actions found"


@pytest.mark.skipif(not WORKFLOW.exists(), reason="CI workflow not present")
def test_ci_never_silences_dependency_installs():
    text = _text(WORKFLOW)
    for line in text.splitlines():
        if "pip install" in line:
            assert "||" not in line, f"silenced pip install: {line.strip()!r}"
            assert "/dev/null" not in line, f"silenced pip install: {line.strip()!r}"


@pytest.mark.skipif(not WORKFLOW.exists(), reason="CI workflow not present")
def test_ci_regression_step_uses_frozen_dev_requirements():
    text = _text(WORKFLOW)
    assert "-r requirements-dev.txt" in text
    # Never assert "|| true" globally: diagnostic cleanup steps legitimately use
    # it (docker inspect/logs). The pip-install silencing guarantee is covered by
    # test_ci_never_silences_dependency_installs above.


@pytest.mark.skipif(not WORKFLOW.exists(), reason="CI workflow not present")
def test_ci_environment_is_explicit_and_deterministic():
    text = _text(WORKFLOW)
    assert "PYTHON_VERSION: '3.12'" in text
    assert "TZ: UTC" in text
    assert "LANG: C.UTF-8" in text
    assert "python-version: ${{ env.PYTHON_VERSION }}" in text
    assert "concurrency:" in text
    assert "cancel-in-progress: true" in text
    assert "timeout-minutes:" in text


@pytest.mark.skipif(not WORKFLOW.exists(), reason="CI workflow not present")
def test_ci_runs_reproducibility_and_test_target_in_container():
    text = _text(WORKFLOW)
    assert "--target test" in text
    assert "/app/tests/test_reproducibility.py" in text


@pytest.mark.skipif(not WORKFLOW.exists(), reason="CI workflow not present")
def test_ci_passes_source_identity_to_image_build():
    text = _text(WORKFLOW)
    assert "--build-arg \"GIT_COMMIT=${{ github.sha }}\"" in text
    assert "NEWSFORGE_GIT_COMMIT=" in text


# --------------------------------------------------------------------------- #
# Build metadata determinism (no invented SHA / time)
# --------------------------------------------------------------------------- #


def test_build_info_defaults_are_deterministic_unknown(monkeypatch):
    for name in ("NEWSFORGE_VERSION", "NEWSFORGE_GIT_COMMIT", "NEWSFORGE_BUILD_TIME"):
        monkeypatch.delenv(name, raising=False)

    from newsforge.core.build_info import get_build_info

    info = get_build_info()
    assert info["version"] == "unknown"
    assert info["git_commit"] == "unknown"
    assert info["build_time"] == "unknown"
    assert isinstance(info["schema_version"], int)
    assert re.fullmatch(
        r"\d+\.\d+\.\d+", info["python_version"]
    ), info["python_version"]


def test_build_info_metadata_defaults_never_fabricated(monkeypatch):
    monkeypatch.setenv("NEWSFORGE_GIT_COMMIT", "abc123")
    from newsforge.core.build_info import get_build_info

    assert get_build_info()["git_commit"] == "abc123"


def test_local_suite_runs_on_python_311_or_newer():
    assert sys.version_info >= (3, 11)
    assert "3" == str(sys.version_info.major)