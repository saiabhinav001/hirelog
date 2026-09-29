# ADR 0009 — KT ingestion, source registry, and provider-neutral generation

**Status:** Accepted

## Context

Prompt 5 asks for private document upload/quarantine/scan/parse, an approved
source registry with an SSRF-safe URL resolver, exam blueprints, a
provider-neutral generation adapter using the pack's "Backend model prompt
A" contract, structured validation, faculty review/publish, and variant
reservation. Several pieces (real malware scanning, real OCR, a real
generation model) require external services/binaries not available in this
environment. This ADR records what was built for real, what is an honest
interface-only stub, and the safe-by-default behavior in between.

## Decisions

### 1. Malware scanning fails closed, always

`NullMalwareScanner` (the default — no real scanner configured) never
reports a file clean. Every upload stays `SCAN_UNAVAILABLE` and is never
parsed or citable until a real scanner (ClamAV daemon, cloud AV API, etc.)
is wired in behind the same `MalwareScanner` protocol. This is a deliberate
choice: silently treating "no scanner" as "clean" would be worse than
having no integration at all. `EXAM_MALWARE_SCANNER_ENDPOINT` exists as a
typed config placeholder; setting it today only logs a warning that no
adapter reads it yet — it does not silently start "scanning."

### 2. Real parsers for txt/docx/pdf; no OCR

`pypdf`/`python-docx` (pure Python, no native/ML deps — kept out of the
legacy `requirements.txt`, added only to `requirements-exams.txt`) do real
paragraph/page-anchored extraction. Structural validation
(`file_validation.py`) runs BEFORE any parser sees the bytes: magic-byte
sniffing, a zip-bomb guard (entry count, per-entry compression ratio,
cumulative uncompressed size) for the DOCX zip container, embedded-macro
detection (`vbaProject.bin`), and an encryption marker check for both PDF
and DOCX. OCR is not implemented — it needs a real Tesseract binary, which
this environment cannot install/validate; `DocumentParser` is a Protocol so
an OCR implementation can be added later without touching call sites.

**Known limitation:** `run_with_timeout` bounds *observed* wait time via a
thread pool (`ThreadPoolExecutor.result(timeout=...)`), not a hard kill of a
stuck native call — Python has no portable way to do that without a
subprocess. A production deployment should run parsing in an isolated
worker process it can actually kill, the same way Prompt 6 isolates the
code judge.

### 3. SSRF-safe URL resolver, not a full egress-proxy replacement

`url_resolver.py` enforces `https`-only, resolves DNS and rejects
private/loopback/link-local/multicast/reserved/metadata-service addresses,
and re-validates the host at **every** redirect hop (not just the initial
URL) — this is the practically important SSRF-via-redirect case. **Known
limitation:** validation happens immediately before connecting, which closes
the straightforward DNS-rebinding window but is not a hard guarantee against
an adversarial DNS server changing answers between the check and the TCP
handshake. Production should route this through a network-layer
egress-allowlist/proxy, not rely on the application check alone.

### 4. Generation provider is untrusted input, always re-validated

`DeterministicFakeProvider` (default, used in all tests and until a real
provider is configured) produces schema-conforming, seed-reproducible
output — no network call, no real model. `HttpGenerationProviderAdapter` is
a genuine interface stub: it raises until both
`EXAM_GENERATION_PROVIDER_BASE_URL` and `_API_KEY` are configured, and even
then raises `NotImplementedError` — no real provider call was implemented or
tested here (none was configured). Whatever either provider returns is
**never trusted at face value**: `validation.py` reconstructs each
candidate field-by-field from the raw dict (never `Candidate(**raw)`), so a
compromised/malfunctioning provider cannot smuggle extra fields (an
`"approved": true` claim has nowhere to land) and its own `status` claim is
always discarded — status is recomputed from the actual validation issues
found. This is verified directly by
`test_generation_validation.py::test_provider_cannot_self_approve`.

### 5. Structural validation, not execution

