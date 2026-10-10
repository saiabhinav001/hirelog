/**
 * Typed wrappers around `examApiFetch` for the endpoints the Prompt 7
 * screens call. Centralizes the request/response shapes in one place
 * instead of repeating them per page.
 */

import { examApiFetch } from "@/lib/examsApi";

export type ExamDefinition = {
  id: string;
  title: string;
  owner_uid: string;
  institution_id?: string | null;
  latest_published_revision_id: string | null;
};

export type ExamRunSummary = {
  id: string;
  definition_id: string;
  revision_id: string;
  status: "draft" | "published" | "released" | "paused" | "closed" | "ended";
  version: number;
  enrolled_uids: string[];
};

export type StudentRunSummary = {
  run_id: string;
  definition_id: string;
  status: string;
  version: number;
  released_at: string | null;
  attempt_status: string | null;
  deadline: string | null;
};

export type Attempt = {
  run_id: string;
  uid: string;
  status: "not_started" | "admitted" | "in_progress" | "submitted" | "expired" | "blocked" | "finalized";
  version: number;
  epoch: number;
  deadline: string | null;
  warning_count: number;
  current_slot_index: number;
  submit_receipt: { receipt_id: string; finalized_at: string } | null;
};

export type PublicQuestion = {
  question_id: string;
  slot_index: number;
  total_slots: number;
  public_content: Record<string, unknown>;
};

export type MyAccommodations = {
  extra_minutes: number;
  camera_waived: boolean;
};

export type LockdownProfile = {
  run_id: string;
  enabled: boolean;
  expected_config_key_hash: string | null;
  expected_browser_exam_key_hashes: string[];
  piloted_platforms: string[];
  updated_by: string | null;
  updated_at: string | null;
};

export type LockdownAdmissionLogEntry = {
  id: string;
  run_id: string;
  uid: string;
  outcome: "admitted" | "denied_not_configured" | "denied_missing_headers" | "denied_hash_mismatch";
  config_key_header_present: boolean;
  request_hash_header_present: boolean;
  at: string;
};

export type GenerationCandidate = {
  id: string;
  item_type: "single_choice" | "multiple_select" | "short_answer" | "coding";
  public_prompt: string;
  status: string;
  marks: number;
};

export type ApprovedSourceSummary = {
  id: string;
  kind: "upload" | "url";
  review_status: "pending" | "approved" | "rejected";
  url?: string;
};

export type IncidentLogEntry = {
  id: string;
  kind: string;
  counted: boolean;
  client_timestamp: string;
  received_at: string;
  duration_seconds: number | null;
  note: string;
};

export type Appeal = {
  id: string;
  run_id: string;
  uid: string;
  student_statement: string;
  status: "pending" | "upheld" | "overturned";
  created_at: string;
  resolved_by: string | null;
  resolved_at: string | null;
  resolution_notes: string | null;
  version: number;
};

export type EvidenceSnapshotSummary = {
  id: string;
  kind: "check_in" | "periodic" | "submission";
  status: "requested" | "uploaded" | "verified" | "missing";
  requested_at: string;
  missing_reason: string | null;
};

// ── Gradebook / similarity / export / feedback (Prompt 9) ───────────────────

export type QuestionOutcome = "blank" | "pending" | "partially_solved" | "fully_solved";

export type StudentQuestionResult = {
  uid: string;
  question_id: string;
  slot_index: number;
  item_type: string;
  presented: boolean;
  outcome: QuestionOutcome;
  weighted_score: number;
  max_score: number;
  grading_version: number;
  tests_passed: number | null;
  tests_total: number | null;
  failure_category: string | null;
  submission_hash: string | null;
};

export type IntegrityDisposition = "clean" | "blocked_pending_review" | "blocked_upheld" | "blocked_resolved_continued";

export type StudentGradebookRow = {
  uid: string;
  attempt_status: string;
  finalized: boolean;
  raw_score: number;
  raw_max_score: number;
  integrity_disposition: IntegrityDisposition;
  warning_count: number;
  question_results: StudentQuestionResult[];
};

