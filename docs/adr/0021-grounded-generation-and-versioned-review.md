# ADR 0021 — Grounded, provider-neutral question generation with versioned review

**Status:** Accepted

## Context

Prompt 5 asks for a blueprint and a provider-neutral generation pipeline: teacher-set topics, counts, item types, marks,
negative marking, difficulty distribution, duration, language allowlist, randomization and variant policy; backend-computed
requested / generated / valid / approved counts that block publication when approved capacity is short; only approved source
excerpts with exact source / version / page anchors sent to the model; no student data; stored provider/model, prompt version,
source versions, job id, retry and cost metadata; structured output with deterministic validation; an independent review;
the lifecycle draft → validating → needs_review → approved → published where the model can never approve or publish; teacher
edit / reject / regenerate with a new version on every substantive edit; variants prepared and assigned **before** the attempt,
and no model call during a live exam. Prompt 0 classified GEN-01…GEN-11 and RND-01 (`requirements-traceability.md` §5).
ADR 0009 built the schema, fake provider, validation skeleton, independent review and the variant seed.

Reading the code against that list found problems beyond the missing features:

1. **The model was never given real source text.** `_resolve_source_bundle` sent the literal string
   `"[excerpt for anchor …]"` and pinned `source_version = 1`; an anchor id was never checked against the extracted version.
2. The **provider's hidden tests were dropped** on the way into the candidate, and a candidate's answer key was never
   checked against its own options.
3. Variant **assignments were overwritten** on every re-reserve (a student could be handed a different question under their feet),
   and the approved pool was read live, so later approvals changed what an already scheduled run served.
4. **Review was not transactional** (an approval could race an edit) and there was no edit at all; `published` was never set.
5. The job runner had **no atomic claim** (two workers or a double click both ran), no retry cap and stored no latency / attempts /
   usage; the real HTTP adapters were stubs.
6. **No answer-key, duplicate-text, option-uniqueness or shuffle-safety checks**; "duplicate" meant an identical signature string.
7. A slot with fewer approved variants than requested was only a **warning** at publication.
8. There was **no option shuffle**, so no per-student answer order existed.

## Decisions

### 1. Grounding is real and fail-closed (`generation/grounding.py`)
The bundle is built server-side from the approved source's **pinned extracted version** (the version the reviewer approved,
not "latest"): each excerpt is the stored anchor text with exact `source_id`, `source_version`, `anchor_id`, `page` and `section`.
A requested anchor that does not exist is refused (`422 unknown_anchor`) before any provider call. With no anchors requested the
server picks the passages most relevant to the slot's objectives and the teacher-confirmed topics (the earlier UI's
`"full-document"` still means this). Budget: 40 passages / 60 000 characters, shared across sources. The bundle's SHA-256, the
source versions and the withheld passages are stored on the request. The request model has no field for student data, so none can
be sent.

### 2. Source text is untrusted data
Passages that address a model (ignore-instructions, role reassignment, prompt disclosure, approval or answer-key demands,
exfiltration, chat-template markup) are **withheld and reported** to the teacher instead of being sent; a source made only of such
text cannot be used (`422 no_usable_passages`). Everything that is sent still travels as data in a structured request with an
explicit `source_text_is_untrusted_data` marker, and nothing the model returns is trusted: the response envelope forbids
`approved`, each candidate is schema- and rule-checked, the model's status claims are ignored, and the independent review can only
pull an item back. The detector is a deterministic pattern list; it reduces, it does not eliminate, injection risk.

### 3. Provider-neutral transport (`generation/provider_http.py`)
One JSON object POSTed over https (http only for localhost), bearer key, bounded timeout, retries with backoff on timeout / 408 /
425 / 429 / 5xx, a 2 MB response cap, invalid-JSON and non-object bodies rejected. The adapter envelope may be bare or
`{"output", "model", "usage"}`. A vendor whose API differs sits behind a thin gateway the institution runs; no vendor is named in
the code. Timeouts surface as `ProviderTimeoutError` (retryable), everything else as `ProviderError`. Tested only against an httpx
mock transport: **no real provider endpoint has been exercised.**

### 4. Job runner (`generation/jobs.py`)
`claim_request` is a compare-and-set transaction: a succeeded request or a live hold by another worker returns nothing (a hold
older than 10 minutes is a crashed worker and may be taken over). Attempts are capped (`EXAM_GENERATION_MAX_ATTEMPTS`, default 3);
`POST …/generation-requests/{id}/retry` re-runs a failed one. Stored per request: provider class, model, prompt version, source
versions, bundle hash, attempts, retries, latency, usage / cost estimate, completion time, last error, excluded passages. A provider
can return at most `requested_variant_count` candidates; one malformed candidate is recorded and does not sink the batch.

### 5. Deterministic validation (`generation/validation.py`)
In addition to the existing schema / anchor-in-bundle / objective / difficulty / marks / signature checks: answer key names existing
options (single: exactly one; multi: canonicalised to option order, not "all options"); option keys and texts unique; options that
point at other options by position ("both A and B", "above") are a blocker when the blueprint shuffles; short-answer rubric points
must total the marks; coding needs a reference solution for **every** permitted language, public tests and hidden test groups with
`stdin` / `expected_output`; **near-duplicate** prompts (3-gram Jaccard ≥ 0.8) against the batch and every live candidate of the
slot. A blocker → `needs_revision`, otherwise `ready_for_human_review`. The independent review additionally has the **backend**
compare the reviewer's blind answer with the key, so a reviewer that says "agree" while answering differently (or not at all) still
pulls the item back.

