"use client";

import { useEffect, useState } from "react";
import Link from "next/link";

import { ProtectedRoute } from "@/components/ProtectedRoute";
import { useAuth } from "@/context/AuthContext";
import { apiFetch, updateDisplayName } from "@/lib/api";
import { useToast } from "@/context/ToastContext";
import { auth } from "@/lib/firebase";

// ─────────────────────────────────────────────────────────────────────────────
// Types
// ─────────────────────────────────────────────────────────────────────────────

interface ProfileData {
  identity: {
    uid: string;
    name: string;
    display_name?: string;
    email: string;
    role: "viewer" | "contributor";
    created_at?: string;
    can_edit_name?: boolean;
    next_name_edit_date?: string | null;
  };
  contribution_summary: {
    total_experiences: number;
    active: number;
    hidden: number;
    questions_extracted: number;
    questions_added_later: number;
    anonymous_contributions: number;
    companies_covered: string[];
    topics_covered: string[];
  };
  practice_summary: {
    total_lists: number;
    total_questions: number;
    revised: number;
    practicing: number;
    unvisited: number;
  };
}

// ─────────────────────────────────────────────────────────────────────────────
// Stat Card
// ─────────────────────────────────────────────────────────────────────────────

function StatCard({ label, value, sub }: { label: string; value: string | number; sub?: string }) {
  return (
    <div className="stat-card">
      <p className="text-xs text-[var(--text-muted)] uppercase tracking-wide">{label}</p>
      <p className="mt-1 text-2xl font-semibold">{value}</p>
      {sub && <p className="text-xs text-[var(--text-muted)] mt-0.5">{sub}</p>}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Progress Ring (practice completion)
// ─────────────────────────────────────────────────────────────────────────────

function ProgressRing({ revised, practicing, total }: { revised: number; practicing: number; total: number }) {
  if (total === 0) return null;
  const revisedPct = Math.round((revised / total) * 100);
  const practicingPct = Math.round((practicing / total) * 100);
  const circumference = 2 * Math.PI * 40;
  const revisedDash = (revisedPct / 100) * circumference;
  const practicingDash = (practicingPct / 100) * circumference;

  return (
    <div className="flex items-center gap-4">
      <svg width="96" height="96" viewBox="0 0 96 96" className="shrink-0">
        {/* Background circle */}
        <circle cx="48" cy="48" r="40" fill="none" stroke="var(--surface-muted)" strokeWidth="8" />
        {/* Revised (green) */}
        <circle
          cx="48" cy="48" r="40" fill="none"
          stroke="var(--success, #22c55e)" strokeWidth="8"
          strokeDasharray={`${revisedDash} ${circumference - revisedDash}`}
          strokeDashoffset={circumference / 4}
          strokeLinecap="round"
        />
        {/* Practicing (blue) */}
        <circle
          cx="48" cy="48" r="40" fill="none"
          stroke="var(--primary)" strokeWidth="8"
          strokeDasharray={`${practicingDash} ${circumference - practicingDash}`}
          strokeDashoffset={circumference / 4 - revisedDash}
          strokeLinecap="round"
        />
        <text x="48" y="48" textAnchor="middle" dominantBaseline="central"
          className="text-lg font-semibold fill-[var(--text)]">
          {revisedPct}%
        </text>
      </svg>
      <div className="space-y-1 text-xs">
        <div className="flex items-center gap-2">
          <span className="h-2 w-2 rounded-full bg-[var(--success,#22c55e)]" />
          Revised ({revised})
        </div>
        <div className="flex items-center gap-2">
          <span className="h-2 w-2 rounded-full bg-[var(--primary)]" />
          Practicing ({practicing})
        </div>
        <div className="flex items-center gap-2">
          <span className="h-2 w-2 rounded-full bg-[var(--surface-muted)]" />
          Unvisited ({total - revised - practicing})
        </div>
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Main Page
// ─────────────────────────────────────────────────────────────────────────────

export default function ProfilePage() {
  const { user, loading: authLoading, refreshProfile } = useAuth();
  const { toast } = useToast();
  const [data, setData] = useState<ProfileData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Name editing state
  const [editingName, setEditingName] = useState(false);
  const [nameInput, setNameInput] = useState("");
  const [nameSaving, setNameSaving] = useState(false);

  useEffect(() => {
    // Wait for auth to fully hydrate before making API calls
    if (authLoading) return;
    if (!user || !auth.currentUser) {
      setLoading(false);
      return;
    }

    const load = async () => {
      setLoading(true);
      setError(null);
      try {
        const token = await auth.currentUser!.getIdToken();
        const result = await apiFetch<ProfileData>("/api/users/profile", { method: "GET" }, token);
        setData(result);
      } catch {
        setError("Couldn't load your profile. Please try again.");
      } finally {
        setLoading(false);
      }
    };
    load();
  }, [user, authLoading]);

  const cs = data?.contribution_summary;
  const ps = data?.practice_summary;
  const identity = data?.identity;

  const handleNameSave = async () => {
    if (!auth.currentUser || !nameInput.trim()) return;
    setNameSaving(true);
    try {
      const token = await auth.currentUser.getIdToken();
      await updateDisplayName(nameInput.trim(), token);
      // Reload profile data to reflect new identity
      const result = await apiFetch<ProfileData>("/api/users/profile", { method: "GET" }, token);
      setData(result);
      refreshProfile().catch(() => {});
      setEditingName(false);
      toast("Name updated successfully.", "success");
    } catch (err: unknown) {
      toast(err instanceof Error ? err.message : "Failed to update name.", "error");
    } finally {
      setNameSaving(false);
    }
  };

  const canEdit = identity?.can_edit_name !== false;
  const nextEligible = identity?.next_name_edit_date
    ? new Date(identity.next_name_edit_date).toLocaleDateString("en-US", {
        month: "long",
        day: "numeric",
        year: "numeric",
      })
    : null;

  return (
    <ProtectedRoute>
      <div className="page-container py-12">
        {loading ? (
          <div className="space-y-6">
            <div className="flex items-center gap-4">
              <div className="skeleton h-14 w-14 rounded-full" />
              <div className="space-y-2">
                <div className="skeleton skeleton-heading w-40" />
                <div className="skeleton skeleton-text w-48" />
              </div>
            </div>
            <div className="grid gap-3 sm:grid-cols-4">
              {[1, 2, 3, 4].map((i) => <div key={i} className="skeleton skeleton-card" />)}
            </div>
          </div>
        ) : error ? (
          <div className="card p-8 text-center max-w-md mx-auto">
            <svg className="h-10 w-10 text-[var(--text-muted)] mx-auto" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z" />
            </svg>
            <p className="mt-3 text-sm text-[var(--text-muted)]">{error}</p>
            <button className="btn-secondary mt-4" onClick={() => window.location.reload()}>Try again</button>
          </div>
        ) : data ? (
          <>
            {/* Identity */}
            <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
              <div className="flex items-center gap-4">
                <div className="flex h-14 w-14 items-center justify-center rounded-full bg-[var(--primary)] text-xl font-bold text-white">
                  {(identity?.name || "U").charAt(0).toUpperCase()}
                </div>
                <div className="min-w-0">
                  {editingName ? (
                    <div className="flex items-center gap-2">
                      <input
                        className="input-field text-lg font-semibold py-1 px-2 w-56"
                        value={nameInput}
                        onChange={(e) => setNameInput(e.target.value)}
                        onKeyDown={(e) => { if (e.key === "Enter") handleNameSave(); if (e.key === "Escape") setEditingName(false); }}
                        autoFocus
                        maxLength={120}
                        disabled={nameSaving}
                      />
                      <button className="btn-primary text-xs px-3 py-1" onClick={handleNameSave} disabled={nameSaving || !nameInput.trim()}>
                        {nameSaving ? "Saving…" : "Save"}
                      </button>
                      <button className="btn-ghost text-xs px-2 py-1" onClick={() => setEditingName(false)} disabled={nameSaving}>
                        Cancel
                      </button>
                    </div>
                  ) : (
                    <div className="flex items-center gap-2">
                      <h1 className="text-2xl font-semibold">{identity?.name || "User"}</h1>
                      {canEdit ? (
                        <button
                          onClick={() => { setNameInput(identity?.name || ""); setEditingName(true); }}
                          className="btn-ghost p-1 rounded-md"
                          title="Edit name"
                        >
                          <svg className="h-4 w-4 text-[var(--text-muted)]" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                            <path strokeLinecap="round" strokeLinejoin="round" d="M16.862 4.487l1.687-1.688a1.875 1.875 0 112.652 2.652L6.832 19.82a4.5 4.5 0 01-1.897 1.13l-2.685.8.8-2.685a4.5 4.5 0 011.13-1.897L16.863 4.487z" />
                          </svg>
                        </button>
                      ) : (
                        <span className="text-[10px] text-[var(--text-muted)] bg-[var(--surface-muted)] px-2 py-0.5 rounded-full">
                          Editable {nextEligible}
                        </span>
                      )}
                    </div>
                  )}
                  {identity?.display_name && !editingName && (
                    <p className="text-xs text-[var(--text-muted)] mt-0.5">
                      Appears as: <span className="font-medium text-[var(--text-secondary)]">{identity.display_name}</span>
                    </p>
                  )}
                  <p className="text-sm text-[var(--text-muted)]">{identity?.email}</p>
                </div>
              </div>
              <div className="flex items-center gap-2">
                <span className={`inline-flex items-center rounded-full px-3 py-1 text-xs font-medium ${
                  identity?.role === "contributor"
                    ? "bg-green-500/10 text-green-400"
                    : "bg-blue-500/10 text-blue-400"
                }`}>
                  {identity?.role}
                </span>
                {identity?.created_at && (
                  <span className="text-xs text-[var(--text-muted)]">
                    Since {new Date(identity.created_at).toLocaleDateString("en-US", { month: "short", year: "numeric" })}
                  </span>
                )}
              </div>
            </div>

            {/* Name cooldown trust microcopy */}
            <p className="mt-2 text-[10px] text-[var(--text-muted)]">
              For trust and accountability, name changes are limited to once every 30 days.
            </p>

            {/* Nudge for viewers — auto-upgrade happens on first submit */}
            {identity?.role === "viewer" && (
              <div className="mt-6 p-4 rounded-lg border border-[var(--border)] bg-[var(--surface)] flex items-start gap-3">
                <svg className="h-5 w-5 text-[var(--primary)] shrink-0 mt-0.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09zM18.259 8.715L18 9.75l-.259-1.035a3.375 3.375 0 00-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 002.455-2.456L18 2.25l.259 1.035a3.375 3.375 0 002.455 2.456L21.75 6l-1.036.259a3.375 3.375 0 00-2.455 2.456zM16.894 20.567L16.5 21.75l-.394-1.183a2.25 2.25 0 00-1.423-1.423L13.5 18.75l1.183-.394a2.25 2.25 0 001.423-1.423l.394-1.183.394 1.183a2.25 2.25 0 001.423 1.423l1.183.394-1.183.394a2.25 2.25 0 00-1.423 1.423z" />
                </svg>
                <div>
                  <p className="text-sm font-medium">Ready to contribute?</p>
                  <p className="text-xs text-[var(--text-muted)] mt-0.5">
                    Submit your first interview experience and you&apos;ll be automatically upgraded to contributor.
                    No manual steps needed.
                  </p>
                  <Link href="/submit" className="btn-primary text-xs mt-3 inline-block">
                    Share an experience
                  </Link>
                </div>
              </div>
            )}

            {/* ── Contribution Summary ─────────────────────────────────── */}
            <section className="mt-8">
              <div className="flex items-baseline justify-between">
                <h2 className="text-lg font-semibold">Contribution Summary</h2>
                {cs && cs.total_experiences > 0 && (
                  <Link href="/contributions" className="text-xs text-[var(--primary)] hover:underline">
                    Manage contributions →
                  </Link>
                )}
              </div>

              {cs && cs.total_experiences > 0 ? (
                <>
                  <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                    <StatCard label="Experiences" value={cs.total_experiences} sub={`${cs.active} active · ${cs.hidden} hidden`} />
                    <StatCard label="Questions extracted" value={cs.questions_extracted} sub={cs.questions_added_later > 0 ? `+${cs.questions_added_later} added later` : undefined} />
                    <StatCard label="Companies" value={cs.companies_covered.length} />
                    <StatCard label="Topics" value={cs.topics_covered.length} />
                  </div>

                  {/* Privacy note */}
                  {cs.anonymous_contributions > 0 && (
                    <p className="mt-3 text-xs text-[var(--text-muted)]">
                      {cs.anonymous_contributions} of your {cs.total_experiences} contribution{cs.total_experiences !== 1 ? "s" : ""} submitted anonymously — identity hidden publicly, preserved for moderation.
                    </p>
                  )}

                  {/* Topic coverage */}
                  {cs.topics_covered.length > 0 && (
                    <div className="mt-4 flex flex-wrap gap-1.5">
                      {cs.topics_covered.map((topic) => (
                        <span key={topic} className="badge">{topic}</span>
                      ))}
                    </div>
                  )}
                </>
              ) : (
                <div className="mt-4 card p-6 text-center">
                  <p className="text-sm text-[var(--text-muted)]">
                    No contributions yet. Share interview experiences to build the archive.
                  </p>
                  <Link href="/submit" className="btn-primary text-sm mt-3 inline-block">
                    Contribute an experience
                  </Link>
                </div>
              )}
            </section>

            {/* ── Practice Activity ────────────────────────────────────── */}
            <section className="mt-8">
              <div className="flex items-baseline justify-between">
                <h2 className="text-lg font-semibold">Practice Activity</h2>
                {ps && ps.total_lists > 0 && (
                  <Link href="/practice" className="text-xs text-[var(--primary)] hover:underline">
                    View practice lists →
                  </Link>
                )}
              </div>

              {ps && ps.total_lists > 0 ? (
                <div className="mt-4 flex flex-col gap-6 sm:flex-row sm:items-start">
                  <div className="grid gap-3 sm:grid-cols-2 flex-1">
                    <StatCard label="Practice lists" value={ps.total_lists} />
                    <StatCard label="Total questions" value={ps.total_questions} />
                    <StatCard label="Revised" value={ps.revised} />
                    <StatCard label="In progress" value={ps.practicing} />
                  </div>
                  {ps.total_questions > 0 && (
                    <ProgressRing
                      revised={ps.revised}
                      practicing={ps.practicing}
                      total={ps.total_questions}
                    />
                  )}
                </div>
              ) : (
                <div className="mt-4 card p-6 text-center">
                  <p className="text-sm text-[var(--text-muted)]">
                    No practice lists yet. Save questions from search results to start practicing.
                  </p>
                  <Link href="/search" className="btn-primary text-sm mt-3 inline-block">
                    Search the archive
                  </Link>
                </div>
              )}
            </section>

            {/* ── Privacy & Controls ───────────────────────────────────── */}
            <section className="mt-8">
              <h2 className="text-lg font-semibold">Privacy & Data Controls</h2>
              <div className="mt-4 card p-5 space-y-4">
                <div className="flex items-start gap-3">
                  <svg className="h-5 w-5 text-[var(--text-muted)] shrink-0 mt-0.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M16.5 10.5V6.75a4.5 4.5 0 10-9 0v3.75m-.75 11.25h10.5a2.25 2.25 0 002.25-2.25v-6.75a2.25 2.25 0 00-2.25-2.25H6.75a2.25 2.25 0 00-2.25 2.25v6.75a2.25 2.25 0 002.25 2.25z" />
                  </svg>
                  <div>
                    <p className="text-sm font-medium">Anonymous contributions</p>
                    <p className="text-xs text-[var(--text-muted)]">
                      Choosing anonymous is permanent — no identity references are stored, and the decision cannot be reversed.
                      Anonymous submissions always display as &ldquo;Anonymous&rdquo;.
                    </p>
                  </div>
                </div>
                <div className="flex items-start gap-3">
                  <svg className="h-5 w-5 text-[var(--text-muted)] shrink-0 mt-0.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M3.98 8.223A10.477 10.477 0 001.934 12C3.226 16.338 7.244 19.5 12 19.5c.993 0 1.953-.138 2.863-.395M6.228 6.228A10.45 10.45 0 0112 4.5c4.756 0 8.773 3.162 10.065 7.498a10.523 10.523 0 01-4.293 5.774M6.228 6.228L3 3m3.228 3.228l3.65 3.65m7.894 7.894L21 21m-3.228-3.228l-3.65-3.65m0 0a3 3 0 10-4.243-4.243m4.242 4.242L9.88 9.88" />
                  </svg>
                  <div>
                    <p className="text-sm font-medium">Visibility controls</p>
                    <p className="text-xs text-[var(--text-muted)]">
                      Hide any contribution from public search and analytics via{" "}
                      <Link href="/contributions" className="text-[var(--primary)] hover:underline">My Contributions</Link>.
                      Hidden entries are excluded from all public views but never permanently deleted.
                    </p>
                  </div>
                </div>
                <div className="flex items-start gap-3">
                  <svg className="h-5 w-5 text-[var(--text-muted)] shrink-0 mt-0.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M9 12.75L11.25 15 15 9.75m-3-7.036A11.959 11.959 0 013.598 6 11.99 11.99 0 003 9.749c0 5.592 3.824 10.29 9 11.623 5.176-1.332 9-6.03 9-11.622 0-1.31-.21-2.571-.598-3.751h-.152c-3.196 0-6.1-1.248-8.25-3.285z" />
                  </svg>
                  <div>
                    <p className="text-sm font-medium">Edit auditability</p>
                    <p className="text-xs text-[var(--text-muted)]">
                      All metadata edits and question additions are tracked with timestamps.
                      Original AI extractions are preserved — later additions are clearly marked.
                    </p>
                  </div>
                </div>
              </div>
            </section>
          </>
        ) : null}
      </div>
    </ProtectedRoute>
  );
}
