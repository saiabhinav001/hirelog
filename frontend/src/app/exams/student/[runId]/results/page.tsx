"use client";

import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { ExamProtectedRoute } from "@/components/exams/ExamProtectedRoute";
import { ErrorState, LoadingState } from "@/components/States";
import { useExamAuth } from "@/lib/examAuth";
import { describeConflict } from "@/lib/examConflicts";
import { examEndpoints, type GradeAppeal, type LearningFeedback, type StudentGradebookRow } from "@/lib/examEndpoints";

const OUTCOME_LABEL: Record<string, string> = {
  fully_solved: "Fully solved",
  partially_solved: "Partially solved",
  blank: "Not attempted",
  pending: "Grading in progress",
};

function AppealForm({ runId, questionId, onSubmitted }: { runId: string; questionId: string; onSubmitted: () => void }) {
  const { getToken } = useExamAuth();
  const [open, setOpen] = useState(false);
  const [statement, setStatement] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!open) {
    return (
      <button className="btn-secondary mt-2 text-xs" onClick={() => setOpen(true)}>
        Dispute this grade
      </button>
    );
  }

  const submit = async () => {
    if (!statement.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const token = await getToken();
      await examEndpoints.submitGradeAppeal(token, runId, questionId, statement.trim());
      setOpen(false);
      setStatement("");
      onSubmitted();
    } catch (err) {
      setError(describeConflict(err, "Could not submit the appeal."));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mt-2">
      <textarea
        className="input-field w-full"
        rows={3}
        aria-label="Why you believe this grade is incorrect"
        placeholder="Explain why you believe this grade is incorrect…"
        value={statement}
        onChange={(e) => setStatement(e.target.value)}
      />
      <div className="mt-2 flex gap-2">
        <button className="btn-primary text-xs" disabled={busy} onClick={submit}>
          {busy ? "Submitting…" : "Submit dispute"}
        </button>
        <button className="btn-secondary text-xs" onClick={() => setOpen(false)}>
          Cancel
        </button>
      </div>
      {error && (
        <p role="alert" className="mt-1 text-xs text-[var(--error)]">
          {error}
        </p>
      )}
    </div>
  );
}

function ResultsContent() {
  const { runId } = useParams<{ runId: string }>();
  const { getToken } = useExamAuth();
  const [row, setRow] = useState<StudentGradebookRow | null>(null);
  const [releaseVersion, setReleaseVersion] = useState<number | null>(null);
  const [feedback, setFeedback] = useState<LearningFeedback | null>(null);
  const [appeals, setAppeals] = useState<GradeAppeal[]>([]);
  const [notReleased, setNotReleased] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  const load = useCallback(async () => {
    try {
      const token = await getToken();
      const resultsData = await examEndpoints.getMyResults(token, runId);
      setRow(resultsData.row);
      setReleaseVersion(resultsData.release_version);
      const [feedbackData, appealsData] = await Promise.all([
        examEndpoints.getMyFeedback(token, runId).catch(() => null),
        examEndpoints.listMyGradeAppeals(token, runId),
      ]);
      setFeedback(feedbackData);
      setAppeals(appealsData.results);
      setLoaded(true);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Could not load your results.";
      if (message.toLowerCase().includes("not been released")) {
        setNotReleased(true);
        setLoaded(true);
      } else {
        setError(message);
      }
    }
  }, [getToken, runId]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- initial fetch on mount
    load();
  }, [load]);

  if (error) return <ErrorState title="Could not load results" description={error} />;
  if (!loaded) return <LoadingState message="Loading your results…" />;

  if (notReleased) {
    return (
      <div className="page-container max-w-xl py-16 text-center">
        <h1 className="text-2xl font-semibold">Results not released yet</h1>
        <p className="mt-3 text-[var(--text-muted)]">
          Your instructor has not released results for this exam yet. Check back later.
        </p>
      </div>
    );
  }

  if (!row) return null;

  const appealedQuestionIds = new Set(appeals.map((a) => a.question_id));

  return (
    <div className="page-container max-w-2xl py-10">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Your results</h1>
        {releaseVersion !== null && <span className="badge">version {releaseVersion}</span>}
      </div>
      {releaseVersion !== null && releaseVersion > 1 && (
        <p className="mt-2 text-sm text-[var(--text-muted)]">
          This is a corrected release. It replaces the earlier version of your results.
        </p>
      )}
      <div className="card mt-4 p-6">
        <p className="text-sm text-[var(--text-muted)]">Total score</p>
        <p className="text-3xl font-semibold">
          {row.raw_score.toFixed(1)} / {row.raw_max_score.toFixed(1)}
        </p>
      </div>

      <h2 className="mt-8 text-lg font-semibold">Per-question breakdown</h2>
      <div className="mt-3 space-y-3">
        {row.question_results.map((qr) => (
          <div key={qr.question_id} className="card p-4">
            <div className="flex items-center justify-between">
              <p className="text-sm font-medium">Question {qr.slot_index + 1}</p>
              <span className="badge">{OUTCOME_LABEL[qr.outcome] ?? qr.outcome}</span>
            </div>
            <p className="mt-1 text-sm text-[var(--text-muted)]">
              {qr.weighted_score.toFixed(1)} / {qr.max_score.toFixed(1)} marks
              {qr.tests_passed !== null && qr.tests_total !== null && ` · ${qr.tests_passed}/${qr.tests_total} tests passed`}
            </p>
            {appealedQuestionIds.has(qr.question_id) ? (
              <p className="mt-2 text-xs text-[var(--text-muted)]">Dispute submitted for this question.</p>
            ) : qr.outcome === "pending" ? (
              <p className="mt-2 text-xs text-[var(--text-muted)]">Still being graded. You can dispute a grade once it is final.</p>
            ) : (
              <AppealForm runId={runId} questionId={qr.question_id} onSubmitted={load} />
            )}
          </div>
        ))}
      </div>

      {feedback && (feedback.strengths.length > 0 || feedback.gaps.length > 0) && (
        <div className="mt-8">
          <h2 className="text-lg font-semibold">Learning feedback</h2>
          {feedback.strengths.length > 0 && (
            <div className="card mt-3 p-4">
              <p className="text-sm font-medium text-[var(--success)]">Strengths</p>
              <ul className="mt-1 list-disc pl-5 text-sm">
                {feedback.strengths.map((s) => (
                  <li key={s}>{s}</li>
                ))}
              </ul>
            </div>
          )}
          {feedback.gaps.length > 0 && (
            <div className="card mt-3 p-4">
              <p className="text-sm font-medium">Areas to improve</p>
              <ul className="mt-1 list-disc pl-5 text-sm">
                {feedback.gaps.map((g) => (
                  <li key={g}>{g}</li>
                ))}
              </ul>
              {feedback.practice_steps.length > 0 && (
                <>
                  <p className="mt-3 text-sm font-medium">Suggested next steps</p>
                  <ul className="mt-1 list-disc pl-5 text-sm">
                    {feedback.practice_steps.map((p) => (
                      <li key={p}>{p}</li>
                    ))}
                  </ul>
                </>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default function StudentResultsPage() {
  return (
    <ExamProtectedRoute requireRole="student">
      <ResultsContent />
    </ExamProtectedRoute>
  );
}
