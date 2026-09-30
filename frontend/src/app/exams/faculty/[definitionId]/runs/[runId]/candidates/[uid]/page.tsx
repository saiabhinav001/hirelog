"use client";

import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { ExamProtectedRoute } from "@/components/exams/ExamProtectedRoute";
import { ErrorState, LoadingState } from "@/components/States";
import { useExamAuth } from "@/lib/examAuth";
import { examEndpoints, type Attempt } from "@/lib/examEndpoints";
import { useLiveSocket } from "@/lib/liveSocket";

function CandidateDetailContent() {
  const { runId, uid } = useParams<{ runId: string; uid: string }>();
  const { getToken } = useExamAuth();
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [extraMinutes, setExtraMinutes] = useState(10);
  const [granting, setGranting] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const token = await getToken();
      // Reuses the paginated attempts listing, filtered client-side to this
      // one uid — there is no single-attempt faculty GET endpoint yet.
      let cursor: string | undefined;
      for (let page = 0; page < 200; page += 1) {
        const data = await examEndpoints.listAttemptsForRun(token, runId, cursor);
        const found = data.results.find((a) => a.uid === uid);
        if (found) {
          setAttempt(found);
          return;
        }
        if (!data.next_cursor_uid) break;
        cursor = data.next_cursor_uid;
      }
      setError("Candidate not found in this run.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load candidate detail.");
    }
  }, [getToken, runId, uid]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useLiveSocket({ runId, getToken, onReconcile: refresh });

  const handleGrantExtension = async () => {
    setGranting(true);
    try {
      const token = await getToken();
      await examEndpoints.extendTime(token, runId, uid, extraMinutes);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not grant the extension.");
    } finally {
      setGranting(false);
    }
  };

  if (error) return <ErrorState title="Something went wrong" description={error} />;
  if (!attempt) return <LoadingState message="Loading candidate detail…" />;

  return (
    <div className="page-container max-w-xl py-10">
      <h1 className="text-2xl font-semibold">Candidate detail</h1>
      <p className="mt-1 font-mono text-sm text-[var(--text-muted)]">{uid}</p>

      <div className="card mt-6 p-5">
        <dl className="space-y-2 text-sm">
          <div className="flex justify-between"><dt className="text-[var(--text-muted)]">Status</dt><dd><span className="badge">{attempt.status}</span></dd></div>
          <div className="flex justify-between"><dt className="text-[var(--text-muted)]">Warning count</dt><dd>{attempt.warning_count}</dd></div>
          <div className="flex justify-between"><dt className="text-[var(--text-muted)]">Current question</dt><dd>{attempt.current_slot_index + 1}</dd></div>
          <div className="flex justify-between"><dt className="text-[var(--text-muted)]">Deadline</dt><dd>{attempt.deadline ? new Date(attempt.deadline).toLocaleString() : "—"}</dd></div>
          <div className="flex justify-between"><dt className="text-[var(--text-muted)]">Version</dt><dd>{attempt.version}</dd></div>
        </dl>
      </div>

      <div className="card mt-4 p-5">
        <h2 className="text-lg font-semibold">Grant time extension</h2>
        <p className="mt-1 text-sm text-[var(--text-muted)]">Explicit, audited — never automatic.</p>
        <div className="mt-3 flex items-center gap-3">
          <input type="number" min={1} className="input-field w-24" value={extraMinutes} onChange={(e) => setExtraMinutes(Number(e.target.value))} />
          <span className="text-sm text-[var(--text-muted)]">minutes</span>
          <button className="btn-primary" onClick={handleGrantExtension} disabled={granting}>
            {granting ? "Granting…" : "Grant"}
          </button>
        </div>
      </div>
    </div>
  );
}

export default function CandidateDetailPage() {
  return (
    <ExamProtectedRoute requireRole="faculty">
      <CandidateDetailContent />
    </ExamProtectedRoute>
  );
}
