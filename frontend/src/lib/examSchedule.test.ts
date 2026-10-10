import { describe, expect, it } from "vitest";

import { buildRunPolicy, parseTopics, parseWarningMinutes } from "@/lib/examSchedule";

describe("parseWarningMinutes", () => {
  it("defaults to ten minutes when empty", () => {
    expect(parseWarningMinutes("", 60)).toEqual({ ok: true, minutes: [10] });
  });
  it("sorts descending and removes duplicates", () => {
    expect(parseWarningMinutes("5, 30,10,10", 60)).toEqual({ ok: true, minutes: [30, 10, 5] });
  });
  it("rejects non-numbers, zero, and times longer than the exam", () => {
    expect(parseWarningMinutes("ten", 60).ok).toBe(false);
    expect(parseWarningMinutes("0", 60).ok).toBe(false);
    expect(parseWarningMinutes("90", 60).ok).toBe(false);
  });
  it("rejects more than eight", () => {
    expect(parseWarningMinutes("1,2,3,4,5,6,7,8,9", 60).ok).toBe(false);
  });
});

describe("buildRunPolicy", () => {
  const base = { durationMinutes: 60, warningMinutesText: "10", start: "", end: "" };

  it("builds an unscheduled policy with the warnings", () => {
    const result = buildRunPolicy({ ...base, warningMinutesText: "30, 5" });
    expect(result).toEqual({ ok: true, policy: { duration_minutes: 60, warning_threshold: 3, time_warning_minutes: [30, 5] } });
  });

  it("sends the window as UTC instants with the zone", () => {
    const result = buildRunPolicy({ ...base, start: "2026-06-01T09:00", end: "2026-06-01T12:00", timezone: "Asia/Kolkata" });
    if (!result.ok) throw new Error(result.error);
    expect(String(result.policy.scheduled_start)).toMatch(/Z$/);
    expect(result.policy.timezone).toBe("Asia/Kolkata");
    expect(new Date(String(result.policy.scheduled_end)).getTime()).toBeGreaterThan(new Date(String(result.policy.scheduled_start)).getTime());
  });

  it("refuses an end before the start", () => {
    const result = buildRunPolicy({ ...base, start: "2026-06-01T12:00", end: "2026-06-01T09:00" });
    expect(result).toEqual({ ok: false, error: "The end time must be after the start time." });
  });

  it("refuses a bad duration", () => {
    expect(buildRunPolicy({ ...base, durationMinutes: 0 }).ok).toBe(false);
    expect(buildRunPolicy({ ...base, durationMinutes: 2000 }).ok).toBe(false);
  });
});

describe("parseTopics", () => {
  it("splits on commas and newlines, trims, de-duplicates", () => {
    expect(parseTopics("Recursion, Sorting\n  Recursion ,, Big-O")).toEqual(["Recursion", "Sorting", "Big-O"]);
  });
  it("caps at 50", () => {
    expect(parseTopics(Array.from({ length: 80 }, (_, i) => `t${i}`).join(","))).toHaveLength(50);
  });
});
