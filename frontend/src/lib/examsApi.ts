const EXAMS_API_BASE = (process.env.NEXT_PUBLIC_EXAMS_API_BASE_URL || "http://localhost:8020").replace(/\/+$/, "");

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
  if (options.body && !(options.body instanceof FormData) && !headers.has("Content-Type")) {
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
    throw new ExamApiError(errorMessage(data), response.status, errorCode(data));
  }
  return data as T;
}

/** Carries the HTTP status and the server's stable error code so screens can say what actually conflicted. */
export class ExamApiError extends Error {
  status: number;
  code: string | null;

  constructor(message: string, status: number, code: string | null = null) {
    super(message);
    this.name = "ExamApiError";
    this.status = status;
    this.code = code;
  }
}

function errorCode(data: Record<string, unknown>): string | null {
  const detail = data?.detail;
  if (detail && typeof detail === "object") {
    const d = detail as { code?: unknown; reason?: unknown };
    if (typeof d.reason === "string") return d.reason;
    if (typeof d.code === "string") return d.code;
  }
  return null;
}

/** Structured errors (`{code, message}`) show their message; plain strings are unchanged. */
function errorMessage(data: Record<string, unknown>): string {
  const detail = data?.detail;
  if (detail && typeof detail === "object" && typeof (detail as { message?: unknown }).message === "string") {
    return (detail as { message: string }).message;
  }
  const message = (detail as string) || (data?.message as string) || "Request failed";
  return typeof message === "string" ? message : JSON.stringify(message);
}

/**
 * Multipart upload with byte progress (fetch cannot report upload progress). Same pinned origin,
 * bearer token and error handling as `examApiFetch`; the browser sets the multipart boundary.
 */
export function examApiUpload<T>(
  path: string,
  file: File,
  token?: string,
  onProgress?: (fraction: number) => void
): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("POST", `${EXAMS_API_BASE}${path}`);
    if (token) request.setRequestHeader("Authorization", `Bearer ${token}`);
    request.upload.onprogress = (event) => {
      if (event.lengthComputable && onProgress) onProgress(event.loaded / event.total);
    };
    request.onerror = () => reject(new Error("The upload could not reach the server. Check your connection and try again."));
    request.onload = () => {
      let data: Record<string, unknown> = {};
      try {
        data = request.responseText ? JSON.parse(request.responseText) : {};
      } catch {
        data = { message: request.responseText };
      }
      if (request.status >= 200 && request.status < 300) resolve(data as T);
      else reject(new Error(errorMessage(data)));
    };
    const form = new FormData();
    form.append("file", file);
    request.send(form);
  });
}
