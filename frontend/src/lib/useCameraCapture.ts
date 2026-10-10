"use client";

/**
 * Explicit-permission camera snapshot capture (check-in/periodic/manual).
 * NEVER continuous audio/video — each capture starts the camera, grabs one
 * still frame, and stops the stream immediately. A denied permission or
 * capture failure records an honest MISSING reason instead of blocking
 * anything (contract: submission acceptance never depends on this).
 */

import { useCallback, useEffect, useRef, useState } from "react";

const EXAMS_API_BASE = (process.env.NEXT_PUBLIC_EXAMS_API_BASE_URL || "http://localhost:8020").replace(/\/+$/, "");

export type CaptureState = {
  lastCaptureAt: string | null;
  permission: "unknown" | "granted" | "denied" | "unsupported" | "waived";
};

async function captureOneFrame(): Promise<Blob | { missingReason: string }> {
  if (!navigator.mediaDevices?.getUserMedia) {
    return { missingReason: "unsupported_media" };
  }
  let stream: MediaStream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ video: true });
  } catch {
    return { missingReason: "permission_denied" };
  }
  try {
    const track = stream.getVideoTracks()[0];
    const video = document.createElement("video");
    video.srcObject = stream;
    await video.play();
    await new Promise((resolve) => setTimeout(resolve, 150)); // let one frame render
    const canvas = document.createElement("canvas");
    canvas.width = video.videoWidth || 320;
    canvas.height = video.videoHeight || 240;
    const ctx = canvas.getContext("2d");
    if (!ctx) return { missingReason: "capture_failed" };
    ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
    track.stop();
    const blob: Blob | null = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.7));
    return blob ?? { missingReason: "capture_failed" };
  } catch {
    stream.getTracks().forEach((t) => t.stop());
    return { missingReason: "capture_failed" };
  }
}

async function sha256Hex(blob: Blob): Promise<string> {
  const buffer = await blob.arrayBuffer();
  const digest = await crypto.subtle.digest("SHA-256", buffer);
  return Array.from(new Uint8Array(digest)).map((b) => b.toString(16).padStart(2, "0")).join("");
}

export function useCameraCapture(
  runId: string | null,
  getToken: () => Promise<string | undefined>,
  cameraWaived = false
) {
  const [state, setState] = useState<CaptureState>({ lastCaptureAt: null, permission: "unknown" });
  const runIdRef = useRef(runId);
  useEffect(() => {
    runIdRef.current = runId;
  }, [runId]);

  const cameraWaivedRef = useRef(cameraWaived);
  useEffect(() => {
    cameraWaivedRef.current = cameraWaived;
  }, [cameraWaived]);

  const capture = useCallback(
    async (kind: "check_in" | "periodic" | "submission") => {
      const currentRunId = runIdRef.current;
      if (!currentRunId) return;
      const token = await getToken();

      if (cameraWaivedRef.current) {
        // Institution-approved accommodation — never even request camera
        // permission. One honest, explained MISSING record per capture
        // moment, not silence (contract: 'camera alternatives approved by
        // institution').
        setState((s) => ({ ...s, permission: "waived" }));
        await fetch(`${EXAMS_API_BASE}/api/exams/v1/student/proctoring/runs/${currentRunId}/snapshots/missing`, {
          method: "POST",
          headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) },
          body: JSON.stringify({ kind, reason: "camera_waived_accommodation" }),
          cache: "no-store",
        }).catch(() => undefined);
        return;
      }

      const result = await captureOneFrame();

      if (!(result instanceof Blob)) {
        setState((s) => ({ ...s, permission: result.missingReason === "permission_denied" ? "denied" : "unsupported" }));
        await fetch(`${EXAMS_API_BASE}/api/exams/v1/student/proctoring/runs/${currentRunId}/snapshots/missing`, {
          method: "POST",
          headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) },
          body: JSON.stringify({ kind, reason: result.missingReason }),
          cache: "no-store",
        }).catch(() => undefined);
        return;
      }

      setState((s) => ({ ...s, permission: "granted" }));
      try {
        const requested = await fetch(`${EXAMS_API_BASE}/api/exams/v1/student/proctoring/runs/${currentRunId}/snapshots`, {
          method: "POST",
          headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) },
          body: JSON.stringify({ kind }),
          cache: "no-store",
        });
        if (!requested.ok) {
          // Storage unavailable or rate limited: say so honestly and carry on - the exam never depends on this.
          await fetch(`${EXAMS_API_BASE}/api/exams/v1/student/proctoring/runs/${currentRunId}/snapshots/missing`, {
            method: "POST",
            headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) },
            body: JSON.stringify({ kind, reason: "evidence_service_unavailable" }),
            cache: "no-store",
          }).catch(() => undefined);
          return;
        }
        const { upload_ticket } = (await requested.json()) as { snapshot_id: string; upload_ticket: string };

        const form = new FormData();
        form.append("file", result, "snapshot.jpg");
        const digest = await sha256Hex(result);
        void digest; // server independently recomputes and verifies the hash

        const uploaded = await fetch(`${EXAMS_API_BASE}/api/exams/v1/student/proctoring/snapshots/upload/${upload_ticket}`, {
          method: "POST",
          headers: token ? { Authorization: `Bearer ${token}` } : undefined,
          body: form,
          cache: "no-store",
        });
        // A refused upload is already recorded server-side as missing with its reason.
        if (uploaded.ok) setState((s) => ({ ...s, lastCaptureAt: new Date().toISOString() }));
      } catch {
        // Upload failure is also an honest, non-blocking outcome — the
        // snapshot stays in its "requested" state server-side and is marked missing once its ticket lapses.
      }
    },
    [getToken]
  );

  useEffect(() => {
    if (!runId) return undefined;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- initial check-in capture on mount
    void capture("check_in");
    const interval = setInterval(() => void capture("periodic"), 5 * 60_000);
    return () => clearInterval(interval);
  }, [runId, capture]);

  return { state, captureNow: capture };
}
