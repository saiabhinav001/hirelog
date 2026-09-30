"""Firestore repositories for the judge domain. Private collections — no
client rule grants them; covered by the default-deny catch-all.
"""

from __future__ import annotations

from typing import Optional

from firebase_admin import firestore

from app.domain.judge.models import CodeSubmission, GradingResult, McqGradeResult, ShortAnswerReview

SUBMISSIONS_COLLECTION = "judge_submissions"
GRADING_RESULTS_COLLECTION = "judge_grading_results"
MCQ_RESULTS_COLLECTION = "judge_mcq_results"
SHORT_ANSWER_REVIEWS_COLLECTION = "judge_short_answer_reviews"


def _model_dict(model) -> dict:
    return model.model_dump(mode="json")


class SubmissionRepository:
    def __init__(self, db) -> None:
        self._db = db

    def _ref(self, submission_id: str):
        return self._db.collection(SUBMISSIONS_COLLECTION).document(submission_id)

    def create(self, submission: CodeSubmission) -> None:
        self._ref(submission.id).set(_model_dict(submission))

    def get(self, submission_id: str) -> Optional[CodeSubmission]:
        snap = self._ref(submission_id).get()
        if not snap.exists:
            return None
        return CodeSubmission.model_validate({**(snap.to_dict() or {}), "id": snap.id})

    def update(self, submission_id: str, update: dict) -> None:
        self._ref(submission_id).update(update)

    def list_pending_or_polling(self, *, limit: int) -> list[CodeSubmission]:
        query = self._db.collection(SUBMISSIONS_COLLECTION).where(
            filter=firestore.FieldFilter("status", "in", ["submitted", "polling"])
        ).limit(limit)
        return [CodeSubmission.model_validate({**(d.to_dict() or {}), "id": d.id}) for d in query.stream()]

    def list_infra_errors(self, *, limit: int) -> list[CodeSubmission]:
        query = self._db.collection(SUBMISSIONS_COLLECTION).where(
            filter=firestore.FieldFilter("status", "==", "infra_error")
        ).limit(limit)
        return [CodeSubmission.model_validate({**(d.to_dict() or {}), "id": d.id}) for d in query.stream()]


class GradingResultRepository:
    def __init__(self, db) -> None:
        self._db = db

    def create(self, result: GradingResult) -> None:
        self._db.collection(GRADING_RESULTS_COLLECTION).document(result.id).set(_model_dict(result))

    def get(self, result_id: str) -> Optional[GradingResult]:
        snap = self._db.collection(GRADING_RESULTS_COLLECTION).document(result_id).get()
        if not snap.exists:
            return None
        return GradingResult.model_validate({**(snap.to_dict() or {}), "id": snap.id})

    def latest_for_submission(self, submission_id: str) -> Optional[GradingResult]:
        query = (
            self._db.collection(GRADING_RESULTS_COLLECTION)
            .where(filter=firestore.FieldFilter("submission_id", "==", submission_id))
        )
        results = [GradingResult.model_validate({**(d.to_dict() or {}), "id": d.id}) for d in query.stream()]
        if not results:
            return None
        return max(results, key=lambda r: r.version)


class McqResultRepository:
    def __init__(self, db) -> None:
        self._db = db

    def _doc_id(self, run_id: str, uid: str, question_id: str) -> str:
        return f"{run_id}__{uid}__{question_id}"

    def save(self, result: McqGradeResult) -> None:
        self._db.collection(MCQ_RESULTS_COLLECTION).document(
            self._doc_id(result.run_id, result.uid, result.question_id)
        ).set(_model_dict(result))


class ShortAnswerReviewRepository:
    def __init__(self, db) -> None:
        self._db = db

    def _ref(self, review_id: str):
        return self._db.collection(SHORT_ANSWER_REVIEWS_COLLECTION).document(review_id)

    def create(self, review: ShortAnswerReview) -> None:
        self._ref(review.id).set(_model_dict(review))

    def get(self, review_id: str) -> Optional[ShortAnswerReview]:
        snap = self._ref(review_id).get()
        if not snap.exists:
            return None
        return ShortAnswerReview.model_validate({**(snap.to_dict() or {}), "id": snap.id})

    def update(self, review_id: str, update: dict) -> None:
        self._ref(review_id).update(update)

    def list_pending(self, *, limit: int = 100) -> list[ShortAnswerReview]:
        query = (
            self._db.collection(SHORT_ANSWER_REVIEWS_COLLECTION)
            .where(filter=firestore.FieldFilter("status", "==", "pending"))
            .limit(limit)
        )
        return [ShortAnswerReview.model_validate({**(d.to_dict() or {}), "id": d.id}) for d in query.stream()]
