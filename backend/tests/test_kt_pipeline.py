"""Prompt 4 (ADR 0020): the durable ingestion pipeline - intake, quarantine, scan, parse, retry,
replace, delete, redelivery and the outbox split - on the in-memory Firestore (`exam_inmemory.py`)."""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest

from app.domain.exams.clock import FixedClock
from app.domain.exams.errors import CommandRejectedError
from app.domain.exams.outbox import drain_outbox_once
from app.domain.exams.repository import OutboxRepository
from app.domain.kt.ingestion import (
    KT_INGEST_KIND,
    DuplicateUploadError,
    KtIngestionOutboxHandler,
    KtIngestionService,
    TransientIngestionError,
    UploadLimitError,
)
from app.domain.kt.malware_scan import DeterministicFakeScanner, NullMalwareScanner, ScanResult
from app.domain.kt.models import ApprovedSource, SourceReviewStatus, UploadStatus
from app.domain.kt.parse_isolation import IngestionParseError
from app.domain.kt.repository import ANCHOR_CHUNK_BYTES
from app.domain.proctoring.evidence_storage import InMemoryEvidenceStorage
from exam_inmemory import T0, FakeFirestore, audit_kinds, install_optimistic_transactions, outbox_kinds
from kt_fixtures import inproc_parse, make_docx, make_encrypted_pdf, make_pdf

OWNER = "teacher-1"


class Env:
    def __init__(self, monkeypatch, *, scanner=None, parse_fn=inproc_parse, max_active=200) -> None:
        self.db = FakeFirestore()
        install_optimistic_transactions(monkeypatch, self.db)
        self.storage = InMemoryEvidenceStorage()
        self.clock = FixedClock(T0)
        self.scanner = scanner or DeterministicFakeScanner()
        self.service = KtIngestionService(
            self.db, self.storage, self.scanner, parse_fn=parse_fn, clock=self.clock, max_active_uploads=max_active
        )

    def upload(self, content: bytes, *, kind="txt", filename="notes.txt", owner=OWNER, **kw):
        return self.service.accept(owner_uid=owner, filename=filename, declared_type="text/plain", content=content,
                                   detected_kind=kind, **kw)

    def doc(self, upload_id: str) -> dict:
        return self.db.get("kt_uploads", upload_id)

    def run(self, upload, **kw) -> str:
        return self.service.process(upload.id, kw.pop("attempt", 1), **kw)


@pytest.fixture
def env(monkeypatch):
    return Env(monkeypatch)


# -- intake: quarantine, generated names, one transaction -----------------------------------------------------

def test_accept_stores_opaque_bytes_under_a_generated_name_and_queues_one_job(env) -> None:
    upload = env.upload(b"Some notes.", filename="../../etc/passwd.txt")
    assert upload.status == UploadStatus.QUARANTINED and upload.ingest_attempt == 1
    assert upload.storage_ref.startswith("kt-quarantine/") and "passwd" not in upload.storage_ref
    assert env.storage.get(upload.storage_ref) == b"Some notes."
    jobs = env.db.collection_docs("exam_outbox")
    assert [j["kind"] for j in jobs] == [KT_INGEST_KIND]
    assert jobs[0]["payload"] == {"upload_id": upload.id, "ingest_attempt": 1} and jobs[0]["run_id"] == ""
    assert audit_kinds(env.db) == ["kt_upload_accepted"]


def test_two_uploads_never_share_an_object_name(env) -> None:
    refs = {env.upload(f"notes {i}".encode()).storage_ref for i in range(5)}
    assert len(refs) == 5


def test_a_duplicate_of_an_active_upload_is_refused_but_other_owners_and_deleted_copies_are_fine(env) -> None:
    first = env.upload(b"same bytes")
    with pytest.raises(DuplicateUploadError) as caught:
        env.upload(b"same bytes", filename="renamed.txt")
    assert caught.value.existing_upload_id == first.id
    assert env.upload(b"same bytes", owner="teacher-2").id != first.id
    env.service.delete(first.id, actor_uid=OWNER)
    assert env.upload(b"same bytes").id != first.id
    assert len(env.storage._objects) == 2  # the refused duplicate stored nothing


