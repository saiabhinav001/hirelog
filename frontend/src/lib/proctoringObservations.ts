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

import { useEffect, useRef } from "react";

import { examApiFetch } from "@/lib/examsApi";

type ObservationKind =
  | "page_hidden" | "page_visible" | "fullscreen_exit" | "fullscreen_reentered" | "blur" | "focus";

type QueuedObservation = { kind: ObservationKind; client_event_id: string; client_timestamp: string };

export function useProctoringObservations(runId: string | null, getToken: () => Promise<string | undefined>) {
  const queueRef = useRef<QueuedObservation[]>([]);
  const flushingRef = useRef(false);

  useEffect(() => {
    if (!runId) return undefined;

    const flush = async () => {
      if (flushingRef.current) return;
      flushingRef.current = true;
      try {
        while (queueRef.current.length > 0) {
          const next = queueRef.current[0];
          try {
            const token = await getToken();
            await examApiFetch(
              `/api/exams/v1/student/proctoring/runs/${runId}/observations`,
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
    };

    const enqueue = (kind: ObservationKind) => {
      queueRef.current.push({
        kind,
        client_event_id: crypto.randomUUID(),
        client_timestamp: new Date().toISOString(),
      });
      void flush();
    };

    const onVisibilityChange = () => enqueue(document.hidden ? "page_hidden" : "page_visible");
    const onBlur = () => enqueue("blur");
    const onFocus = () => enqueue("focus");
    const onFullscreenChange = () => enqueue(document.fullscreenElement ? "fullscreen_reentered" : "fullscreen_exit");
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
  }, [runId, getToken]);
}
