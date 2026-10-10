"use client";

import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { ExamProtectedRoute } from "@/components/exams/ExamProtectedRoute";
import { ErrorState, LoadingState } from "@/components/States";
import { useExamAuth } from "@/lib/examAuth";
import { examEndpoints, type Attempt, type EvidenceSnapshotSummary, type IncidentLogEntry } from "@/lib/examEndpoints";
import { useLiveSocket } from "@/lib/liveSocket";

function CandidateDetailContent() {
  const { runId, uid } = useParams<{ runId: string; uid: string }>();
  const { getToken } = useExamAuth();
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  const [incidents, setIncidents] = useState<IncidentLogEntry[]>([]);
  const [snapshots, setSnapshots] = useState<EvidenceSnapshotSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [extraMinutes, setExtraMinutes] = useState(10);
  const [granting, setGranting] = useState(false);
  const [resolveNotes, setResolveNotes] = useState("");
  const [resolving, setResolving] = useState(false);
  const [viewedImageUrl, setViewedImageUrl] = useState<string | null>(null);

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

  useLiveSocket({ runId, getToken, onReconcile: refresh, throttleMs: 20000 });

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

  const handleResolveBlock = async (allowContinue: boolean) => {
    setResolving(true);
    try {
      const token = await getToken();
      await examEndpoints.resolveBlock(token, runId, uid, allowContinue, resolveNotes);
      setResolveNotes("");
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not resolve the block.");
    } finally {
      setResolving(false);
    }
  };

  const handleViewSnapshot = async (snapshotId: string) => {
    try {
      const token = await getToken();
      const { view_ticket } = await examEndpoints.issueViewTicket(token, snapshotId);
      const apiBase = (process.env.NEXT_PUBLIC_EXAMS_API_BASE_URL || "http://localhost:8020").replace(/\/+$/, "");
      const response = await fetch(`${apiBase}/api/exams/v1/faculty/proctoring/view/${view_ticket}`, {
        headers: token ? { Authorization: `Bearer ${token}` } : undefined,
        cache: "no-store",
      });
      if (!response.ok) throw new Error("Could not load evidence image.");
      const blob = await response.blob();
      setViewedImageUrl(URL.createObjectURL(blob));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not view evidence.");
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

      {attempt.status === "blocked" && (
        <div className="card mt-4 p-5">
          <h2 className="text-lg font-semibold">Resolve block</h2>
          <p className="mt-1 text-sm text-[var(--text-muted)]">
            Explicit decision required — never automatic. Upholding leaves the block in place; allowing continue
            resumes the attempt.
          </p>
          <textarea
            className="input-field mt-3 w-full"
            placeholder="Review notes (audited)"
            value={resolveNotes}
            onChange={(e) => setResolveNotes(e.target.value)}
            aria-label="Resolution notes"
          />
          <div className="mt-3 flex gap-3">
            <button className="btn-secondary" onClick={() => handleResolveBlock(false)} disabled={resolving}>Uphold</button>
            <button className="btn-primary" onClick={() => handleResolveBlock(true)} disabled={resolving}>Allow to continue</button>
          </div>
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
