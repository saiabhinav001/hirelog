"""Black-box smoke checks for a deployed (or local) HireLog Exams API.

Standard library only, read-only, no credentials: it proves the service is up, that
the storage boundary is on, that CORS admits only the configured web origin, and that
protected routes refuse anonymous and forged callers.

    python scripts/smoke_exams_api.py --base-url https://<owner>-hirelog-exams-staging.hf.space \
        --allowed-origin https://<staging-web>.vercel.app --expect-ready
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Optional

FOREIGN_ORIGIN = "https://evil.example"


def request(base_url: str, path: str, method: str = "GET", headers: Optional[dict] = None, timeout: float = 20.0):
    req = urllib.request.Request(f"{base_url}{path}", method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def _json(body: bytes) -> dict:
    try:
        return json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}


def run_checks(base_url: str, allowed_origin: str, expect_ready: bool) -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []

    def record(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))

    status, _, body = request(base_url, "/health/live")
    record("liveness answers 200", status == 200 and _json(body).get("status") == "alive", f"status={status}")

    status, _, body = request(base_url, "/health/ready")
    ready = _json(body)
    boundary = (ready.get("storage_boundary") or {}).get("enforced")
    record("storage boundary is enforced", boundary is True, f"enforced={boundary}")
    record("exams module is enabled", ready.get("exams_enabled") is True, f"exams_enabled={ready.get('exams_enabled')}")
    if expect_ready:
        record("readiness is 200 (Firestore reachable)", status == 200, f"status={status} firestore={ready.get('firestore')}")

    if allowed_origin:
        status, headers, _ = request(
            base_url,
            "/api/exams/v1/me",
            method="OPTIONS",
            headers={"Origin": allowed_origin, "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "authorization"},
        )
        lowered = {k.lower(): v for k, v in headers.items()}
        record(
            "CORS admits the staging web origin",
            lowered.get("access-control-allow-origin") == allowed_origin,
            f"status={status} allow-origin={lowered.get('access-control-allow-origin')}",
        )
    status, headers, _ = request(
        base_url,
        "/api/exams/v1/me",
        method="OPTIONS",
        headers={"Origin": FOREIGN_ORIGIN, "Access-Control-Request-Method": "GET"},
    )
    lowered = {k.lower(): v for k, v in headers.items()}
    allow_origin = lowered.get("access-control-allow-origin")
    record("CORS refuses a foreign origin", allow_origin not in (FOREIGN_ORIGIN, "*"), f"allow-origin={allow_origin}")

    for path in ("/api/exams/v1/me", "/api/exams/v1/faculty/definitions", "/api/exams/v1/student/runs"):
        status, _, _ = request(base_url, path)
        record(f"anonymous call to {path} is refused", status in (401, 403), f"status={status}")
    status, _, _ = request(base_url, "/api/exams/v1/me", headers={"Authorization": "Bearer not-a-real-token"})
    record("a forged bearer token is refused", status in (401, 403), f"status={status}")

    status, _, _ = request(base_url, "/api/experiences")
    record("the legacy API is not served here", status == 404, f"status={status}")
    return results


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke-test an exam API deployment")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--allowed-origin", default="", help="The staging web origin CORS must admit")
    parser.add_argument("--expect-ready", action="store_true", help="Also require /health/ready to be 200")
    args = parser.parse_args(argv)

    results = run_checks(args.base_url.rstrip("/"), args.allowed_origin.rstrip("/"), args.expect_ready)
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not ok else ""))
    failed = [name for name, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