`validate_candidate_dict` checks schema conformance, that every cited
anchor actually exists in the supplied source bundle (not merely
well-formed), duplicate `uniqueness_signature`s (against prior history and
within the same batch), objective/difficulty/marks/item-type match against
the request, and — for coding items — that a reference solution and public
tests are *present* per permitted language. It does **not** execute
anything; correctness of a reference solution is Prompt 6's isolated judge's
job. "Incorrect answers" in the test list is interpreted as validation
catching structurally wrong candidates (missing solution, wrong
objective/marks), not as proof that a given answer is mathematically
correct.

### 6. Approval and publication are exclusively faculty actions

`review.py::decide_review_candidate` is the only path to `APPROVED`
(requires `READY_FOR_HUMAN_REVIEW` first — a `NEEDS_REVISION` candidate must
be regenerated or explicitly re-validated, not force-approved) and the only
path to `REJECTED`. Nothing reaches `PUBLISHED` in this phase — immutable
publish of a whole blueprint slot is a natural next step once real question
content (Prompt 5's own remaining scope) and delivery (Prompt 7) exist; this
phase stops at `APPROVED` + variant reservation, which is the piece Prompt 4
already needs a seam for.

### 7. Variant reservation: insufficient pool blocks, balanced reuse is opt-in

`reserve_variants` assigns one **unique** approved candidate per student
whenever the pool is large enough; when it is not, reservation is
**blocked** with an explicit reason unless the caller passes
`allow_balanced_reuse=True`, in which case it round-robins the pool evenly
(never dumping all reuse onto one variant). Assignments are stored in a new,
private `exam_variant_assignments` collection keyed by
`(run_id, uid, slot_index)` — deliberately NOT folded into the Prompt 3/4
`Attempt` model, so this phase's diff does not touch already-shipped exam
domain code. A later phase (real question delivery) reads this collection
by key when resolving what content to serve a given student's current slot.

### 8. Generation is a faculty-authoring action, never invoked during a live attempt

`POST /faculty/generation-requests` and `POST .../reserve-variants` are only
reachable from the faculty router and only make sense before a run is
released (there is no student-facing code path that can trigger generation).
This satisfies "No live generation during student attempts" by construction
— there is no route for a student to reach.

## Consequences

- **Positive:** 63 new tests cover every explicitly listed Prompt 5 scenario
  (malformed provider JSON, nonexistent citations, provider-claimed
  self-approval, invalid parameter domains, duplicate variants, provider
  timeout, insufficient capacity/source, worker-restart idempotency, zip-bomb/
  macro/encryption rejection, SSRF scheme/host/redirect/size guards) with
  real parsing libraries, not mocks, for the document formats that matter.
- **Negative / follow-up required:** no real malware scanner, OCR, or
  generation model is wired in — this phase is honestly a safe scaffold, not
  a working pipeline, until an institution supplies those. No faculty UI
  exists yet (Prompt 7). Publish-time "insufficient verified pool blocks
  publication" is implemented at the reservation-call level; there is no
  blueprint-level "publish" command yet tying it to an immutable exam
  revision — that lands when Prompt 5's remaining UI/publish flow and
  Prompt 7's screens exist together.

## Migration / configuration steps

- New optional settings, all unset/off by default: `EXAM_MALWARE_SCANNER_ENDPOINT`,
  `EXAM_GENERATION_PROVIDER_BASE_URL`, `EXAM_GENERATION_PROVIDER_API_KEY`.
- New Firestore collections (`kt_uploads` (+ `extracted_versions`
  subcollection), `kt_approved_sources`, `exam_blueprints`,
  `generation_requests`, `generation_candidates`,
  `generation_malformed_candidates`, `exam_variant_assignments`) — no rules
  changes needed; covered by the existing default-deny catch-all (comment
  added to `firestore.rules` for documentation).
- New backend dependencies (exam API only, not the legacy app):
  `pypdf==6.19.0`, `python-docx==1.2.0`, `httpx==0.28.1`,
  `python-multipart==0.0.32` in `backend/requirements-exams.txt`.
- **Institutional decisions still needed** before this is production-usable:
  a real malware-scan provider, a real generation-model provider/endpoint,
  and (later) an OCR provider for scanned documents.
