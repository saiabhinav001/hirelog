/**
 * A clock anchored to the SERVER's time, not the browser's wall clock (ADR 0023).
 *
 * Each sample pairs the server's reported time with a monotonic reading (`performance.now()`), so the
 * estimate keeps advancing between samples even if the user changes the device clock, and a sample is
 * ignored if it is older than one already accepted (a late response). Half the request's round trip is
 * added to the server's reading. The deadline is always the server's; this only decides what to display.
 */

export type ServerClock = {
  sample: (serverTimeIso: string, roundTripMs?: number) => void;
  now: () => number | null;
  isSynced: () => boolean;
};

export function createServerClock(perfNow: () => number = () => performance.now()): ServerClock {
  let anchor: { serverMs: number; perf: number } | null = null;

  return {
    sample(serverTimeIso, roundTripMs = 0) {
      const parsed = Date.parse(serverTimeIso);
      if (Number.isNaN(parsed)) return;
      const serverMs = parsed + Math.max(0, roundTripMs) / 2;
      const perf = perfNow();
      const current = anchor ? anchor.serverMs + (perf - anchor.perf) : null;
      if (current !== null && serverMs < current - 2000) return; // an older response arriving late
      anchor = { serverMs, perf };
    },
    now() {
      return anchor ? anchor.serverMs + (perfNow() - anchor.perf) : null;
    },
    isSynced() {
      return anchor !== null;
    },
  };
}

export function remainingMs(deadlineIso: string | null | undefined, serverNowMs: number | null): number | null {
  if (!deadlineIso || serverNowMs === null) return null;
  const deadline = Date.parse(deadlineIso);
  return Number.isNaN(deadline) ? null : Math.max(0, deadline - serverNowMs);
}
