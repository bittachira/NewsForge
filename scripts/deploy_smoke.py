#!/usr/bin/env python3
"""Post-deployment smoke test (PRODUCTION_SECRETS_AND_DEPLOYMENT §16).

DESIGN ONLY — this script is targeting-agnostic and intentionally never runs
against production automatically. It encodes the exact post-deployment contract:

  public  (no auth): /live, /ready, /health, /articles, /sitemap.xml, /feed.xml
  internal (auth):   /metrics (must require the admin token; fail-closed)

Usage (when a REAL deployment target exists):

    NEWSFORGE_SMOKE_BASE_URL=https://newsforge.example \\
    NEWSFORGE_ADMIN_TOKEN=<token> \\
    python scripts/deploy_smoke.py

Exit code 0 when every probe passes; non-zero with a summary otherwise. The admin
token (a secret) is consumed from the environment only and NEVER appears in
output. Run this AFTER the deployment smoke phase of a manual promotion — never
from CI against a production host.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

PUBLIC_ENDPOINTS = ("/live", "/ready", "/health", "/articles",
                    "/sitemap.xml", "/feed.xml")
REDACTED = "[REDACTED]"


def _get(url: str, token: str | None = None, timeout: float = 10.0):
    req = urllib.request.Request(url)
    if token:
        req.add_header("X-Admin-Token", token)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310 (operator-driven URL)
        raw = resp.read()
        return resp.getcode(), raw


def _redact(text: str, token: str | None) -> str:
    if token and token in text:
        text = text.replace(token, REDACTED)
    return text


def main() -> int:
    base = os.getenv("NEWSFORGE_SMOKE_BASE_URL", "").rstrip("/")
    token = os.getenv("NEWSFORGE_ADMIN_TOKEN")
    if not base:
        print("NEWSFORGE_SMOKE_BASE_URL is required (set it to the public URL).")
        return 2

    results: list[tuple[str, str]] = []
    for endpoint in PUBLIC_ENDPOINTS:
        url = f"{base}{endpoint}"
        try:
            code, body = _get(url)
            ok = code == 200
            results.append((f"{endpoint} HTTP {code}", "PASS" if ok else "FAIL"))
        except urllib.error.HTTPError as exc:
            results.append((f"{endpoint} HTTP {exc.code}", "FAIL"))
        except Exception as exc:  # noqa: BLE001 - probe summary keeps secrets out
            results.append((f"{endpoint} {type(exc).__name__}", "FAIL"))

    # Internal /metrics: fail-closed. Anonymous must be 403; authorized must be
    # 200 and must NOT contain the token.
    metrics_url = f"{base}/metrics"
    try:
        anon_code, _ = _get(metrics_url)
    except urllib.error.HTTPError as exc:
        anon_code = exc.code
    except Exception:  # noqa: BLE001
        anon_code = 0
    results.append((f"/metrics anonymous HTTP {anon_code}",
                    "PASS" if anon_code == 403 else "FAIL"))
    if token:
        try:
            code, body = _get(metrics_url, token=token)
            body_text = body.decode("utf-8", errors="replace")
            leaked = token in body_text
            results.append((f"/metrics authorized HTTP {code}"
                            + (" LEAK!" if leaked else ""),
                            "PASS" if code == 200 and not leaked else "FAIL"))
        except urllib.error.HTTPError as exc:
            results.append((f"/metrics authorized HTTP {exc.code}", "FAIL"))
        except Exception as exc:  # noqa: BLE001
            results.append((f"/metrics {type(exc).__name__}", "FAIL"))

    failed = [name for name in results if "FAIL" in name]
    for name in results:
        print(f"{'PASS' if 'PASS' in name[1] else 'FAIL'}  {name[0]}")
    print()
    if failed:
        print(f"{len(failed)} smoke check(s) FAILED — do not promote.")
        return 1
    print("All smoke checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())