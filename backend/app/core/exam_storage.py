"""Write/read boundary for the exam process on a Firebase project it shares
with the legacy HireLog app.

Admin-SDK access bypasses Firestore Security Rules, so isolation has to be
enforced in code: with `EXAM_STORAGE_BOUNDARY=true` the exam process can only
open the collections listed here (never `users`, `interview_experiences`,
`practice_lists`, `metadata`, `search_index_tasks`, ...), optionally under a
namespace prefix, and cannot use collection-group/listing APIs at all.
"""

from __future__ import annotations

from typing import Any

EXAM_COLLECTIONS: frozenset[str] = frozenset(
    {
        # exam core
        "exam_definitions", "exam_runs", "exam_command_receipts", "exam_outbox", "exam_health",
        "exam_lockdown_profiles", "exam_lockdown_admission_log",
        # generation / knowledge transfer
        "exam_blueprints", "exam_variant_assignments", "generation_requests", "generation_candidates",
        "generation_malformed_candidates", "kt_uploads", "kt_approved_sources",
        # institution identity
        "institutions", "institution_memberships", "institution_roster_imports", "institution_otp_challenges",
        # judge
        "judge_submissions", "judge_grading_results", "judge_mcq_results", "judge_short_answer_reviews",
        # live control
        "live_tickets",
        # proctoring / evidence
        "proctoring_states", "proctoring_log", "proctoring_appeals", "evidence_snapshots",
        "evidence_upload_tickets", "evidence_view_tickets", "evidence_access_log",
        # gradebook
        "gradebook_releases", "gradebook_defective_questions", "gradebook_grade_appeals",
        "gradebook_export_jobs", "gradebook_export_tickets", "gradebook_export_access_log", "similarity_matches",
    }
)

# Collections that must never be reachable from the exam process.
PROTECTED_LEGACY_COLLECTIONS: frozenset[str] = frozenset(
    {"users", "interview_experiences", "practice_lists", "metadata", "search_index_tasks"}
)

_ALLOWED_CLIENT_ATTRS = frozenset({"collection", "transaction", "batch"})


class CollectionBoundaryError(PermissionError):
    """Raised when exam code reaches for storage outside its boundary."""


class ExamStorageBoundary:
    def __init__(self, client: Any, *, allowed: frozenset[str] = EXAM_COLLECTIONS, prefix: str = "") -> None:
        self._client = client
        self._allowed = allowed
        self._prefix = prefix

    def collection(self, name: str):
        if name not in self._allowed:
            raise CollectionBoundaryError(f"collection {name!r} is outside the exam storage boundary")
        return self._client.collection(f"{self._prefix}{name}")

    def transaction(self):
        return self._client.transaction()

    def batch(self):
        return self._client.batch()

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)
        raise CollectionBoundaryError(f"Firestore API {name!r} is not available inside the exam storage boundary")

    @property
    def boundary_prefix(self) -> str:
        return self._prefix
