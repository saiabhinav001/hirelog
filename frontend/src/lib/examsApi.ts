const EXAMS_API_BASE = (process.env.NEXT_PUBLIC_EXAMS_API_BASE_URL || "http://localhost:8020").replace(/\/+$/, "");

export const EXAM_SESSION_INVALID_EVENT = "exam-session-invalid";

/** An HTTP failure from the exam API, with the machine-readable code when the server sent one. */
export class ExamApiError extends Error {
  status: number;
  code?: string;

  constructor(message: string, status: number, code?: string) {
    super(message);
    this.name = "ExamApiError";
    this.status = status;
    this.code = code;
  }
}

const FRIENDLY_MESSAGES: Record<string, string> = {
  membership_not_active: "Your institutional access is no longer active. Please contact your institution.",
  stale_authorization: "Your session is out of date. Please sign in again.",
  otp_delivery_not_configured: "Sign-in codes are not available right now. Please contact your institution.",
  invalid_or_expired_code: "That code is invalid or has expired. Request a new one.",
  reauthentication_required: "Please sign in again to continue with this action.",
};

function describeFailure(status: number, data: Record<string, unknown>): { message: string; code?: string } {
  const detail = data?.detail ?? data?.message;
  let code: string | undefined;
  let message: string | undefined;
  if (typeof detail === "string") {
    code = detail in FRIENDLY_MESSAGES ? detail : undefined;
    message = detail;
  } else if (detail && typeof detail === "object") {
    const obj = detail as Record<string, unknown>;
    code = typeof obj.code === "string" ? obj.code : undefined;
    message = typeof obj.message === "string" ? obj.message : undefined;
  }
  if (status === 429) {
    return { message: "Too many attempts. Please wait a moment and try again.", code };
  }
  return { message: (code && FRIENDLY_MESSAGES[code]) || message || "Request failed", code };
}

/**
 * Pinned to ONE configured origin — no multi-base failover (contract #4:
 * "Pin the exam API client to one configured origin"). This is deliberately
 * separate from `lib/api.ts`'s `apiFetch`, which targets the legacy backend
 * and has its own multi-base failover — the exam API is an independently
 * deployed service (see docs/adr/0008).
 */
export async function examApiFetch<T>(path: string, options: RequestInit = {}, token?: string): Promise<T> {
  const headers = new Headers(options.headers || {});
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  if (options.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetch(`${EXAMS_API_BASE}${path}`, {
    ...options,
    headers,
    cache: "no-store", // contract #9: exam responses are network-only/no-store
  });

  const text = await response.text();
  let data: Record<string, unknown> = {};
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = { message: text };
    }
  }

  if (!response.ok) {
    const { message, code } = describeFailure(response.status, data);
    if (typeof window !== "undefined" && (code === "membership_not_active" || code === "stale_authorization")) {
      window.dispatchEvent(new CustomEvent(EXAM_SESSION_INVALID_EVENT, { detail: { code, message } }));
    }
    throw new ExamApiError(message, response.status, code);
  }
  return data as T;
}
