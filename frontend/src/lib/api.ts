const rawBase = process.env.NEXT_PUBLIC_API_BASE_URL;
export const API_BASE =
  rawBase && rawBase.startsWith("http") ? rawBase : "http://localhost:8000";

const MAX_RETRIES = 3;
const RETRY_BASE_MS = 500;

async function sleep(ms: number) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export async function apiFetch<T>(
  path: string,
  options: RequestInit,
  token?: string
): Promise<T> {
  const headers = new Headers(options.headers || {});
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  if (options.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  let response!: Response;

  for (let attempt = 0; attempt < MAX_RETRIES; attempt++) {
    try {
      response = await fetch(`${API_BASE}${path}`, {
        ...options,
        headers,
        cache: "no-store",
      });
      // Success — break out of the retry loop
      break;
    } catch {
      // Only retry on network errors (server unreachable, CORS pre-flight, etc.)
      if (attempt < MAX_RETRIES - 1) {
        // Exponential backoff with jitter to avoid thundering herd
        const backoff = RETRY_BASE_MS * Math.pow(2, attempt);
        const jitter = Math.random() * backoff * 0.5;
        await sleep(backoff + jitter);
        continue;
      }
      throw new Error(
        "Unable to reach the server. Please check your connection and try again."
      );
    }
  }

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
    throw new Error(message);
  }

  return data as T;
}

// ─────────────────────────────────────────────────────────────────────────────
// Contribution management helpers
// ─────────────────────────────────────────────────────────────────────────────

import type {
  Experience,
  ModerationQueueResponse,
  PlacementCellAdminResponse,
} from "./types";

export async function fetchMyContributions(
  token: string
): Promise<{ results: Experience[]; total: number }> {
  return apiFetch("/api/experiences/mine", { method: "GET" }, token);
}

export async function softDeleteExperience(
  experienceId: string,
  token: string
): Promise<{ status: string; experience_id: string }> {
  return apiFetch(`/api/experiences/${experienceId}`, { method: "DELETE" }, token);
}

export async function restoreExperience(
  experienceId: string,
  token: string
): Promise<{ status: string; experience_id: string }> {
  return apiFetch(`/api/experiences/${experienceId}/restore`, { method: "POST" }, token);
}

export async function updateExperienceMetadata(
  experienceId: string,
  payload: { role?: string; year?: number; round?: string; difficulty?: string },
  token: string
): Promise<Experience> {
  return apiFetch(`/api/experiences/${experienceId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  }, token);
}

export async function addQuestionsToExperience(
  experienceId: string,
  questions: string[],
  token: string
): Promise<Experience> {
  return apiFetch(`/api/experiences/${experienceId}/questions`, {
    method: "PATCH",
    body: JSON.stringify({ questions }),
  }, token);
}

// ─────────────────────────────────────────────────────────────────────────────
// Identity helpers
// ─────────────────────────────────────────────────────────────────────────────

export async function updateDisplayName(
  name: string,
  token: string
): Promise<Record<string, unknown>> {
  return apiFetch("/api/users/me/name", {
    method: "PATCH",
    body: JSON.stringify({ name }),
  }, token);
}

export async function fetchPlacementCellAdmin(
  token: string
): Promise<PlacementCellAdminResponse> {
  return apiFetch("/api/dashboard/admin", { method: "GET" }, token);
}

export async function fetchModerationQueue(
  token: string,
  options?: {
    status?: "all" | "pending" | "processing" | "done" | "failed";
    active?: "all" | "active" | "hidden";
    limit?: number;
  }
): Promise<ModerationQueueResponse> {
  const params = new URLSearchParams();
  if (options?.status) {
    params.set("status", options.status);
  }
  if (options?.active) {
    params.set("active", options.active);
  }
  if (typeof options?.limit === "number") {
    params.set("limit", String(options.limit));
  }
  const suffix = params.toString() ? `?${params.toString()}` : "";
  return apiFetch(`/api/experiences/admin/queue${suffix}`, { method: "GET" }, token);
}

export async function reprocessExperienceAsPlacementCell(
  experienceId: string,
  token: string
): Promise<{ status: string; experience_id: string; user_question_count: number }> {
  return apiFetch(`/api/experiences/admin/${experienceId}/reprocess`, {
    method: "POST",
  }, token);
}

export async function updateExperienceVisibilityAsPlacementCell(
  experienceId: string,
  payload: { is_active: boolean; note?: string },
  token: string
): Promise<{ status: string; experience_id: string; changed: boolean }> {
  return apiFetch(`/api/experiences/admin/${experienceId}/visibility`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  }, token);
}

export async function resetSearchRuntimeAsPlacementCell(
  token: string
): Promise<{ status: string; search_runtime: Record<string, unknown> }> {
  return apiFetch("/api/dashboard/admin/search/runtime/reset", { method: "POST" }, token);
}

export async function clearSearchCacheAsPlacementCell(
  token: string
): Promise<{ status: string; cache: Record<string, unknown> }> {
  return apiFetch("/api/dashboard/admin/search/cache/clear", { method: "POST" }, token);
}

export async function warmupSearchAsPlacementCell(
  token: string
): Promise<{ status: string; warmup: Record<string, unknown>; search_runtime: Record<string, unknown> }> {
  return apiFetch("/api/dashboard/admin/search/warmup", { method: "POST" }, token);
}
