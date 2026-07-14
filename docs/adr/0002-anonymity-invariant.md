# ADR 0002 — Anonymity invariant for contributions

**Status:** Accepted

## Context

Contributors may submit interview experiences anonymously. Anonymity is a
privacy promise: once a contribution is anonymous, no later processing may
re-attach identity. The risk is background enrichment (NLP topic tagging,
summaries, embeddings) accidentally writing identity fields back onto a document.

## Decision

Identity fields — `is_anonymous`, `author`, `show_name`, `contributor_name`,
`created_by` — form an **invariant set** that background enrichment must never
write. Every enrichment write path checks the outgoing update against this set
and **aborts** (raising, logging `critical`) if any identity field is present.

The check is an explicit `if … raise`, **not** a Python `assert`: assertions are
stripped under `python -O` / `PYTHONOPTIMIZE`, which would silently disable the
guard in an optimized production image. Both enrichment paths
(`_run_background_nlp` and the add-questions enrichment) enforce it identically.

## Consequences

- **Positive**: the privacy guarantee is enforced in code, survives optimized
  builds, and fails loud (critical log + aborted write) instead of silently
  leaking identity.
- **Negative**: enrichment writes carry a small guard cost and must keep the
  field set in sync if the schema grows; a violation aborts enrichment for that
  document (it is marked failed and can be reprocessed) rather than partially
  applying.
