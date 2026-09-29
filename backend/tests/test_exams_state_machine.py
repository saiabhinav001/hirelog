"""Legal/illegal state transition tests for exam runs and attempts."""

from __future__ import annotations

import pytest

from app.domain.exams.errors import IllegalTransitionError
from app.domain.exams.models import AttemptStatus, RunStatus
from app.domain.exams.state_machine import (
    can_transition_attempt,
    can_transition_run,
    require_attempt_transition,
    require_run_transition,
)


@pytest.mark.parametrize(
    "current,target",
    [
        (RunStatus.DRAFT, RunStatus.PUBLISHED),
        (RunStatus.PUBLISHED, RunStatus.RELEASED),
        (RunStatus.RELEASED, RunStatus.PAUSED),
        (RunStatus.RELEASED, RunStatus.CLOSED),
        (RunStatus.PAUSED, RunStatus.RELEASED),
        (RunStatus.PAUSED, RunStatus.CLOSED),
        (RunStatus.CLOSED, RunStatus.ENDED),
    ],
)
def test_legal_run_transitions(current: RunStatus, target: RunStatus) -> None:
    assert can_transition_run(current, target)
    require_run_transition(current, target)  # must not raise


@pytest.mark.parametrize(
    "current,target",
    [
        (RunStatus.DRAFT, RunStatus.RELEASED),  # skipping publish
        (RunStatus.DRAFT, RunStatus.CLOSED),
        (RunStatus.PUBLISHED, RunStatus.CLOSED),  # must release first
        (RunStatus.RELEASED, RunStatus.PUBLISHED),  # never go backwards
        (RunStatus.CLOSED, RunStatus.RELEASED),  # closed cannot reopen
        (RunStatus.ENDED, RunStatus.RELEASED),  # ended is terminal
        (RunStatus.ENDED, RunStatus.CLOSED),
    ],
)
def test_illegal_run_transitions_raise(current: RunStatus, target: RunStatus) -> None:
    assert not can_transition_run(current, target)
    with pytest.raises(IllegalTransitionError) as exc:
        require_run_transition(current, target)
    assert exc.value.code == "illegal_transition"


@pytest.mark.parametrize(
    "current,target",
    [
        (AttemptStatus.NOT_STARTED, AttemptStatus.ADMITTED),
        (AttemptStatus.ADMITTED, AttemptStatus.IN_PROGRESS),
        (AttemptStatus.IN_PROGRESS, AttemptStatus.SUBMITTED),
        (AttemptStatus.IN_PROGRESS, AttemptStatus.EXPIRED),
        (AttemptStatus.IN_PROGRESS, AttemptStatus.BLOCKED),
        (AttemptStatus.BLOCKED, AttemptStatus.IN_PROGRESS),
        (AttemptStatus.BLOCKED, AttemptStatus.EXPIRED),
        (AttemptStatus.SUBMITTED, AttemptStatus.FINALIZED),
        (AttemptStatus.EXPIRED, AttemptStatus.FINALIZED),
    ],
)
def test_legal_attempt_transitions(current: AttemptStatus, target: AttemptStatus) -> None:
    assert can_transition_attempt(current, target)
    require_attempt_transition(current, target)


@pytest.mark.parametrize(
    "current,target",
    [
        (AttemptStatus.NOT_STARTED, AttemptStatus.IN_PROGRESS),  # must admit first
        (AttemptStatus.FINALIZED, AttemptStatus.IN_PROGRESS),  # never reopen
        (AttemptStatus.FINALIZED, AttemptStatus.SUBMITTED),
        (AttemptStatus.SUBMITTED, AttemptStatus.IN_PROGRESS),  # no going back
        (AttemptStatus.EXPIRED, AttemptStatus.IN_PROGRESS),
    ],
)
def test_illegal_attempt_transitions_raise(current: AttemptStatus, target: AttemptStatus) -> None:
    assert not can_transition_attempt(current, target)
    with pytest.raises(IllegalTransitionError):
        require_attempt_transition(current, target)


def test_finalized_attempt_has_no_legal_targets() -> None:
    """Never reopened, even by faculty preview/navigation (contract #4)."""
    from app.domain.exams.state_machine import ATTEMPT_TRANSITIONS

    assert ATTEMPT_TRANSITIONS[AttemptStatus.FINALIZED] == frozenset()


def test_ended_run_has_no_legal_targets() -> None:
    from app.domain.exams.state_machine import RUN_TRANSITIONS

    assert RUN_TRANSITIONS[RunStatus.ENDED] == frozenset()
