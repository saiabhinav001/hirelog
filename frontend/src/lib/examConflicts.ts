import { ExamApiError } from "@/lib/examsApi";

/**
 * Plain-language text for the conflicts a teacher can actually hit when two people (or a sweeper) act on the same
 * run or attempt. The server's `reason` / `code` is the key; anything unknown falls back to the server's own message.
 */
const CONFLICT_TEXT: Record<string, string> = {
  version_conflict: "Someone else changed this run since you opened it. The page has been refreshed - check the new state and try again.",
  illegal_transition: "That action is not available in the run's current state. The page has been refreshed.",
  unresolved_attempts: "Some students are still working. Close the run again and choose to seal them, or wait for them to finish.",
  deadline_passed: "That student's time has already run out, so it can't be extended. Their attempt is sealed automatically.",
  attempt_resolved: "That attempt is already submitted or sealed; it cannot be changed.",
  attempt_not_active: "That student is not in an active attempt.",
  attempt_not_sealed: "Recovery only applies to an attempt that has already been sealed.",
  attempt_exists: "That student has already started, so they can't be removed. Terminate the attempt instead.",
  extension_limit_exceeded: "That would exceed the maximum total extension for this student.",
  run_ended: "This run has ended.",
  run_not_admitting: "This run no longer admits students, so a join link can't be created.",
  short_links_not_enabled: "Short links are not enabled on this deployment.",
  code_space_exhausted: "Could not find a free short code. Try again.",
  publication_blocked: "Publishing is blocked - the blockers are listed on the exam page.",
  judge_unavailable: "The code runner is not available right now, so your code was not run. Try again in a moment.",
  judge_busy: "The code runner is busy. Wait a moment and try again.",
};

export function describeConflict(err: unknown, fallback = "That did not work."): string {
  if (err instanceof ExamApiError) {
    if (err.code && CONFLICT_TEXT[err.code]) return CONFLICT_TEXT[err.code];
    if (err.status === 429) return "Too many requests. Wait a moment and try again.";
    if (err.status === 403) return "You are not allowed to do that for this run.";
    return err.message || fallback;
  }
  return err instanceof Error ? err.message : fallback;
}

/** A 409 means the screen may be out of date, so the caller should reload its data as well as show the message. */
export function isStaleStateError(err: unknown): boolean {
  return err instanceof ExamApiError && err.status === 409;
}
