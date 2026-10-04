"use client";

import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

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
  const [closedOtherApps, setClosedOtherApps] = useState(false);
  const [disabledExtensions, setDisabledExtensions] = useState(false);
  const [fullscreenSupported, setFullscreenSupported] = useState(false);
  const [fullscreenActive, setFullscreenActive] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  // Capability-aware, same pattern as proctoringObservations.ts — fullscreen
  // may be unsupported (some embedded/kiosk browsers), so this step is
  // skipped rather than permanently blocking "Begin exam" on those.
  useEffect(() => {
    setFullscreenSupported(typeof document.fullscreenEnabled === "boolean" && document.fullscreenEnabled);
    const onChange = () => setFullscreenActive(Boolean(document.fullscreenElement));
    document.addEventListener("fullscreenchange", onChange);
    return () => document.removeEventListener("fullscreenchange", onChange);
  }, []);

  const handleEnterFullscreen = useCallback(async () => {
    try {
      await document.documentElement.requestFullscreen();
    } catch {
      // Denied/unsupported at call time — fullscreenActive stays false and
      // the button remains available to retry; never silently pretend it
      // worked.
    }
  }, []);

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

  const canBegin =
    acknowledged && rehearsed && closedOtherApps && disabledExtensions && (!fullscreenSupported || fullscreenActive) && !submitting;

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
        <h2 className="text-lg font-semibold">Lockdown checklist</h2>
        <p className="mt-2 text-sm text-[var(--text-secondary)]">
          A browser tab cannot actually verify other applications, browser extensions, or background processes on
          your device — these are self-declared. Switching tabs or windows during the exam is still detected and
          logged automatically, regardless of what you confirm here.
        </p>
        <label className="mt-4 flex items-center gap-2 text-sm">
          <input type="checkbox" checked={closedOtherApps} onChange={(e) => setClosedOtherApps(e.target.checked)} />
          I have closed all other applications and browser tabs.
        </label>
        <label className="mt-2 flex items-center gap-2 text-sm">
          <input type="checkbox" checked={disabledExtensions} onChange={(e) => setDisabledExtensions(e.target.checked)} />
          I have disabled browser extensions that could interfere with this exam.
        </label>

        {fullscreenSupported ? (
          <div className="mt-4 flex items-center gap-3">
            {fullscreenActive ? (
              <span className="badge-success badge">Fullscreen active</span>
            ) : (
              <button type="button" className="btn-secondary" onClick={handleEnterFullscreen}>
                Enter fullscreen
              </button>
            )}
            <span className="text-sm text-[var(--text-muted)]">Required before you can begin.</span>
          </div>
        ) : (
          <p className="mt-4 text-sm text-[var(--text-muted)]">
            Your browser does not support fullscreen mode — this step is skipped.
          </p>
        )}
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

      <button className="btn-primary mt-6" disabled={!canBegin} onClick={handleBegin}>
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