def test_the_active_upload_cap_counts_only_live_uploads(monkeypatch) -> None:
    env = Env(monkeypatch, max_active=2)
    a = env.upload(b"one")
    env.upload(b"two")
    with pytest.raises(UploadLimitError) as caught:
        env.upload(b"three")
    assert caught.value.limit == 2
    env.service.delete(a.id, actor_uid=OWNER)
    env.upload(b"three")  # room again
    env.service.record_rejected(owner_uid=OWNER, filename="bad.pdf", declared_type="x", content=b"x", reason="bad")
    env.service.record_rejected(owner_uid=OWNER, filename="bad2.pdf", declared_type="x", content=b"y", reason="bad")
    assert len(env.service.uploads.list_by_owner(OWNER)) == 5  # rejected rows are history, not quota


def test_a_failed_intake_leaves_no_orphaned_bytes(env) -> None:
    with pytest.raises(CommandRejectedError):
        env.upload(b"replacement", replaces_upload_id="does-not-exist")
    assert env.storage._objects == {} and env.db.collection_docs("exam_outbox") == []
    assert env.db.collection_docs("kt_uploads") == []


def test_a_rejected_file_is_recorded_without_bytes(env) -> None:
    upload = env.service.record_rejected(owner_uid=OWNER, filename="x.pdf", declared_type="text/plain", content=b"%PDF-1.4",
                                         reason="content-type does not match PDF magic bytes")
    doc = env.doc(upload.id)
    assert doc["status"] == "rejected" and doc["failure_code"] == "invalid_file" and doc["storage_ref"] == ""
    assert env.storage._objects == {} and env.db.collection_docs("exam_outbox") == []


# -- worker: success path ---------------------------------------------------------------------------------------

def test_a_clean_document_is_parsed_into_an_immutable_hashed_anchored_version(env) -> None:
    upload = env.upload(make_pdf(["Normalisation basics", None, "Third normal form"]), kind="pdf", filename="db.pdf")
    assert env.run(upload) == "parsed"

    doc = env.doc(upload.id)
    assert doc["status"] == "parsed" and doc["failure_code"] is None
    version = env.service.uploads.get_extracted_version(upload.id, doc["latest_extracted_version_id"])
    assert version.version_number == 1 and version.parser == "pypdf" and version.total_pages == 3
    assert [(a.page, a.text) for a in version.anchors] == [(1, "Normalisation basics"), (3, "Third normal form")]
    assert version.content_sha256 and len(version.content_sha256) == 64
    assert [w.code for w in version.warnings] == ["pages_without_text"]
    assert version.stats["empty_pages"] == 1
    assert audit_kinds(env.db) == ["kt_upload_accepted", "kt_upload_parsed"]


def test_anchor_ids_are_stable_for_identical_bytes_and_differ_by_position(monkeypatch) -> None:
    ids = []
    for _ in range(2):  # two independent deployments parsing the same file
        env = Env(monkeypatch)
        upload = env.upload(b"First paragraph.\n\nSecond paragraph.")
        env.run(upload)
        version = env.service.uploads.list_extracted_versions(upload.id, include_anchors=True)[0]
        ids.append([a.anchor_id for a in version.anchors])
    assert ids[0] == ids[1] and len(set(ids[0])) == 2


def test_the_version_content_hash_changes_with_the_text(monkeypatch) -> None:
    hashes = []
    for text in (b"Alpha paragraph.", b"Beta paragraph."):
        env = Env(monkeypatch)
        upload = env.upload(text)
        env.run(upload)
        hashes.append(env.service.uploads.list_extracted_versions(upload.id)[0].content_sha256)
    assert hashes[0] != hashes[1]


