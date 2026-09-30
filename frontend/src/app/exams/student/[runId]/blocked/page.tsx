"use client";

import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { ExamProtectedRoute } from "@/components/exams/ExamProtectedRoute";
import { ErrorState, LoadingState } from "@/components/States";
import { useExamAuth } from "@/lib/examAuth";
import { examEndpoints, type Attempt } from "@/lib/examEndpoints";
import { useLiveSocket } from "@/lib/liveSocket";

function BlockedContent() {
  const { runId } = useParams<{ runId: string }>();
  const { getToken } = useExamAuth();
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const token = await getToken();
      const state = await examEndpoints.getState(token, runId);
      setAttempt(state);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load status.");
    }
  }, [getToken, runId]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- initial fetch on mount
    refresh();
  }, [refresh]);

  useLiveSocket({ runId, getToken, onReconcile: refresh });

  if (error) return <ErrorState title="Could not load status" description={error} />;
  if (!attempt) return <LoadingState message="Checking your attempt status…" />;

  return (
    <div className="page-container max-w-xl py-16 text-center">
      <span className="badge-error badge">Attempt blocked for review</span>
      <h1 className="mt-4 text-2xl font-semibold">Your exam session is paused</h1>
      <p className="mt-3 text-[var(--text-muted)]">
        Your work is preserved and nothing has been lost. A faculty member must review your session before you can
        continue. Warning count so far: {attempt.warning_count}.
      </p>
      <div className="card mt-8 p-6 text-left">
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
