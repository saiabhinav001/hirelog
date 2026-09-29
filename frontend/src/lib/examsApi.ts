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
    const message = (data?.detail as string) || (data?.message as string) || "Request failed";
    throw new Error(typeof message === "string" ? message : JSON.stringify(message));
  }
  return data as T;
}
