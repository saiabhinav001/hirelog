"use client";

/**
 * Institutional login (Prompt 2 backend, Prompt 7 UI). After verification,
 * routes based on SERVER-reported capabilities from `/api/exams/v1/me`
 * (contract: "Role routing is based on server capabilities"), never a
 * client-side guess.
 */

import { useRouter } from "next/navigation";
import { useState } from "react";
import { signInWithCustomToken } from "firebase/auth";

import { auth } from "@/lib/firebase";
import { examApiFetch } from "@/lib/examsApi";

const EXAMS_ENABLED = process.env.NEXT_PUBLIC_EXAMS_ENABLED === "true";

type Step = "challenge" | "verify";

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

  if (!EXAMS_ENABLED) {
    return <p className="page-container py-12 text-sm text-[var(--text-muted)]">Institutional exam login is not enabled.</p>;
  }

  const handleChallenge = async (event: React.FormEvent) => {
    event.preventDefault();
    setLoading(true);
    setError(null);
    try {
      await examApiFetch("/api/exams/v1/auth/challenge", {
        method: "POST",
        body: JSON.stringify({ institution_id: institutionId, identity_key: identityKey, contact_email: contactEmail }),
      });
      setMessage("If this identifier is valid, a code was sent to your registered email.");
      setStep("verify");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Request failed.");
    } finally {
      setLoading(false);
    }
  };

  const handleVerify = async (event: React.FormEvent) => {
    event.preventDefault();
    setLoading(true);
    setError(null);
    try {
      const result = await examApiFetch<{ custom_token: string; uid: string }>(
        "/api/exams/v1/auth/verify",
        { method: "POST", body: JSON.stringify({ institution_id: institutionId, identity_key: identityKey, code }) }
      );
      await signInWithCustomToken(auth, result.custom_token);
      const token = await auth.currentUser?.getIdToken();
      const me = await examApiFetch<{ is_bootstrap_faculty: boolean; memberships: { role_scope: string; status: string }[] }>(
        "/api/exams/v1/me",
        { method: "GET" },
        token
      );
      const isFaculty =
        me.is_bootstrap_faculty || me.memberships.some((m) => m.status === "active" && (m.role_scope === "faculty" || m.role_scope === "admin"));
      router.push(isFaculty ? "/exams/faculty" : "/exams/student");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Invalid or expired code.");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="page-container max-w-sm py-16">
      <h1 className="text-2xl font-semibold">Institution sign-in</h1>
      {message && <p className="mt-3 text-sm text-[var(--text-muted)]">{message}</p>}
      {error && <p className="mt-3 text-sm text-[var(--error)]">{error}</p>}

      {step === "challenge" && (
        <form onSubmit={handleChallenge} className="mt-6 space-y-4">
          <div>
            <label htmlFor="institution-id" className="label">Institution ID</label>
            <input id="institution-id" className="input-field w-full" value={institutionId} onChange={(e) => setInstitutionId(e.target.value)} required />
          </div>
          <div>
            <label htmlFor="identity-key" className="label">College ID</label>
            <input id="identity-key" className="input-field w-full" value={identityKey} onChange={(e) => setIdentityKey(e.target.value)} required />
          </div>
          <div>
            <label htmlFor="contact-email" className="label">Registered email</label>
            <input id="contact-email" type="email" className="input-field w-full" value={contactEmail} onChange={(e) => setContactEmail(e.target.value)} required />
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
            <input id="otp-code" className="input-field w-full" value={code} onChange={(e) => setCode(e.target.value)} maxLength={6} required />
          </div>
          <button type="submit" className="btn-primary w-full" disabled={loading}>
            {loading ? "Verifying…" : "Verify"}
          </button>
        </form>
      )}
    </div>
  );
}

