const rawBase = process.env.NEXT_PUBLIC_API_BASE_URL;
export const API_BASE =
  rawBase && rawBase.startsWith("http") ? rawBase : "http://localhost:8000";

const MAX_RETRIES = 3;
const RETRY_DELAY_MS = 1000;

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
        await sleep(RETRY_DELAY_MS * (attempt + 1));
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

import type { Experience } from "./types";

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
