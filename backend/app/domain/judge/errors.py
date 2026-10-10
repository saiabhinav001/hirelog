"""Domain exceptions for the coding judge. Separate from FastAPI so the pure
decision/aggregation layer stays framework-free."""

from __future__ import annotations


class JudgeDomainError(Exception):
    code: str = "judge_domain_error"


class RuntimeNotAllowedError(JudgeDomainError):
    code = "runtime_not_allowed"


class QuotaExceededError(JudgeDomainError):
    code = "quota_exceeded"


class ResultOwnershipError(JudgeDomainError):
    """A provider result/callback does not correspond to any outstanding
    submission this server issued — reject it rather than trust it blindly
    (contract: "do not assume signed provider callbacks")."""

    code = "result_ownership_mismatch"


class SubmissionNotFoundError(JudgeDomainError):
    code = "submission_not_found"


class JudgeUnavailableError(JudgeDomainError):
    """The judge is not configured or cannot be reached. Never a score: the work stays pending or is reported as unavailable."""

    code = "judge_unavailable"


class JudgeBusyError(JudgeDomainError):
    """Too much work is already queued for the judge; a student-initiated run is refused so the queue stays bounded."""

    code = "judge_busy"


class ScoreOutOfRangeError(JudgeDomainError):
    """A reviewer-entered score outside 0..max_score."""

    code = "score_out_of_range"
