import { describe, expect, it } from "vitest";

import { describeConflict, isStaleStateError } from "@/lib/examConflicts";
import { ExamApiError } from "@/lib/examsApi";

describe("describeConflict", () => {
  it("translates known server reasons", () => {
    expect(describeConflict(new ExamApiError("x", 409, "version_conflict"))).toMatch(/Someone else changed/);
    expect(describeConflict(new ExamApiError("x", 409, "attempt_exists"))).toMatch(/already started/);
    expect(describeConflict(new ExamApiError("x", 409, "extension_limit_exceeded"))).toMatch(/maximum total extension/);
  });
  it("falls back sensibly for rate limits, permission and unknown reasons", () => {
    expect(describeConflict(new ExamApiError("x", 429))).toMatch(/Too many requests/);
    expect(describeConflict(new ExamApiError("x", 403))).toMatch(/not allowed/);
    expect(describeConflict(new ExamApiError("Server said no", 422, "something_new"))).toBe("Server said no");
  });
  it("handles plain errors and unknown values", () => {
    expect(describeConflict(new Error("boom"))).toBe("boom");
    expect(describeConflict("nope", "Fallback")).toBe("Fallback");
  });
  it("flags only 409s as possibly stale", () => {
    expect(isStaleStateError(new ExamApiError("x", 409))).toBe(true);
    expect(isStaleStateError(new ExamApiError("x", 500))).toBe(false);
    expect(isStaleStateError(new Error("x"))).toBe(false);
  });
});