def test_a_large_extract_is_chunked_under_the_firestore_document_limit(env) -> None:
    paragraphs = [f"Paragraph {i:05d} " + "word " * 150 for i in range(2500)]  # ~2 MB of text
    upload = env.upload("\n\n".join(paragraphs).encode())
    env.run(upload)

    version_ref = ("kt_uploads", upload.id, "extracted_versions", env.doc(upload.id)["latest_extracted_version_id"])
    header = env.db.get(*version_ref)
    assert header["anchors"] == [] and header["anchor_count"] == 2500 and header["chunk_count"] >= 3
    chunks = env.db.collection_docs(*version_ref, "anchor_chunks")
    assert len(chunks) == header["chunk_count"]
    import json
    assert all(len(json.dumps(c).encode()) < ANCHOR_CHUNK_BYTES * 1.2 for c in chunks)

    loaded = env.service.uploads.get_extracted_version(upload.id, header["id"])
    assert len(loaded.anchors) == 2500 and loaded.anchors[0].text.startswith("Paragraph 00000")
    assert [a.paragraph_index for a in loaded.anchors] == list(range(2500))


def test_multiple_documents_of_every_supported_type_are_processed_independently(env) -> None:
    uploads = [
        env.upload(make_pdf(["pdf text"]), kind="pdf", filename="a.pdf"),
        env.upload(make_docx(["docx text"]), kind="docx", filename="b.docx"),
        env.upload(b"plain text", kind="txt", filename="c.txt"),
        env.upload(b"<h1>Title</h1><p>html text</p>", kind="html", filename="https://docs.example/x", source_kind="url"),
        env.upload(make_pdf([None]), kind="pdf", filename="scan.pdf"),
    ]
    outcomes = [env.run(u) for u in uploads]
    assert outcomes == ["parsed", "parsed", "parsed", "parsed", "parse_failed"]
    texts = [env.service.uploads.list_extracted_versions(u.id, include_anchors=True) for u in uploads[:4]]
    assert [v[0].anchors[0].text for v in texts] == ["pdf text", "docx text", "plain text", "Title"]
    assert env.doc(uploads[4].id)["failure_code"] == "no_text"


# -- worker: idempotency and redelivery ------------------------------------------------------------------------------

def test_a_redelivered_job_does_nothing(env) -> None:
    upload = env.upload(b"Some notes.")
    assert env.run(upload) == "parsed"
    before = env.doc(upload.id)
    assert env.run(upload) == "already_settled"
    assert env.doc(upload.id) == before and len(env.service.uploads.list_extracted_versions(upload.id)) == 1


def test_a_job_for_an_older_attempt_or_a_missing_upload_is_ignored(env) -> None:
    upload = env.upload(b"Some notes.")
    assert env.service.process(upload.id, 7) == "stale"
    assert env.service.process("nope", 1) == "stale"
    assert env.doc(upload.id)["status"] == "quarantined"


def test_a_crash_between_parse_and_status_update_is_repaired_by_the_redelivery(env, monkeypatch) -> None:
    upload = env.upload(b"Some notes.")
    real_update = env.service.uploads.update
    calls = {"parsed": 0}

    def crashing_update(upload_id, update):
        if update.get("status") == "parsed" and calls["parsed"] == 0:
            calls["parsed"] += 1
            raise RuntimeError("process died")
        return real_update(upload_id, update)

    monkeypatch.setattr(env.service.uploads, "update", crashing_update)
    with pytest.raises(RuntimeError):
        env.run(upload)
    assert env.doc(upload.id)["status"] == "parsing"
    assert env.run(upload, delivery_attempt=1) == "parsed"  # the version already exists: not written twice
    assert len(env.service.uploads.list_extracted_versions(upload.id)) == 1


