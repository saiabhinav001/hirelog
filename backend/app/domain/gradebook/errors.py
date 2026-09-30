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
