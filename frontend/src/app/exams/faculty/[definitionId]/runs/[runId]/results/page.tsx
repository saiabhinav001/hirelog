"use client";

import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { ExamProtectedRoute } from "@/components/exams/ExamProtectedRoute";
import { ErrorState, LoadingState } from "@/components/States";
import { useExamAuth } from "@/lib/examAuth";
import { examEndpoints, type Attempt } from "@/lib/examEndpoints";

function ResultsContent() {
  const { runId } = useParams<{ runId: string }>();
  const { getToken } = useExamAuth();
  const [attempts, setAttempts] = useState<Attempt[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const token = await getToken();
      const all: Attempt[] = [];
      let cursor: string | undefined;
      for (let page = 0; page < 200; page += 1) {
        const data = await examEndpoints.listAttemptsForRun(token, runId, cursor);
        all.push(...data.results);
        if (!data.next_cursor_uid) break;
        cursor = data.next_cursor_uid;
      }
      setAttempts(all);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load results.");
    }
  }, [getToken, runId]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- initial fetch on mount
    load();
  }, [load]);

  if (error) return <ErrorState title="Something went wrong" description={error} />;
  if (attempts.length === 0) return <LoadingState message="Loading results…" />;

  const counts = attempts.reduce<Record<string, number>>((acc, a) => {
    acc[a.status] = (acc[a.status] ?? 0) + 1;
    return acc;
  }, {});

  return (
    <div className="page-container max-w-2xl py-10">
      <h1 className="text-2xl font-semibold">Results overview</h1>
      <p className="mt-2 text-sm text-[var(--text-muted)]">
        Attempt status summary. Detailed per-question grading (coding test results, MCQ scores, short-answer review)
        is reviewed via the grading endpoints from the coding-judge module.
      </p>

      <div className="mt-6 grid grid-cols-2 gap-3 sm:grid-cols-3">
        {Object.entries(counts).map(([status, count]) => (
          <div key={status} className="stat-card">
            <p className="text-2xl font-semibold">{count}</p>
            <p className="text-xs text-[var(--text-muted)]">{status}</p>
          </div>
        ))}
      </div>
    </div>
  );
}

export default function ResultsPage() {
  return (
    <ExamProtectedRoute requireRole="faculty">
      <ResultsContent />
    </ExamProtectedRoute>
  );
}
