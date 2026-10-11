"""One-off dev script: creates (or reuses) a Firebase test-student account and
directly provisions an ACTIVE institution membership (role_scope=student) for
it, bypassing the OTP roster flow — the workaround docs/exams/user-guide.md
§2 recommends for local demo/testing, since students have no email-based
bootstrap the way faculty does via EXAM_FACULTY_EMAILS.

Usage (from backend/), with a throwaway account of your choosing:
    $env:DEMO_STUDENT_EMAIL = "you+demo@example.com"
    $env:DEMO_STUDENT_PASSWORD = "<choose a password>"
    .venv\\Scripts\\python.exe scripts\\seed_demo_student.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.append(str(root))

from firebase_admin import auth as firebase_auth  # noqa: E402

from app.core.firebase import db, initialize_firebase  # noqa: E402
from app.domain.institution.models import Institution, Membership, MembershipStatus, RoleScope  # noqa: E402
from app.domain.institution.repository import InstitutionRepository, MembershipRepository  # noqa: E402

STUDENT_EMAIL = os.environ.get("DEMO_STUDENT_EMAIL", "").strip()
STUDENT_PASSWORD = os.environ.get("DEMO_STUDENT_PASSWORD", "")
STUDENT_NAME = "Demo Student"
INSTITUTION_ID = "demo-university"
INSTITUTION_NAME = "Demo University"
IDENTITY_KEY = "DEMO-STU-0001"


def get_or_create_student_uid() -> str:
    try:
        user = firebase_auth.get_user_by_email(STUDENT_EMAIL)
        return user.uid
    except firebase_auth.UserNotFoundError:
        user = firebase_auth.create_user(email=STUDENT_EMAIL, password=STUDENT_PASSWORD, display_name=STUDENT_NAME)
        return user.uid


def get_or_create_institution() -> str:
    repo = InstitutionRepository(db)
    if repo.get(INSTITUTION_ID) is None:
        now = datetime.now(timezone.utc)
        repo.create(Institution(id=INSTITUTION_ID, name=INSTITUTION_NAME, created_at=now, updated_at=now))
    return INSTITUTION_ID


def upsert_student_membership(uid: str, institution_id: str) -> None:
    repo = MembershipRepository(db)
    now = datetime.now(timezone.utc)
    existing = repo.get(institution_id, IDENTITY_KEY)
    membership = Membership(
        institution_id=institution_id,
        identity_key=IDENTITY_KEY,
        contact_email=STUDENT_EMAIL,
        role_scope=RoleScope.STUDENT,
        status=MembershipStatus.ACTIVE,
        uid=uid,
        authorization_version=existing.authorization_version if existing else 1,
        created_at=existing.created_at if existing else now,
        updated_at=now,
    )
    repo.upsert(membership)


def main() -> None:
    if not STUDENT_EMAIL or not STUDENT_PASSWORD:
        sys.exit("Set DEMO_STUDENT_EMAIL and DEMO_STUDENT_PASSWORD in the environment first.")
    initialize_firebase()
    uid = get_or_create_student_uid()
    institution_id = get_or_create_institution()
    upsert_student_membership(uid, institution_id)
    print(f"uid={uid}")
    print(f"institution_id={institution_id}")
    print(f"email={STUDENT_EMAIL}")


if __name__ == "__main__":
    main()
