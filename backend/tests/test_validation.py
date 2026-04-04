from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.schemas import AddQuestionsRequest, ExperienceCreate, PracticeQuestionCreate


def test_experience_create_normalizes_raw_text() -> None:
    payload = ExperienceCreate(
        company="Example Co",
        role="SDE",
        year=2025,
        round="Round 1",
        difficulty="Medium",
        raw_text="  This    is a sufficiently long interview experience text for validation.  ",
        user_questions=["  Explain ACID properties in DBMS?  "],
    )
    assert payload.raw_text.startswith("This is")
    assert payload.user_questions == ["Explain ACID properties in DBMS?"]


def test_experience_create_rejects_short_user_question() -> None:
    with pytest.raises(ValidationError):
        ExperienceCreate(
            company="Example Co",
            role="SDE",
            year=2025,
            round="Round 1",
            difficulty="Medium",
            raw_text="This is a sufficiently long interview experience text for validation.",
            user_questions=["bad"],
        )


def test_add_questions_request_requires_valid_question() -> None:
    payload = AddQuestionsRequest(questions=["   Explain CAP theorem with an example.   "])
    assert payload.questions == ["Explain CAP theorem with an example."]


def test_practice_question_validation() -> None:
    payload = PracticeQuestionCreate(question_text="   How does consistent hashing work?   ")
    assert payload.question_text == "How does consistent hashing work?"

    with pytest.raises(ValidationError):
        PracticeQuestionCreate(question_text="no")
