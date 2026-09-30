"use client";

/**
 * Shared header for every exam route: labelled timer, connection health,
 * own camera capture-age, save receipt, warning count, delivery profile.
 * Unknown capabilities say "Unknown" rather than a fabricated value
 * (contract: "No fabricated background-app or Wi-Fi-strength indicators.
 * Unknown capabilities say unknown.") — managed-lockdown detection isn't
 * implemented until Prompt 10, so that slot stays an honest placeholder.
 */

import type { ConnectionHealth } from "@/lib/liveSocket";

function formatRemaining(deadline: string | null): string {
  if (!deadline) return "--:--";
  const ms = new Date(deadline).getTime() - Date.now();
  if (ms <= 0) return "00:00";
  const totalSeconds = Math.floor(ms / 1000);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  const mm = String(minutes).padStart(2, "0");
  const ss = String(seconds).padStart(2, "0");
  return hours > 0 ? `${hours}:${mm}:${ss}` : `${mm}:${ss}`;
}

function formatCaptureAge(lastCaptureAt: string | null): string {
  if (!lastCaptureAt) return "No capture yet";
  const seconds = Math.max(0, Math.floor((Date.now() - new Date(lastCaptureAt).getTime()) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  return `${Math.floor(seconds / 60)}m ago`;
}

const HEALTH_LABEL: Record<ConnectionHealth, string> = {
  connecting: "Connecting…",
  connected: "Connected",
  reconnecting: "Reconnecting…",
  polling: "Offline — retrying (polling)",
  offline: "Offline",
};

const HEALTH_BADGE_CLASS: Record<ConnectionHealth, string> = {
  connecting: "badge",
  connected: "badge-success",
  reconnecting: "badge-warning",
  polling: "badge-warning",
  offline: "badge-error",
};

export function ExamShellHeader({
  deadline,
  connectionHealth,
  saveReceipt,
  warningCount,
  camera,
}: {
  deadline?: string | null;
  connectionHealth: ConnectionHealth;
  saveReceipt?: { savedAt: string; version: number } | null;
  warningCount?: number;
  camera?: { permission: "unknown" | "granted" | "denied" | "unsupported" | "waived"; lastCaptureAt: string | null };
}) {
  const cameraLabel =
    !camera || camera.permission === "unknown" ? "Unknown"
      : camera.permission === "denied" ? "Permission denied"
      : camera.permission === "unsupported" ? "Unsupported"
      : camera.permission === "waived" ? "Waived (accommodation)"
      : formatCaptureAge(camera.lastCaptureAt);

  return (
    <header
      className="card flex flex-wrap items-center justify-between gap-3 px-4 py-3"
      role="banner"
      aria-label="Exam session status"
    >
      <div className="flex items-center gap-4">
        <div aria-live="polite">
          <span className="label mb-0">Time remaining</span>
          <p className="text-lg font-semibold tabular-nums text-[var(--text)]">{formatRemaining(deadline ?? null)}</p>
        </div>

        <div className="flex flex-col">
          <span className="label mb-0">Connection</span>
          <span className={`badge ${HEALTH_BADGE_CLASS[connectionHealth]}`} role="status">
            {HEALTH_LABEL[connectionHealth]}
          </span>
        </div>
      </div>

      <div className="flex items-center gap-4">
        <div className="flex flex-col items-center" aria-label="Camera status">
          <div className="flex h-10 w-14 items-center justify-center rounded-md border border-[var(--border)] bg-[var(--surface-muted)] text-[0.65rem] text-[var(--text-muted)]">
            {camera?.permission === "granted" ? "●" : camera?.permission === "waived" ? "—" : "Unknown"}
          </div>
          <span className="mt-1 text-[0.65rem] text-[var(--text-muted)]">{cameraLabel}</span>
        </div>

        <div className="flex flex-col">
          <span className="label mb-0">Save status</span>
          <span className="text-sm text-[var(--text-secondary)]">
            {saveReceipt ? `Saved (v${saveReceipt.version})` : "Not saved yet"}
          </span>
        </div>

        <div className="flex flex-col">
          <span className="label mb-0">Warnings</span>
          <span className={warningCount ? "badge-warning badge" : "badge"}>{warningCount ?? 0}</span>
        </div>

        <div className="flex flex-col">
          <span className="label mb-0">Delivery profile</span>
          <span className="badge">Standard browser</span>
        </div>
      </div>
    </header>
  );
}
