"use client";

import { useState } from "react";

import { ConfirmDialog } from "@/components/exams/ConfirmDialog";
import { describeConflict } from "@/lib/examConflicts";
import { examEndpoints, type Attempt } from "@/lib/examEndpoints";

type Action = "extend" | "terminate" | "recover";

type Props = {
  runId: string;
  attempt: Attempt;
  getToken: () => Promise<string | undefined>;
  /** Called after any attempt, success or not, so the parent can refetch authoritative state. */
  onChanged: () => void | Promise<void>;
  /** Called with the outcome so the page can show it in one live region. */
  onFeedback: (message: { kind: "ok" | "error"; text: string }) => void;
};

const ACTIVE = new Set(["admitted", "in_progress"]);
const SEALED = new Set(["submitted", "expired", "finalized"]);

/**
 * Per-student teacher actions. Each one names the student and the consequence, asks for a reason, is sent with a
 * key generated when the dialog opens (so a retry cannot apply it twice), and is re-checked by the server against fresh state.
 */
export function AttemptActions({ runId, attempt, getToken, onChanged, onFeedback }: Props) {
  const [open, setOpen] = useState<{ action: Action; key: string } | null>(null);
  const [busy, setBusy] = useState(false);

  const start = (action: Action) => setOpen({ action, key: crypto.randomUUID() });

  const run = async (reason: string, amount: number) => {
    if (!open) return;
    setBusy(true);
    try {
      const token = await getToken();
      if (open.action === "extend") {
        await examEndpoints.extendTime(token, runId, attempt.uid, amount, open.key, reason);
        onFeedback({ kind: "ok", text: `Added ${amount} minute${amount === 1 ? "" : "s"} for ${attempt.uid}.` });
      } else if (open.action === "terminate") {
        await examEndpoints.terminateAttempt(token, runId, attempt.uid, open.key, reason);
        onFeedback({ kind: "ok", text: `Ended the attempt for ${attempt.uid}. Their saved answers were kept.` });
      } else {
        await examEndpoints.recoverAttempt(token, runId, attempt.uid, open.key, reason);
        onFeedback({ kind: "ok", text: `Recovery started for ${attempt.uid}.` });
      }
    } catch (err) {
      onFeedback({ kind: "error", text: describeConflict(err) });
    } finally {
      setBusy(false);
      setOpen(null);
      await onChanged();
    }
  };

  const who = [{ label: "Student", value: attempt.uid }, { label: "Status", value: attempt.status }];

  return (
    <>
      <div className="flex flex-wrap gap-2">
        {attempt.status === "in_progress" && (
          <button className="btn-secondary" onClick={() => start("extend")} aria-label={`Extend time for ${attempt.uid}`}>Extend</button>
        )}
        {ACTIVE.has(attempt.status) && (
          <button className="btn-secondary" onClick={() => start("terminate")} aria-label={`End attempt for ${attempt.uid}`}>End attempt</button>
        )}
        {SEALED.has(attempt.status) && (
          <button className="btn-secondary" onClick={() => start("recover")} aria-label={`Recover grading for ${attempt.uid}`}>Recover</button>
        )}
      </div>

      {open?.action === "extend" && (
        <ConfirmDialog
          title="Extend this student's time"
          details={[...who, { label: "Current deadline", value: attempt.deadline ? new Date(attempt.deadline).toLocaleString() : "-" }]}
          consequence="Moves only this student's deadline. It cannot be undone, and it is refused once their time has already run out."
          numberLabel="Extra minutes"
          numberDefault={10}
          reasonLabel="Reason"
          reasonRequired
          confirmLabel="Extend time"
          busy={busy}
          onCancel={() => setOpen(null)}
          onConfirm={run}
        />
      )}
      {open?.action === "terminate" && (
        <ConfirmDialog
          title="End this student's attempt now?"
          details={who}
          consequence="Seals the attempt immediately with the answers saved so far. It cannot be reopened."
          reasonLabel="Reason"
          reasonRequired
          danger
          confirmLabel="End attempt"
          busy={busy}
          onCancel={() => setOpen(null)}
          onConfirm={run}
        />
      )}
      {open?.action === "recover" && (
        <ConfirmDialog
          title="Re-run grading for this attempt?"
          details={who}
          consequence="Restarts grading for an attempt that is already sealed. It never reopens the attempt or changes saved answers."
          reasonLabel="Reason"
          reasonRequired
          confirmLabel="Recover"
          busy={busy}
          onCancel={() => setOpen(null)}
          onConfirm={run}
        />
      )}
    </>
  );
}
