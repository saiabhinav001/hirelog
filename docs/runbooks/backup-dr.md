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

## Post-incident checklist

1. Confirm Firestore document counts look sane (`/api/dashboard/stats` →
   `total_experiences`, which is an exact `count()` aggregation).
2. Confirm FAISS vector count via `/health/deep` (`vectors`) roughly matches
   active experiences; if 0 with data present, trigger the forced rebuild.
3. Confirm search returns semantic results for a known query.
4. Check logs (JSON, filter by `request_id`) for reconciliation summaries.
