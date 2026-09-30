"use client";

import { useExamServiceWorker } from "@/lib/useExamServiceWorker";

/**
 * Mounted once per exam page tree. Registers the static-shell service
 * worker with no attempt-active constraint — safe everywhere except the
 * actual exam-taking page, which separately calls `useExamServiceWorker({
 * attemptActive: true })` itself while `attempt.status === "in_progress"`
 * (see `app/exams/student/[runId]/exam/page.tsx`) to override this default
 * and hold back any pending update until the attempt ends.
 */
export function ExamServiceWorkerRegistrar() {
  useExamServiceWorker();
  return null;
}
