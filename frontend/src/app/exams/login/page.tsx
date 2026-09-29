"use client";

/**
 * Minimal, unstyled institutional login (Prompt 2 — /exams/login). This is a
 * FUNCTIONAL PLACEHOLDER: Prompt 7 owns the real "student screens" (design
 * tokens, navigation, preflight, etc.) and will replace this page's markup
 * without changing the backend calls it makes.
 */

import { useState } from "react";
import { signInWithCustomToken } from "firebase/auth";

import { auth } from "@/lib/firebase";
import { examApiFetch } from "@/lib/examsApi";

const EXAMS_ENABLED = process.env.NEXT_PUBLIC_EXAMS_ENABLED === "true";

type Step = "challenge" | "verify" | "done";

export default function ExamsLoginPage() {
  const [step, setStep] = useState<Step>("challenge");
  const [institutionId, setInstitutionId] = useState("");
  const [identityKey, setIdentityKey] = useState("");
  const [contactEmail, setContactEmail] = useState("");
  const [code, setCode] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  if (!EXAMS_ENABLED) {
    return <p style={{ padding: 24 }}>Institutional exam login is not enabled.</p>;
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
      setStep("done");
      setMessage("Signed in.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Invalid or expired code.");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={{ padding: 24, maxWidth: 360 }}>
      <h1>Institution sign-in</h1>
      {message && <p>{message}</p>}
      {error && <p style={{ color: "red" }}>{error}</p>}

      {step === "challenge" && (
        <form onSubmit={handleChallenge}>
          <label htmlFor="institution-id">Institution ID</label>
          <input id="institution-id" value={institutionId} onChange={(e) => setInstitutionId(e.target.value)} required />
          <label htmlFor="identity-key">College ID</label>
          <input id="identity-key" value={identityKey} onChange={(e) => setIdentityKey(e.target.value)} required />
          <label htmlFor="contact-email">Registered email</label>
          <input id="contact-email" type="email" value={contactEmail} onChange={(e) => setContactEmail(e.target.value)} required />
          <button type="submit" disabled={loading}>Send code</button>
        </form>
      )}

      {step === "verify" && (
        <form onSubmit={handleVerify}>
          <label htmlFor="otp-code">6-digit code</label>
          <input id="otp-code" value={code} onChange={(e) => setCode(e.target.value)} maxLength={6} required />
          <button type="submit" disabled={loading}>Verify</button>
        </form>
      )}
    </div>
  );
}