export type GradebookCounts = {
  question_id: string;
  assigned: number;
  presented: number;
  finalized: number;
  nonblank_attempted: number;
  fully_solved: number;
  partially_solved: number;
  blank: number;
  pending: number;
};

export type ReleaseRecord = {
  id: string;
  run_id: string;
  version: number;
  released_by: string;
  released_at: string;
  note: string;
};

export type DefectiveQuestionResolution = {
  id: string;
  run_id: string;
  question_id: string;
  resolution: "full_credit_all" | "exclude_from_total";
  reason: string;
  resolved_by: string;
  resolved_at: string;
};

export type GradeAppeal = {
  id: string;
  run_id: string;
  uid: string;
  question_id: string;
  student_statement: string;
  status: "pending" | "upheld" | "corrected";
  created_at: string;
  resolved_by: string | null;
  resolved_at: string | null;
  resolution_notes: string | null;
  version: number;
};

export type SimilarityMatch = {
  id: string;
  run_id: string;
  question_id: string;
  language: string;
  uid_a: string;
  uid_b: string;
  submission_id_a: string;
  submission_id_b: string;
  similarity_percent: number;
  matched_spans: Record<string, unknown>[];
  provider: string;
  computed_at: string;
};

export type ExportJob = {
  id: string;
  run_id: string;
  requested_by: string;
  format: "csv" | "json";
  status: "pending" | "running" | "succeeded" | "failed";
  created_at: string;
  completed_at: string | null;
  storage_ref: string | null;
  row_count: number | null;
  error: string | null;
};

export type LearningFeedback = {
  run_id: string;
  uid: string;
  strengths: string[];
  gaps: string[];
  practice_steps: string[];
  source_anchors: string[];
  missing_material: string[];
  generated_at: string;
};

async function authed<T>(path: string, token: string | undefined, init: RequestInit = {}): Promise<T> {
  return examApiFetch<T>(path, init, token);
}

export type InstitutionSummary = { id: string; name: string; can_administer: boolean };
export type InstitutionList = { is_platform_operator: boolean; results: InstitutionSummary[] };
export type InstitutionMember = {
  identity_key: string;
  role_scope: string;
  status: string;
  linked: boolean;
  contact_email?: string;
};
export type RosterRowInput = { identity_key: string; contact_email: string; role_scope: string };
export type RosterRowResult = {
  row_index: number;
  identity_key: string;
  ok: boolean;
  action: string;
  error: string | null;
};
export type RosterJob = {
  id: string;
  institution_id: string;
  status: string;
  total_rows: number;
  applied_cursor: number;
  row_results: RosterRowResult[];
};

const institutionPath = (institutionId: string) => `/api/exams/v1/faculty/institutions/${encodeURIComponent(institutionId)}`;