### 6. Lifecycle and the teacher's authority
`draft → needs_revision | ready_for_human_review → approved → published`; `rejected` and `superseded` are terminal. Only
`POST …/candidates/{id}/review` (a teacher, in a transaction with an audit event) approves; only publishing a revision moves
`approved → published`. A **substantive** edit (question text, options, key, rubric, reference solutions, tests, constraints,
starter code) creates the next version as a new candidate (`version`, `previous_version_id`), re-validates it, clears approval and
independent review, and marks the old one `superseded`; explanation/rationale edits are minor and keep the approval; whitespace-only
changes are no edit. A candidate already frozen in a published revision cannot be edited. `regenerate` asks for fresh candidates
re-grounded on the same sources, passing prior signatures so they differ; the original stays until the teacher rejects it.

### 7. Capacity and the publication gate
`GET …/blueprints/{id}/capacity` computes requested / generated / valid / approved (+ pending, needs-revision, rejected,
failed requests) per slot **from stored candidates**, never from a provider's claim. Publication now **blocks** a slot with fewer
approved variants than requested unless the teacher has explicitly accepted the shortfall (`POST …/shortfall`, reason required,
audited, withdrawable). A slot with **no** approved variant always blocks. The preview shows requested/approved totals and the
accepted shortfall; the revision freezes topics, option shuffle, variant policy and the slots published short. Languages outside the
blueprint allowlist block publication.

### 8. Blueprint spec
`POST …/blueprints` accepts either explicit `slots` or a compact `spec` (question count, per-type counts, marks / minutes /
negative marks per type, difficulty percentages totalling 100, topics, languages, variants per slot) that the server expands
deterministically (largest-remainder distribution, interleaved difficulties). Topics are stored only when `topics_confirmed` is true
(suggestions from `POST …/definitions/{id}/topic-suggestions` — extractive: headings and frequent terms with page-level evidence —
are never stored automatically). Languages must exist in the judge allowlist; slot indexes must be contiguous.

### 9. Variants before the attempt
`POST …/runs/{id}/variants/assign` gives every enrolled student a variant — and, when the revision shuffles, a seeded **option
order** stored with the assignment — for every slot, drawn from the candidates **pinned in the run's revision** (not the live pool),
before anyone starts. Assignments are create-only, so re-running only fills gaps and never moves a student; unique policy needs
enough approved variants (otherwise the slot is reported as blocked), `balanced_reuse` spreads them. The student question route
applies the stored order; the key stays attached to its text, so grading is unaffected. Nothing on the live-exam path calls a model
(asserted by a test).

## Behavior changes (intentional)

| Before | Now |
|---|---|
| `POST /generation-requests` accepted any anchor ids and sent a placeholder excerpt | Real excerpts; unknown anchor `422`; no sources `422 no_sources`; source without extracted content `409` |
| Short approved pool was a publication **warning** | A **blocker** unless the shortfall is explicitly accepted (an empty slot always blocks) |
| `variant_policy` literal on a blueprint | `unique` / `balanced_reuse`, same as the run policy |
| Fake provider option text "Option A …" | "Statement A …" (the former is a positional reference the new shuffle check rightly flags) |
| `exam_variant_assignments` overwritten on re-reserve | Create-only |
| Approved pool = `approved` only | `approved` + `published` (publishing no longer empties it) |
| Candidate options accepted as bare strings | `{key, text}` objects required for choice items |

## Not done / residual risks

- **Coding verification (D-5).** Reference solutions and tests are structurally checked and stored; they are **not executed** in the
  isolated judge, so a coding candidate's `execution_verified` stays false. `EXAM_REQUIRE_EXECUTED_CODING_VERIFICATION` is the
  switch that will make approval require it once the judge-backed step exists; it is off by default.
- **SQL items.** `ItemType` has no SQL type and there is no disposable-database evaluator; adding one needs the judge and a
  decision on the engine. Not claimed.
- **Multi-select grading format (D-8).** The student UI submits an array while the grader compares a string; unchanged here and
  recorded for Prompt 10. Negative marking is still rejected unless `EXAM_NEGATIVE_MARKING_ENABLED`.
- **Real provider.** Both HTTP adapters were tested with a mock transport only; token/cost accounting depends on what the gateway
  returns in `usage`.
- **Injection detection** is heuristic. The structural defences (no approval field, validation, teacher approval, independent
  review) carry the weight, not the pattern list.
- **Near-duplicate detection** is lexical; paraphrases of one idea are not caught.
- `publish_revision` marks candidates `published` after the revision is written; a crash between the two leaves them `approved`,
  which is still servable and gradable (re-publishing is idempotent).

## Decisions needed

| ID | Decision |
|---|---|
| D-5 | Isolated judge for executing reference solutions against tests (and SQL engine choice) |
| D-8 | Multi-select and negative-marking scoring semantics |
| D-13 | Which provider / gateway the institution will run for generation and for the independent review (separate vendors recommended) |

## Test evidence

Backend `EXAMS_ENABLED=false`: 1222 passed (1117 before this phase), `ruff check .` clean. New: `test_generation_p5_domain.py`
(grounding, injection, spec, topics, capacity, validation incl. malformed JSON / nonexistent anchors / wrong answer key /
near-duplicates, independent-review answer check, edit versioning, option order, provider transport incl. timeout and hostile
responses) and `test_exams_generation_routes_p5.py` (HTTP + in-memory Firestore: grounded generation, source injection,
insufficient source, provider timeout and capped retries, fewer-than-requested output, flood cap, review / edit / regenerate,
accepted-shortfall publication, pre-attempt assignment, idempotency, ended runs). Frontend: `tsc` and `eslint` clean, 9 new Vitest
tests (`BlueprintPanel.test.tsx`); `submit/page.test.tsx` can time out under full-suite load and passes alone.
