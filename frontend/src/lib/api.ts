const rawBase = process.env.NEXT_PUBLIC_API_BASE_URL;
const PROD_FALLBACK_API_BASE = "https://rockstar00-hirelog-backend.hf.space";
const DEV_FALLBACK_API_BASE = "http://localhost:8000";
const API_BASE_STORAGE_KEY = "hirelog_preferred_api_base";

let preferredApiBaseMemory: string | null = null;

function getPreferredApiBase(): string | null {
  if (preferredApiBaseMemory) {
    return preferredApiBaseMemory;
  }

  if (typeof window === "undefined") {
    return null;
  }

  try {
    const stored = window.localStorage.getItem(API_BASE_STORAGE_KEY);
    preferredApiBaseMemory = normalizeApiBase(stored ?? undefined);
    return preferredApiBaseMemory;
  } catch {
    return null;
  }
}

function setPreferredApiBase(base: string) {
  preferredApiBaseMemory = base;
  if (typeof window === "undefined") {
    return;
  }
  try {
    window.localStorage.setItem(API_BASE_STORAGE_KEY, base);
  } catch {
    // Ignore storage failures (private mode/quota).
  }
}

function clearPreferredApiBase(base: string) {
  if (preferredApiBaseMemory === base) {
    preferredApiBaseMemory = null;
  }
  if (typeof window === "undefined") {
    return;
  }
  try {
    const stored = window.localStorage.getItem(API_BASE_STORAGE_KEY);
    if (stored === base) {
      window.localStorage.removeItem(API_BASE_STORAGE_KEY);
    }
  } catch {
    // Ignore storage failures.
  }
}

function normalizeApiBase(value: string | undefined): string | null {
  if (!value) return null;
  const trimmed = value.trim();
  if (!trimmed || !trimmed.startsWith("http")) return null;
  return trimmed.replace(/\/+$/, "");
}

function apiBaseCandidates(): string[] {
  const candidates: string[] = [];
  const preferred = getPreferredApiBase();
  if (preferred) {
    candidates.push(preferred);
  }

  const fromEnv = normalizeApiBase(rawBase);
  if (fromEnv) {
    candidates.push(fromEnv);
  }

  if (process.env.NODE_ENV === "production") {
    candidates.push(PROD_FALLBACK_API_BASE);
  } else {
    candidates.push(DEV_FALLBACK_API_BASE);
  }

  return [...new Set(candidates)];
}

export const API_BASE = apiBaseCandidates()[0];

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
  const method = (options.method ?? "GET").toUpperCase();

  const headers = new Headers(options.headers || {});
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  if (options.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  const bases = apiBaseCandidates();
  let lastError = "Unable to reach the server. Please try again.";

  for (const base of bases) {
    let response: Response | null = null;

    for (let attempt = 0; attempt < MAX_RETRIES; attempt++) {
      try {
        response = await fetch(`${base}${path}`, {
          ...options,
          headers,
          cache: method === "GET" ? "default" : "no-store",
        });
        break;
      } catch (error) {
        const isNetworkOrCorsError = error instanceof TypeError;

        // Fast-fail obvious bad endpoints (for example CORS-blocked env base)
        // and move to the next candidate immediately.
        if (isNetworkOrCorsError) {
          clearPreferredApiBase(base);
          break;
        }

        if (attempt < MAX_RETRIES - 1) {
          const backoff = RETRY_BASE_MS * Math.pow(2, attempt);
          const jitter = Math.random() * backoff * 0.5;
          await sleep(backoff + jitter);
          continue;
        }
      }
    }

    if (!response) {
      lastError = "Unable to reach the server. Please check your connection and try again.";
      continue;
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

    if (response.ok) {
      setPreferredApiBase(base);
      return data as T;
    }

    const message = (data?.detail as string) || (data?.message as string) || "Request failed";
    const htmlLike = typeof message === "string" && message.includes("<!DOCTYPE html>");
    const canTryNextBase = response.status >= 500 || (response.status === 404 && htmlLike);

    if (canTryNextBase && base !== bases[bases.length - 1]) {
      lastError = "Primary backend is unavailable. Switched to fallback endpoint.";
      continue;
    }

    throw new Error(message);
  }

  throw new Error(lastError);
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
