# ADR 0020 — Durable, private source ingestion

**Status:** Accepted

## Context

Prompt 4 asks for a private source registry and a durable ingestion pipeline: multiple
PDF/DOCX (TXT optional) resources and approved URLs; private quarantine storage with
generated object names; real signature/extension/MIME/size/count/decompression/timeout
validation; malware scanning when configured; bounded-worker parsing with immutable,
hashed, anchored extracted versions; honest reporting of what could not be read; progress,
failure reason, retry, replace and delete with history and audit; SSRF-safe URL fetching.
Prompt 0 classified the rows ING-01…ING-09 (`docs/exams/requirements-traceability.md` §4).
ADR 0009 had built the validation, parsers, registry lifecycle and URL resolver but left the
rest as documented gaps.

Reading the code against the list found problems beyond the missing endpoints:

1. **Bytes were never stored.** `storage_ref` pointed at nothing, so no upload could be
   retried, replaced or re-parsed.
2. **Parsing ran inside the request**, and with the default (fail-closed) scanner nothing
   was ever parsed; `EXAM_MALWARE_SCANNER_ENDPOINT` was read nowhere.
3. **URL sources were never ingested**: the fetched bytes were discarded, the upload row
   stayed `QUARANTINED` forever, and the source could still be approved with no text behind it.
4. **An extracted version was one Firestore document**, so any document above ~1 MiB of text
   could not be saved.
5. **Silent loss**: paragraphs over 4000 characters were cut; tables, images and scanned
   pages were dropped; a scanned PDF "parsed" to zero anchors.
6. **Weak validation**: the extension was never checked and any zip with the DOCX MIME type
   passed; URL fetching buffered the whole body before the size check, mishandled relative
   redirects and accepted shared-address-space, IPv4-mapped IPv6, credentialed and odd-port URLs.

## Decisions

### 1. One pipeline, on the existing outbox
`accept` (in the request) validates, writes the bytes to private storage and, in **one
transaction**, creates the upload record, an outbox job (`kt_ingest`) and an audit event. The
existing outbox drain gets a second loop dedicated to this kind (one job at a time, lease ≥ 120 s)
and the main loop excludes it, so a slow parse never delays grading or run commands. No new
queue, collection or worker type. The job is idempotent: a settled upload is ignored, a job for
an older attempt is ignored, the extracted version has a deterministic id and is create-only, so
redelivery or two workers produce one version. Unexpected errors are redelivered and, on the last
allowed delivery, settled as `parse_failed / processing_error` instead of looping forever.

### 2. Private quarantine storage
Bytes go to the evidence storage adapter (same bucket setting, `kt-quarantine/` prefix) under a
random generated key, as `application/octet-stream`, never under the client's filename. In
production a missing bucket is a hard `503` (the in-memory fallback would lose bytes on restart
and make retry meaningless); development uses the in-memory store. A failed intake deletes the
object. Nothing returns a storage path or the bytes to a client.

