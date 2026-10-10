export type RunPolicyInput = {
  durationMinutes: number;
  /** Comma-separated minutes-remaining thresholds, e.g. "30, 10, 5". Empty means the default (10). */
  warningMinutesText: string;
  /** `datetime-local` values (no zone), interpreted in the browser's time zone. Empty = not scheduled. */
  start: string;
  end: string;
  timezone?: string;
  /** Proctoring profile, all optional; empty = the deployment default. */
  graceSeconds?: string;
  cooldownSeconds?: string;
  allowanceAfterReview?: string;
  retentionDays?: string;
};

export type RunPolicyResult =
  | { ok: true; policy: Record<string, unknown> }
  | { ok: false; error: string };

export function parseWarningMinutes(text: string, durationMinutes: number): { ok: true; minutes: number[] } | { ok: false; error: string } {
  const parts = text.split(",").map((p) => p.trim()).filter(Boolean);
  if (parts.length === 0) return { ok: true, minutes: [10] };
  const values: number[] = [];
  for (const part of parts) {
    if (!/^\d+$/.test(part)) return { ok: false, error: `"${part}" is not a whole number of minutes.` };
    const value = Number(part);
    if (value < 1) return { ok: false, error: "Warning times must be at least 1 minute." };
    if (value >= durationMinutes) return { ok: false, error: `A warning at ${value} minutes is longer than the exam itself.` };
    values.push(value);
  }
  if (values.length > 8) return { ok: false, error: "Use at most 8 warning times." };
  return { ok: true, minutes: [...new Set(values)].sort((a, b) => b - a) };
}

/** Builds the run policy the server validates again. The checks here only save a round trip. */
export function buildRunPolicy(input: RunPolicyInput): RunPolicyResult {
  if (!Number.isInteger(input.durationMinutes) || input.durationMinutes < 1 || input.durationMinutes > 24 * 60) {
    return { ok: false, error: "Duration must be between 1 and 1440 minutes." };
  }
  const warnings = parseWarningMinutes(input.warningMinutesText, input.durationMinutes);
  if (!warnings.ok) return warnings;

  const policy: Record<string, unknown> = {
    duration_minutes: input.durationMinutes,
    warning_threshold: 3,
    time_warning_minutes: warnings.minutes,
  };

  const hasStart = input.start.trim() !== "";
  const hasEnd = input.end.trim() !== "";
  if (hasStart || hasEnd) {
    const start = hasStart ? new Date(input.start) : null;
    const end = hasEnd ? new Date(input.end) : null;
    if ((start && Number.isNaN(start.getTime())) || (end && Number.isNaN(end.getTime()))) {
      return { ok: false, error: "Enter a valid start and end time." };
    }
    if (start && end && end.getTime() <= start.getTime()) return { ok: false, error: "The end time must be after the start time." };
    if (start) policy.scheduled_start = start.toISOString();
    if (end) policy.scheduled_end = end.toISOString();
    if (input.timezone) policy.timezone = input.timezone;
  }
  const profile = buildProfile(input);
  if (!profile.ok) return profile;
  if (profile.value) policy.proctoring_profile = profile.value;
  return { ok: true, policy };
}

function optionalNumber(text: string | undefined, label: string, min: number, max: number, integer: boolean): { ok: true; value?: number } | { ok: false; error: string } {
  const raw = (text ?? "").trim();
  if (raw === "") return { ok: true };
  const value = Number(raw);
  if (!Number.isFinite(value) || (integer && !Number.isInteger(value))) return { ok: false, error: `${label} must be ${integer ? "a whole number" : "a number"}.` };
  if (value < min || value > max) return { ok: false, error: `${label} must be between ${min} and ${max}.` };
  return { ok: true, value };
}

/** What the server does with browser-reported signals for this run. Unset fields use the deployment default. */
function buildProfile(input: RunPolicyInput): { ok: true; value?: Record<string, unknown> } | { ok: false; error: string } {
  const grace = optionalNumber(input.graceSeconds, "The grace period", 0, 60, false);
  if (!grace.ok) return grace;
  const cooldown = optionalNumber(input.cooldownSeconds, "The cooldown", 0, 60, false);
  if (!cooldown.ok) return cooldown;
  const allowance = optionalNumber(input.allowanceAfterReview, "The allowance after a review", 0, 10, true);
  if (!allowance.ok) return allowance;
  const retention = optionalNumber(input.retentionDays, "Evidence retention", 7, 365, true);
  if (!retention.ok) return retention;
  const value: Record<string, unknown> = {};
  if (grace.value !== undefined) value.grace_seconds = grace.value;
  if (cooldown.value !== undefined) value.cooldown_seconds = cooldown.value;
  if (allowance.value !== undefined) value.allowance_after_review = allowance.value;
  if (retention.value !== undefined) value.evidence_retention_days = retention.value;
  return { ok: true, value: Object.keys(value).length > 0 ? value : undefined };
}

/** Comma/newline separated topics, trimmed, de-duplicated, capped at the server's 50. */
export function parseTopics(text: string): string[] {
  const seen = new Set<string>();
  for (const raw of text.split(/[,\n]/)) {
    const topic = raw.trim();
    if (topic) seen.add(topic);
  }
  return [...seen].slice(0, 50);
}
