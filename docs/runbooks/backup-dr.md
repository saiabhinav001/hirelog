# Runbook — Backup & Disaster Recovery

Scope: what state HireLog holds, how it's protected, and how to recover it.

## State inventory

| Store | Role | Durability | Rebuildable from |
|---|---|---|---|
| **Firestore** | System of record (experiences, users, practice lists, metadata) | Managed, replicated by GCP | — (source of truth) |
| **FAISS index** | Vector index for semantic search | Local disk (often ephemeral) | **Yes** — Firestore, via startup reconciliation |
| **Redis** (optional) | Search cache + rate-limit windows | Ephemeral by design | N/A — safe to lose (repopulates) |
| **Typesense** (optional) | Alternate lexical engine | Depends on deployment | Firestore backfill queue |

**Key property:** Firestore is the only authoritative store. Everything else is a
derived cache/index and is reconstructable from it.

## Recovery objectives

| Scenario | RPO | RTO | Procedure |
|---|---|---|---|
| FAISS index lost (disk wiped / new container) | 0 (no data loss) | Minutes | Automatic — see below |
| Redis lost | 0 | Seconds | Automatic — caches/limits repopulate; no action |
| Firestore data loss / corruption | ≤ backup interval | Hours | Restore from export (below) |
| Bad deploy | 0 | Minutes | Roll back to previous image; startup reconciles derived state |

## FAISS recovery (automatic)

On startup, `faiss_reconcile.rebuild_faiss_from_firestore()` checks the index. If
it is empty (lost disk) while Firestore has experiences, it re-embeds active
documents and rebuilds the index. No manual action is required; the only cost is
a one-time re-embedding pass. To avoid that cost, mount a persistent volume and
point `FAISS_DIR` at it (see `backend/Dockerfile`).

Force a manual rebuild (e.g. after suspected drift):

```python
from app.services.faiss_reconcile import rebuild_faiss_from_firestore
rebuild_faiss_from_firestore(force=True)
```

## Firestore backup

Firestore is authoritative, so it must be backed up independently of the app.

**Scheduled export** (recommended — GCP-native, no app involvement):

```bash
# One-off export to a GCS bucket
gcloud firestore export gs://<BACKUP_BUCKET>/$(date +%F)

# Recurring: enable scheduled backups (daily retention) once per project
gcloud firestore backups schedules create --database='(default)' \
  --recurrence=daily --retention=7d
```

**Restore:**

```bash
gcloud firestore import gs://<BACKUP_BUCKET>/<EXPORT_PATH>
```

After a Firestore restore, restart the backend (or run the forced FAISS rebuild
above) so the vector index and dashboard caches reconcile to the restored data.

## Exams module — private collections and evidence storage (Prompt 11)

The exam domain's Firestore collections (`exam_definitions` + `revisions`
subcollection, `exam_runs` + `attempts`/`drafts` subcollections,
`exam_command_receipts`, `exam_outbox`, `kt_uploads`, `kt_approved_sources`,
`exam_blueprints`, `generation_requests`, `generation_candidates`,
`generation_malformed_candidates`, `exam_variant_assignments`,
`institutions`, `institution_memberships`, `institution_roster_imports`,
`institution_otp_challenges`, gradebook/similarity/feedback collections)
live in the **same Firestore project/database** as the legacy collections —
the `gcloud firestore export`/`import` commands above back up and restore
them automatically, with no exam-specific procedure needed. They are
"private" only in the `firestore.rules` sense (no client rule grants them;
Admin-SDK-only, covered by the default-deny catch-all) — not in a separate
database requiring separate export commands.

**Gap: evidence storage is not covered by this runbook.** Proctoring
evidence blobs (`EXAM_EVIDENCE_STORAGE_BUCKET`, a Firebase Storage bucket —
see ADR 0013) are **not** Firestore documents and are **not** included in the
`gcloud firestore export`/`import` commands above. This bucket needs its own
backup/retention strategy (e.g. GCS bucket versioning + a lifecycle policy
matching `EXAM_EVIDENCE_RETENTION_DAYS`) before a real deployment — not yet
documented anywhere in this repo. Track this as an open item in
[institution-onboarding-checklist.md](../exams/institution-onboarding-checklist.md).

**Rollback vs. restore:** the levers in
[rollback-runbook.md](../exams/rollback-runbook.md) (per-run pause,
new-admissions-pause, module-wide disable) are traffic/admission controls,
not data-recovery procedures — none of them delete data, and none of them
require or trigger a Firestore restore. Only use the restore procedure above
for genuine data loss/corruption, never as a rollback mechanism.

## Post-incident checklist

1. Confirm Firestore document counts look sane (`/api/dashboard/stats` →
   `total_experiences`, which is an exact `count()` aggregation).
2. Confirm FAISS vector count via `/health/deep` (`vectors`) roughly matches
   active experiences; if 0 with data present, trigger the forced rebuild.
3. Confirm search returns semantic results for a known query.
4. Check logs (JSON, filter by `request_id`) for reconciliation summaries.
