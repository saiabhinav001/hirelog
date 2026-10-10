import { afterEach, describe, expect, it, vi } from "vitest";

import { EXAM_SESSION_INVALID_EVENT, ExamApiError, examApiFetch } from "@/lib/examsApi";

function respondWith(status: number, body: unknown) {
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(body), { status }));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("examApiFetch", () => {
  it("sends the bearer token with no-store caching and returns the parsed body", async () => {
    const fetchMock = respondWith(200, { ok: true });
    await expect(examApiFetch("/api/exams/v1/me", {}, "tok")).resolves.toEqual({ ok: true });
    const [, init] = fetchMock.mock.calls[0];
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer tok");
    expect(init.cache).toBe("no-store");
  });

  it("ends the session when the server says the membership is no longer active", async () => {
    respondWith(403, { detail: { code: "membership_not_active", message: "revoked" } });
    const listener = vi.fn();
    window.addEventListener(EXAM_SESSION_INVALID_EVENT, listener);
    try {
      await expect(examApiFetch("/api/exams/v1/student/runs")).rejects.toMatchObject({
        status: 403,
        code: "membership_not_active",
        message: expect.stringContaining("no longer active"),
      });
      expect(listener).toHaveBeenCalledTimes(1);
    } finally {
      window.removeEventListener(EXAM_SESSION_INVALID_EVENT, listener);
    }
  });

  it("does not end the session for an ordinary authorization failure", async () => {
    respondWith(403, { detail: "Exam faculty authority required." });
    const listener = vi.fn();
    window.addEventListener(EXAM_SESSION_INVALID_EVENT, listener);
    try {
      const failure = await examApiFetch("/api/exams/v1/faculty/definitions").catch((err: unknown) => err);
      expect(failure).toBeInstanceOf(ExamApiError);
      expect((failure as ExamApiError).message).toBe("Exam faculty authority required.");
      expect(listener).not.toHaveBeenCalled();
    } finally {
      window.removeEventListener(EXAM_SESSION_INVALID_EVENT, listener);
    }
  });

  it("explains rate limiting instead of surfacing a raw status", async () => {
    respondWith(429, { detail: "rate limited" });
    await expect(examApiFetch("/api/exams/v1/auth/otp/request")).rejects.toMatchObject({
      status: 429,
      message: expect.stringContaining("Too many attempts"),
    });
  });
});
