"use client";

import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { AttemptActions } from "@/components/exams/AttemptActions";
import { ConfirmDialog } from "@/components/exams/ConfirmDialog";
import { ExamProtectedRoute } from "@/components/exams/ExamProtectedRoute";
import { ErrorState, LoadingState } from "@/components/States";
import { useExamAuth } from "@/lib/examAuth";
import { describeConflict } from "@/lib/examConflicts";
import { examEndpoints, type Attempt, type EvidenceSnapshotSummary, type IncidentLogEntry } from "@/lib/examEndpoints";
import { useLiveSocket } from "@/lib/liveSocket";

function CandidateDetailContent() {
  const { runId, uid } = useParams<{ runId: string; uid: string }>();
  const { getToken } = useExamAuth();
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  const [incidents, setIncidents] = useState<IncidentLogEntry[]>([]);
  const [snapshots, setSnapshots] = useState<EvidenceSnapshotSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const [resolveDialog, setResolveDialog] = useState<"allow" | "uphold" | null>(null);
  const [resolving, setResolving] = useState(false);
  const [viewedImageUrl, setViewedImageUrl] = useState<string | null>(null);

  useEffect(() => () => {
    if (viewedImageUrl) URL.revokeObjectURL(viewedImageUrl);
  }, [viewedImageUrl]);

  const refresh = useCallback(async () => {
    try {
      const token = await getToken();
      // Reuses the paginated attempts listing, filtered client-side to this
      // one uid — there is no single-attempt faculty GET endpoint yet.
      let cursor: string | undefined;
      let found: Attempt | undefined;
      for (let page = 0; page < 200; page += 1) {
        const data = await examEndpoints.listAttemptsForRun(token, runId, cursor);
        found = data.results.find((a) => a.uid === uid);
        if (found || !data.next_cursor_uid) break;
        cursor = data.next_cursor_uid;
      }
      if (!found) {
        setError("Candidate not found in this run.");
        return;
      }
      setAttempt(found);
      const [incidentData, snapshotData] = await Promise.all([
        examEndpoints.listIncidentsForStudent(token, runId, uid),
        examEndpoints.listSnapshotsForStudent(token, runId, uid),
      ]);
      setIncidents(incidentData.results);
      setSnapshots(snapshotData.results);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load candidate detail.");
    }
  }, [getToken, runId, uid]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useLiveSocket({ runId, getToken, onReconcile: refresh });

  const handleResolveBlock = async (allowContinue: boolean, reason: string) => {
    setResolving(true);
    setFeedback(null);
    try {
      const token = await getToken();
      await examEndpoints.resolveBlock(token, runId, uid, allowContinue, reason);
      setFeedback({ kind: "ok", text: allowContinue ? "The student can continue. Their incident count was kept." : "The block stands. Your decision was recorded." });
    } catch (err) {
      setFeedback({ kind: "error", text: describeConflict(err, "Could not record the decision.") });
    } finally {
      setResolving(false);
      setResolveDialog(null);
      await refresh(); // after a conflict too: the attempt may already have moved on
    }
  };

  const handleViewSnapshot = async (snapshotId: string) => {
    setFeedback(null);
    try {
      const token = await getToken();
      const { view_ticket } = await examEndpoints.issueViewTicket(token, snapshotId);
      const apiBase = (process.env.NEXT_PUBLIC_EXAMS_API_BASE_URL || "http://localhost:8020").replace(/\/+$/, "");
      const response = await fetch(`${apiBase}/api/exams/v1/faculty/proctoring/view/${view_ticket}`, {
        headers: token ? { Authorization: `Bearer ${token}` } : undefined,
        cache: "no-store",
      });
      if (!response.ok) throw new Error("Could not load the evidence image. Request it again.");
      const blob = await response.blob();
      setViewedImageUrl(URL.createObjectURL(blob));
    } catch (err) {
      setFeedback({ kind: "error", text: describeConflict(err, "Could not view the evidence.") });
    }
  };

  if (error) return <ErrorState title="Something went wrong" description={error} />;
  if (!attempt) return <LoadingState message="Loading candidate detail…" />;

  return (
    <div className="page-container max-w-xl py-10">
      <h1 className="text-2xl font-semibold">Candidate detail</h1>
      <p className="mt-1 font-mono text-sm text-[var(--text-muted)]">{uid}</p>
      <p role={feedback?.kind === "error" ? "alert" : "status"} className={`mt-3 text-sm ${feedback?.kind === "error" ? "text-[var(--error)]" : ""}`}>{feedback?.text}</p>

      <div className="card mt-6 p-5">
        <dl className="space-y-2 text-sm">
          <div className="flex justify-between"><dt className="text-[var(--text-muted)]">Status</dt><dd><span className="badge">{attempt.status}</span></dd></div>
          <div className="flex justify-between"><dt className="text-[var(--text-muted)]">Warning count</dt><dd>{attempt.warning_count}</dd></div>
          <div className="flex justify-between"><dt className="text-[var(--text-muted)]">Current question</dt><dd>{attempt.current_slot_index + 1}</dd></div>
          <div className="flex justify-between"><dt className="text-[var(--text-muted)]">Deadline</dt><dd>{attempt.deadline ? new Date(attempt.deadline).toLocaleString() : "—"}</dd></div>
          <div className="flex justify-between"><dt className="text-[var(--text-muted)]">Version</dt><dd>{attempt.version}</dd></div>
        </dl>
      </div>

      {attempt.status === "blocked" && (
        <div className="card mt-4 p-5">
          <h2 className="text-lg font-semibold">Review this block</h2>
          <p className="mt-1 text-sm text-[var(--text-muted)]">
            A person decides - never automatic. The student&apos;s answers are kept either way and the incident history is never
            reset. Upholding leaves the block in place; allowing the student to continue resumes the attempt on its existing
            deadline (extend their time separately if they lost some).
          </p>
          <div className="mt-3 flex gap-3">
            <button className="btn-secondary" onClick={() => setResolveDialog("uphold")} disabled={resolving}>Uphold the block</button>
            <button className="btn-primary" onClick={() => setResolveDialog("allow")} disabled={resolving}>Allow to continue</button>
          </div>
          {resolveDialog && (
            <ConfirmDialog
              title={resolveDialog === "allow" ? "Let this student continue?" : "Uphold the block?"}
              details={[{ label: "Student", value: uid }, { label: "Incidents counted", value: String(attempt.warning_count) }]}
              consequence={
                resolveDialog === "allow"
                  ? "The attempt resumes now. Every incident stays on record. Browser signals are reported by the student's browser and are not proof of misconduct."
                  : "The attempt stays blocked. The student can appeal, and the window can still close on it."
              }
              reasonLabel="Reason"
              reasonRequired
              confirmLabel={resolveDialog === "allow" ? "Allow to continue" : "Uphold"}
              busy={resolving}
              onCancel={() => setResolveDialog(null)}
              onConfirm={(reason) => handleResolveBlock(resolveDialog === "allow", reason)}
            />
          )}
        </div>
      )}

      <div className="card mt-4 p-5">
        <h2 className="text-lg font-semibold">Incident history</h2>
        <p className="mt-1 text-xs text-[var(--text-muted)]">Browser telemetry is client-reported, not cryptographic proof of misconduct.</p>
        <ul className="mt-3 space-y-2 text-sm">
          {incidents.length === 0 && <li className="text-[var(--text-muted)]">No observations recorded.</li>}
          {incidents.map((entry) => (
            <li key={entry.id} className="rounded-md bg-[var(--surface-muted)] p-2">
              <span className={`badge mr-2 ${entry.counted ? "badge-warning" : ""}`}>{entry.kind}</span>
              {entry.source === "reviewer" && <span className="badge badge-primary mr-2">reviewer decision</span>}
              {entry.source === "server" && <span className="badge mr-2">server</span>}
              {entry.duration_seconds != null && `${Math.round(entry.duration_seconds)}s — `}
              {new Date(entry.client_timestamp).toLocaleTimeString()} — {entry.note}
            </li>
          ))}
        </ul>
      </div>

      <div className="card mt-4 p-5">
        <h2 className="text-lg font-semibold">Evidence snapshots</h2>
        <p className="mt-1 text-xs text-[var(--text-muted)]">
          A manual review aid only — not automatic identity/liveness proof. Every view is audited.
        </p>
        <ul className="mt-3 space-y-2 text-sm">
          {snapshots.length === 0 && <li className="text-[var(--text-muted)]">No snapshots yet.</li>}
          {snapshots.map((s) => (
            <li key={s.id} className="flex items-center justify-between rounded-md bg-[var(--surface-muted)] p-2">
              <span>
                <span className="badge mr-2">{s.kind}</span>
                <span className={`badge ${s.status === "verified" ? "badge-success" : s.status === "missing" ? "badge-error" : ""}`}>{s.status}</span>
                {s.missing_reason && <span className="ml-2 text-[var(--text-muted)]">({s.missing_reason})</span>}
              </span>
              {s.status === "verified" && (
                <button className="btn-secondary" onClick={() => handleViewSnapshot(s.id)}>View</button>
              )}
            </li>
          ))}
        </ul>
        {viewedImageUrl && (
          // eslint-disable-next-line @next/next/no-img-element -- authenticated blob URL, not an optimizable remote asset
          <img src={viewedImageUrl} alt="Evidence snapshot" className="mt-3 max-w-xs rounded-md border border-[var(--border)]" />
        )}
      </div>

      <div className="card mt-4 p-5">
        <h2 className="text-lg font-semibold">Actions for this student</h2>
        <p className="mt-1 text-sm text-[var(--text-muted)]">Explicit, audited, and checked against the latest state - never automatic.</p>
        <div className="mt-3">
          <AttemptActions runId={runId} attempt={attempt} getToken={getToken} onChanged={refresh} onFeedback={setFeedback} />
        </div>
        {attempt.status === "not_started" && <p className="mt-2 text-sm text-[var(--text-muted)]">This student has not started, so there is nothing to extend or end.</p>}
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