def test_concurrent_workers_on_one_upload_produce_one_version(env) -> None:
    upload = env.upload(b"Some notes.\n\nMore notes.")
    barrier = threading.Barrier(4)

    def work():
        barrier.wait()
        env.run(upload)

    threads = [threading.Thread(target=work) for _ in range(4)]
    [t.start() for t in threads]
    [t.join(timeout=30) for t in threads]
    assert env.doc(upload.id)["status"] == "parsed"
    assert len(env.service.uploads.list_extracted_versions(upload.id)) == 1
    assert len(env.service.uploads.get_extracted_version(upload.id, f"{upload.id}-a1").anchors) == 2


# -- worker: scan outcomes ---------------------------------------------------------------------------------------------

def test_an_infected_file_is_removed_and_never_parsed(env) -> None:
    upload = env.upload(b"harmless EICAR-TEST-MARKER harmless")
    assert env.run(upload) == "infected"
    doc = env.doc(upload.id)
    assert doc["status"] == "infected" and doc["failure_code"] == "infected"
    assert env.storage.get(upload.storage_ref) is None and env.service.uploads.list_extracted_versions(upload.id) == []
    assert "kt_upload_infected" in audit_kinds(env.db)


def test_with_no_scanner_configured_nothing_is_read_and_the_message_says_what_to_do(monkeypatch) -> None:
    env = Env(monkeypatch, scanner=NullMalwareScanner())
    upload = env.upload(b"Some notes.")
    assert env.run(upload) == "scan_unavailable"
    doc = env.doc(upload.id)
    assert doc["failure_code"] == "scanner_unavailable" and "administrator" in doc["rejection_reason"]
    assert env.service.uploads.list_extracted_versions(upload.id) == []
    assert env.storage.get(upload.storage_ref) == b"Some notes."  # kept: a retry after enabling the scanner works


class _FlakyScanner:
    configured = True

    def __init__(self) -> None:
        self.calls = 0
        self.down = True

    def scan(self, content: bytes) -> ScanResult:
        self.calls += 1
        if self.down:
            return ScanResult(clean=False, detail="scanner unreachable (ConnectionRefusedError)", unavailable=True)
        return ScanResult(clean=True)


def test_an_unreachable_scanner_is_redelivered_then_settled_and_retryable(monkeypatch) -> None:
    scanner = _FlakyScanner()
    env = Env(monkeypatch, scanner=scanner)
    upload = env.upload(b"Some notes.")

    with pytest.raises(TransientIngestionError):
        env.run(upload, delivery_attempt=0, final_delivery=False)
    assert env.run(upload, delivery_attempt=4, final_delivery=True) == "scan_unavailable"
    assert "could not be reached" in env.doc(upload.id)["rejection_reason"]

    scanner.down = False
    retried = env.service.retry(upload.id, actor_uid=OWNER)
    assert retried.ingest_attempt == 2 and retried.status == UploadStatus.QUARANTINED
    assert env.service.process(upload.id, 2) == "parsed"
    assert env.service.process(upload.id, 1) == "stale"  # the old job can never undo the new attempt


# -- worker: parse failures ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("content,kind,code", [
    (make_encrypted_pdf(), "pdf", "encrypted"),
    (make_pdf([None, None]), "pdf", "no_text"),
    (b"%PDF-1.4 truncated junk", "pdf", "corrupt"),
    (b"PK\x03\x04 broken docx", "docx", "corrupt"),
])
def test_unreadable_documents_settle_as_parse_failed_with_an_actionable_reason(env, content, kind, code) -> None:
    upload = env.upload(content, kind=kind, filename=f"f.{kind}")
    assert env.run(upload) == "parse_failed"
    doc = env.doc(upload.id)
    assert doc["failure_code"] == code and doc["rejection_reason"]
    assert env.service.uploads.list_extracted_versions(upload.id) == []
    assert "kt_upload_failed" in audit_kinds(env.db)


def _crashing(kind, content):
    raise IngestionParseError("parse_crash", "The document reader stopped unexpectedly.", retryable=True)


