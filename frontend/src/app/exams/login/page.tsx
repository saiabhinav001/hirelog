"use client";

/**
 * Institutional login (Prompt 2 backend, Prompt 7 UI). After verification,
 * routes based on SERVER-reported capabilities from `/api/exams/v1/me`
 * (contract: "Role routing is based on server capabilities"), never a
 * client-side guess.
 *
 * Two ways in: a college-ID one-time code (students and institution staff), and
 * Google sign-in for platform/faculty accounts the operator has allow-listed.
 */

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { GoogleAuthProvider, signInWithCustomToken, signInWithPopup, signOut } from "firebase/auth";

import { EXAM_SESSION_NOTICE_KEY } from "@/lib/examAuth";
import { auth } from "@/lib/firebase";
import { examApiFetch } from "@/lib/examsApi";

const EXAMS_ENABLED = process.env.NEXT_PUBLIC_EXAMS_ENABLED === "true";
const RESEND_COOLDOWN_SECONDS = 30;

type Step = "challenge" | "verify";
type Me = {
  is_bootstrap_faculty: boolean;
  memberships: { role_scope: string; status: string }[];
};

function destinationFor(me: Me): string | null {
  const active = me.memberships.filter((m) => m.status === "active");
  if (me.is_bootstrap_faculty || active.some((m) => m.role_scope === "faculty" || m.role_scope === "admin")) {
    return "/exams/faculty";
  }
  if (active.some((m) => m.role_scope === "student")) {
    return "/exams/student";
  }
  return null;
}

function errorMessage(err: unknown, fallback: string): string {
  return err instanceof Error && err.message ? err.message : fallback;
}

export default function ExamsLoginPage() {
  const router = useRouter();
  const [step, setStep] = useState<Step>("challenge");
  const [institutionId, setInstitutionId] = useState("");
  const [identityKey, setIdentityKey] = useState("");
  const [contactEmail, setContactEmail] = useState("");
  const [code, setCode] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [resendIn, setResendIn] = useState(0);

  useEffect(() => {
    try {
      const notice = window.sessionStorage.getItem(EXAM_SESSION_NOTICE_KEY);
      if (notice) {
        window.sessionStorage.removeItem(EXAM_SESSION_NOTICE_KEY);
        setError(notice);
      }
    } catch {
      // Storage unavailable — nothing to show.
    }
  }, []);

  useEffect(() => {
    if (resendIn <= 0) return undefined;
    const timer = setTimeout(() => setResendIn((seconds) => seconds - 1), 1000);
    return () => clearTimeout(timer);
  }, [resendIn]);

  if (!EXAMS_ENABLED) {
    return <p className="page-container py-12 text-sm text-[var(--text-muted)]">Institutional exam login is not enabled.</p>;
  }

  const routeByCapabilities = async () => {
    const token = await auth.currentUser?.getIdToken();
    const me = await examApiFetch<Me>("/api/exams/v1/me", { method: "GET" }, token);
    const destination = destinationFor(me);
    if (!destination) {
      await signOut(auth);
      setError("This account has no active exam access. Contact your institution if you expected to have it.");
      return;
    }
    router.push(destination);
  };

  const sendCode = async () => {
    setLoading(true);
    setError(null);
    try {
      await examApiFetch("/api/exams/v1/auth/challenge", {
        method: "POST",
        body: JSON.stringify({ institution_id: institutionId.trim(), identity_key: identityKey.trim(), contact_email: contactEmail.trim() }),
      });
      setMessage("If these details match an active member, a code was sent to the registered email.");
      setStep("verify");
      setCode("");
      setResendIn(RESEND_COOLDOWN_SECONDS);
    } catch (err) {
      setError(errorMessage(err, "Could not send a code. Please try again."));
    } finally {
      setLoading(false);
    }
  };

  const handleChallenge = async (event: React.FormEvent) => {
    event.preventDefault();
    await sendCode();
  };

  const handleVerify = async (event: React.FormEvent) => {
    event.preventDefault();
    setLoading(true);
    setError(null);
    try {
      const result = await examApiFetch<{ custom_token: string; uid: string }>("/api/exams/v1/auth/verify", {
        method: "POST",
        body: JSON.stringify({ institution_id: institutionId.trim(), identity_key: identityKey.trim(), code: code.trim() }),
      });
      await signInWithCustomToken(auth, result.custom_token);
      await routeByCapabilities();
    } catch (err) {
      setError(errorMessage(err, "That code is invalid or has expired."));
    } finally {
      setLoading(false);
    }
  };

  const handleGoogle = async () => {
    setLoading(true);
    setError(null);
    try {
      await signInWithPopup(auth, new GoogleAuthProvider());
      await routeByCapabilities();
    } catch (err) {
      setError(errorMessage(err, "Google sign-in did not complete."));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="page-container max-w-sm py-16">
      <h1 className="text-2xl font-semibold">Institution sign-in</h1>
      <div aria-live="polite">
        {message && <p className="mt-3 text-sm text-[var(--text-muted)]">{message}</p>}
        {error && (
          <p role="alert" className="mt-3 text-sm text-[var(--error)]">
            {error}
          </p>
        )}
      </div>

      {step === "challenge" && (
        <form onSubmit={handleChallenge} className="mt-6 space-y-4">
          <div>
            <label htmlFor="institution-id" className="label">Institution ID</label>
            <input id="institution-id" className="input-field w-full" value={institutionId} onChange={(e) => setInstitutionId(e.target.value)} autoComplete="off" required />
          </div>
          <div>
            <label htmlFor="identity-key" className="label">College ID</label>
            <input id="identity-key" className="input-field w-full" value={identityKey} onChange={(e) => setIdentityKey(e.target.value)} autoComplete="username" required />
          </div>
          <div>
            <label htmlFor="contact-email" className="label">Registered email</label>
            <input id="contact-email" type="email" className="input-field w-full" value={contactEmail} onChange={(e) => setContactEmail(e.target.value)} autoComplete="email" required />
          </div>
          <button type="submit" className="btn-primary w-full" disabled={loading}>
            {loading ? "Sending…" : "Send code"}
          </button>
        </form>
      )}

      {step === "verify" && (
        <form onSubmit={handleVerify} className="mt-6 space-y-4">
          <div>
            <label htmlFor="otp-code" className="label">6-digit code</label>
            <input
              id="otp-code"
              className="input-field w-full"
              value={code}
              onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))}
              inputMode="numeric"
              autoComplete="one-time-code"
              pattern="[0-9]{6}"
              maxLength={6}
              required
            />
          </div>
          <button type="submit" className="btn-primary w-full" disabled={loading}>
            {loading ? "Verifying…" : "Verify"}
          </button>
          <div className="flex items-center justify-between text-sm">
            <button type="button" className="btn-secondary" onClick={() => { setStep("challenge"); setError(null); }}>
              Use a different ID
            </button>
            <button type="button" className="btn-secondary" disabled={loading || resendIn > 0} onClick={() => void sendCode()}>
              {resendIn > 0 ? `Resend in ${resendIn}s` : "Resend code"}
            </button>
          </div>
        </form>
      )}

      <div className="mt-8 border-t border-[var(--border)] pt-6">
        <p className="text-sm text-[var(--text-muted)]">Faculty or platform administrator with a Google account?</p>
        <button type="button" className="btn-secondary mt-3 w-full" onClick={() => void handleGoogle()} disabled={loading}>
          Continue with Google
        </button>
      </div>
    </div>
  );
}
