"""Approved-source review lifecycle — only an explicit faculty review can
move a source to approved/rejected; nothing in the ingestion pipeline itself
can set review_status."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.domain.kt.models import ApprovedSource, RightsEvidence, SourceReviewStatus
from app.domain.kt.source_registry import decide_approve_source, decide_reject_source, is_citable

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _pending_source(**overrides) -> ApprovedSource:
    defaults = dict(id="src-1", kind="upload", upload_id="up-1", created_at=NOW, updated_at=NOW)
    defaults.update(overrides)
    return ApprovedSource(**defaults)


def test_approve_pending_source_attaches_rights_evidence() -> None:
    evidence = RightsEvidence(statement="Faculty-authored lecture notes", attested_by="fac-1", attested_at=NOW)
    updated = decide_approve_source(_pending_source(), reviewer="fac-1", rights_evidence=evidence, now=NOW)
    assert updated.review_status == SourceReviewStatus.APPROVED
    assert updated.rights_evidence.statement == "Faculty-authored lecture notes"
    assert is_citable(updated)


def test_reject_pending_source_records_reason() -> None:
    updated = decide_reject_source(_pending_source(), reviewer="fac-1", reason="no rights evidence", now=NOW)
    assert updated.review_status == SourceReviewStatus.REJECTED
    assert updated.rejection_reason == "no rights evidence"
    assert not is_citable(updated)


def test_cannot_re_review_an_already_approved_source() -> None:
    evidence = RightsEvidence(statement="ok", attested_by="fac-1", attested_at=NOW)
    approved = decide_approve_source(_pending_source(), reviewer="fac-1", rights_evidence=evidence, now=NOW)
    with pytest.raises(ValueError):
        decide_approve_source(approved, reviewer="fac-2", rights_evidence=evidence, now=NOW)


def test_cannot_review_an_already_rejected_source() -> None:
    rejected = decide_reject_source(_pending_source(), reviewer="fac-1", reason="bad", now=NOW)
    with pytest.raises(ValueError):
        decide_reject_source(rejected, reviewer="fac-2", reason="bad again", now=NOW)


def test_pending_source_is_not_citable() -> None:
    assert not is_citable(_pending_source())
    assert not is_citable(None)
