"""Queue and lag figures an operator alerts on (Prompt 11, OPS-05): outbox backlog and dead entries, judge submissions
waiting / retrying / exhausted, and attempts still unsealed after their deadline. Read-only; reuses the repositories the
workers use. Counts only: no student identifiers, answers or payloads leave this module.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Optional

from firebase_admin import firestore

from app.domain.exams.models import RunStatus
from app.domain.exams.repository import OUTBOX_COLLECTION, AttemptRepository, ExamRunRepository
from app.domain.judge.repository import SubmissionRepository


def _age_seconds(now: datetime, value) -> Optional[float]:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return None if value is None else max(0.0, (now - value).total_seconds())


def _oldest(ages: list[Optional[float]]) -> Optional[float]:
    known = [a for a in ages if a is not None]
    return round(max(known), 1) if known else None


def collect_queue_health(db, *, now: datetime, outbox_max_attempts: int, scan_limit: int = 2000) -> dict:
    # Outbox: unprocessed entries. `dead` ones reached the attempt limit and will not be retried without an operator.
    docs = list(
        db.collection(OUTBOX_COLLECTION).where(filter=firestore.FieldFilter("processed", "==", False)).limit(scan_limit).stream()
    )
    entries = [d.to_dict() or {} for d in docs]
    outbox = {
        "pending": len(entries),
        "oldest_age_seconds": _oldest([_age_seconds(now, e.get("created_at")) for e in entries]),
        "dead": sum(1 for e in entries if int(e.get("attempts", 0)) >= outbox_max_attempts),
        "with_errors": sum(1 for e in entries if e.get("last_error")),
        "by_kind": dict(Counter(e.get("kind", "?") for e in entries)),
        "truncated": len(entries) >= scan_limit,
    }

    submissions = SubmissionRepository(db)
    waiting = submissions.list_pending_or_polling(limit=scan_limit)
    retryable = submissions.list_infra_errors(limit=scan_limit)
    exhausted = submissions.list_by_status(["infra_exhausted"], limit=scan_limit)
    judge = {
        "waiting": len(waiting),
        "oldest_waiting_age_seconds": _oldest([_age_seconds(now, s.created_at) for s in waiting]),
        "infra_error_retrying": len(retryable),
        "infra_exhausted_needs_operator": len(exhausted),
        "max_attempts_seen": max([s.attempts for s in waiting + retryable + exhausted], default=0),
    }

    # Finalization lag: attempts the sweeper should already have sealed.
    runs = ExamRunRepository(db).list_by_statuses([RunStatus.RELEASED, RunStatus.PAUSED, RunStatus.CLOSED])
    attempts = AttemptRepository(db)
    overdue_lags: list[float] = []
    open_attempts = 0
    for run in runs:
        for attempt in attempts.list_in_progress(run.id) + attempts.list_blocked(run.id):
            open_attempts += 1
            lag = _age_seconds(now, attempt.deadline)
            if attempt.deadline is not None and lag is not None and now > attempt.deadline:
                overdue_lags.append(lag)
    finalization = {
        "active_runs": len(runs),
        "open_attempts": open_attempts,
        "overdue_unsealed": len(overdue_lags),
        "max_overdue_seconds": round(max(overdue_lags), 1) if overdue_lags else None,
    }
    return {"generated_at": now.isoformat(), "outbox": outbox, "judge": judge, "finalization": finalization}
