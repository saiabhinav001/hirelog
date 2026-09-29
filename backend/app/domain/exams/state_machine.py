"""Pure state-transition tables for exam runs and attempts. No I/O, no clock —
just legality checks, so these are trivially and exhaustively unit-testable.
"""

from __future__ import annotations

from app.domain.exams.errors import IllegalTransitionError
from app.domain.exams.models import AttemptStatus, RunStatus

# ── Run transitions ───────────────────────────────────────────────────────────
# publish: DRAFT -> PUBLISHED
# release: PUBLISHED|PAUSED -> RELEASED   (release is also "resume")
# pause:   RELEASED -> PAUSED
# close:   RELEASED|PAUSED -> CLOSED
# end:     CLOSED -> ENDED                (hard stop, terminal)
RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.DRAFT: frozenset({RunStatus.PUBLISHED}),
    RunStatus.PUBLISHED: frozenset({RunStatus.RELEASED}),
    RunStatus.RELEASED: frozenset({RunStatus.PAUSED, RunStatus.CLOSED}),
    RunStatus.PAUSED: frozenset({RunStatus.RELEASED, RunStatus.CLOSED}),
    RunStatus.CLOSED: frozenset({RunStatus.ENDED}),
    RunStatus.ENDED: frozenset(),
}

RUN_COMMAND_TARGET: dict[str, RunStatus] = {
    "publish": RunStatus.PUBLISHED,
    "release": RunStatus.RELEASED,
    "pause": RunStatus.PAUSED,
    "resume": RunStatus.RELEASED,
    "close": RunStatus.CLOSED,
    "end": RunStatus.ENDED,
}


def can_transition_run(current: RunStatus, target: RunStatus) -> bool:
    return target in RUN_TRANSITIONS.get(current, frozenset())


def require_run_transition(current: RunStatus, target: RunStatus) -> None:
    if not can_transition_run(current, target):
        raise IllegalTransitionError("run", current.value, target.value)


# ── Attempt transitions ───────────────────────────────────────────────────────
ATTEMPT_TRANSITIONS: dict[AttemptStatus, frozenset[AttemptStatus]] = {
    AttemptStatus.NOT_STARTED: frozenset({AttemptStatus.ADMITTED}),
    AttemptStatus.ADMITTED: frozenset({AttemptStatus.IN_PROGRESS}),
    AttemptStatus.IN_PROGRESS: frozenset(
        {AttemptStatus.SUBMITTED, AttemptStatus.EXPIRED, AttemptStatus.BLOCKED}
    ),
    # Reserved for Prompt 8: unblock resumes progress or the window still expires.
    AttemptStatus.BLOCKED: frozenset({AttemptStatus.IN_PROGRESS, AttemptStatus.EXPIRED}),
    AttemptStatus.SUBMITTED: frozenset({AttemptStatus.FINALIZED}),
    AttemptStatus.EXPIRED: frozenset({AttemptStatus.FINALIZED}),
    AttemptStatus.FINALIZED: frozenset(),  # terminal — never reopened, even by faculty preview
}


def can_transition_attempt(current: AttemptStatus, target: AttemptStatus) -> bool:
    return target in ATTEMPT_TRANSITIONS.get(current, frozenset())


def require_attempt_transition(current: AttemptStatus, target: AttemptStatus) -> None:
    if not can_transition_attempt(current, target):
        raise IllegalTransitionError("attempt", current.value, target.value)