def test_a_parser_crash_gets_one_automatic_retry_then_is_left_to_the_teacher(monkeypatch) -> None:
    env = Env(monkeypatch, parse_fn=_crashing)
    upload = env.upload(b"Some notes.")
    with pytest.raises(TransientIngestionError):
        env.run(upload, delivery_attempt=0, final_delivery=False)
    assert env.doc(upload.id)["status"] == "parsing"
    assert env.run(upload, delivery_attempt=1, final_delivery=False) == "parse_failed"
    assert env.doc(upload.id)["failure_code"] == "parse_crash"


def test_a_crash_that_stops_happening_succeeds_on_the_automatic_retry(monkeypatch) -> None:
    attempts = {"n": 0}

    def flaky(kind, content):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise IngestionParseError("parse_timeout", "too slow", retryable=True)
        return inproc_parse(kind, content)

    env = Env(monkeypatch, parse_fn=flaky)
    upload = env.upload(b"Some notes.")
    with pytest.raises(TransientIngestionError):
        env.run(upload, delivery_attempt=0, final_delivery=False)
    assert env.run(upload, delivery_attempt=1, final_delivery=False) == "parsed"


def test_missing_or_tampered_bytes_settle_as_non_retryable(env) -> None:
    gone = env.upload(b"gone soon")
    env.storage.delete(gone.storage_ref)
    assert env.run(gone) == "parse_failed" and env.doc(gone.id)["failure_code"] == "storage_missing"
    tampered = env.upload(b"original bytes")
    env.storage.put(tampered.storage_ref, b"swapped bytes", content_type="x")
    assert env.run(tampered) == "parse_failed" and env.doc(tampered.id)["failure_code"] == "integrity_mismatch"
    for upload in (gone, tampered):
        with pytest.raises(CommandRejectedError, match="replace"):
            env.service.retry(upload.id, actor_uid=OWNER)


def test_the_real_child_process_path_works_end_to_end(monkeypatch) -> None:
    env = Env(monkeypatch, parse_fn=None)  # production wiring: parse in a killable child
    ok = env.upload(make_docx(["Child process text"], tables=1), kind="docx", filename="a.docx")
    bad = env.upload(make_encrypted_pdf(), kind="pdf", filename="b.pdf")
    assert env.run(ok) == "parsed" and env.run(bad) == "parse_failed"
    version = env.service.uploads.list_extracted_versions(ok.id, include_anchors=True)[0]
    assert [a.text for a in version.anchors] == ["Child process text"]
    assert [w.code for w in version.warnings] == ["tables_not_extracted"]
    assert env.doc(bad.id)["failure_code"] == "encrypted"


# -- retry / replace / delete -----------------------------------------------------------------------------------------------

def test_retry_is_only_for_failed_uploads_and_keeps_the_history(env) -> None:
    upload = env.upload(make_pdf([None]), kind="pdf", filename="scan.pdf")
    env.run(upload)
    retried = env.service.retry(upload.id, actor_uid=OWNER)
    assert (retried.status, retried.ingest_attempt, retried.failure_code) == (UploadStatus.QUARANTINED, 2, None)
    assert outbox_kinds(env.db).count(KT_INGEST_KIND) == 2
    assert [k for k in audit_kinds(env.db) if k.startswith("kt_upload_r")] == ["kt_upload_retried"]
    event = [e for e in env.db.collection_docs("exam_authz_audit") if e["kind"] == "kt_upload_retried"][0]
    assert event["detail"]["previous_failure"] == "no_text" and event["actor_uid"] == OWNER

    good = env.upload(b"fine notes")
    env.run(good)
    for settled in (good,):
        with pytest.raises(CommandRejectedError):
            env.service.retry(settled.id, actor_uid=OWNER)
    with pytest.raises(LookupError):
        env.service.retry("missing", actor_uid=OWNER)


