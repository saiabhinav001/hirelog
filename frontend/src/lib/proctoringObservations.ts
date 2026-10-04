"use client";

/**
 * Capability-aware browser observation reporting. Listens only to APIs that
 * actually exist (fullscreen may be unsupported) and never infers OS
 * process names from focus changes — it only ever reports what the browser
 * itself observed (page-hidden/blur/fullscreen-exit and their reverses).
 * Failed sends are queued and retried (network loss / offline replay) using
 * the SAME client_event_id each time so server-side dedup absorbs any
 * eventual duplicate delivery.
 */

import { useCallback, useEffect, useRef } from "react";

import { examApiFetch } from "@/lib/examsApi";

type ObservationKind =
  | "page_hidden"
  | "page_visible"
  | "fullscreen_exit"
  | "fullscreen_reentered"
  | "blur"
  | "focus"
  | "clipboard_blocked";

type QueuedObservation = { kind: ObservationKind; client_event_id: string; client_timestamp: string };

export function useProctoringObservations(
  runId: string | null,
  getToken: () => Promise<string | undefined>,
  onObserved?: (kind: ObservationKind) => void
) {
  const queueRef = useRef<QueuedObservation[]>([]);
  const flushingRef = useRef(false);
  const runIdRef = useRef(runId);
  runIdRef.current = runId;
  const getTokenRef = useRef(getToken);
  getTokenRef.current = getToken;
  const onObservedRef = useRef(onObserved);
  onObservedRef.current = onObserved;

  const flush = useCallback(async () => {
    const activeRunId = runIdRef.current;
    if (!activeRunId || flushingRef.current) return;
    flushingRef.current = true;
    try {
      while (queueRef.current.length > 0) {
        const next = queueRef.current[0];
        try {
          const token = await getTokenRef.current();
          await examApiFetch(
            `/api/exams/v1/student/proctoring/runs/${activeRunId}/observations`,
            { method: "POST", body: JSON.stringify(next) },
            token
          );
          queueRef.current.shift();
        } catch {
          break; // still offline/failing — stop and retry later
        }
      }
    } finally {
      flushingRef.current = false;
    }
  }, []);

  // Exposed so callers can report a one-off observation outside this hook's
  // own listeners (e.g. the code editor reporting a blocked clipboard
  // attempt) — reuses the same queue/retry/dedup machinery as every other
  // kind, rather than being a special one-off fetch.
  const report = useCallback(
    (kind: ObservationKind) => {
      if (!runIdRef.current) return;
      onObservedRef.current?.(kind);
      queueRef.current.push({
        kind,
        client_event_id: crypto.randomUUID(),
        client_timestamp: new Date().toISOString(),
      });
      void flush();
    },
    [flush]
  );

  useEffect(() => {
    if (!runId) return undefined;

    const onVisibilityChange = () => report(document.hidden ? "page_hidden" : "page_visible");
    const onBlur = () => report("blur");
    const onFocus = () => report("focus");
    const onFullscreenChange = () => report(document.fullscreenElement ? "fullscreen_reentered" : "fullscreen_exit");
    const onOnline = () => void flush();
    const retryTimer = setInterval(() => void flush(), 10_000);

    document.addEventListener("visibilitychange", onVisibilityChange);
    window.addEventListener("blur", onBlur);
    window.addEventListener("focus", onFocus);
    window.addEventListener("online", onOnline);
    // Capability-aware: only listen for fullscreen changes if the API exists.
    if (typeof document.fullscreenEnabled === "boolean") {
      document.addEventListener("fullscreenchange", onFullscreenChange);
    }

    return () => {
      document.removeEventListener("visibilitychange", onVisibilityChange);
      window.removeEventListener("blur", onBlur);
      window.removeEventListener("focus", onFocus);
      window.removeEventListener("online", onOnline);
      document.removeEventListener("fullscreenchange", onFullscreenChange);
      clearInterval(retryTimer);
    };
  }, [runId, flush, report]);

  return { report };
}
