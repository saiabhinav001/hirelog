"""Prompt 4 (ADR 0020): HTTP behaviour of the KT ingestion routes - upload/replace/retry/delete,
spoofed and oversized files, URL submission - with authorization replaced by one teacher and the
data layer on the in-memory Firestore. The worker step is driven explicitly so each state is visible."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.api import exams_dependencies as deps
from app.api import exams_scope as scope
from app.api.routes import exams_kt as routes
from app.core.config import settings
from app.core.rate_limit import SlidingWindowLimiter
from app.domain.kt import ingestion
from app.domain.kt.malware_scan import DeterministicFakeScanner, NullMalwareScanner
from app.domain.kt.models import ApprovedSource, SourceReviewStatus, UploadStatus
from app.domain.kt.storage import StorageNotConfiguredError
from app.domain.kt.url_resolver import FetchedResource, UnsafeUrlError
from app.domain.proctoring.evidence_storage import InMemoryEvidenceStorage
from app.exam_main import app
from exam_inmemory import audit_kinds, install_optimistic_transactions, FakeFirestore
from kt_fixtures import DOCX_TYPE, inproc_parse, make_docx, make_encrypted_pdf, make_pdf

BASE = "/api/exams/v1/faculty/kt"


class Api:
    def __init__(self, client: TestClient, db: FakeFirestore, storage: InMemoryEvidenceStorage) -> None:
        self.client, self.db, self.storage = client, db, storage

    def process(self, upload_id: str, attempt: int = 1) -> str:
        return routes._service().process(upload_id, attempt)

    def post_file(self, path: str, content: bytes, *, name: str, mime: str):
        return self.client.post(f"{BASE}{path}", files={"file": (name, content, mime)})

    def upload(self, content: bytes, *, name="notes.txt", mime="text/plain") -> str:
        response = self.post_file("/uploads", content, name=name, mime=mime)
        assert response.status_code == 202, response.text
        return response.json()["upload_id"]


@pytest.fixture
def api(monkeypatch):
    db = FakeFirestore(jitter=0)
    install_optimistic_transactions(monkeypatch, db)
    storage = InMemoryEvidenceStorage()
    user = {"uid": "teacher-1"}
    monkeypatch.setattr(routes, "db", db)
    monkeypatch.setattr(routes, "kt_storage", lambda: storage)
    monkeypatch.setattr(routes, "_scanner", lambda: DeterministicFakeScanner())
    monkeypatch.setattr(routes, "_upload_limiter", SlidingWindowLimiter(1000, 60, namespace="t_upload"))
    monkeypatch.setattr(routes, "_url_limiter", SlidingWindowLimiter(1000, 60, namespace="t_url"))
    monkeypatch.setattr(routes, "authorize_upload", lambda _user, upload_id: routes._uploads_repo().get(upload_id))
    monkeypatch.setattr(routes, "filter_accessible_sources", lambda _user, sources: list(sources))
    monkeypatch.setattr(ingestion, "parse_in_subprocess", lambda kind, content, **_: inproc_parse(kind, content))

    app.dependency_overrides[deps.require_exams_enabled] = lambda: None
    app.dependency_overrides[deps.require_exam_faculty] = lambda: user
    app.dependency_overrides[scope.require_upload_scope] = lambda upload_id: user
    app.dependency_overrides[scope.require_source_scope] = lambda source_id: user
    yield Api(TestClient(app), db, storage)
    app.dependency_overrides.clear()


# -- upload: validate, quarantine, queue --------------------------------------------------------------------

def test_an_upload_is_validated_stored_privately_and_queued_not_parsed_in_the_request(api) -> None:
    response = api.post_file("/uploads", b"Some KT notes.", name="notes.txt", mime="text/plain")
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "quarantined" and body["queued"] is True
    detail = api.client.get(f"{BASE}/uploads/{body['upload_id']}").json()
    assert detail["status"] == "quarantined" and detail["versions"] == []
    assert "storage_ref" not in detail and "kt-quarantine" not in str(detail)
    assert len(api.storage._objects) == 1


def test_polling_shows_progress_then_the_parsed_result_with_warnings(api) -> None:
    upload_id = api.upload(make_docx(["Body"], tables=1), name="notes.docx", mime=DOCX_TYPE)
    assert api.process(upload_id) == "parsed"
    detail = api.client.get(f"{BASE}/uploads/{upload_id}").json()
    assert detail["status"] == "parsed" and detail["failure_code"] is None
    [version] = detail["versions"]
    assert version["anchor_count"] == 1 and version["warnings"][0]["code"] == "tables_not_extracted"
    assert version["content_sha256"] and detail["latest_extracted_version_id"] == version["id"]

    page = api.client.get(f"{BASE}/uploads/{upload_id}/versions/{version['id']}/anchors", params={"limit": 1}).json()
    assert [a["text"] for a in page["anchors"]] == ["Body"] and page["next_offset"] is None
    assert api.client.get(f"{BASE}/uploads/{upload_id}/versions/nope/anchors").status_code == 404


def test_the_upload_list_is_newest_first_and_hides_deleted_unless_asked(api) -> None:
    first = api.upload(b"first notes")
    second = api.upload(b"second notes")
    assert api.client.delete(f"{BASE}/uploads/{first}").status_code == 200
    listed = api.client.get(f"{BASE}/uploads").json()["results"]
    assert [u["id"] for u in listed] == [second]
    everything = api.client.get(f"{BASE}/uploads", params={"include_deleted": "true"}).json()["results"]
    assert {u["id"] for u in everything} == {first, second}


@pytest.mark.parametrize("content,name,mime,fragment", [
    (make_pdf(["x"]), "notes.pdf", "text/plain", "content-type"),
    (make_pdf(["x"]), "notes.txt", "application/pdf", "extension"),
    (b"MZ\x90\x00 not a document", "notes.pdf", "application/pdf", "unsupported"),
    (b"", "empty.txt", "text/plain", "empty"),
    (make_docx(["x"])[:300], "cut.docx", DOCX_TYPE, "valid zip"),
    (make_encrypted_pdf(), "locked.pdf", "application/pdf", "encrypted"),
])
def test_spoofed_malformed_and_encrypted_files_are_refused_with_a_reason_and_kept_as_history(api, content, name, mime, fragment) -> None:
    response = api.post_file("/uploads", content, name=name, mime=mime)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "invalid_file" and fragment in detail["message"]
    row = api.client.get(f"{BASE}/uploads/{detail['upload_id']}").json()
    assert row["status"] == "rejected" and row["failure_reason"] == detail["message"]
    assert api.storage._objects == {} and api.db.collection_docs("exam_outbox") == []


def test_an_oversized_file_is_refused_before_anything_is_stored(api, monkeypatch) -> None:
    monkeypatch.setattr(routes, "MAX_UPLOAD_BYTES", 1000)
    response = api.post_file("/uploads", b"x" * 1001, name="big.txt", mime="text/plain")
    assert response.status_code == 413
    assert api.storage._objects == {} and api.db.collection_docs("kt_uploads") == []


def test_a_duplicate_upload_is_a_409_naming_the_existing_one(api) -> None:
    first = api.upload(b"same notes")
    response = api.post_file("/uploads", b"same notes", name="again.txt", mime="text/plain")
    assert response.status_code == 409
    assert response.json()["detail"] == {"code": "duplicate_upload", "message": "this exact file is already uploaded", "existing_upload_id": first}


def test_the_per_teacher_upload_cap_is_enforced(api, monkeypatch) -> None:
    monkeypatch.setattr(settings, "EXAM_KT_MAX_ACTIVE_UPLOADS_PER_USER", 2)
    api.upload(b"one")
    api.upload(b"two")
    response = api.post_file("/uploads", b"three", name="three.txt", mime="text/plain")
    assert response.status_code == 409 and response.json()["detail"]["code"] == "upload_limit_reached"


def test_uploads_are_rate_limited(api, monkeypatch) -> None:
    monkeypatch.setattr(routes, "_upload_limiter", SlidingWindowLimiter(2, 60, namespace="t_upload_small"))
    api.upload(b"one")
    api.upload(b"two")
    assert api.post_file("/uploads", b"three", name="three.txt", mime="text/plain").status_code == 429


def test_production_without_private_storage_refuses_uploads_with_a_503(api, monkeypatch) -> None:
    def unconfigured():
        raise StorageNotConfiguredError("no bucket")

    monkeypatch.setattr(routes, "kt_storage", unconfigured)
    response = api.post_file("/uploads", b"notes", name="notes.txt", mime="text/plain")
    assert response.status_code == 503 and response.json()["detail"]["code"] == "kt_storage_not_configured"


# -- worker outcomes visible to the teacher --------------------------------------------------------------------

def test_a_scanned_pdf_shows_an_actionable_failure_and_retry_is_offered_only_while_it_makes_sense(api) -> None:
    upload_id = api.upload(make_pdf([None, None]), name="scan.pdf", mime="application/pdf")
    api.process(upload_id)
    row = api.client.get(f"{BASE}/uploads/{upload_id}").json()
    assert row["status"] == "parse_failed" and row["failure_code"] == "no_text" and "OCR" in row["failure_reason"]
    retried = api.client.post(f"{BASE}/uploads/{upload_id}/retry")
    assert retried.status_code == 200 and retried.json()["status"] == "quarantined" and retried.json()["ingest_attempt"] == 2


def test_retry_of_a_healthy_upload_is_a_409(api) -> None:
    upload_id = api.upload(b"fine notes")
    api.process(upload_id)
    response = api.client.post(f"{BASE}/uploads/{upload_id}/retry")
    assert response.status_code == 409 and response.json()["detail"]["code"] == "command_rejected"


def test_an_infected_file_is_reported_and_cannot_be_retried(api) -> None:
    upload_id = api.upload(b"notes with EICAR-TEST-MARKER inside")
    api.process(upload_id)
    row = api.client.get(f"{BASE}/uploads/{upload_id}").json()
    assert row["status"] == "infected" and "malware" in row["failure_reason"]
    assert api.client.post(f"{BASE}/uploads/{upload_id}/retry").status_code == 409


def test_without_a_scanner_the_teacher_is_told_what_to_do(api, monkeypatch) -> None:
    monkeypatch.setattr(routes, "_scanner", lambda: NullMalwareScanner())
    upload_id = api.upload(b"notes")
    api.process(upload_id)
    row = api.client.get(f"{BASE}/uploads/{upload_id}").json()
    assert row["status"] == "scan_unavailable" and "administrator" in row["failure_reason"]


# -- replace / delete ---------------------------------------------------------------------------------------------

def test_replace_uploads_a_corrected_file_and_keeps_the_original_row(api) -> None:
    old = api.upload(b"first draft")
    api.process(old)
    response = api.post_file(f"/uploads/{old}/replace", b"second draft", name="v2.txt", mime="text/plain")
    assert response.status_code == 202 and response.json()["replaces_upload_id"] == old
    new = response.json()["upload_id"]
    assert api.client.get(f"{BASE}/uploads/{old}").json()["superseded_by_upload_id"] == new
    assert api.client.get(f"{BASE}/uploads/{new}").json()["replaces_upload_id"] == old
    assert api.client.get(f"{BASE}/uploads/{old}").json()["status"] == "parsed"


def test_replace_validates_the_new_file_like_any_upload(api) -> None:
    old = api.upload(b"first draft")
    response = api.post_file(f"/uploads/{old}/replace", make_pdf(["x"]), name="v2.pdf", mime="text/plain")
    assert response.status_code == 422
    assert api.client.get(f"{BASE}/uploads/{old}").json()["superseded_by_upload_id"] is None


def test_delete_removes_the_file_and_text_and_is_refused_for_an_approved_source(api) -> None:
    upload_id = api.upload(b"lecture notes")
    api.process(upload_id)
    response = api.client.delete(f"{BASE}/uploads/{upload_id}")
    assert response.status_code == 200 and response.json()["status"] == "deleted" and response.json()["deleted_at"]
    assert api.storage._objects == {} and api.db.collection_docs("kt_uploads", upload_id, "extracted_versions") == []
    assert "kt_upload_deleted" in audit_kinds(api.db)

    kept = api.upload(b"notes that back a published question")
    api.process(kept)
    api.db.put("kt_approved_sources", "src-ok", ApprovedSource(
        id="src-ok", kind="upload", upload_id=kept, review_status=SourceReviewStatus.APPROVED, created_by="teacher-1",
        created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc)).model_dump(mode="json"))
    refused = api.client.delete(f"{BASE}/uploads/{kept}")
    assert refused.status_code == 409 and "approved" in refused.json()["detail"]["reason"]


def test_unknown_uploads_are_404s(api) -> None:
    monkey = api.client.get(f"{BASE}/uploads/nope")
    assert monkey.status_code == 404


# -- registering and approving sources ---------------------------------------------------------------------------------

def test_a_source_can_only_be_registered_from_a_parsed_upload_and_pins_the_text_it_was_built_on(api) -> None:
    upload_id = api.upload(b"Lecture notes.")
    early = api.client.post(f"{BASE}/sources/from-upload", json={"upload_id": upload_id})
    assert early.status_code == 409 and "quarantined" in early.json()["detail"]

    api.process(upload_id)
    created = api.client.post(f"{BASE}/sources/from-upload", json={"upload_id": upload_id})
    assert created.status_code == 200
    version_id = api.client.get(f"{BASE}/uploads/{upload_id}").json()["latest_extracted_version_id"]
    assert created.json()["extracted_version_id"] == version_id and created.json()["review_status"] == "pending"

    approved = api.client.post(
        f"{BASE}/sources/{created.json()['id']}/review", json={"decision": "approve", "rights_statement": "own course notes"}
    )
    assert approved.status_code == 200 and approved.json()["extracted_version_id"] == version_id


def test_an_unread_source_cannot_be_approved(api) -> None:
    upload_id = api.upload(b"Lecture notes.")
    now = datetime.now(timezone.utc)
    api.db.put("kt_approved_sources", "src-1", ApprovedSource(
        id="src-1", kind="upload", upload_id=upload_id, created_by="teacher-1", created_at=now, updated_at=now).model_dump(mode="json"))
    response = api.client.post(f"{BASE}/sources/src-1/review", json={"decision": "approve", "rights_statement": "mine"})
    assert response.status_code == 409 and response.json()["detail"]["code"] == "content_not_ready"
    api.process(upload_id)
    assert api.client.post(f"{BASE}/sources/src-1/review", json={"decision": "approve", "rights_statement": "mine"}).status_code == 200


def test_approval_still_needs_a_rights_statement_and_rejection_still_works(api) -> None:
    upload_id = api.upload(b"Lecture notes.")
    api.process(upload_id)
    source = api.client.post(f"{BASE}/sources/from-upload", json={"upload_id": upload_id}).json()
    assert api.client.post(f"{BASE}/sources/{source['id']}/review", json={"decision": "approve"}).status_code == 400
    rejected = api.client.post(f"{BASE}/sources/{source['id']}/review", json={"decision": "reject", "rejection_reason": "off topic"})
    assert rejected.json()["review_status"] == "rejected"


# -- URL sources ----------------------------------------------------------------------------------------------------------------

def _serve(monkeypatch, content: bytes, *, content_type: str, final_url="https://docs.example/final"):
    seen = {}

    def fake_fetch(url, *, http_client, allowed_hosts=()):
        seen["url"], seen["allowed"] = url, list(allowed_hosts)
        return FetchedResource(final_url=final_url, content=content, content_type=content_type,
                               fetched_at=datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc))

    monkeypatch.setattr(routes, "fetch_url_safely", fake_fetch)
    return seen


def test_a_url_source_is_fetched_recorded_and_parsed_through_the_same_pipeline(api, monkeypatch) -> None:
    seen = _serve(monkeypatch, b"<h1>Indexes</h1><p>B-trees keep keys sorted.</p><script>x()</script>", content_type="text/html")
    response = api.client.post(f"{BASE}/sources/from-url", json={"url": "https://docs.example/start"})
    assert response.status_code == 202
    upload_id, source = response.json()["upload_id"], response.json()["source"]
    assert source["url"] == "https://docs.example/final" and source["review_status"] == "pending"

    row = api.client.get(f"{BASE}/uploads/{upload_id}").json()
    assert row["source_kind"] == "url" and row["final_url"] == "https://docs.example/final"
    assert row["fetched_at"].startswith("2026-06-01T09:00:00") and row["kind"] == "html"
    assert api.process(upload_id) == "parsed"
    version = api.client.get(f"{BASE}/uploads/{upload_id}").json()["versions"][0]
    assert version["content_sha256"] and version["parser"] == "html.parser"
    texts = [a["text"] for a in api.client.get(f"{BASE}/uploads/{upload_id}/versions/{version['id']}/anchors").json()["anchors"]]
    assert texts == ["Indexes", "B-trees keep keys sorted."]
    assert seen["url"] == "https://docs.example/start"


def test_a_url_serving_a_pdf_with_a_lying_content_type_is_detected_by_its_bytes(api, monkeypatch) -> None:
    _serve(monkeypatch, make_pdf(["Hello from a PDF"]), content_type="text/plain")
    response = api.client.post(f"{BASE}/sources/from-url", json={"url": "https://docs.example/a.pdf"})
    assert response.status_code == 202
    assert api.client.get(f"{BASE}/uploads/{response.json()['upload_id']}").json()["kind"] == "pdf"


def test_a_url_serving_binary_junk_is_refused(api, monkeypatch) -> None:
    _serve(monkeypatch, b"\x00\x01\x02\x03 binary", content_type="text/html")
    response = api.client.post(f"{BASE}/sources/from-url", json={"url": "https://docs.example/x"})
    assert response.status_code == 422 and response.json()["detail"]["code"] == "invalid_file"
    assert api.storage._objects == {}


def test_unsafe_urls_are_refused_with_a_reason(api, monkeypatch) -> None:
    def refuse(url, *, http_client, allowed_hosts=()):
        raise UnsafeUrlError("host 'internal' resolves to a disallowed address (10.0.0.5)")

    monkeypatch.setattr(routes, "fetch_url_safely", refuse)
    response = api.client.post(f"{BASE}/sources/from-url", json={"url": "https://internal.example/x"})
    assert response.status_code == 422 and response.json()["detail"]["code"] == "unsafe_or_unreachable_url"
    assert "disallowed" in response.json()["detail"]["message"]
    assert api.client.post(f"{BASE}/sources/from-url", json={"url": "http://docs.example/x"}).status_code == 422
    assert api.client.post(f"{BASE}/sources/from-url", json={"url": "https://u:p@docs.example/x"}).status_code == 422
    assert api.storage._objects == {} and api.db.collection_docs("kt_approved_sources") == []


def test_the_configured_host_allowlist_is_enforced(api, monkeypatch) -> None:
    monkeypatch.setattr(settings, "EXAM_KT_URL_ALLOWED_HOSTS", "college.edu, library.example.org")
    seen = _serve(monkeypatch, b"plain text", content_type="text/plain")
    assert api.client.post(f"{BASE}/sources/from-url", json={"url": "https://other.example/x"}).status_code == 422
    assert api.client.post(f"{BASE}/sources/from-url", json={"url": "https://cs.college.edu/x"}).status_code == 202
    assert seen["allowed"] == ["college.edu", "library.example.org"]


def test_the_same_page_twice_is_a_duplicate(api, monkeypatch) -> None:
    _serve(monkeypatch, b"identical page text", content_type="text/plain")
    first = api.client.post(f"{BASE}/sources/from-url", json={"url": "https://docs.example/a"})
    second = api.client.post(f"{BASE}/sources/from-url", json={"url": "https://docs.example/b"})
    assert first.status_code == 202 and second.status_code == 409
    assert second.json()["detail"]["existing_upload_id"] == first.json()["upload_id"]


def test_a_url_that_never_became_readable_cannot_be_approved(api, monkeypatch) -> None:
    _serve(monkeypatch, b"page text", content_type="text/plain")
    created = api.client.post(f"{BASE}/sources/from-url", json={"url": "https://docs.example/a"}).json()
    response = api.client.post(f"{BASE}/sources/{created['source']['id']}/review", json={"decision": "approve", "rights_statement": "public docs"})
    assert response.status_code == 409 and response.json()["detail"]["code"] == "content_not_ready"
    assert api.db.get("kt_uploads", created["upload_id"])["status"] == UploadStatus.QUARANTINED.value
