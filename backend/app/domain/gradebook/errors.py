"""Domain exceptions for gradebook, similarity, export, and feedback."""

from __future__ import annotations


class GradebookDomainError(Exception):
    code: str = "gradebook_domain_error"


class ResultsNotReleasedError(GradebookDomainError):
    """A student tried to view results before faculty released them."""

    code = "results_not_released"


class ExportTicketInvalidError(GradebookDomainError):
    """Expired, already-consumed, or scope-mismatched export download ticket."""

    code = "export_ticket_invalid"


class ExportJobNotReadyError(GradebookDomainError):
    """Download attempted before the export job finished (or it failed)."""

    code = "export_job_not_ready"


class ReleaseReasonRequiredError(GradebookDomainError):
    """A corrected re-release (version 2+) must say why."""

    code = "release_reason_required"


class ReleaseIncompleteError(GradebookDomainError):
    """Results were released while attempts were still live or answers still
    ungraded, without the releaser acknowledging that."""

    code = "release_incomplete"


class ExportExpiredError(GradebookDomainError):
    """The export's retention window has passed and its content was deleted."""

    code = "export_expired"


class GradeAppealNotAllowedError(GradebookDomainError):
    """A grade appeal that is not for a released result / a real question, or
    that duplicates a still-pending appeal."""

    code = "grade_appeal_not_allowed"
