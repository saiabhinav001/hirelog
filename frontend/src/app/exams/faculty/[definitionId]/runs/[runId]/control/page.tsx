"use client";

import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import Link from "next/link";

import { AttemptActions } from "@/components/exams/AttemptActions";
import { ConfirmDialog } from "@/components/exams/ConfirmDialog";
import { ExamProtectedRoute } from "@/components/exams/ExamProtectedRoute";
import { ShortLinkPanel } from "@/components/exams/ShortLinkPanel";
import { ErrorState, LoadingState } from "@/components/States";
import { useExamAuth } from "@/lib/examAuth";
import { describeConflict } from "@/lib/examConflicts";
import { examEndpoints, type Appeal, type Attempt, type ExamRunSummary } from "@/lib/examEndpoints";
import { useLiveSocket } from "@/lib/liveSocket";

/** Older than this, with the live feed not connected, the grid may no longer match the server. */
const STALE_AFTER_MS = 30_000;

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
  const [serverTime, setServerTime] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const [feedback, setFeedback] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const [pendingCommand, setPendingCommand] = useState<{ command: "pause" | "resume" | "close" | "end" } | null>(null);
  const [acting, setActing] = useState(false);
  const [accessUid, setAccessUid] = useState("");
  const [accessAction, setAccessAction] = useState<{ action: "grant" | "revoke"; uid: string; key: string } | null>(null);
  const [appealDialog, setAppealDialog] = useState<{ appeal: Appeal; decision: "upheld" | "overturned" } | null>(null);

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);

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
        setServerTime(attemptsData.server_time ?? runData.server_time ?? null);
        setFetchedAt(Date.now());
        setNow(Date.now());
      } catch (err) {
        setError(err instanceof Error ? err.message : "Could not load the candidate grid.");
      }
    },
    [getToken, runId]
  );

  useEffect(() => {
    loadPage(cursor);
  }, [cursor, loadPage]);

  const { health } = useLiveSocket({ runId, getToken, onReconcile: () => loadPage(cursor) });

  const runCommand = async (command: "pause" | "resume" | "close" | "end", reason: string) => {
    if (!run) return;
    setActing(true);
    setFeedback(null);
    try {
      const token = await getToken();
      await examEndpoints.runCommand(token, runId, command, run.version, `${command}-${runId}-${run.version}`, false, reason);
      setFeedback({ kind: "ok", text: `Run ${command === "end" ? "ended" : command === "close" ? "closed" : command === "pause" ? "paused" : "resumed"}.` });
    } catch (err) {
      setFeedback({ kind: "error", text: describeConflict(err, `Could not ${command} the run.`) });
    } finally {
      setActing(false);
      setPendingCommand(null);
      // Reload after success AND after any rejection, so a stale screen never survives a conflict.
      await loadPage(cursor);
    }
  };

  const changeAccess = async (reason: string) => {
    if (!accessAction) return;
    setActing(true);
    setFeedback(null);
    try {
      const token = await getToken();
      const result = await examEndpoints.changeRunAccess(token, runId, accessAction.action, accessAction.uid, accessAction.key, reason);
      setFeedback({
        kind: "ok",
        text: result.changed
          ? `${accessAction.action === "grant" ? "Added" : "Removed"} ${accessAction.uid} ${accessAction.action === "grant" ? "to" : "from"} this run.`
          : `No change: ${accessAction.uid} ${accessAction.action === "grant" ? "was already enrolled" : "was not enrolled"}.`,
      });
      if (accessAction.action === "grant") setAccessUid("");
    } catch (err) {
      setFeedback({ kind: "error", text: describeConflict(err) });
    } finally {
      setActing(false);
      setAccessAction(null);
      await loadPage(cursor);
    }
  };

  const decideAppeal = async (reason: string) => {
    if (!appealDialog) return;
    setActing(true);
    setFeedback(null);
    try {
      const token = await getToken();
      await examEndpoints.resolveAppeal(token, appealDialog.appeal.id, appealDialog.decision, reason);
      setFeedback({
        kind: "ok",
        text: appealDialog.decision === "overturned"
          ? `Appeal from ${appealDialog.appeal.uid} overturned. They stay blocked until you review the block.`
          : `Block upheld for ${appealDialog.appeal.uid}.`,
      });
    } catch (err) {
      setFeedback({ kind: "error", text: describeConflict(err, "Could not record the decision.") });
    } finally {
      setActing(false);
      setAppealDialog(null);
      await loadPage(cursor);
    }
  };

  if (error) return <ErrorState title="Something went wrong" description={error} />;
  if (!run) return <LoadingState message="Loading live control room…" />;

  const ageMs = fetchedAt ? Math.max(0, now - fetchedAt) : 0;
  const stale = health !== "connected" && ageMs > STALE_AFTER_MS;

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

      <p role={feedback?.kind === "error" ? "alert" : "status"} className={`mt-3 text-sm ${feedback?.kind === "error" ? "text-[var(--error)]" : ""}`}>{feedback?.text}</p>

      {pendingCommand && (
        <ConfirmDialog
          title={`Confirm: ${pendingCommand.command}`}
          details={[
            { label: "Scope", value: `All ${run.enrolled_uids.length} enrolled students` },
            { label: "Current version", value: String(run.version) },
          ]}
          consequence={
            pendingCommand.command === "close"
              ? "Stops new admissions. Unresolved in-progress attempts will be sealed."
              : pendingCommand.command === "end"
                ? "Content access ends for all students, even accommodated ones."
                : pendingCommand.command === "pause"
                  ? "Students keep their deadline but lose content access temporarily."
                  : "Content access resumes for all eligible students."
          }
          reasonLabel="Reason"
          reasonRequired={pendingCommand.command === "end" || pendingCommand.command === "close"}
          danger={pendingCommand.command === "end"}
          confirmLabel={`Confirm ${pendingCommand.command}`}
          busy={acting}
          onCancel={() => setPendingCommand(null)}
          onConfirm={(reason) => runCommand(pendingCommand.command, reason)}
        />
      )}

      <div className="card mt-4 overflow-x-auto p-0">
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-[var(--border)] p-3 text-xs text-[var(--text-muted)]">
          <span>Candidates ({attempts.length} shown)</span>
          <span data-testid="freshness">
            Server time {serverTime ? new Date(serverTime).toLocaleTimeString() : "-"} · refreshed {Math.floor(ageMs / 1000)}s ago
          </span>
        </div>
        {stale && (
          <p role="alert" className="border-b border-[var(--border)] p-3 text-sm text-[var(--error)]">
            Live updates are {health}. These figures may be out of date.
          </p>
        )}
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-[var(--text-muted)]">
              <th className="p-3">UID</th>
              <th className="p-3">Status</th>
              <th className="p-3">Warnings</th>
              <th className="p-3">Question</th>
              <th className="p-3">Actions</th>
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
                  <div className="flex flex-wrap items-center gap-2">
                    <Link className="btn-secondary" href={`/exams/faculty/${definitionId}/runs/${runId}/candidates/${a.uid}`}>
                      Detail
                    </Link>
                    <AttemptActions runId={runId} attempt={a} getToken={getToken} onChanged={() => loadPage(cursor)} onFeedback={setFeedback} />
                  </div>
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

      <section className="card mt-4 p-5" aria-labelledby="access-title">
        <h2 id="access-title" className="text-lg font-semibold">Student access</h2>
        <p className="mt-1 text-sm text-[var(--text-muted)]">
          Add a student to this run, or remove one who has not started. A student who has started can&apos;t be removed; end their attempt instead.
        </p>
        <div className="mt-3 flex flex-wrap items-end gap-3">
          <div>
            <label htmlFor="access-uid" className="label">Student ID</label>
            <input id="access-uid" className="input-field font-mono" value={accessUid} onChange={(e) => setAccessUid(e.target.value)} />
          </div>
          <button className="btn-primary" disabled={!accessUid.trim() || run.status === "ended"} onClick={() => setAccessAction({ action: "grant", uid: accessUid.trim(), key: crypto.randomUUID() })}>Add to run</button>
          <button className="btn-secondary" disabled={!accessUid.trim() || run.status === "ended"} onClick={() => setAccessAction({ action: "revoke", uid: accessUid.trim(), key: crypto.randomUUID() })}>Remove from run</button>
        </div>
      </section>

      {accessAction && (
        <ConfirmDialog
          title={accessAction.action === "grant" ? "Add student to this run?" : "Remove student from this run?"}
          details={[{ label: "Student", value: accessAction.uid }, { label: "Run", value: runId }]}
          consequence={accessAction.action === "grant" ? "They can then sign in, run the readiness check and be admitted." : "They lose access to this run. Refused if they have already started."}
          reasonLabel="Reason"
          danger={accessAction.action === "revoke"}
          confirmLabel={accessAction.action === "grant" ? "Add student" : "Remove student"}
          busy={acting}
          onCancel={() => setAccessAction(null)}
          onConfirm={(reason) => changeAccess(reason)}
        />
      )}

      <ShortLinkPanel runId={runId} getToken={getToken} />

      <section className="card mt-4 p-5" aria-labelledby="appeals-title">
        <h2 id="appeals-title" className="text-lg font-semibold">Appeals</h2>
        <p className="mt-1 text-sm text-[var(--text-muted)]">
          Deciding an appeal records your decision and the reason. It does not unblock the student: open their candidate page and review the block there.
        </p>
        <ul className="mt-3 space-y-3 text-sm">
          {appeals.filter((a) => a.status === "pending").length === 0 && (
            <li className="text-[var(--text-muted)]">No pending appeals.</li>
          )}
          {appeals.filter((a) => a.status === "pending").map((appeal) => (
            <li key={appeal.id} className="rounded-md border border-[var(--border)] p-3">
              <p className="font-mono text-xs text-[var(--text-muted)]">{appeal.uid}</p>
              <p className="mt-1">{appeal.student_statement}</p>
              <div className="mt-2 flex flex-wrap gap-2">
                <button className="btn-secondary" onClick={() => setAppealDialog({ appeal, decision: "upheld" })} aria-label={`Uphold the block for ${appeal.uid}`}>
                  Uphold block
                </button>
                <button className="btn-primary" onClick={() => setAppealDialog({ appeal, decision: "overturned" })} aria-label={`Overturn the block for ${appeal.uid}`}>
                  Overturn
                </button>
                <Link className="btn-secondary" href={`/exams/faculty/${definitionId}/runs/${runId}/candidates/${appeal.uid}`}>Candidate page</Link>
              </div>
            </li>
          ))}
        </ul>
        {appeals.some((a) => a.status !== "pending") && (
          <>
            <h3 className="mt-4 text-sm font-semibold">Decided</h3>
            <ul className="mt-2 space-y-2 text-sm">
              {appeals.filter((a) => a.status !== "pending").map((appeal) => (
                <li key={appeal.id} className="rounded-md bg-[var(--surface-muted)] p-2">
                  <span className="font-mono text-xs">{appeal.uid}</span> - {appeal.status}
                  {(appeal.decisions?.length ?? 0) > 1 && ` (reversed: ${appeal.decisions?.map((d) => d.status).join(" → ")})`}
                </li>
              ))}
            </ul>
          </>
        )}
      </section>

      {appealDialog && (
        <ConfirmDialog
          title={appealDialog.decision === "overturned" ? "Overturn this block?" : "Uphold this block?"}
          details={[{ label: "Student", value: appealDialog.appeal.uid }]}
          consequence={
            appealDialog.decision === "overturned"
              ? "Records that the appeal succeeded. The student stays blocked until you review the block on their candidate page."
              : "Records that the block stands. The student can be heard again later if they have new information."
          }
          reasonLabel="Reason"
          reasonRequired
          confirmLabel={appealDialog.decision === "overturned" ? "Overturn" : "Uphold block"}
          busy={acting}
          onCancel={() => setAppealDialog(null)}
          onConfirm={(reason) => decideAppeal(reason)}
        />
      )}
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
