"use client";

import { useEffect, useState } from "react";

import { createServerClock, type ServerClock } from "@/lib/serverClock";

/** One server clock per component instance, plus a tick that re-renders it every `intervalMs`. */
export function useServerClock(intervalMs = 1000): { clock: ServerClock; tick: number } {
  const [clock] = useState<ServerClock>(() => createServerClock());
  const [tick, setTick] = useState(0);

  useEffect(() => {
    const id = setInterval(() => setTick((n) => n + 1), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);

  return { clock, tick };
}

/** Thresholds (in minutes) the remaining time has crossed, largest first. Zero remaining counts as "time is up", not a warning. */
export function crossedThresholds(remaining: number | null, minutes: number[]): number[] {
  if (remaining === null || remaining <= 0) return [];
  return [...minutes].sort((a, b) => b - a).filter((m) => remaining <= m * 60_000);
}

export function warningText(minutes: number): string {
  return minutes === 1 ? "Less than 1 minute remains." : `Less than ${minutes} minutes remain.`;
}

/**
 * The message for the tightest threshold crossed. Remaining time only shrinks, so the text changes once per threshold and a live
 * region announces each warning once (not every second); a student who opens the page late hears only the tightest one they are past.
 */
export function timeWarningMessage(remaining: number | null, minutes: number[]): string | null {
  const crossed = crossedThresholds(remaining, minutes);
  return crossed.length === 0 ? null : warningText(Math.min(...crossed));
}