export const examEndpoints = {
  // ── Faculty ──────────────────────────────────────────────────────────────
  listMyDefinitions: (token?: string) => authed<{ results: ExamDefinition[] }>("/api/exams/v1/faculty/definitions", token),
  createDefinition: (token: string | undefined, title: string, institutionId?: string) =>
    authed<ExamDefinition>("/api/exams/v1/faculty/definitions", token, {
      method: "POST",
      body: JSON.stringify(institutionId ? { title, institution_id: institutionId } : { title }),
    }),

  // Institutions and rosters (faculty)
  listInstitutions: (token?: string) => authed<InstitutionList>("/api/exams/v1/faculty/institutions", token),
  createInstitution: (token: string | undefined, name: string) =>
    authed<{ id: string; name: string }>("/api/exams/v1/faculty/institutions", token, {
      method: "POST",
      body: JSON.stringify({ name }),
    }),
  listInstitutionMembers: (token: string | undefined, institutionId: string) =>
    authed<{ results: InstitutionMember[] }>(`${institutionPath(institutionId)}/members`, token),
  rosterDryRun: (token: string | undefined, institutionId: string, rows: RosterRowInput[]) =>
    authed<RosterJob>(`${institutionPath(institutionId)}/roster/dry-run`, token, {
      method: "POST",
      body: JSON.stringify({ rows }),
    }),
  rosterApply: (token: string | undefined, institutionId: string, jobId: string, rows: RosterRowInput[]) =>
    authed<RosterJob>(`${institutionPath(institutionId)}/roster/${encodeURIComponent(jobId)}/apply`, token, {
      method: "POST",
      body: JSON.stringify({ rows }),
    }),
  revokeMembership: (token: string | undefined, institutionId: string, identityKey: string) =>
    authed(`${institutionPath(institutionId)}/memberships/revoke`, token, {
      method: "POST",
      body: JSON.stringify({ identity_key: identityKey }),
    }),

  listRunsForDefinition: (token: string | undefined, definitionId: string) =>
    authed<{ results: ExamRunSummary[] }>(`/api/exams/v1/faculty/definitions/${definitionId}/runs`, token),
  listBlueprintsForDefinition: (token: string | undefined, definitionId: string) =>
    authed<{ results: { id: string; title: string; slots: unknown[] }[] }>(
      `/api/exams/v1/faculty/definitions/${definitionId}/blueprints`,
      token
    ),
  listCandidatesForBlueprint: (token: string | undefined, blueprintId: string) =>
    authed<{ results: GenerationCandidate[] }>(`/api/exams/v1/faculty/blueprints/${blueprintId}/candidates`, token),
  createGenerationRequest: (
    token: string | undefined,
    payload: { blueprint_id: string; slot_index: number; source_anchors: { source_id: string; anchor_id: string }[]; deterministic_seed: number }
  ) =>
    authed<{ status: string; ready_count: number; needs_revision_count: number; shortfall: number; error: string | null }>(
      "/api/exams/v1/faculty/generation-requests",
      token,
      { method: "POST", body: JSON.stringify(payload) }
    ),
  reserveVariantsForSlot: (
    token: string | undefined,
    blueprintId: string,
    slotIndex: number,
    payload: { run_id: string; uids: string[]; allow_balanced_reuse?: boolean }
  ) =>
    authed<{ assignments: Record<string, string> }>(
      `/api/exams/v1/faculty/blueprints/${blueprintId}/slots/${slotIndex}/reserve-variants`,
      token,
      { method: "POST", body: JSON.stringify(payload) }
    ),
  reviewCandidate: (token: string | undefined, candidateId: string, decision: "approve" | "reject", rejectionReason?: string) =>
    authed(`/api/exams/v1/faculty/candidates/${candidateId}/review`, token, {
      method: "POST",
      body: JSON.stringify({ decision, rejection_reason: rejectionReason ?? null }),
    }),
  listSources: (token?: string) => authed<{ results: ApprovedSourceSummary[] }>("/api/exams/v1/faculty/kt/sources", token),
  reviewSource: (token: string | undefined, sourceId: string, decision: "approve" | "reject", rightsStatement?: string) =>
    authed(`/api/exams/v1/faculty/kt/sources/${sourceId}/review`, token, {
      method: "POST",
      body: JSON.stringify({ decision, rights_statement: rightsStatement ?? null }),
    }),
  getRun: (token: string | undefined, runId: string) => authed<ExamRunSummary>(`/api/exams/v1/faculty/runs/${runId}`, token),
  runCommand: (
    token: string | undefined,
    runId: string,
    command: "publish" | "release" | "pause" | "resume" | "close" | "end",
    expectedVersion: number,
    idempotencyKey: string,
    force = false
  ) =>
    authed(`/api/exams/v1/faculty/runs/${runId}/commands/${command}`, token, {
      method: "POST",
      body: JSON.stringify({ expected_version: expectedVersion, idempotency_key: idempotencyKey, force }),
    }),
  listAttemptsForRun: (token: string | undefined, runId: string, cursorUid?: string) =>
    authed<{ results: Attempt[]; next_cursor_uid: string | null }>(
      `/api/exams/v1/faculty/runs/${runId}/attempts${cursorUid ? `?cursor_uid=${encodeURIComponent(cursorUid)}` : ""}`,
      token
    ),
  extendTime: (token: string | undefined, runId: string, uid: string, extraMinutes: number) =>
    authed(`/api/exams/v1/faculty/runs/${runId}/attempts/${uid}/extend`, token, {
      method: "POST",
      body: JSON.stringify({ extra_minutes: extraMinutes }),
    }),

  // ── Managed lockdown (faculty) ───────────────────────────────────────────
  getLockdownProfile: (token: string | undefined, runId: string) =>
    authed<LockdownProfile>(`/api/exams/v1/faculty/lockdown/runs/${runId}/profile`, token),
  configureLockdownProfile: (
    token: string | undefined,
    runId: string,
    payload: { enabled: boolean; expected_config_key_hash: string | null; expected_browser_exam_key_hashes: string[]; piloted_platforms: string[] }
  ) =>
    authed<LockdownProfile>(`/api/exams/v1/faculty/lockdown/runs/${runId}/profile`, token, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  listLockdownAdmissionLog: (token: string | undefined, runId: string) =>
    authed<{ results: LockdownAdmissionLogEntry[] }>(`/api/exams/v1/faculty/lockdown/runs/${runId}/admission-log`, token),

  // ── Student ──────────────────────────────────────────────────────────────
  listMyRuns: (token?: string) => authed<{ results: StudentRunSummary[] }>("/api/exams/v1/student/runs", token),
  getState: (token: string | undefined, runId: string) => authed<Attempt>(`/api/exams/v1/student/runs/${runId}/state`, token),
  getMyAccommodations: (token: string | undefined, runId: string) =>
    authed<MyAccommodations>(`/api/exams/v1/student/runs/${runId}/accommodations`, token),
  admit: (token: string | undefined, runId: string) =>
    authed<Attempt>(`/api/exams/v1/student/runs/${runId}/admit`, token, { method: "POST" }),
  getCurrentQuestion: (token: string | undefined, runId: string) =>
    authed<PublicQuestion>(`/api/exams/v1/student/runs/${runId}/question`, token),
  advanceToNextQuestion: (token: string | undefined, runId: string) =>
    authed<{ accepted: boolean; slot_index: number }>(`/api/exams/v1/student/runs/${runId}/next-question`, token, {
      method: "POST",
    }),
  saveDraft: (token: string | undefined, runId: string, questionId: string, expectedVersion: number, answer: Record<string, unknown>) =>
    authed<{ accepted: boolean; version: number }>(`/api/exams/v1/student/runs/${runId}/drafts/${encodeURIComponent(questionId)}`, token, {
      method: "PATCH",
      body: JSON.stringify({ expected_version: expectedVersion, answer }),
    }),
  listMyDraftVersions: (token: string | undefined, runId: string) =>
    authed<{ versions: Record<string, number> }>(`/api/exams/v1/student/runs/${runId}/drafts`, token),
  getMyDraft: (token: string | undefined, runId: string, questionId: string) =>
    authed<{ version: number; answer: Record<string, unknown> | null }>(
      `/api/exams/v1/student/runs/${runId}/drafts/${encodeURIComponent(questionId)}`,
      token
    ),
  submit: (token: string | undefined, runId: string, idempotencyKey: string, clientEpoch: number, expectedDraftVersions: Record<string, number>) =>
    authed(`/api/exams/v1/student/runs/${runId}/submit`, token, {
      method: "POST",
      body: JSON.stringify({ idempotency_key: idempotencyKey, client_epoch: clientEpoch, expected_draft_versions: expectedDraftVersions }),
    }),
  finish: (token: string | undefined, runId: string, idempotencyKey: string, clientEpoch: number) =>
    authed(`/api/exams/v1/student/runs/${runId}/finish`, token, {
      method: "POST",
      body: JSON.stringify({ idempotency_key: idempotencyKey, client_epoch: clientEpoch }),
    }),
  getReceipt: (token: string | undefined, runId: string) => authed(`/api/exams/v1/student/runs/${runId}/receipt`, token),

  // ── Judge (student) ──────────────────────────────────────────────────────
  publicRun: (token: string | undefined, runId: string, questionId: string, languageKey: string, sourceCode: string) =>
    authed<{ submission_id: string; status: string }>("/api/exams/v1/student/judge/public-run", token, {
      method: "POST",
      body: JSON.stringify({ run_id: runId, question_id: questionId, language_key: languageKey, source_code: sourceCode }),
    }),
  getPublicRun: (token: string | undefined, submissionId: string, runId: string, questionId: string) =>
    authed<{ status: string; results: unknown[] }>(
      `/api/exams/v1/student/judge/public-run/${submissionId}?run_id=${encodeURIComponent(runId)}&question_id=${encodeURIComponent(questionId)}`,
      token
    ),

  // ── Proctoring (student) ─────────────────────────────────────────────────
  listMyIncidents: (token: string | undefined, runId: string) =>
    authed<{ results: IncidentLogEntry[] }>(`/api/exams/v1/student/proctoring/runs/${runId}/incidents`, token),
  submitAppeal: (token: string | undefined, runId: string, studentStatement: string) =>
    authed<Appeal>(`/api/exams/v1/student/proctoring/runs/${runId}/appeals`, token, {
      method: "POST",
      body: JSON.stringify({ student_statement: studentStatement }),
    }),

  // ── Proctoring (faculty) ──────────────────────────────────────────────────
  listIncidentsForStudent: (token: string | undefined, runId: string, uid: string) =>
    authed<{ results: IncidentLogEntry[]; client_reported_disclaimer: string }>(
      `/api/exams/v1/faculty/proctoring/runs/${runId}/uids/${uid}/incidents`,
      token
    ),
  resolveBlock: (token: string | undefined, runId: string, uid: string, allowContinue: boolean, notes: string) =>
    authed(`/api/exams/v1/faculty/proctoring/runs/${runId}/uids/${uid}/resolve`, token, {
      method: "POST",
      body: JSON.stringify({ allow_continue: allowContinue, notes }),
    }),
  listSnapshotsForStudent: (token: string | undefined, runId: string, uid: string) =>
    authed<{ results: EvidenceSnapshotSummary[] }>(`/api/exams/v1/faculty/proctoring/runs/${runId}/uids/${uid}/snapshots`, token),
  issueViewTicket: (token: string | undefined, snapshotId: string) =>
    authed<{ view_ticket: string; expires_at: string }>(`/api/exams/v1/faculty/proctoring/snapshots/${snapshotId}/view-ticket`, token),
  listAppealsForRun: (token: string | undefined, runId: string) =>
    authed<{ results: Appeal[] }>(`/api/exams/v1/faculty/proctoring/runs/${runId}/appeals`, token),
  resolveAppeal: (token: string | undefined, appealId: string, status: "upheld" | "overturned", notes: string) =>
    authed<Appeal>(`/api/exams/v1/faculty/proctoring/appeals/${appealId}/resolve`, token, {
      method: "POST",
      body: JSON.stringify({ status, resolution_notes: notes }),
    }),

  // ── Gradebook (student) ───────────────────────────────────────────────────
  getMyResults: (token: string | undefined, runId: string) =>
    authed<{ row: StudentGradebookRow; release_version: number | null }>(
      `/api/exams/v1/student/gradebook/runs/${runId}/results`,
      token
    ),
  getMyFeedback: (token: string | undefined, runId: string) =>
    authed<LearningFeedback>(`/api/exams/v1/student/gradebook/runs/${runId}/feedback`, token),
  submitGradeAppeal: (token: string | undefined, runId: string, questionId: string, studentStatement: string) =>
    authed<GradeAppeal>(`/api/exams/v1/student/gradebook/runs/${runId}/questions/${encodeURIComponent(questionId)}/appeals`, token, {
      method: "POST",
      body: JSON.stringify({ student_statement: studentStatement }),
    }),
  listMyGradeAppeals: (token: string | undefined, runId: string) =>
    authed<{ results: GradeAppeal[] }>(`/api/exams/v1/student/gradebook/runs/${runId}/appeals`, token),

  // ── Gradebook (faculty) ────────────────────────────────────────────────────
  listGradebookRows: (token: string | undefined, runId: string, cursorUid?: string) =>
    authed<{ results: StudentGradebookRow[]; next_cursor_uid: string | null }>(
      `/api/exams/v1/faculty/gradebook/runs/${runId}/rows${cursorUid ? `?cursor_uid=${encodeURIComponent(cursorUid)}` : ""}`,
      token
    ),
  getGradebookCounts: (token: string | undefined, runId: string) =>
    authed<{ results: GradebookCounts[] }>(`/api/exams/v1/faculty/gradebook/runs/${runId}/counts`, token),
  releaseResults: (token: string | undefined, runId: string, note: string) =>
    authed<ReleaseRecord>(`/api/exams/v1/faculty/gradebook/runs/${runId}/release`, token, {
      method: "POST",
      body: JSON.stringify({ note }),
    }),
  listReleases: (token: string | undefined, runId: string) =>
    authed<{ results: ReleaseRecord[] }>(`/api/exams/v1/faculty/gradebook/runs/${runId}/releases`, token),
  resolveDefectiveQuestion: (
    token: string | undefined,
    runId: string,
    questionId: string,
    resolution: "full_credit_all" | "exclude_from_total",
    reason: string
  ) =>
    authed<DefectiveQuestionResolution>(
      `/api/exams/v1/faculty/gradebook/runs/${runId}/questions/${encodeURIComponent(questionId)}/defective`,
      token,
      { method: "POST", body: JSON.stringify({ resolution, reason }) }
    ),
  listDefectiveResolutions: (token: string | undefined, runId: string) =>
    authed<{ results: DefectiveQuestionResolution[] }>(`/api/exams/v1/faculty/gradebook/runs/${runId}/defective`, token),
  listGradeAppealsForRun: (token: string | undefined, runId: string) =>
    authed<{ results: GradeAppeal[] }>(`/api/exams/v1/faculty/gradebook/runs/${runId}/appeals`, token),
  resolveGradeAppeal: (token: string | undefined, appealId: string, uphold: boolean, resolutionNotes: string) =>
    authed<GradeAppeal>(`/api/exams/v1/faculty/gradebook/appeals/${appealId}/resolve`, token, {
      method: "POST",
      body: JSON.stringify({ uphold, resolution_notes: resolutionNotes }),
    }),
  runSimilarityCheck: (token: string | undefined, runId: string, questionId: string) =>
    authed<{ results: SimilarityMatch[]; disclaimer: string }>(
      `/api/exams/v1/faculty/gradebook/runs/${runId}/questions/${encodeURIComponent(questionId)}/similarity`,
      token,
      { method: "POST", body: JSON.stringify({}) }
    ),
  listSimilarityMatchesForRun: (token: string | undefined, runId: string) =>
    authed<{ results: SimilarityMatch[] }>(`/api/exams/v1/faculty/gradebook/runs/${runId}/similarity`, token),
  createExport: (token: string | undefined, runId: string, format: "csv" | "json") =>
    authed<ExportJob>(`/api/exams/v1/faculty/gradebook/runs/${runId}/export`, token, {
      method: "POST",
      body: JSON.stringify({ format }),
    }),
  getExportJob: (token: string | undefined, jobId: string) =>
    authed<ExportJob>(`/api/exams/v1/faculty/gradebook/export/${jobId}`, token),
  issueExportDownloadTicket: (token: string | undefined, jobId: string) =>
    authed<{ download_ticket: string; expires_at: string }>(`/api/exams/v1/faculty/gradebook/export/${jobId}/download-ticket`, token, {
      method: "POST",
    }),
};
