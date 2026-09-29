// SECURITY: this flag fully bypasses Firebase auth on the client (see
// AuthContext/authToken). It must never be true in a production build — a
// leaked build-time env var would let anyone sign in as any role with no
// credentials. Fail loud at module load instead of silently shipping it.
const rawE2EBypass = process.env.NEXT_PUBLIC_E2E_BYPASS_AUTH === "true";
if (rawE2EBypass && process.env.NODE_ENV === "production") {
  throw new Error(
    "SECURITY: NEXT_PUBLIC_E2E_BYPASS_AUTH=true in a production build. " +
      "Refusing to start with authentication bypassed. Unset this variable " +
      "for any production/staging deployment (see docs/adr/0007-role-escalation-fix.md)."
  );
}
export const E2E_AUTH_BYPASS = rawE2EBypass;

const E2E_SESSION_KEY = "hirelog_e2e_session";
const E2E_TOKEN_KEY = "hirelog_e2e_token";

export type E2ESession = {
  uid: string;
  name: string;
  email: string;
  role: "viewer" | "contributor" | "placement_cell";
};

export function readE2ESession(): E2ESession | null {
  if (!E2E_AUTH_BYPASS || typeof window === "undefined") {
    return null;
  }

  try {
    const raw = localStorage.getItem(E2E_SESSION_KEY);
    if (!raw) {
      return null;
    }
    const parsed = JSON.parse(raw) as E2ESession;
    if (!parsed?.uid || !parsed?.email) {
      return null;
    }
    return parsed;
  } catch {
    return null;
  }
}

export function writeE2ESession(session: E2ESession): void {
  if (!E2E_AUTH_BYPASS || typeof window === "undefined") {
    return;
  }

  localStorage.setItem(E2E_SESSION_KEY, JSON.stringify(session));
  if (!localStorage.getItem(E2E_TOKEN_KEY)) {
    localStorage.setItem(E2E_TOKEN_KEY, "e2e-token");
  }
}

export function clearE2ESession(): void {
  if (!E2E_AUTH_BYPASS || typeof window === "undefined") {
    return;
  }

  localStorage.removeItem(E2E_SESSION_KEY);
  localStorage.removeItem(E2E_TOKEN_KEY);
}

export function readE2EToken(): string {
  if (!E2E_AUTH_BYPASS || typeof window === "undefined") {
    return "";
  }

  const fromStorage = localStorage.getItem(E2E_TOKEN_KEY);
  if (fromStorage && fromStorage.trim()) {
    return fromStorage;
  }
  const fallback = "e2e-token";
  localStorage.setItem(E2E_TOKEN_KEY, fallback);
  return fallback;
}
