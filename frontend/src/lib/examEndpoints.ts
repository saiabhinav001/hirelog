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

async function authed<T>(path: string, token: string | undefined, init: RequestInit = {}): Promise<T> {
  return examApiFetch<T>(path, init, token);
}

export const examEndpoints = {
  // ── Faculty ──────────────────────────────────────────────────────────────
  listMyDefinitions: (token?: string) => authed<{ results: ExamDefinition[] }>("/api/exams/v1/faculty/definitions", token),
  createDefinition: (token: string | undefined, title: string) =>
    authed<ExamDefinition>("/api/exams/v1/faculty/definitions", token, { method: "POST", body: JSON.stringify({ title }) }),
  listRunsForDefinition: (token: string | undefined, definitionId: string) =>
    authed<{ results: ExamRunSummary[] }>(`/api/exams/v1/faculty/definitions/${definitionId}/runs`, token),
  listBlueprintsForDefinition: (token: string | undefined, definitionId: string) =>
    authed<{ results: { id: string; title: string; slots: unknown[] }[] }>(
      `/api/exams/v1/faculty/definitions/${definitionId}/blueprints`,
      token
    ),
  listCandidatesForBlueprint: (token: string | undefined, blueprintId: string) =>
    authed<{ results: GenerationCandidate[] }>(`/api/exams/v1/faculty/blueprints/${blueprintId}/candidates`, token),
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

  // ── Student ──────────────────────────────────────────────────────────────
  listMyRuns: (token?: string) => authed<{ results: StudentRunSummary[] }>("/api/exams/v1/student/runs", token),
  getState: (token: string | undefined, runId: string) => authed<Attempt>(`/api/exams/v1/student/runs/${runId}/state`, token),
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
};
