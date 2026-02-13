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
  match_reason?: string;
  is_anonymous?: boolean;
  is_active?: boolean;
  nlp_status?: "pending" | "processing" | "done" | "failed";
  edit_history?: EditHistoryEntry[];
};

export type SearchResponse = {
  results: Experience[];
  total: number;
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
  contribution_impact: ContributionImpact;
  insights: string[];
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
