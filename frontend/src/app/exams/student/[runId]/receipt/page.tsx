"use client";

import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { ExamProtectedRoute } from "@/components/exams/ExamProtectedRoute";
import { ErrorState, LoadingState } from "@/components/States";
import { useExamAuth } from "@/lib/examAuth";
import { examEndpoints } from "@/lib/examEndpoints";

type Receipt = { receipt_id: string; finalized_at: string; sealed_draft_versions: Record<string, number> };

function ReceiptContent() {
  const { runId } = useParams<{ runId: string }>();
  const { getToken } = useExamAuth();
  const [receipt, setReceipt] = useState<Receipt | null>(null);
  const [notYetFinalized, setNotYetFinalized] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const token = await getToken();
      const data = await examEndpoints.getReceipt(token, runId);
      setReceipt(data as Receipt);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Could not load your receipt.";
      if (message.toLowerCase().includes("not finalized")) {
        setNotYetFinalized(true);
      } else {
        setError(message);
      }
    }
  }, [getToken, runId]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- initial fetch on mount
    load();
  }, [load]);

  if (error) return <ErrorState title="Could not load receipt" description={error} />;
  if (notYetFinalized) {
    return (
      <div className="page-container max-w-xl py-16 text-center">
        <h1 className="text-2xl font-semibold">Submission received</h1>
        <p className="mt-3 text-[var(--text-muted)]">
          Your exam was submitted and is being finalized. This page will show your receipt shortly — you can safely
          refresh it or come back later. This never re-authorizes late work.
        </p>
        <button className="btn-secondary mt-6" onClick={load}>
          Check again
        </button>
      </div>
    );
  }
  if (!receipt) return <LoadingState message="Loading your receipt…" />;

  return (
    <div className="page-container max-w-xl py-16 text-center">
      <span className="badge-success badge">Submission confirmed</span>
      <h1 className="mt-4 text-2xl font-semibold">Your exam has been submitted</h1>
      <div className="card mt-6 p-6 text-left">
        <p className="text-sm text-[var(--text-muted)]">Receipt ID</p>
        <p className="font-mono text-sm">{receipt.receipt_id}</p>
        <p className="mt-3 text-sm text-[var(--text-muted)]">Finalized at</p>
        <p className="text-sm">{new Date(receipt.finalized_at).toLocaleString()}</p>
        <p className="mt-3 text-sm text-[var(--text-muted)]">Questions sealed</p>
        <p className="text-sm">{Object.keys(receipt.sealed_draft_versions).length}</p>
      </div>
      <p className="mt-6 text-sm text-[var(--text-muted)]">
        Results are released by your institution separately and will appear here once available.
      </p>
    </div>
  );
}

export default function ReceiptPage() {
  return (
    <ExamProtectedRoute requireRole="student">
      <ReceiptContent />
    </ExamProtectedRoute>
  );
}
