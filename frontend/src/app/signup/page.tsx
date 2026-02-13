"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { useAuth } from "@/context/AuthContext";

// ─────────────────────────────────────────────────────────────────────────────
// Floating-label text field
// ─────────────────────────────────────────────────────────────────────────────

function FloatingField({
  id,
  label,
  type = "text",
  value,
  onChange,
  required,
  helper,
  autoComplete,
}: {
  id: string;
  label: string;
  type?: string;
  value: string;
  onChange: (v: string) => void;
  required?: boolean;
  helper?: string;
  autoComplete?: string;
}) {
  return (
    <div>
      <div className="floating-group">
        <input
          id={id}
          type={type}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          required={required}
          placeholder=" "
          autoComplete={autoComplete}
          className="floating-input"
        />
        <label htmlFor={id} className="floating-label">
          {label}
        </label>
      </div>
      {helper && (
        <p className="mt-1 text-[11px] text-[var(--text-disabled)]">{helper}</p>
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Password field with visibility toggle + caps-lock warning
// ─────────────────────────────────────────────────────────────────────────────

function PasswordField({
  id,
  label,
  value,
  onChange,
  helper,
  autoComplete,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (v: string) => void;
  helper?: string;
  autoComplete?: string;
}) {
  const [visible, setVisible] = useState(false);
  const [capsLock, setCapsLock] = useState(false);

  return (
    <div>
      <div className="floating-group">
        <input
          id={id}
          type={visible ? "text" : "password"}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={(e) => setCapsLock(e.getModifierState("CapsLock"))}
          onKeyUp={(e) => setCapsLock(e.getModifierState("CapsLock"))}
          required
          placeholder=" "
          autoComplete={autoComplete}
          className="floating-input pr-10"
        />
        <label htmlFor={id} className="floating-label">
          {label}
        </label>
        <button
          type="button"
          tabIndex={-1}
          onClick={() => setVisible((v) => !v)}
          className="absolute right-3 top-1/2 -translate-y-1/2 text-[var(--text-disabled)] hover:text-[var(--text-muted)] transition-colors"
          aria-label={visible ? "Hide password" : "Show password"}
        >
          {visible ? (
            <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M3.98 8.223A10.477 10.477 0 001.934 12C3.226 16.338 7.244 19.5 12 19.5c.993 0 1.953-.138 2.863-.395M6.228 6.228A10.45 10.45 0 0112 4.5c4.756 0 8.773 3.162 10.065 7.498a10.523 10.523 0 01-4.293 5.774M6.228 6.228L3 3m3.228 3.228l3.65 3.65m7.894 7.894L21 21m-3.228-3.228l-3.65-3.65m0 0a3 3 0 10-4.243-4.243m4.242 4.242L9.88 9.88" />
            </svg>
          ) : (
            <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M2.036 12.322a1.012 1.012 0 010-.639C3.423 7.51 7.36 4.5 12 4.5c4.638 0 8.573 3.007 9.963 7.178.07.207.07.431 0 .639C20.577 16.49 16.64 19.5 12 19.5c-4.638 0-8.573-3.007-9.963-7.178z" />
              <path strokeLinecap="round" strokeLinejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
            </svg>
          )}
        </button>
      </div>
      {capsLock && (
        <p className="mt-1 text-[11px] text-[var(--warning)] flex items-center gap-1">
          <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z" />
          </svg>
          Caps Lock is on
        </p>
      )}
      {helper && !capsLock && (
        <p className="mt-1 text-[11px] text-[var(--text-disabled)]">{helper}</p>
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Divider
// ─────────────────────────────────────────────────────────────────────────────

function OrDivider() {
  return (
    <div className="relative my-5 sm:my-6">
      <div className="absolute inset-0 flex items-center">
        <div className="w-full border-t border-[var(--border)]" />
      </div>
      <div className="relative flex justify-center">
        <span className="bg-[var(--bg)] px-3 text-xs text-[var(--text-disabled)]">or</span>
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Page — All users sign up as "viewer". Role upgrades automatically on first
// contribution (server-side, idempotent).
// ─────────────────────────────────────────────────────────────────────────────

export default function SignupPage() {
  const router = useRouter();
  const { signUp, signInWithGoogle } = useAuth();
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    setLoading(true);
    setError(null);
    try {
      await signUp(name, email, password);
      router.push("/search");
    } catch (err: any) {
      setError(err.message || "Unable to create account.");
    } finally {
      setLoading(false);
    }
  };

  const handleGoogle = async () => {
    setLoading(true);
    setError(null);
    try {
      await signInWithGoogle();
      router.push("/search");
    } catch (err: any) {
      setError(err.message || "Unable to sign in with Google.");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="flex items-center justify-center px-5 sm:px-6 py-8 sm:py-12 min-h-[calc(100dvh-3.5rem)]">
      <div className="w-full max-w-[380px]">
        {/* Header */}
        <div className="text-center mb-6 sm:mb-8">
          <h1 className="text-xl sm:text-2xl font-semibold tracking-tight">Join the Archive</h1>
          <p className="mt-1.5 sm:mt-2 text-sm text-[var(--text-muted)]">
            Create an account to access CBIT&apos;s placement intelligence
          </p>
        </div>

        {/* Google */}
        <button
          className="btn-secondary w-full gap-2.5 !h-11 sm:!h-10"
          onClick={handleGoogle}
          disabled={loading}
        >
          <svg className="h-4 w-4" viewBox="0 0 24 24">
            <path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92a5.06 5.06 0 01-2.2 3.32v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.1z" />
            <path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" />
            <path fill="#FBBC05" d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z" />
            <path fill="#EA4335" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" />
          </svg>
          Continue with Google
        </button>

        <OrDivider />

        {/* Email form */}
        <form className="space-y-3.5 sm:space-y-4" onSubmit={handleSubmit}>
          <FloatingField
            id="signup-name"
            label="Full name"
            value={name}
            onChange={setName}
            required
            autoComplete="name"
            helper="Your real name — visible only on your profile page"
          />

          <FloatingField
            id="signup-email"
            label="Email address"
            type="email"
            value={email}
            onChange={setEmail}
            required
            autoComplete="email"
            helper="Use your institutional or personal email"
          />

          <PasswordField
            id="signup-password"
            label="Password"
            value={password}
            onChange={setPassword}
            autoComplete="new-password"
            helper="Minimum 6 characters"
          />

          {error && (
            <div className="rounded-lg bg-[var(--error-soft)] px-4 py-3 text-sm text-[var(--error)]">
              {error}
            </div>
          )}

          <button className="btn-primary w-full !h-11 sm:!h-10" disabled={loading}>
            {loading ? "Creating account…" : "Create account"}
          </button>
        </form>

        <p className="mt-4 text-center text-[10px] text-[var(--text-disabled)] leading-relaxed">
          You&apos;ll start as a viewer. Your role upgrades automatically to contributor after your first submission.
        </p>

        {/* Footer */}
        <p className="mt-5 sm:mt-6 text-center text-sm text-[var(--text-muted)]">
          Already have an account?{" "}
          <Link href="/login" className="text-[var(--primary)] hover:underline font-medium">
            Sign in
          </Link>
        </p>

        {/* Trust */}
        <p className="mt-6 sm:mt-8 text-center text-[11px] text-[var(--text-disabled)]">
          Trusted by CBIT students across batches.
        </p>
      </div>
    </div>
  );
}
