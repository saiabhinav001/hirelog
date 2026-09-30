"use client";

import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

import { ExamProtectedRoute } from "@/components/exams/ExamProtectedRoute";
import { ErrorState } from "@/components/States";
import { useExamAuth } from "@/lib/examAuth";
import { examEndpoints, type Attempt } from "@/lib/examEndpoints";
import { useLiveSocket } from "@/lib/liveSocket";

function WaitingRoomContent() {
  const { runId } = useParams<{ runId: string }>();
  const router = useRouter();
  const { getToken } = useExamAuth();
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  const [error, setError] = useState<string | null>(null);
  const redirectedRef = useRef(false);

  const refresh = useCallback(async () => {
    try {
      const token = await getToken();
      const data = await examEndpoints.getState(token, runId);
      setAttempt(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load your attempt state.");
    }
  }, [getToken, runId]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- initial fetch on mount
    refresh();
  }, [refresh]);

  const { health } = useLiveSocket({ runId, getToken, onReconcile: refresh });

  useEffect(() => {
    if (attempt?.status === "in_progress" && !redirectedRef.current) {
      redirectedRef.current = true;
      router.push(`/exams/student/${runId}/exam`);
    }
    if (attempt?.status === "blocked" && !redirectedRef.current) {
      redirectedRef.current = true;
      router.push(`/exams/student/${runId}/blocked`);
    }
  }, [attempt, router, runId]);

  if (error) return <ErrorState title="Could not load waiting room" description={error} />;

  return (
    <div className="page-container max-w-xl py-16 text-center">
      <h1 className="text-2xl font-semibold">Waiting for your exam to start</h1>
      <p className="mt-3 text-[var(--text-muted)]">
        This page will move to your exam automatically once faculty releases it. No action is needed.
      </p>
      <div className="card mt-8 p-6">
        <span className="badge">{health === "connected" ? "Live updates connected" : "Reconnecting…"}</span>
        <p className="mt-3 text-sm text-[var(--text-muted)]">Status: {attempt?.status ?? "checking…"}</p>
      </div>
    </div>
  );
}

export default function WaitingRoomPage() {
  return (
    <ExamProtectedRoute requireRole="student">
      <WaitingRoomContent />
    </ExamProtectedRoute>
  );
}