def test_a_successful_retry_records_which_attempt_produced_the_version(env) -> None:
    flaky = env.upload(b"Other notes.")
    calls = {"n": 0}
    real = env.service._parse

    def fail_once(kind, content):
        calls["n"] += 1
        if calls["n"] == 1:
            raise IngestionParseError("corrupt", "could not read")
        return real(kind, content)

    env.service._parse = fail_once
    assert env.run(flaky) == "parse_failed"
    env.service.retry(flaky.id, actor_uid=OWNER)
    assert env.service.process(flaky.id, 2) == "parsed"
    versions = env.service.uploads.list_extracted_versions(flaky.id)
    assert [(v.version_number, v.ingest_attempt) for v in versions] == [(1, 2)]


def test_replace_keeps_the_old_upload_and_links_both_ways(env) -> None:
    old = env.upload(b"First draft of the notes.")
    env.run(old)
    new = env.upload(b"Corrected notes.", replaces_upload_id=old.id)
    assert new.replaces_upload_id == old.id
    assert env.doc(old.id)["superseded_by_upload_id"] == new.id and env.doc(old.id)["status"] == "parsed"
    assert env.storage.get(old.storage_ref) == b"First draft of the notes."
    assert "kt_upload_replaced" in audit_kinds(env.db)
    assert env.run(new) == "parsed"


def test_a_deleted_upload_cannot_be_replaced(env) -> None:
    old = env.upload(b"First draft.")
    env.service.delete(old.id, actor_uid=OWNER)
    with pytest.raises(CommandRejectedError, match="upload_not_replaceable"):
        env.upload(b"Second draft.", replaces_upload_id=old.id)


def _register(env, upload, status=SourceReviewStatus.PENDING) -> ApprovedSource:
    source = ApprovedSource(id=f"src-{upload.id[:6]}", kind="upload", upload_id=upload.id, review_status=status,
                            created_by=OWNER, created_at=T0, updated_at=T0)
    env.service.sources.create(source)
    return source


def test_delete_removes_bytes_and_text_but_keeps_the_row_and_the_audit_trail(env) -> None:
    upload = env.upload(b"Confidential lecture notes.")
    env.run(upload)
    pending = _register(env, upload)

    deleted = env.service.delete(upload.id, actor_uid=OWNER)

    assert deleted.status == UploadStatus.DELETED and deleted.deleted_by == OWNER and deleted.deleted_at
    assert env.storage.get(upload.storage_ref) is None
    assert env.service.uploads.list_extracted_versions(upload.id) == []
    assert env.db.collection_docs("kt_uploads", upload.id, "extracted_versions") == []
    assert env.doc(upload.id)["sha256"] == upload.sha256 and env.doc(upload.id)["original_filename"] == "notes.txt"
    assert "kt_upload_deleted" in audit_kinds(env.db) and "kt_upload_parsed" in audit_kinds(env.db)
    source = env.service.sources.get(pending.id)
    assert source.review_status == SourceReviewStatus.REJECTED and "deleted" in source.rejection_reason


def test_delete_is_refused_while_an_approved_source_depends_on_the_upload(env) -> None:
    upload = env.upload(b"Notes that back published questions.")
    env.run(upload)
    _register(env, upload, status=SourceReviewStatus.APPROVED)
    with pytest.raises(CommandRejectedError, match="approved"):
        env.service.delete(upload.id, actor_uid=OWNER)
    assert env.storage.get(upload.storage_ref) is not None and env.doc(upload.id)["status"] == "parsed"


def test_delete_is_idempotent_and_finishes_an_interrupted_cleanup(env) -> None:
    upload = env.upload(b"Some notes.")
    env.run(upload)
    env.service.delete(upload.id, actor_uid=OWNER)
    env.storage.put(upload.storage_ref, b"resurrected", content_type="x")  # as if the first cleanup died midway
    again = env.service.delete(upload.id, actor_uid=OWNER)
    assert again.status == UploadStatus.DELETED and env.storage.get(upload.storage_ref) is None
    assert audit_kinds(env.db).count("kt_upload_deleted") == 1
    assert env.service.process(upload.id, 1) == "already_settled"


