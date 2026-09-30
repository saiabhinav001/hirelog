"use client";

import { useParams, useRouter } from "next/navigation";
import { useCallback, useState } from "react";

import { ExamProtectedRoute } from "@/components/exams/ExamProtectedRoute";
import { ErrorState } from "@/components/States";
import { useExamAuth } from "@/lib/examAuth";
import { examEndpoints } from "@/lib/examEndpoints";

function PreflightContent() {
  const { runId } = useParams<{ runId: string }>();
  const router = useRouter();
  const { getToken } = useExamAuth();
  const [acknowledged, setAcknowledged] = useState(false);
  const [rehearsed, setRehearsed] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const handleBegin = useCallback(async () => {
    setSubmitting(true);
    setError(null);
    try {
      const token = await getToken();
      await examEndpoints.admit(token, runId);
      router.push(`/exams/student/${runId}/waiting`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not start your attempt.");
    } finally {
      setSubmitting(false);
    }
  }, [getToken, router, runId]);

  return (
    <div className="page-container max-w-2xl py-10">
      <h1 className="text-2xl font-semibold">Before you begin</h1>
      {error && <div className="mt-4"><ErrorState title="Could not begin" description={error} /></div>}

      <section className="card mt-6 p-5">
        <h2 className="text-lg font-semibold">Notice</h2>
        <ul className="mt-2 list-disc space-y-1 pl-5 text-sm text-[var(--text-secondary)]">
          <li>Your progress saves automatically as a draft; only final submission is irreversible.</li>
          <li>Once released, only the current question is shown — you cannot revisit earlier questions.</li>
          <li>Connection or camera issues alone are not treated as misconduct.</li>
          <li>This session is monitored for warnings per your institution&apos;s exam policy.</li>
        </ul>
        <label className="mt-4 flex items-center gap-2 text-sm">
          <input type="checkbox" checked={acknowledged} onChange={(e) => setAcknowledged(e.target.checked)} />
          I have read and understood this notice.
        </label>
      </section>

      <section className="card mt-4 p-5">
        <h2 className="text-lg font-semibold">Rehearsal</h2>
        <p className="mt-2 text-sm text-[var(--text-secondary)]">
          Confirm your device and browser can run the exam interface (this page itself).
        </p>
        <label className="mt-4 flex items-center gap-2 text-sm">
          <input type="checkbox" checked={rehearsed} onChange={(e) => setRehearsed(e.target.checked)} />
          I can see and interact with this page normally.
        </label>
      </section>

      <button
        className="btn-primary mt-6"
        disabled={!acknowledged || !rehearsed || submitting}
        onClick={handleBegin}
      >
        {submitting ? "Starting…" : "Begin exam"}
      </button>
    </div>
  );
}

export default function PreflightPage() {
  return (
    <ExamProtectedRoute requireRole="student">
      <PreflightContent />
    </ExamProtectedRoute>
  );
}
