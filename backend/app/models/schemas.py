from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class UserCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    role: Literal["viewer", "contributor", "placement_cell"] = "viewer"


class NameUpdate(BaseModel):
    """Name change request — limited to once every 30 days."""
    name: str = Field(min_length=1, max_length=120)


class ExperienceCreate(BaseModel):
    company: str = Field(min_length=1, max_length=120)
    role: str = Field(min_length=1, max_length=120)
    year: int = Field(ge=2000, le=2100)
    round: str = Field(min_length=1, max_length=120)
    difficulty: str = Field(min_length=1, max_length=20)
    raw_text: str = Field(min_length=20, max_length=20000)
    is_anonymous: bool = False
    show_name: bool = False
    allow_contact: bool = False
    contact_linkedin: Optional[str] = Field(default=None, max_length=200)
    contact_email: Optional[str] = Field(default=None, max_length=200)
    user_questions: List[str] = Field(
        default_factory=list,
        max_length=120,
        description="Questions explicitly provided by the user — stored verbatim, never filtered or dropped.",
    )

    @field_validator("raw_text")
    @classmethod
    def _normalize_raw_text(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if len(normalized) < 20:
            raise ValueError("raw_text must be at least 20 characters after normalization")
        if len(normalized) > 20000:
            raise ValueError("raw_text must be at most 20000 characters")
        return normalized

    @field_validator("user_questions")
    @classmethod
    def _sanitize_user_questions(cls, values: List[str]) -> List[str]:
        if len(values) > 120:
            raise ValueError("Maximum 120 user questions are allowed")

        cleaned: List[str] = []
        for item in values:
            normalized = " ".join(str(item).split())
            if not normalized:
                continue
            if len(normalized) < 5:
                raise ValueError("Each user question must be at least 5 characters")
            if len(normalized) > 300:
                raise ValueError("Each user question must be at most 300 characters")
            cleaned.append(normalized)
        return cleaned


class ExperienceMetadataUpdate(BaseModel):
    """Controlled metadata edits — only role, year, round, difficulty may be changed.

    Immutable fields (raw_text, summary, questions, topics) are intentionally
    excluded.  Pydantic returns HTTP 422 if the client sends any extra field.
    """
    role: Optional[str] = Field(default=None, min_length=1, max_length=120)
    year: Optional[int] = Field(default=None, ge=2000, le=2100)
    round: Optional[str] = Field(default=None, min_length=1, max_length=120)
    difficulty: Optional[str] = Field(default=None, min_length=1, max_length=20)

    model_config = {"extra": "forbid"}


class AddQuestionsRequest(BaseModel):
    """Questions remembered after initial submission."""
    questions: List[str] = Field(min_length=1, max_length=120, description="List of question texts to add")

    @field_validator("questions")
    @classmethod
    def _sanitize_added_questions(cls, values: List[str]) -> List[str]:
        cleaned: List[str] = []
        for item in values:
            normalized = " ".join(str(item).split())
            if not normalized:
                continue
            if len(normalized) < 5:
                raise ValueError("Each question must be at least 5 characters")
            if len(normalized) > 300:
                raise ValueError("Each question must be at most 300 characters")
            cleaned.append(normalized)

        if not cleaned:
            raise ValueError("At least one valid question is required")
        return cleaned


class AdminQueueFilter(BaseModel):
    status: Literal["all", "pending", "processing", "done", "failed"] = "all"
    active: Literal["all", "active", "hidden"] = "all"
    limit: int = Field(default=50, ge=1, le=200)


class AdminVisibilityUpdate(BaseModel):
    is_active: bool
    note: Optional[str] = Field(default=None, max_length=240)


class ExtractedQuestion(BaseModel):
    question_text: str
    question: str = ""  # Legacy alias for backwards compatibility
    topic: str = "General"
    category: str = "theory"
    confidence: float = 1.0
    question_type: Literal["extracted", "added_later"] = "extracted"
    source: Literal["ai", "user"] = "ai"
    added_later: bool = False
    added_at: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class QuestionStats(BaseModel):
    """Explicit, never-inferred question counts."""
    user_question_count: int = 0
    extracted_question_count: int = 0
    total_question_count: int = 0


class EditHistoryEntry(BaseModel):
    timestamp: str
    field: str
    action: Literal["extracted", "added_later", "metadata_change", "visibility_change", "ai_enrichment"] = "metadata_change"
    old_value: Optional[str] = None
    new_value: Optional[str] = None


class ExperienceResponse(BaseModel):
    id: str
    company: str
    role: str
    year: int
    round: str
    difficulty: str
    raw_text: str
    questions: Optional[dict] = None  # { user_provided: [], ai_extracted: [] }
    extracted_questions: List[ExtractedQuestion] = []  # Legacy flat list for backwards compat
    topics: List[str] = []
    summary: str = ""
    stats: Optional[QuestionStats] = None
    embedding_id: Optional[int] = None
    created_by: str
    created_at: Optional[str] = None
    score: Optional[float] = None
    rerank_score: Optional[float] = None
    match_reason: Optional[str] = None
    is_anonymous: bool = False
    is_active: bool = True
    nlp_status: Literal["pending", "processing", "done", "failed"] = "done"
    edit_history: List[EditHistoryEntry] = []
    contributor_display: Optional[str] = None
    allow_contact: bool = False
    contact_linkedin: Optional[str] = None
    contact_email: Optional[str] = None


class SearchResponse(BaseModel):
    results: List[ExperienceResponse]
    total: int
    total_count: int = 0
    returned_count: int = 0
    has_more: bool = False
    next_cursor: Optional[str] = None
    served_mode: Optional[str] = None
    served_engine: Optional[str] = None


class DashboardResponse(BaseModel):
    total_experiences: int
    topic_frequency: dict
    difficulty_distribution: dict
    frequent_questions: dict = {}
    interview_progression: dict = {}
    contribution_impact: dict = {}
    insights: List[str] = []


# Practice Lists
class PracticeListCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class PracticeListResponse(BaseModel):
    id: str
    name: str
    user_id: str
    created_at: str
    question_count: int = 0
    revised_count: int = 0
    practicing_count: int = 0
    unvisited_count: int = 0
    topic_distribution: dict = {}
    revised_percent: float = 0.0


class PracticeQuestionCreate(BaseModel):
    question_text: str = Field(min_length=1)
    topic: str = Field(default="General")
    difficulty: Optional[str] = None
    source: Literal["manual", "interview_experience"] = "manual"
    source_experience_id: Optional[str] = None
    source_company: Optional[str] = None

    @field_validator("question_text")
    @classmethod
    def _sanitize_practice_question_text(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if len(normalized) < 5:
            raise ValueError("question_text must be at least 5 characters")
        if len(normalized) > 300:
            raise ValueError("question_text must be at most 300 characters")
        return normalized


class PracticeQuestionUpdate(BaseModel):
    question_text: Optional[str] = None
    topic: Optional[str] = None
    difficulty: Optional[str] = None
    status: Optional[Literal["unvisited", "practicing", "revised"]] = None

    @field_validator("question_text")
    @classmethod
    def _sanitize_practice_update_question_text(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        normalized = " ".join(value.split())
        if len(normalized) < 5:
            raise ValueError("question_text must be at least 5 characters")
        if len(normalized) > 300:
            raise ValueError("question_text must be at most 300 characters")
        return normalized


class PracticeQuestionResponse(BaseModel):
    id: str
    list_id: str
    question_text: str
    topic: str
    difficulty: Optional[str]
    status: Literal["unvisited", "practicing", "revised"]
    source: Literal["manual", "interview_experience"]
    source_experience_id: Optional[str]
    source_company: Optional[str]
    created_at: str
