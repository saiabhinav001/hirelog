"use client";

import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { ExamProtectedRoute } from "@/components/exams/ExamProtectedRoute";
import { ErrorState, LoadingState } from "@/components/States";
import { useExamAuth } from "@/lib/examAuth";
import { examEndpoints, type SimilarityMatch } from "@/lib/examEndpoints";

function SimilarityReviewContent() {
  const { runId } = useParams<{ runId: string }>();
  const { getToken } = useExamAuth();
  const [matches, setMatches] = useState<SimilarityMatch[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [questionId, setQuestionId] = useState("");
  const [running, setRunning] = useState(false);

  const load = useCallback(async () => {
    try {
      const token = await getToken();
      const data = await examEndpoints.listSimilarityMatchesForRun(token, runId);
      setMatches(data.results);
      setLoaded(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load similarity matches.");
    }
  }, [getToken, runId]);

  useEffect(() => {
    load();
  }, [load]);

  const handleRun = async () => {
    if (!questionId.trim()) return;
    setRunning(true);
    setError(null);
    try {
      const token = await getToken();
      await examEndpoints.runSimilarityCheck(token, runId, questionId.trim());
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not run the similarity check.");
    } finally {
      setRunning(false);
    }
  };

  if (error) return <ErrorState title="Something went wrong" description={error} />;
  if (!loaded) return <LoadingState message="Loading similarity review…" />;

  return (
    <div className="page-container max-w-2xl py-10">
      <h1 className="text-2xl font-semibold">Similarity review</h1>
      <p className="mt-2 text-sm text-[var(--text-muted)]">
        A review aid for authorized faculty only — never an automatic misconduct finding or score penalty. Instructor
        starter code is excluded before comparison.
      </p>

      <div className="card mt-6 p-6">
        <h2 className="text-lg font-semibold">Run a check</h2>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <input
            className="input-field max-w-xs font-mono text-xs"
            placeholder="question_id (from the gradebook table)"
            value={questionId}
            onChange={(e) => setQuestionId(e.target.value)}
          />
          <button className="btn-primary" disabled={running} onClick={handleRun}>
            {running ? "Running…" : "Run similarity check"}
          </button>
        </div>
      </div>

      <div className="mt-6 space-y-3">
        <h2 className="text-lg font-semibold">Matches ({matches.length})</h2>
        {matches.length === 0 && <p className="text-sm text-[var(--text-muted)]">No matches on file yet.</p>}
        {matches
          .slice()
          .sort((a, b) => b.similarity_percent - a.similarity_percent)
          .map((m) => (
            <div key={m.id} className="card p-4">
              <div className="flex items-center justify-between">
                <p className="font-mono text-xs text-[var(--text-muted)]">
                  {m.uid_a} ↔ {m.uid_b} · {m.question_id} · {m.language}
                </p>
                <span className={`badge ${m.similarity_percent >= 80 ? "badge-warning" : "badge"}`}>
                  {m.similarity_percent.toFixed(1)}%
                </span>
              </div>
              <p className="mt-1 text-xs text-[var(--text-muted)]">
                {m.matched_spans.length} matched span(s) · provider: {m.provider}
              </p>
            </div>
          ))}
      </div>
    </div>
  );
}

export default function SimilarityReviewPage() {
  return (
    <ExamProtectedRoute requireRole="faculty">
      <SimilarityReviewContent />
    </ExamProtectedRoute>
  );
}
