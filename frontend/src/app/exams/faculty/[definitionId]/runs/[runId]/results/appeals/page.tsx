"use client";

import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { ExamProtectedRoute } from "@/components/exams/ExamProtectedRoute";
import { ErrorState, LoadingState } from "@/components/States";
import { useExamAuth } from "@/lib/examAuth";
import { examEndpoints, type GradeAppeal } from "@/lib/examEndpoints";

function AppealRow({ appeal, onResolved }: { appeal: GradeAppeal; onResolved: () => void }) {
  const { getToken } = useExamAuth();
  const [notes, setNotes] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const resolve = async (uphold: boolean) => {
    setBusy(true);
    setError(null);
    try {
      const token = await getToken();
      await examEndpoints.resolveGradeAppeal(token, appeal.id, uphold, notes);
      onResolved();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not resolve the appeal.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="card p-4">
      <div className="flex items-center justify-between">
        <p className="font-mono text-xs text-[var(--text-muted)]">
          {appeal.uid} · {appeal.question_id}
        </p>
        <span
          className={`badge ${appeal.status === "pending" ? "badge-warning" : appeal.status === "upheld" ? "badge" : "badge-success"}`}
        >
          {appeal.status}
        </span>
      </div>
      <p className="mt-2 text-sm">{appeal.student_statement}</p>
      {appeal.status !== "pending" && (
        <p className="mt-2 text-xs text-[var(--text-muted)]">
          Resolved by {appeal.resolved_by} — {appeal.resolution_notes}
        </p>
      )}
      {appeal.status === "pending" && (
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <input
            className="input-field max-w-xs"
            placeholder="Resolution notes"
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
          />
          <button className="btn-secondary" disabled={busy} onClick={() => resolve(true)}>
            Uphold original grade
          </button>
          <button className="btn-primary" disabled={busy} onClick={() => resolve(false)}>
            Grade needs correction
          </button>
        </div>
      )}
      {appeal.status === "pending" && (
        <p className="mt-2 text-xs text-[var(--text-muted)]">
          If corrected, separately apply the actual regrade via the judge grading-results/MCQ/short-answer-review
          endpoints, then re-release results.
        </p>
      )}
      {error && <p className="mt-2 text-xs text-[var(--error)]">{error}</p>}
    </div>
  );
}

function GradeAppealsContent() {
  const { runId } = useParams<{ runId: string }>();
  const { getToken } = useExamAuth();
  const [appeals, setAppeals] = useState<GradeAppeal[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  const load = useCallback(async () => {
    try {
      const token = await getToken();
      const data = await examEndpoints.listGradeAppealsForRun(token, runId);
      setAppeals(data.results);
      setLoaded(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load grade appeals.");
    }
  }, [getToken, runId]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- initial fetch on mount
    load();
  }, [load]);

  if (error) return <ErrorState title="Something went wrong" description={error} />;
  if (!loaded) return <LoadingState message="Loading grade appeals…" />;

  const pending = appeals.filter((a) => a.status === "pending");
  const resolved = appeals.filter((a) => a.status !== "pending");

  return (
    <div className="page-container max-w-2xl py-10">
      <h1 className="text-2xl font-semibold">Grade appeals</h1>
      <p className="mt-2 text-sm text-[var(--text-muted)]">
        Students disputing a specific question&apos;s score. Distinct from proctoring block appeals.
      </p>

      <h2 className="mt-6 text-lg font-semibold">Pending ({pending.length})</h2>
      <div className="mt-3 space-y-3">
        {pending.length === 0 && <p className="text-sm text-[var(--text-muted)]">No pending appeals.</p>}
        {pending.map((a) => (
          <AppealRow key={a.id} appeal={a} onResolved={load} />
        ))}
      </div>

      <h2 className="mt-8 text-lg font-semibold">Resolved ({resolved.length})</h2>
      <div className="mt-3 space-y-3">
        {resolved.map((a) => (
          <AppealRow key={a.id} appeal={a} onResolved={load} />
        ))}
      </div>
    </div>
  );
}

export default function GradeAppealsPage() {
  return (
    <ExamProtectedRoute requireRole="faculty">
      <GradeAppealsContent />
    </ExamProtectedRoute>
  );
}
