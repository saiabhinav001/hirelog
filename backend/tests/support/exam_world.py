"""Scenario builder for HTTP-level exam tests: real routes, real repositories,
in-memory Firestore, fake Firebase token verification."""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Optional

from fastapi.testclient import TestClient

from app.domain.institution.models import Membership
from app.domain.institution.repository import MembershipRepository
from tests.support.fake_firestore import FakeFirestore

OPERATOR_EMAIL = "ops@platform.example"


class TokenIssuer:
    """Hands out opaque bearer tokens whose decoded claims the patched
    `verify_id_token` returns — the claims a real Firebase ID token carries."""

    def __init__(self) -> None:
        self._claims: dict[str, dict] = {}
        self._counter = itertools.count(1)

    def verify(self, token: str, *args, **kwargs) -> dict:
        claims = self._claims.get(token)
        if claims is None:
            raise ValueError("invalid test token")
        return dict(claims)

    def issue(
        self,
        uid: str,
        *,
        email: Optional[str] = None,
        email_verified: bool = True,
        institution_id: Optional[str] = None,
        authz_version: Optional[int] = None,
        auth_time: Optional[float] = None,
        provider: str = "google.com",
    ) -> str:
        import time

        token = f"test-token-{next(self._counter)}"
        claims: dict = {
            "uid": uid, "user_id": uid, "sub": uid, "email_verified": email_verified,
            "auth_time": auth_time if auth_time is not None else time.time(), "exp": time.time() + 3600,
            "firebase": {"sign_in_provider": provider},
        }
        if email is not None:
            claims["email"] = email
        if institution_id is not None:
            claims["exam_institution_id"] = institution_id
        if authz_version is not None:
            claims["exam_authz_version"] = authz_version
        self._claims[token] = claims
        return token

    @staticmethod
    def headers(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}


@dataclass
class World:
    client: TestClient
    db: FakeFirestore
    tokens: TokenIssuer
    _members: dict[tuple[str, str], str] = field(default_factory=dict)

    # ── tokens ──────────────────────────────────────────────────────────────
    def operator(self) -> dict[str, str]:
        return self.tokens.headers(self.tokens.issue("operator-1", email=OPERATOR_EMAIL, email_verified=True))

    def session_for(self, institution_id: str, identity_key: str) -> dict[str, str]:
        """An OTP-style session: custom-token provider, institution claim and
        the authorization version in force when it was minted."""
        member = self.member(institution_id, identity_key)
        token = self.tokens.issue(
            member.uid, institution_id=institution_id, authz_version=member.authorization_version,
            provider="custom", email=None,
        )
        return self.tokens.headers(token)

    # ── institutions / roster ───────────────────────────────────────────────
    def create_institution(self, name: str = "College A") -> str:
        response = self.client.post("/api/exams/v1/faculty/institutions", json={"name": name}, headers=self.operator())
        assert response.status_code == 200, response.text
        return response.json()["id"]

    def import_roster(self, institution_id: str, rows: list[dict], headers: Optional[dict] = None) -> dict:
        headers = headers or self.operator()
        base = f"/api/exams/v1/faculty/institutions/{institution_id}/roster"
        dry = self.client.post(f"{base}/dry-run", json={"rows": rows}, headers=headers)
        assert dry.status_code == 200, dry.text
        applied = self.client.post(f"{base}/{dry.json()['id']}/apply", json={"rows": rows}, headers=headers)
        assert applied.status_code == 200, applied.text
        return applied.json()

    def link_faculty_uid(self, institution_id: str, identity_key: str, uid: str) -> None:
        """First sign-in of a faculty/admin member provisions their uid."""
        member = self.member(institution_id, identity_key)
        MembershipRepository(self.db).upsert(member.model_copy(update={"uid": uid}))

    def member(self, institution_id: str, identity_key: str) -> Membership:
        found = MembershipRepository(self.db).get(institution_id, identity_key)
        assert found is not None, f"no membership {institution_id}/{identity_key}"
        return found

    # ── exams ───────────────────────────────────────────────────────────────
    def create_definition(self, headers: dict, title: str = "Midterm", institution_id: Optional[str] = None) -> dict:
        body = {"title": title, **({"institution_id": institution_id} if institution_id else {})}
        response = self.client.post("/api/exams/v1/faculty/definitions", json=body, headers=headers)
        assert response.status_code == 200, response.text
        return response.json()

    def publish_revision(self, headers: dict, definition_id: str, slot_count: int = 2) -> dict:
        response = self.client.post(
            f"/api/exams/v1/faculty/definitions/{definition_id}/publish", json={"slot_count": slot_count}, headers=headers
        )
        assert response.status_code == 200, response.text
        return response.json()

    def create_run(self, headers: dict, definition_id: str, revision_id: str, **enrolment) -> dict:
        body = {
            "definition_id": definition_id, "revision_id": revision_id,
            "policy": {"duration_minutes": 60, "warning_threshold": 3}, **enrolment,
        }
        response = self.client.post("/api/exams/v1/faculty/runs", json=body, headers=headers)
        assert response.status_code == 200, response.text
        return response.json()

    def command(self, headers: dict, run_id: str, command: str, expected_version: int) -> dict:
        response = self.client.post(
            f"/api/exams/v1/faculty/runs/{run_id}/commands/{command}",
            json={"expected_version": expected_version, "idempotency_key": f"{run_id}-{command}"}, headers=headers,
        )
        assert response.status_code == 200, response.text
        return response.json()

    def released_run(self, headers: dict, definition_id: str, revision_id: str, **enrolment) -> str:
        run = self.create_run(headers, definition_id, revision_id, **enrolment)
        self.command(headers, run["id"], "publish", run["version"])
        self.command(headers, run["id"], "release", run["version"] + 1)
        return run["id"]
