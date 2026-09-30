"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { ExamProtectedRoute } from "@/components/exams/ExamProtectedRoute";
import { EmptyState, ErrorState, LoadingState } from "@/components/States";
import { useExamAuth } from "@/lib/examAuth";
import { examEndpoints, type StudentRunSummary } from "@/lib/examEndpoints";

function statusBadgeClass(status: string): string {
  if (status === "released" || status === "paused") return "badge-primary";
  if (status === "closed" || status === "ended") return "badge";
  return "badge";
}

function StudentExamsList() {
  const { getToken } = useExamAuth();
  const [runs, setRuns] = useState<StudentRunSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const token = await getToken();
      const data = await examEndpoints.listMyRuns(token);
      setRuns(data.results);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load your exams.");
    }
  }, [getToken]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- initial fetch on mount
    load();
  }, [load]);

  if (error) return <ErrorState title="Could not load your exams" description={error} />;
  if (runs === null) return <LoadingState message="Loading your assigned exams…" />;
  if (runs.length === 0) {
    return <EmptyState title="No assigned exams" description="You have no exams assigned yet. Check back later." />;
  }

  return (
    <div className="page-container py-10">
      <h1 className="text-2xl font-semibold">Your exams</h1>
      <div className="mt-6 grid gap-4 sm:grid-cols-2">
        {runs.map((run) => (
          <div key={run.run_id} className="card p-5">
            <div className="flex items-center justify-between">
              <span className={`badge ${statusBadgeClass(run.status)}`}>{run.status}</span>
              {run.attempt_status && <span className="badge">{run.attempt_status}</span>}
            </div>
            <p className="mt-3 text-sm text-[var(--text-muted)]">Run ID: {run.run_id}</p>
            {run.deadline && <p className="mt-1 text-sm text-[var(--text-muted)]">Deadline: {new Date(run.deadline).toLocaleString()}</p>}
            <Link
              href={run.attempt_status ? `/exams/student/${run.run_id}/exam` : `/exams/student/${run.run_id}/preflight`}
              className="btn-primary mt-4 inline-flex"
            >
              {run.attempt_status ? "Continue" : "Begin"}
            </Link>
          </div>
        ))}
      </div>
    </div>
  );
}

export default function StudentExamsPage() {
  return (
    <ExamProtectedRoute>
      <StudentExamsList />
    </ExamProtectedRoute>
  );
}