def test_deleting_before_the_worker_runs_stops_processing(env) -> None:
    upload = env.upload(b"Some notes.")
    env.service.delete(upload.id, actor_uid=OWNER)
    assert env.run(upload) == "already_settled" and env.service.uploads.list_extracted_versions(upload.id) == []


# -- the shared outbox: a slow parse must not stall everything else --------------------------------------------------------------

def test_the_outbox_handler_ignores_other_kinds_and_dispatches_ingest_jobs(env) -> None:
    upload = env.upload(b"Some notes.")
    handler = KtIngestionOutboxHandler(env.service)
    from app.domain.exams.models import OutboxEntry

    handler.handle(OutboxEntry(id="x", kind="attempt_finalized", run_id="r", payload={"uid": "u"}, created_at=T0))
    assert env.doc(upload.id)["status"] == "quarantined"
    handler.handle(OutboxEntry(id="y", kind=KT_INGEST_KIND, run_id="", payload={"upload_id": upload.id, "ingest_attempt": 1},
                               created_at=T0))
    assert env.doc(upload.id)["status"] == "parsed"


def test_the_ingest_loop_claims_only_ingest_jobs_and_the_main_loop_never_touches_them(env) -> None:
    upload = env.upload(b"Some notes.")
    env.db.put("exam_outbox", "other-1", {"id": "other-1", "kind": "run_command", "run_id": "r", "payload": {},
                                           "created_at": T0.isoformat(), "processed": False, "attempts": 0})
    seen: list[str] = []

    class Recorder:
        def handle(self, entry):
            seen.append(entry.kind)

    repo = OutboxRepository(env.db)
    now = T0 + timedelta(seconds=1)
    drain_outbox_once(repo, Recorder(), now=now, limit=25, exclude_kinds=(KT_INGEST_KIND,))
    assert seen == ["run_command"]  # the main loop skipped the ingest job
    assert env.doc(upload.id)["status"] == "quarantined"

    seen.clear()
    drain_outbox_once(repo, KtIngestionOutboxHandler(env.service), now=now, limit=1, kinds=[KT_INGEST_KIND])
    assert env.doc(upload.id)["status"] == "parsed"
    drain_outbox_once(repo, Recorder(), now=now, limit=25, kinds=[KT_INGEST_KIND])
    assert seen == []  # the ingest job is processed; nothing is delivered twice


def test_a_scanner_outage_is_redelivered_by_the_outbox_and_given_up_on_after_max_attempts(monkeypatch) -> None:
    scanner = _FlakyScanner()
    env = Env(monkeypatch, scanner=scanner)
    upload = env.upload(b"Some notes.")
    handler = KtIngestionOutboxHandler(env.service, max_attempts=3)
    repo = OutboxRepository(env.db)
    for tick in range(3):
        drain_outbox_once(repo, handler, now=T0 + timedelta(minutes=10 * (tick + 1)), limit=1, kinds=[KT_INGEST_KIND],
                          lease_seconds=60, max_attempts=3)
    assert scanner.calls == 3
    assert env.doc(upload.id)["status"] == "scan_unavailable"
    assert env.db.collection_docs("exam_outbox")[0]["processed"] is True


def test_an_unexpected_error_is_retried_and_finally_settled_instead_of_looping_forever(monkeypatch) -> None:
    def explode(kind, content):
        raise MemoryError("boom")

    env = Env(monkeypatch, parse_fn=explode)
    upload = env.upload(b"Some notes.")
    handler = KtIngestionOutboxHandler(env.service, max_attempts=2)
    repo = OutboxRepository(env.db)
    for tick in range(2):
        drain_outbox_once(repo, handler, now=T0 + timedelta(minutes=10 * (tick + 1)), limit=1, kinds=[KT_INGEST_KIND],
                          lease_seconds=60, max_attempts=2)
    doc = env.doc(upload.id)
    assert doc["status"] == "parse_failed" and doc["failure_code"] == "processing_error" and "MemoryError" in doc["rejection_reason"]
    assert env.db.collection_docs("exam_outbox")[0]["processed"] is True
