"""Synthetic institution/roster/exam seed script — Prompt 11 (production
qualification). Creates one exam definition, one published revision, and one
RELEASED run enrolling N synthetic student uids, using the exact same
`ExamCommandService` the faculty routes use (never hand-written Firestore
documents), so every state-machine invariant (draft -> published -> released)
is honored for real rather than faked.

This script has NO emulator-specific logic — it writes to whatever Firestore
project the current credentials/environment point at:

  * Real staging: set FIREBASE_SERVICE_ACCOUNT_JSON (or _PATH) +
    FIREBASE_PROJECT_ID as usual, then run without --dry-run.
  * Local rehearsal only (no real GCP project needed): additionally set
    FIRESTORE_EMULATOR_HOST before running, pointed at a locally-running
    `gcloud emulators firestore start` instance. See
    docs/exams/production-qualification-report.md for the exact invocation
    used to validate this script in this phase, and its documented limits
    (an emulator is not a substitute for real Firestore latency/IAM/quota
    behavior).

Deliberately does NOT mint any auth tokens for the synthetic students — that
is a load-testing-environment concern (see exam_load_test.py's
--emulator-insecure-tokens, which only ever works against an emulator-backed
target). Keeping this script token-free means it stays honestly usable
as-is against a real staging project, where tokens can only come from a
real sign-in flow.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from uuid import uuid4

root = Path(__file__).resolve().parents[1]
sys.path.append(str(root))

from app.core.firebase import exam_db as db  # noqa: E402
from app.domain.exams.clock import system_clock  # noqa: E402
from app.domain.exams.commands import ExamCommandService  # noqa: E402
from app.domain.exams.policy import ExamPolicy  # noqa: E402


def build_synthetic_uids(count: int, prefix: str) -> list[str]:
    return [f"{prefix}-{i:04d}" for i in range(1, count + 1)]


def seed(
    *,
    faculty_uid: str,
    title: str,
    student_count: int,
    student_prefix: str,
    duration_minutes: int,
    warning_threshold: int,
    dry_run: bool,
    slot_count: int = 1,
) -> dict:
    uids = build_synthetic_uids(student_count, student_prefix)
    plan = {
        "title": title,
        "faculty_uid": faculty_uid,
        "student_count": student_count,
        "student_uids_sample": uids[:5],
        "duration_minutes": duration_minutes,
        "warning_threshold": warning_threshold,
        "slot_count": slot_count,
    }
    if dry_run:
        return {"dry_run": True, "plan": plan}

    service = ExamCommandService(db, clock=system_clock)
    definition = service.create_definition(title=title, owner_uid=faculty_uid)
    revision = service.publish_revision(definition_id=definition.id, slot_count=slot_count, published_by=faculty_uid)
    policy = ExamPolicy(duration_minutes=duration_minutes, warning_threshold=warning_threshold)
    run = service.create_run(
        definition_id=definition.id, revision=revision, policy=policy, enrolled_uids=uids
    )
    service.execute(
        run_id=run.id, command="publish", expected_version=1, idempotency_key=f"seed-publish-{uuid4().hex}"
    )
    service.execute(
        run_id=run.id, command="release", expected_version=2, idempotency_key=f"seed-release-{uuid4().hex}"
    )

    return {
        "dry_run": False,
        "definition_id": definition.id,
        "revision_id": revision.id,
        "run_id": run.id,
        "student_uids": uids,
        "duration_minutes": duration_minutes,
        "slot_count": slot_count,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Seed a synthetic institution/exam/roster for staging rehearsal or local emulator validation."
    )
    parser.add_argument("--faculty-uid", default="staging-faculty-1", help="Synthetic owning faculty uid")
    parser.add_argument("--title", default="Staging Qualification Exam")
    parser.add_argument("--student-count", type=int, default=50, help="Number of synthetic enrolled students")
    parser.add_argument("--student-prefix", default="staging-student", help="Synthetic student uid prefix")
    parser.add_argument("--duration-minutes", type=int, default=60)
    parser.add_argument("--warning-threshold", type=int, default=3)
    parser.add_argument("--slot-count", type=int, default=1, help="Number of questions (slots) in the exam")
    parser.add_argument(
        "--dry-run", action="store_true", help="Print the plan without writing anything to Firestore"
    )
    parser.add_argument("--output-file", default=None, help="Optional path to write the seed result JSON")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = seed(
        faculty_uid=args.faculty_uid,
        title=args.title,
        student_count=max(args.student_count, 1),
        student_prefix=args.student_prefix,
        duration_minutes=max(args.duration_minutes, 1),
        warning_threshold=max(args.warning_threshold, 1),
        dry_run=args.dry_run,
        slot_count=max(args.slot_count, 1),
    )
    if args.output_file:
        output_path = Path(args.output_file)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
