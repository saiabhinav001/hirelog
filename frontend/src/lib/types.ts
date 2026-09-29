export type ExtractedQuestion = {
  question_text: string;
  question: string; // Legacy alias
  topic: string;
  category: string;
  confidence: number;
  added_later?: boolean;
  added_at?: string;
  question_type?: "extracted" | "added_later";
  source?: "ai" | "user";
  created_at?: string;
  updated_at?: string;
};

export type QuestionsNested = {
  user_provided: ExtractedQuestion[];
  ai_extracted: ExtractedQuestion[];
};

export type QuestionStats = {
  user_question_count: number;
  extracted_question_count: number;
  total_question_count: number;
};

export type EditHistoryEntry = {
  timestamp: string;
  field: string;
  old_value?: string;
  new_value?: string;
  action?: "extracted" | "added_later" | "metadata_change" | "visibility_change" | "ai_enrichment";
};

export type AuthorIdentity = {
  uid?: string;
  visibility: "anonymous" | "public";
  public_label?: string;
};

export type Experience = {
  id: string;
  company: string;
  role: string;
  year: number;
  round: string;
  difficulty: string;
  raw_text: string;
  questions?: QuestionsNested;
  extracted_questions: ExtractedQuestion[];
  topics: string[];
  summary: string;
  stats?: QuestionStats;
  embedding_id?: number;
  created_by: string;
  contributor_name?: string;
  contributor_display?: string;
  author?: AuthorIdentity;
  show_name?: boolean;
  created_at?: string;
  score?: number;
  rerank_score?: number;
  match_reason?: string;
  is_anonymous?: boolean;
  is_active?: boolean;
  nlp_status?: "pending" | "processing" | "done" | "failed";
  edit_history?: EditHistoryEntry[];
  allow_contact?: boolean;
  contact_linkedin?: string;
  contact_email?: string;
};

export type SearchResponse = {
  results: Experience[];
  total: number;
  total_count?: number;
  returned_count?: number;
  has_more?: boolean;
  next_cursor?: string | null;
  served_mode?: string;
  served_engine?: string;
};

export type ContributionImpact = {
  experiences_submitted: number;
  questions_extracted: number;
  archive_size: number;
};

export type DashboardResponse = {
  total_experiences: number;
  topic_frequency: Record<string, Record<string, number>>;
  difficulty_distribution: Record<string, number>;
  frequent_questions: Record<string, number>;
  interview_progression: Record<string, {
    stages: Record<string, { topics: string[]; frequency: number }>;
    total_experiences: number;
  }>;
  data_freshness?: {
    generated_at?: string | null;
    freshness_seconds?: number | null;
  };
  contribution_impact: ContributionImpact;
  insights: string[];
};

export type PlacementCellAdminResponse = {
  archive_overview: {
    total_sampled: number;
    active: number;
    hidden: number;
  };
  privacy_breakdown: {
    anonymous: number;
    public: number;
  };
  quality_metrics: {
    avg_questions_per_experience: number;
    avg_user_questions_per_experience: number;
    contact_opt_in_rate_percent: number;
    nlp_done_rate_percent: number;
  };
  freshness: {
    last_30_days: number;
    last_90_days: number;
  };
  nlp_pipeline: Record<string, number>;
  year_distribution: Record<string, number>;
  submission_role_distribution: Record<string, number>;
  company_distribution: Record<string, number>;
  difficulty_distribution: Record<string, number>;
  moderation: {
    hidden_count: number;
    nlp_failed_count: number;
    failed_examples: Array<{
      id: string;
      company: string;
      year: number;
      created_at: string | null;
    }>;
  };
  top_contributors: Array<{
    uid: string;
    display_name: string;
    submissions: number;
  }>;
  search_runtime?: {
    requests_total: number;
    cache_hits: number;
    cache_hit_rate_percent: number;
    cache_backend?: string;
    semantic_requested_total: number;
    semantic_success_total: number;
    semantic_success_rate_percent: number;
    mode_counts: Record<string, number>;
    fallback_counts: Record<string, number>;
    latency_ms: {
      avg: number;
      p50: number;
      p95: number;
      p99: number;
    };
    semantic_circuit: {
      open: boolean;
      cooldown_remaining_ms: number;
      recent_failure_count: number;
      failure_window_seconds: number;
      failure_threshold: number;
    };
    query_analytics?: {
      tracked_queries: number;
      zero_result_queries: number;
      zero_result_rate_percent: number;
      top_queries: Record<string, number>;
      top_zero_result_queries: Record<string, number>;
      filter_usage: Record<string, number>;
    };
    index_queue?: {
      enabled: boolean;
      started: boolean;
      workers: number;
      queued: number;
    };
  };
};

export type ModerationQueueItem = {
  id: string;
  company: string;
  role: string;
  year: number;
  round: string;
  difficulty: string;
  is_active: boolean;
  nlp_status: "pending" | "processing" | "done" | "failed" | "unknown";
  created_at?: string;
  created_by: string;
  contributor_display: string;
  question_count: number;
  user_question_count: number;
  raw_text_preview: string;
};

export type ModerationQueueResponse = {
  filters: {
    status: "all" | "pending" | "processing" | "done" | "failed";
    active: "all" | "active" | "hidden";
    limit: number;
  };
  results: ModerationQueueItem[];
  total: number;
  sampled: number;
};

export type SearchRuntimeAdminActionResponse = {
  status: string;
  search_runtime?: PlacementCellAdminResponse["search_runtime"];
  warmup?: {
    status: string;
    query_vector_cache_entries: number;
  };
  cache?: {
    status: string;
    search_cache_entries: number;
    query_vector_cache_entries: number;
  };
};

// Practice Lists
export type PracticeList = {
  id: string;
  name: string;
  user_id: string;
  created_at: string;
  question_count: number;
  revised_count: number;
  practicing_count: number;
  unvisited_count: number;
  topic_distribution: Record<string, number>;
  revised_percent: number;
};

export type QuestionStatus = "unvisited" | "practicing" | "revised";
export type QuestionSource = "manual" | "interview_experience";

export type PracticeQuestion = {
  id: string;
  list_id: string;
  question_text: string;
  topic: string;
  difficulty?: string;
  status: QuestionStatus;
  source: QuestionSource;
  source_experience_id?: string;
  source_company?: string;
  created_at: string;
};

export type PracticeListCreate = {
  name: string;
};

export type PracticeQuestionCreate = {
  question_text: string;
  topic: string;
  difficulty?: string;
  source?: QuestionSource;
  source_experience_id?: string;
  source_company?: string;
};

export type PracticeQuestionUpdate = {
  question_text?: string;
  topic?: string;
  difficulty?: string;
  status?: QuestionStatus;
};
