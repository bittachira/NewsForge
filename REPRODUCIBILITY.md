# Reproducibility (OPS_HARDENING_REPRODUCIBILITY)

Build, runtime and CI for NewsForge are designed to reproduce deterministically
from a single Git commit. Nothing below claims byte-for-byte identical images;
it states what is guaranteed, how it is enforced, and what is honestly *not*
(yet) guaranteed.

## Deterministic inputs (guaranteed per commit)

| Input | Guarantee | Enforced by |
|---|---|---|
| Base image | `python:3.12.14-slim` pinned to a full `sha256` digest in `Dockerfile` | `tests/test_reproducibility.py` |
| Python version | `3.12` everywhere: container base, CI setup (explicit `PYTHON_VERSION`), local docs | workflow + `requirements*.txt` comments |
| Runtime dependencies | fully frozen `==` pins incl. transitive closure (`requirements.txt`) | tests (`test_version_of_requirements_files_is_frozen`) |
| Test/dev dependencies | separated into `requirements-dev.txt` (overlay `-r requirements.txt`); never installed into the runtime image | tests (`test_test_dev_dependencies_are_separated_from_runtime`) |
| Build tools | `gcc`/`libc6-dev` exist only in the `builder` stage; the `runtime` image ships no compiler | tests (`test_build_tools_are_isolated_to_builder_stage`) |
| Runtime user | non-root `newsforge`; `/data` (DB + backups) writable; no `.env` baked in | tests + CI steps |
| Source identity | `GIT_COMMIT` / `VERSION` / `BUILD_TIME` injected as build ARGs and surfaced via `NEWSFORGE_*` env + OCI labels; defaults are deterministic `"unknown"` (a SHA/time is never invented) | tests (`test_source_identity_build_metadata_present`, `test_build_info_defaults_are_deterministic_unknown`), CI "Verify image build identity metadata" step |
| CI actions | all third-party actions SHA-pinned by 40-hex commit (no `@vX` bumps) | tests (`test_ci_pins_all_actions_to_full_shas`) |
| CI installs | no silenced installs (no `pip install ... || true` / `/dev/null`); regression step installs the frozen `requirements-dev.txt` | tests |
| CI environment | explicit `TZ: UTC`, `LANG: C.UTF-8`, `PYTHON_VERSION: '3.12'`, job `timeout-minutes`, `concurrency` with cancel-in-progress, least-privilege `permissions` | workflow + tests |

`BUILD_TIME` is derived from the commit timestamp (`github.event.head_commit.timestamp`,
falling back to `"unknown"`), so even build metadata is a pure function of the
commit, not of wall-clock time.

## File layout

- `requirements.txt`        - frozen RUNTIME dependency set (used by the `builder` stage).
- `requirements-dev.txt`    - frozen TEST/DEV overlay (`pytest`, `httpx2`), used only by the CI `test` image target and local development.
- `pyproject.toml`          - human-readable ranges (spec) for runtime deps and local metadata.
- `Dockerfile`              - multi-stage: `builder` -> `runtime` -> `test`.
- `.github/workflows/staging-verification.yml` - deterministic CI.
- `tests/test_reproducibility.py` - offline regression net that locks all contracts above.

## How to reproduce

```sh
# 1. Build the runtime image (no build tools, non-root, /data writable)
docker build -t newsforge-mvp . \
  --build-arg GIT_COMMIT=<sha> --build-arg VERSION=0.1.0

# 2. Build the CI "test" image (runtime + pytest overlay)
docker build -t newsforge-mvp-test --target test \
  --build-arg GIT_COMMIT=<sha> --build-arg VERSION=0.1.0 .

# 3. Run the suite in the container (repo mounted read-only for the offline file asserts)
docker run --rm \
  -v "$PWD/tests:/app/tests:ro" -v "$PWD:/repo:ro" \
  -e NEWSFORGE_MOCK_AI=true -e NEWSFORGE_DB_PATH=/data/newsforge.db \
  -e NEWSFORGE_REPO_ROOT=/repo newsforge-mvp-test \
  python -m pytest /app/tests/... -q
```

Local test environment (Python 3.12 recommended to mirror the container):

```sh
python -m pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

## Known non-reproducible items (measured, not hidden)

- **Wheel content integrity**: versions are pinned, but wheels are not
  `--hash`-pinned, so a rebuilt wheel with the same version string (or a
  compromised index) would change runtime content without failing the build.
  Exact-order approach is pinned `==` + `--no-cache-dir`; hash pinning is a
  documented follow-up, not silently claimed.
- **By-layer image bytes**: layer and image IDs are stable for a given base
  digest + dependency set + source tree, but no tool enforces byte-for-byte image
  identity across builds yet.
- **Regeneration drift**: `requirements*.txt` are the source of truth for builds;
  re-running `pip` resolution today could pick newer versions. Regeneration is a
  deliberate, reviewable act, not an automatic CI step.