### 3. Validation
Signature, MIME type **and extension** must agree; a zip must be a real Word package; size,
entry-count, compression-ratio and uncompressed-size limits and the macro/encryption markers
stay. A rejected file keeps a history row (no bytes). Fetched resources are classified by their
bytes (the server's Content-Type is a hint): PDF, DOCX, HTML or text. A per-teacher active-upload
cap, exact-duplicate refusal (same owner and SHA-256) and sliding-window rate limits apply.

### 4. Scanning
`EXAM_MALWARE_SCANNER_ENDPOINT=host:port` selects a clamd `zINSTREAM` client; anything else keeps
`NullMalwareScanner`. The result has three outcomes: clean, **infected** (bytes deleted, terminal)
and **unavailable** (never treated as clean: redelivered, then settled `scan_unavailable`, which a
teacher can retry once the scanner is back). The adapter is tested against a local stand-in daemon
that speaks the protocol; it has **not** been run against a real ClamAV (decision D-4).

### 5. Parsing in a killable child process
`parse_in_subprocess` runs `python -m app.domain.kt.parse_worker <kind>` with the document on
stdin, a scrubbed environment (no credentials), an address-space limit on POSIX, a wall-clock
deadline and an output cap; a timeout kills the process. Failures are classified and shown to the
teacher: `encrypted`, `no_text`, `corrupt` (the message names the exception type only, never
document text), `parse_timeout` and `parse_crash`. A timeout/crash gets **one** automatic redelivery,
then the teacher decides (retry). The thread-timeout runner is kept for tests only.

### 6. Extracted versions
Immutable and create-only. Each carries a hash of the extracted text, the parser, the ingest
attempt, `stats` and `warnings`; anchors have ids derived from the file hash + position (identical
bytes cite identical anchors), a page where the format has pages and a `section` (nearest heading)
where it has headings. Anchors are stored in `anchor_chunks` (~600 KB each) and the version
document is written **last** as the commit marker; legacy inline versions still read. Long
paragraphs are split on whitespace, never cut.

### 7. What was not read is reported, never hidden
OCR and table/image extraction need a qualified parser and are not done. Instead each version lists
`tables_not_extracted`, `images_not_extracted`, `pages_without_text`, `low_confidence_text`,
`active_content_ignored`, `hidden_content_skipped` and `truncated`, with counts/pages, and the UI shows
them before a teacher registers the upload. A PDF with no extractable text at all is refused with an
explicit "looks scanned; OCR is not available" instead of producing an empty "success". HTML
text hidden with `hidden`/`display:none`, scripts, styles and comments are dropped (hidden text is a
prompt-injection vehicle). Extracted text is returned to teachers as data to be shown as plain text.

### 8. Teacher commands
`retry` (only `parse_failed`/`scan_unavailable`, not when the bytes are gone or altered) starts a
new attempt; `replace` uploads a corrected file as a **new** upload linked both ways and leaves the
old row and its audit trail; `delete` removes the bytes and extracted text, keeps the metadata row and
audit, rejects pending sources built on it and is **refused while an approved source cites it**
(its anchors may back published items). Delete is idempotent and finishes an interrupted cleanup.
All write `kt_*` events to the shared audit collection.

### 9. Sources pin what was reviewed
`from-upload` records `extracted_version_id`; approval requires the upload to be `PARSED` and pins the
latest version at that moment. An unread source can no longer be approved (previously possible for URL
sources).

### 10. URL fetching
Unchanged SSRF stance (https only, address-class denylist re-checked on every redirect hop, metadata
IPs, ≤ 3 redirects, 10 s, 25 MB) plus: `is_global` rather than a list of private ranges, IPv4-mapped IPv6
unwrapped, embedded credentials and non-443 ports refused, relative redirects resolved before re-validation,
HTTP errors not ingested, the size cap enforced while the body streams, an optional host allowlist
(`EXAM_KT_URL_ALLOWED_HOSTS`, suffix match, applied to every hop) and the canonical (post-redirect) URL
and fetch time recorded. The DNS-rebinding TOCTOU between the check and the connection is not closed; an
egress proxy remains an infrastructure gate (Phase 11).

## Consequences

- `POST /uploads` is now asynchronous (`202`, status `quarantined`, poll `GET /uploads/{id}`); it used to
  parse inline and return the result. `POST /sources/from-url` is `202` with the upload id. Invalid files
  return a structured `detail` (`{code, message, upload_id}`).
- With no scanner configured nothing is read (as before) but the teacher is now told why and what to do.
- Requires `EXAM_EVIDENCE_STORAGE_BUCKET` in production and a clamd endpoint to read any document.
- Uploaded sources have **no retention window** yet (only teacher delete); the evidence retention sweeper does
  not cover `kt-quarantine/`. Decision D-4 still owns that policy.
- The outbox claim for the main loop over-fetches and filters out `kt_ingest` (no composite index); a very long
  backlog of ingest jobs could delay it slightly. A composite index (`processed`, `kind`) is not required for the
  ingest loop's equality + `in` query on Firestore, but that has not been verified against a real project.

## Not verified

Everything above is unit-, HTTP- and component-level: in-memory Firestore and object store, a local stand-in
for clamd, mock HTTP transports, and real child-process parsing of synthetic documents. No real bucket, ClamAV,
OCR/table extractor, DNS or internet. The child's POSIX memory limit was not exercised (development is on
Windows). Parser CVE exposure (pypdf, python-docx, lxml) is reduced by the child-process boundary, not removed.
