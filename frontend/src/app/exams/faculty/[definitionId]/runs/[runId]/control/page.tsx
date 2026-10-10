"use client";

import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import Link from "next/link";

import { ExamProtectedRoute } from "@/components/exams/ExamProtectedRoute";
import { ErrorState, LoadingState } from "@/components/States";
import { useExamAuth } from "@/lib/examAuth";
import { examEndpoints, type Appeal, type Attempt, type ExamRunSummary } from "@/lib/examEndpoints";
import { useLiveSocket } from "@/lib/liveSocket";

function statusBadge(status: string): string {
  if (status === "submitted" || status === "finalized") return "badge-success";
  if (status === "blocked" || status === "expired") return "badge-error";
  if (status === "in_progress") return "badge-primary";
  return "badge";
}

function ControlRoomContent() {
  const { definitionId, runId } = useParams<{ definitionId: string; runId: string }>();
  const { getToken } = useExamAuth();
  const [run, setRun] = useState<ExamRunSummary | null>(null);
  const [attempts, setAttempts] = useState<Attempt[]>([]);
  const [appeals, setAppeals] = useState<Appeal[]>([]);
  const [cursor, setCursor] = useState<string | undefined>(undefined);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [fetchedAt, setFetchedAt] = useState<number | null>(null);
  const [pendingCommand, setPendingCommand] = useState<{ command: "pause" | "resume" | "close" | "end" } | null>(null);
  const [acting, setActing] = useState(false);

  const loadPage = useCallback(
    async (cursorUid?: string) => {
      try {
        const token = await getToken();
        const [runData, attemptsData, appealsData] = await Promise.all([
          examEndpoints.getRun(token, runId),
          examEndpoints.listAttemptsForRun(token, runId, cursorUid),
          examEndpoints.listAppealsForRun(token, runId),
        ]);
        setRun(runData);
        setAttempts(attemptsData.results);
        setNextCursor(attemptsData.next_cursor_uid);
        setAppeals(appealsData.results);
        setFetchedAt(Date.now());
      } catch (err) {
        setError(err instanceof Error ? err.message : "Could not load the candidate grid.");
      }
    },
    [getToken, runId]
  );

  useEffect(() => {
    loadPage(cursor);
  }, [cursor, loadPage]);

  const { health } = useLiveSocket({ runId, getToken, onReconcile: () => loadPage(cursor), throttleMs: 20000 });

  const runCommand = async (command: "pause" | "resume" | "close" | "end") => {
    if (!run) return;
    setActing(true);
    try {
      const token = await getToken();
      await examEndpoints.runCommand(token, runId, command, run.version, `${command}-${runId}-${run.version}`);
      await loadPage(cursor);
    } catch (err) {
      setError(err instanceof Error ? err.message : `Could not ${command} the run.`);
    } finally {
      setActing(false);
      setPendingCommand(null);
    }
  };

  if (error) return <ErrorState title="Something went wrong" description={error} />;
  if (!run) return <LoadingState message="Loading live control room…" />;

  return (
    <div className="page-container py-10">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Live control room</h1>
        <span className={`badge ${health === "connected" ? "badge-success" : "badge-warning"}`}>{health}</span>
      </div>

      <div className="card mt-4 flex flex-wrap items-center gap-3 p-4">
        <span className="badge-primary badge">{run.status}</span>
        <span className="badge">version {run.version}</span>
        {run.status === "released" && <button className="btn-secondary" onClick={() => setPendingCommand({ command: "pause" })}>Pause</button>}
        {run.status === "paused" && <button className="btn-secondary" onClick={() => setPendingCommand({ command: "resume" })}>Resume</button>}
        {(run.status === "released" || run.status === "paused") && (
          <button className="btn-secondary" onClick={() => setPendingCommand({ command: "close" })}>Close</button>
        )}
        {run.status !== "ended" && <button className="btn-secondary" onClick={() => setPendingCommand({ command: "end" })}>End</button>}
      </div>

      {pendingCommand && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" role="dialog" aria-modal="true">
          <div className="card max-w-md p-6">
            <h2 className="text-lg font-semibold">Confirm: {pendingCommand.command}</h2>
            <dl className="mt-3 space-y-1 text-sm">
              <div className="flex justify-between"><dt className="text-[var(--text-muted)]">Scope</dt><dd>All {run.enrolled_uids.length} enrolled students</dd></div>
              <div className="flex justify-between"><dt className="text-[var(--text-muted)]">Current version</dt><dd>{run.version}</dd></div>
              <div className="flex justify-between"><dt className="text-[var(--text-muted)]">Consequence</dt><dd>
                {pendingCommand.command === "close" && "Unresolved in-progress attempts will be sealed."}
                {pendingCommand.command === "end" && "Content access ends for all students, even accommodated ones."}
                {pendingCommand.command === "pause" && "Students keep their deadline but lose content access temporarily."}
                {pendingCommand.command === "resume" && "Content access resumes for all eligible students."}
              </dd></div>
            </dl>
            <div className="mt-5 flex justify-end gap-3">
              <button className="btn-secondary" onClick={() => setPendingCommand(null)} disabled={acting}>Cancel</button>
              <button className="btn-primary" onClick={() => runCommand(pendingCommand.command)} disabled={acting}>
                {acting ? "Applying…" : "Confirm"}
              </button>
            </div>
          </div>
        </div>
      )}

      <div className="card mt-4 overflow-x-auto p-0">
        <div className="flex items-center justify-between border-b border-[var(--border)] p-3 text-xs text-[var(--text-muted)]">
          <span>Candidates ({attempts.length} shown)</span>
          <span>Snapshot as of {fetchedAt ? new Date(fetchedAt).toLocaleTimeString() : "—"}</span>
        </div>
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-[var(--text-muted)]">
              <th className="p-3">UID</th>
              <th className="p-3">Status</th>
              <th className="p-3">Warnings</th>
              <th className="p-3">Question</th>
              <th className="p-3">Review</th>
            </tr>
          </thead>
          <tbody>
            {attempts.map((a) => (
              <tr key={a.uid} className="border-t border-[var(--border)]">
                <td className="p-3 font-mono text-xs">{a.uid}</td>
                <td className="p-3"><span className={`badge ${statusBadge(a.status)}`}>{a.status}</span></td>
                <td className="p-3">{a.warning_count}</td>
                <td className="p-3">{a.current_slot_index + 1}</td>
                <td className="p-3">
                  <Link className="btn-secondary" href={`/exams/faculty/${definitionId}/runs/${runId}/candidates/${a.uid}`}>
                    Detail
                  </Link>
                </td>
              </tr>
            ))}
            {attempts.length === 0 && (
              <tr><td colSpan={5} className="p-4 text-center text-[var(--text-muted)]">No admitted attempts yet.</td></tr>
            )}
          </tbody>
        </table>
        <div className="flex justify-end gap-2 p-3">
          <button className="btn-secondary" disabled={!cursor} onClick={() => setCursor(undefined)}>First page</button>
          <button className="btn-secondary" disabled={!nextCursor} onClick={() => setCursor(nextCursor ?? undefined)}>Next page</button>
        </div>
      </div>

      <div className="card mt-4 p-5">
        <h2 className="text-lg font-semibold">Pending appeals</h2>
        <ul className="mt-3 space-y-3 text-sm">
          {appeals.filter((a) => a.status === "pending").length === 0 && (
            <li className="text-[var(--text-muted)]">No pending appeals.</li>
          )}
          {appeals.filter((a) => a.status === "pending").map((appeal) => (
            <li key={appeal.id} className="rounded-md border border-[var(--border)] p-3">
              <p className="font-mono text-xs text-[var(--text-muted)]">{appeal.uid}</p>
              <p className="mt-1">{appeal.student_statement}</p>
              <div className="mt-2 flex gap-2">
                <button
                  className="btn-secondary"
                  onClick={async () => {
                    const token = await getToken();
                    await examEndpoints.resolveAppeal(token, appeal.id, "upheld", "Reviewed — upheld.");
                    await loadPage(cursor);
                  }}
                >
                  Uphold
                </button>
                <button
                  className="btn-primary"
                  onClick={async () => {
                    const token = await getToken();
                    await examEndpoints.resolveAppeal(token, appeal.id, "overturned", "Reviewed — overturned.");
                    await loadPage(cursor);
                  }}
                >
                  Overturn
                </button>
              </div>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

export default function ControlRoomPage() {
  return (
    <ExamProtectedRoute requireRole="faculty">
      <ControlRoomContent />
    </ExamProtectedRoute>
  );
}
