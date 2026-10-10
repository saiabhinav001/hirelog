"use client";

import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { ExamProtectedRoute } from "@/components/exams/ExamProtectedRoute";
import { ErrorState, LoadingState } from "@/components/States";
import { useExamAuth } from "@/lib/examAuth";
import { examEndpoints, type Appeal, type Attempt, type IncidentLogEntry } from "@/lib/examEndpoints";
import { useLiveSocket } from "@/lib/liveSocket";

function BlockedContent() {
  const { runId } = useParams<{ runId: string }>();
  const { getToken } = useExamAuth();
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  const [incidents, setIncidents] = useState<IncidentLogEntry[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [appealText, setAppealText] = useState("");
  const [submittedAppeal, setSubmittedAppeal] = useState<Appeal | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const token = await getToken();
      const [state, incidentData] = await Promise.all([
        examEndpoints.getState(token, runId),
        examEndpoints.listMyIncidents(token, runId),
      ]);
      setAttempt(state);
      setIncidents(incidentData.results);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load status.");
    }
  }, [getToken, runId]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useLiveSocket({ runId, getToken, onReconcile: refresh, throttleMs: 3000 });

  const handleSubmitAppeal = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!appealText.trim()) return;
    setSubmitting(true);
    try {
      const token = await getToken();
      const appeal = await examEndpoints.submitAppeal(token, runId, appealText.trim());
      setSubmittedAppeal(appeal);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not submit your appeal.");
    } finally {
      setSubmitting(false);
    }
  };

  if (error) return <ErrorState title="Could not load status" description={error} />;
  if (!attempt) return <LoadingState message="Checking your attempt status…" />;

  const countedIncidents = incidents.filter((i) => i.counted);

  return (
    <div className="page-container max-w-xl py-16 text-center">
      <span className="badge-error badge">Attempt blocked for review</span>
      <h1 className="mt-4 text-2xl font-semibold">Your exam session is paused</h1>
      <p className="mt-3 text-[var(--text-muted)]">
        Your work is preserved and nothing has been lost. A faculty member must review your session before you can
        continue. Warning count so far: {attempt.warning_count}.
      </p>

      <div className="card mt-8 p-6 text-left">
        <h2 className="text-sm font-semibold">Why this happened</h2>
        <p className="mt-1 text-xs text-[var(--text-muted)]">
          Browser telemetry is client-reported, not cryptographic proof — this is exactly what your browser reported.
        </p>
        <ul className="mt-3 space-y-2 text-sm">
          {countedIncidents.length === 0 && <li className="text-[var(--text-muted)]">No counted departures recorded yet.</li>}
          {countedIncidents.map((entry) => (
            <li key={entry.id} className="rounded-md bg-[var(--surface-muted)] p-2">
              <span className="badge-warning badge mr-2">{entry.kind}</span>
              {entry.duration_seconds != null && `${Math.round(entry.duration_seconds)}s`} — {new Date(entry.client_timestamp).toLocaleTimeString()}
            </li>
          ))}
        </ul>
      </div>

      <div className="card mt-4 p-6 text-left">
        <h2 className="text-sm font-semibold">Appeal this block</h2>
        {submittedAppeal ? (
          <p className="mt-2 text-sm text-[var(--success)]">Your appeal was submitted and is pending faculty review.</p>
        ) : (
          <form onSubmit={handleSubmitAppeal} className="mt-3 space-y-3">
            <textarea
              className="input-field w-full"
              style={{ minHeight: 100 }}
              placeholder="Explain what happened from your side…"
              value={appealText}
              onChange={(e) => setAppealText(e.target.value)}
              aria-label="Appeal statement"
            />
            <button className="btn-primary" type="submit" disabled={submitting}>
              {submitting ? "Submitting…" : "Submit appeal"}
            </button>
          </form>
        )}
      </div>

      <div className="card mt-4 p-6 text-left">
        <h2 className="text-sm font-semibold">Need help?</h2>
        <p className="mt-2 text-sm text-[var(--text-muted)]">
          Contact your invigilator or institution support desk with your run ID below for a faster review.
        </p>
        <p className="mt-2 font-mono text-xs text-[var(--text-secondary)]">{runId}</p>
      </div>
    </div>
  );
}

export default function BlockedPage() {
  return (
    <ExamProtectedRoute requireRole="student">
      <BlockedContent />
    </ExamProtectedRoute>
  );
}
