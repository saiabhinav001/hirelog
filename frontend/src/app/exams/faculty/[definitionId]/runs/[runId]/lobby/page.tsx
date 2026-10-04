"use client";

import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { ExamProtectedRoute } from "@/components/exams/ExamProtectedRoute";
import { ErrorState, LoadingState } from "@/components/States";
import { useExamAuth } from "@/lib/examAuth";
import { examEndpoints, type ExamRunSummary } from "@/lib/examEndpoints";
import { useLiveSocket } from "@/lib/liveSocket";

type Blueprint = { id: string; title: string; slots: { slot_index: number }[] };

function LobbyContent() {
  const { runId } = useParams<{ runId: string }>();
  const router = useRouter();
  const { getToken } = useExamAuth();
  const [run, setRun] = useState<ExamRunSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showReleaseConfirm, setShowReleaseConfirm] = useState(false);
  const [releasing, setReleasing] = useState(false);
  const [publishing, setPublishing] = useState(false);
  const [blueprints, setBlueprints] = useState<Blueprint[] | null>(null);
  const [assigning, setAssigning] = useState(false);
  const [assignedCount, setAssignedCount] = useState<number | null>(null);

  const refresh = useCallback(async () => {
    try {
      const token = await getToken();
      const data = await examEndpoints.getRun(token, runId);
      setRun(data);
      const blueprintsData = await examEndpoints.listBlueprintsForDefinition(token, data.definition_id);
      setBlueprints(blueprintsData.results as Blueprint[]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load run.");
    }
  }, [getToken, runId]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const { health } = useLiveSocket({ runId, getToken, onReconcile: refresh });

  const handlePublish = async () => {
    if (!run) return;
    setPublishing(true);
    try {
      const token = await getToken();
      await examEndpoints.runCommand(token, runId, "publish", run.version, `publish-${runId}-${run.version}`);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not publish the run.");
    } finally {
      setPublishing(false);
    }
  };

  const handleAssignQuestions = async () => {
    if (!run || !blueprints) return;
    setAssigning(true);
    try {
      const token = await getToken();
      let total = 0;
      for (const bp of blueprints) {
        for (const slot of bp.slots) {
          const result = await examEndpoints.reserveVariantsForSlot(token, bp.id, slot.slot_index, {
            run_id: runId,
            uids: run.enrolled_uids,
          });
          total += Object.keys(result.assignments).length;
        }
      }
      setAssignedCount(total);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not assign questions to students.");
    } finally {
      setAssigning(false);
    }
  };

  const handleRelease = async () => {
    if (!run) return;
    setReleasing(true);
    try {
      const token = await getToken();
      await examEndpoints.runCommand(token, runId, "release", run.version, `release-${runId}-${run.version}`);
      await refresh();
      router.push(`/exams/faculty/${run.definition_id}/runs/${runId}/control`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not release the run.");
    } finally {
      setReleasing(false);
      setShowReleaseConfirm(false);
    }
  };

  if (error) return <ErrorState title="Something went wrong" description={error} />;
  if (!run) return <LoadingState message="Loading readiness lobby…" />;

  return (
    <div className="page-container max-w-2xl py-10">
      <h1 className="text-2xl font-semibold">Readiness lobby</h1>
      <p className="mt-2 text-sm text-[var(--text-muted)]">Live updates: {health}</p>

      <div className="card mt-6 p-5">
        <div className="flex items-center gap-2">
          <span className="badge-primary badge">{run.status}</span>
          <span className="badge">version {run.version}</span>
        </div>
        <p className="mt-3 text-sm text-[var(--text-secondary)]">{run.enrolled_uids.length} students enrolled.</p>
        <p className="mt-1 text-sm text-[var(--text-muted)]">
          Releasing opens this exam to every enrolled student immediately. This is a distinct, separate action from
          previewing the run — preview never changes anything.
        </p>

        <p className="mt-3 text-xs text-[var(--text-muted)]">
          Before releasing, assign each enrolled student their own copy of the approved questions. This can be
          re-run safely — existing assignments are kept and only missing ones are filled in.
        </p>
        <button className="btn-secondary mt-2" onClick={handleAssignQuestions} disabled={assigning || !blueprints || blueprints.length === 0}>
          {assigning ? "Assigning…" : "Assign questions to students"}
        </button>
        {assignedCount !== null && (
          <p className="mt-1 text-xs text-[var(--text-muted)]">{assignedCount} assignment(s) confirmed.</p>
        )}

        {run.status === "draft" && (
          <button className="btn-secondary mt-4" onClick={handlePublish} disabled={publishing}>
            {publishing ? "Publishing…" : "Publish run"}
          </button>
        )}

        {run.status !== "released" && (
          <button className="btn-primary mt-4" onClick={() => setShowReleaseConfirm(true)} disabled={run.status !== "published" && run.status !== "paused"}>
            Release to students
          </button>
        )}
      </div>

      {showReleaseConfirm && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" role="dialog" aria-modal="true">
          <div className="card max-w-md p-6">
            <h2 className="text-lg font-semibold">Confirm release</h2>
            <dl className="mt-3 space-y-1 text-sm">
              <div className="flex justify-between"><dt className="text-[var(--text-muted)]">Scope</dt><dd>All {run.enrolled_uids.length} enrolled students</dd></div>
              <div className="flex justify-between"><dt className="text-[var(--text-muted)]">Current version</dt><dd>{run.version}</dd></div>
              <div className="flex justify-between"><dt className="text-[var(--text-muted)]">Consequence</dt><dd>Students may begin admission immediately</dd></div>
            </dl>
            <div className="mt-5 flex justify-end gap-3">
              <button className="btn-secondary" onClick={() => setShowReleaseConfirm(false)} disabled={releasing}>Cancel</button>
              <button className="btn-primary" onClick={handleRelease} disabled={releasing}>{releasing ? "Releasing…" : "Release"}</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default function LobbyPage() {
  return (
    <ExamProtectedRoute requireRole="faculty">
      <LobbyContent />
    </ExamProtectedRoute>
  );
}
