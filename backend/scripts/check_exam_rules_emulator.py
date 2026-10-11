"""Checks, against the local Firebase emulators, that the repository's firestore.rules keep every
exam and institution collection unreachable for browser clients.

Start the emulators first (Auth on 9099 and Firestore on 8080, loaded with ./firestore.rules), then:

    python scripts/check_exam_rules_emulator.py [--project demo-hirelog-exams]

It only talks to localhost and never needs credentials. Exits non-zero if any check fails.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Optional


def _request(method: str, url: str, token: Optional[str] = None, body: Optional[dict] = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return exc.code, {}


def _string_fields(**values: str) -> dict:
    return {"fields": {key: {"stringValue": value} for key, value in values.items()}}


def run(project: str, auth_host: str, firestore_host: str) -> list[tuple[str, bool, int, int]]:
    status, signup = _request(
        "POST",
        f"http://{auth_host}/identitytoolkit.googleapis.com/v1/accounts:signUp?key=fake",
        body={"email": "rules-check@example.edu", "password": "password123", "returnSecureToken": True},
    )
    if status != 200:  # the account already exists from an earlier run
        status, signup = _request(
            "POST",
            f"http://{auth_host}/identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key=fake",
            body={"email": "rules-check@example.edu", "password": "password123", "returnSecureToken": True},
        )
    token, uid = signup["idToken"], signup["localId"]
    docs = f"http://{firestore_host}/v1/projects/{project}/databases/(default)/documents"

    cases = [
        ("anonymous read of exam_runs", "GET", f"{docs}/exam_runs/r1", None, None),
        ("signed-in read of exam_runs", "GET", f"{docs}/exam_runs/r1", token, None),
        ("signed-in list of institution_memberships", "GET", f"{docs}/institution_memberships", token, None),
        ("signed-in read of institution_otp_challenges", "GET", f"{docs}/institution_otp_challenges/x", token, None),
        ("signed-in read of institutions", "GET", f"{docs}/institutions/i1", token, None),
        (
            "signed-in write to exam_runs",
            "PATCH",
            f"{docs}/exam_runs/r1?updateMask.fieldPaths=status",
            token,
            _string_fields(status="released"),
        ),
        (
            "signed-in write to exam_outbox",
            "PATCH",
            f"{docs}/exam_outbox/o1?updateMask.fieldPaths=x",
            token,
            _string_fields(x="y"),
        ),
        (
            "signed-in write to their own attempt",
            "PATCH",
            f"{docs}/exam_runs/r1/attempts/{uid}?updateMask.fieldPaths=status",
            token,
            _string_fields(status="submitted"),
        ),
        (
            "signed-in write to a membership",
            "PATCH",
            f"{docs}/institution_memberships/i1::s1?updateMask.fieldPaths=role_scope",
            token,
            _string_fields(role_scope="admin"),
        ),
        ("signed-in read of an unknown collection", "GET", f"{docs}/something_new/doc", token, None),
        ("signed-in read of another user's profile", "GET", f"{docs}/users/someone-else", token, None),
    ]
    results = []
    for name, method, url, case_token, body in cases:
        got, _ = _request(method, url, case_token, body)
        results.append((name, got == 403, 403, got))
    return results


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Check firestore.rules against the local emulators")
    parser.add_argument("--project", default="demo-hirelog-exams")
    parser.add_argument("--auth-host", default="127.0.0.1:9099")
    parser.add_argument("--firestore-host", default="127.0.0.1:8080")
    args = parser.parse_args(argv)

    results = run(args.project, args.auth_host, args.firestore_host)
    for name, ok, expected, got in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}  (expected {expected}, got {got})")
    failed = [name for name, ok, _, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} rule checks behaved as expected")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
