"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";

import { ProtectedRoute } from "@/components/ProtectedRoute";
import { useAuth } from "@/context/AuthContext";
import { getClientAuthToken } from "@/lib/authToken";
import {
  fetchMyContributions,
  softDeleteExperience,
  restoreExperience,
  updateExperienceMetadata,
  addQuestionsToExperience,
} from "@/lib/api";
import type { Experience } from "@/lib/types";

// ─────────────────────────────────────────────────────────────────────────────
// Edit Metadata Modal
// ─────────────────────────────────────────────────────────────────────────────

function EditMetadataModal({
  experience,
  onClose,
  onSaved,
}: {
  experience: Experience;
  onClose: () => void;
  onSaved: (updated: Experience) => void;
}) {
  const [role, setRole] = useState(experience.role);
  const [year, setYear] = useState(experience.year);
  const [round, setRound] = useState(experience.round);
  const [difficulty, setDifficulty] = useState(experience.difficulty);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSave = async () => {
    setSaving(true);
    setError(null);
    try {
      const token = await getClientAuthToken();
      if (!token) {
        throw new Error("Not authenticated.");
      }
      const payload: Record<string, string | number> = {};
      if (role !== experience.role) payload.role = role;
      if (year !== experience.year) payload.year = year;
      if (round !== experience.round) payload.round = round;
      if (difficulty !== experience.difficulty) payload.difficulty = difficulty;

      if (Object.keys(payload).length === 0) {
        onClose();
        return;
      }

      const updated = await updateExperienceMetadata(experience.id, payload, token);
      onSaved(updated);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to save changes.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-[rgba(15,23,42,0.42)] p-4">
      <div className="card w-full max-w-md max-h-[calc(100dvh-2rem)] overflow-y-auto p-5 sm:p-6 space-y-4">
        <h3 className="text-lg font-semibold">Edit Metadata</h3>
        <p className="text-xs text-[var(--text-muted)]">
          Changes are tracked in the edit history for auditability.
        </p>
        <div className="rounded-md bg-[var(--primary-soft)] border border-[var(--primary)]/25 px-3 py-2">
          <p className="text-xs text-[var(--primary)] leading-relaxed">
            <strong>🔒 Original experience text is locked.</strong>{" "}
            The AI-extracted narrative, questions, and summary cannot be edited.
            This preserves the integrity of the institutional record. You can
            only update role, year, round, and difficulty here.
          </p>
        </div>

        <div className="grid gap-3 sm:grid-cols-2">
          <div>
            <label className="label">Role</label>
            <input className="input-field" value={role} onChange={(e) => setRole(e.target.value)} />
          </div>
          <div>
            <label className="label">Year</label>
            <input
              className="input-field"
              type="number"
              value={year}
              onChange={(e) => setYear(Number(e.target.value))}
            />
          </div>
          <div>
            <label className="label">Round</label>
            <input className="input-field" value={round} onChange={(e) => setRound(e.target.value)} />
          </div>
          <div>
            <label className="label">Difficulty</label>
            <select
              className="input-field"
              value={difficulty}
              onChange={(e) => setDifficulty(e.target.value)}
            >
              {["Easy", "Medium", "Hard"].map((opt) => (
                <option key={opt} value={opt}>
                  {opt}
                </option>
              ))}
            </select>
          </div>
        </div>

        {error && (
          <p className="text-sm text-[var(--error)]">{error}</p>
        )}

        <div className="flex justify-end gap-2 pt-2">
          <button className="btn-ghost text-sm" onClick={onClose} disabled={saving}>
            Cancel
          </button>
          <button className="btn-primary text-sm" onClick={handleSave} disabled={saving}>
            {saving ? "Saving…" : "Save changes"}
          </button>
        </div>
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Add Questions Modal
// ─────────────────────────────────────────────────────────────────────────────

function AddQuestionsModal({
  experience,
  onClose,
  onSaved,
}: {
  experience: Experience;
  onClose: () => void;
  onSaved: (updated: Experience) => void;
}) {
  const [input, setInput] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleAdd = async () => {
    const lines = input
      .split("\n")
      .map((l) => l.trim())
      .filter((l) => l.length >= 5);
    if (lines.length === 0) {
      setError("Enter at least one question (min 5 characters).");
      return;
    }

    setSaving(true);
    setError(null);

    // Optimistic update: build what the saved state will look like
    const now = new Date().toISOString();
    const optimisticQuestions = lines.map((q) => ({
      question_text: q,
      question: q,
      topic: "General",
      category: "theory",
      confidence: 1,
      question_type: "extracted" as const,
      source: "user" as const,
      added_later: true,
      added_at: now,
      created_at: now,
      updated_at: now,
    }));

    const existingFlat = experience.extracted_questions ?? [];
    const optimisticFlat = [...existingFlat, ...optimisticQuestions];
    const existingUserProvided = experience.questions?.user_provided ?? [];
    const optimisticUserProvided = [...existingUserProvided, ...optimisticQuestions];
    const aiExtracted = experience.questions?.ai_extracted ?? [];

    const optimisticExperience: Experience = {
      ...experience,
      extracted_questions: optimisticFlat,
      questions: {
        user_provided: optimisticUserProvided,
        ai_extracted: aiExtracted,
      },
      stats: {
        user_question_count: optimisticUserProvided.length,
        extracted_question_count: aiExtracted.length,
        total_question_count: optimisticUserProvided.length + aiExtracted.length,
      },
    };

    // Apply optimistic update immediately
    onSaved(optimisticExperience);
    onClose();

    // Fire the API call — reconcile on response
    try {
      const token = await getClientAuthToken();
      if (!token) {
        throw new Error("Not authenticated.");
      }
      const serverResult = await addQuestionsToExperience(experience.id, lines, token);
      // Reconcile with server state
      onSaved(serverResult);
    } catch {
      // Revert to original on failure
      onSaved(experience);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-[rgba(15,23,42,0.42)] p-4">
      <div className="card w-full max-w-lg max-h-[calc(100dvh-2rem)] overflow-y-auto p-5 sm:p-6 space-y-4">
        <h3 className="text-lg font-semibold">Add Remembered Questions</h3>
        <p className="text-xs text-[var(--text-muted)]">
          Enter one question per line. Questions are saved instantly — topic classification
          happens in the background.
        </p>
        <div className="rounded-md bg-[var(--success-soft)] border border-[var(--success)]/25 px-3 py-2">
          <p className="text-xs text-[var(--success)] leading-relaxed">
            <strong>⚡ Instant save.</strong>{" "}
            Questions are written to the archive immediately.
            NLP classification and search indexing run in the background — you can navigate away.
          </p>
        </div>

        <textarea
          className="input-field min-h-[120px]"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder={"What is the time complexity of quicksort?\nExplain ACID properties in DBMS.\nHow does virtual memory work?"}
        />

        {error && <p className="text-sm text-[var(--error)]">{error}</p>}

        <div className="flex justify-end gap-2 pt-2">
          <button className="btn-ghost text-sm" onClick={onClose} disabled={saving}>
            Cancel
          </button>
          <button className="btn-primary text-sm" onClick={handleAdd} disabled={saving}>
            {saving ? "Saving…" : "Add questions"}
          </button>
        </div>
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Edit History Panel
// ─────────────────────────────────────────────────────────────────────────────

function EditHistoryPanel({ experience, onClose }: { experience: Experience; onClose: () => void }) {
  const history = [...(experience.edit_history ?? [])].sort(
    (a, b) => new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime()
  );

  const actionLabel = (entry: import("@/lib/types").EditHistoryEntry) => {
    switch (entry.action) {
      case "extracted":
        return "Submission Snapshot";
      case "ai_enrichment":
        return "Question Classification";
      case "added_later":
        return "Questions Added";
      case "metadata_change":
        return "Metadata Updated";
      case "visibility_change":
        return "Visibility Changed";
      default:
        return entry.field?.replace(/_/g, " ") ?? "Edit";
    }
  };

  const actionColor = (entry: import("@/lib/types").EditHistoryEntry) => {
    switch (entry.action) {
      case "extracted":
        return "text-[var(--info)]";
      case "ai_enrichment":
        return "text-[var(--primary)]";
      case "added_later":
        return "text-[var(--success)]";
      case "visibility_change":
        return "text-[var(--warning)]";
      default:
        return "text-[var(--text)]";
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-[rgba(15,23,42,0.42)] p-4">
      <div className="card w-full max-w-md p-5 sm:p-6 space-y-4 max-h-[calc(100dvh-2rem)] overflow-y-auto">
        <h3 className="text-lg font-semibold">Edit History</h3>
        {history.length === 0 ? (
          <p className="text-sm text-[var(--text-muted)]">No edits have been made to this contribution.</p>
        ) : (
          <div className="space-y-3">
            {history.map((entry, i) => (
              <div key={i} className="p-3 rounded-lg bg-[var(--surface-muted)] text-sm">
                <div className="flex items-baseline justify-between">
                  <span className={`font-medium ${actionColor(entry)}`}>{actionLabel(entry)}</span>
                  <span className="text-xs text-[var(--text-muted)]">
                    {new Date(entry.timestamp).toLocaleString()}
                  </span>
                </div>
                <div className="mt-1 text-xs text-[var(--text-muted)]">
                  {entry.old_value && (
                    <span className="line-through mr-2">{entry.old_value}</span>
                  )}
                  {entry.new_value && (
                    <span className="text-[var(--text)]">{entry.new_value}</span>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
        <div className="flex justify-end pt-2">
          <button className="btn-ghost text-sm" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Contribution Card
// ─────────────────────────────────────────────────────────────────────────────

function ContributionCard({
  experience,
  onUpdate,
}: {
  experience: Experience;
  onUpdate: (updated: Experience) => void;
}) {
  const [editModal, setEditModal] = useState(false);
  const [questionsModal, setQuestionsModal] = useState(false);
  const [historyModal, setHistoryModal] = useState(false);

  const isActive = experience.is_active !== false;
  const userCount = experience.stats?.user_question_count
    ?? (experience.questions?.user_provided?.length ?? 0);
  const aiCount = experience.stats?.extracted_question_count
    ?? (experience.questions?.ai_extracted?.length ?? 0);
  const totalCount = experience.stats?.total_question_count
    ?? (experience.extracted_questions?.length ?? 0);
  const nlpStatus = experience.nlp_status ?? "done";

  const handleToggleVisibility = async () => {
    // Optimistic: update UI immediately
    const prevActive = isActive;
    onUpdate({ ...experience, is_active: !prevActive });
    try {
      const token = await getClientAuthToken();
      if (!token) {
        throw new Error("Not authenticated.");
      }
      if (prevActive) {
        await softDeleteExperience(experience.id, token);
      } else {
        await restoreExperience(experience.id, token);
      }
    } catch {
      // Revert on failure
      onUpdate({ ...experience, is_active: prevActive });
    }
  };

  return (
    <>
      <div
        className={`card p-5 transition-opacity ${
          isActive ? "" : "opacity-60 border-dashed"
        }`}
      >
        {/* Header */}
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <h3 className="font-semibold truncate">{experience.company}</h3>
              <span
                className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${
                  isActive
                    ? "bg-[var(--success-soft)] text-[var(--success)]"
                    : "bg-[var(--warning-soft)] text-[var(--warning)]"
                }`}
              >
                {isActive ? "Active" : "Hidden"}
              </span>
            </div>
            <p className="text-sm text-[var(--text-muted)] mt-0.5">
              {experience.role} · {experience.round} · {experience.year} · {experience.difficulty}
            </p>
            <p className="text-xs text-[var(--text-muted)] mt-1">
              {experience.author?.visibility === "public" && experience.author?.public_label
                ? <>Attributed as: <span className="text-[var(--text-secondary)] font-medium">{experience.author.public_label}</span></>
                : <>Submitted as: <span className="font-medium">Anonymous</span> — identity permanently hidden</>}
            </p>
          </div>
          <div className="flex items-center gap-1 shrink-0">
            <button
              onClick={() => setHistoryModal(true)}
              className="btn-secondary text-sm px-3"
              title="View edit history"
            >
              <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M12 6v6h4.5m4.5 0a9 9 0 11-18 0 9 9 0 0118 0z" />
              </svg>
              History
            </button>
          </div>
        </div>

        {/* Summary */}
        <p className="mt-3 text-sm text-[var(--text-secondary)] line-clamp-2">
          {experience.summary}
        </p>

        {/* Questions summary */}
        <div className="mt-3 flex flex-wrap items-center gap-2.5 text-xs text-[var(--text-muted)]">
          <span>
            {totalCount === 0 ? (
              "No questions yet"
            ) : (
              <>
                <span className="font-medium text-[var(--text)]">{userCount} user-provided</span>
                {aiCount > 0 && (
                  <> · {aiCount} AI-extracted</>
                )}
                {" "}— {totalCount} total question{totalCount !== 1 ? "s" : ""}
              </>
            )}
          </span>
          {nlpStatus === "pending" && (
            <span className="inline-flex items-center gap-1 text-xs px-1.5 py-0.5 rounded-full bg-[var(--warning-soft)] text-[var(--warning)] font-medium">
              <span className="inline-block h-1.5 w-1.5 rounded-full bg-[var(--warning)] animate-pulse" />
              AI processing
            </span>
          )}
          {nlpStatus === "failed" && (
            <span className="text-xs px-1.5 py-0.5 rounded-full bg-[var(--error-soft)] text-[var(--error)] font-medium">
              AI enrichment failed
            </span>
          )}
          {experience.topics?.length > 0 && (
            <span>· {experience.topics.slice(0, 3).join(", ")}</span>
          )}
        </div>

        {/* Topics */}
        {experience.topics?.length > 0 && (
          <div className="mt-3 flex flex-wrap gap-1.5">
            {experience.topics.map((topic) => (
              <span key={topic} className="badge">
                {topic}
              </span>
            ))}
          </div>
        )}

        {/* Actions */}
        <div className="mt-4 border-t border-[var(--border)] pt-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="text-xs font-semibold uppercase tracking-[0.08em] text-[var(--text-muted)]">
              Actions
            </p>
            {experience.created_at && (
              <span className="text-xs text-[var(--text-muted)]">
                Added {new Date(experience.created_at).toLocaleDateString()}
              </span>
            )}
          </div>

          <div className="mt-3 grid gap-2 sm:grid-cols-3">
            <button
              onClick={() => setEditModal(true)}
              className="btn-secondary w-full justify-start px-3 text-sm"
            >
              <svg className="h-4 w-4 text-[var(--primary)]" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.6}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M16.862 3.487a2.1 2.1 0 113 2.97L8.91 17.407a4 4 0 01-1.69 1.01l-2.51.75.75-2.51a4 4 0 011.01-1.69l10.392-10.48z" />
              </svg>
              Edit metadata
            </button>

            <button
              onClick={() => setQuestionsModal(true)}
              className="btn-secondary w-full justify-start px-3 text-sm"
            >
              <svg className="h-4 w-4 text-[var(--success)]" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.6}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M12 5v14m7-7H5" />
              </svg>
              Add questions
            </button>

            <button
              onClick={handleToggleVisibility}
              className="btn-secondary w-full justify-start px-3 text-sm"
            >
              <svg
                className={`h-4 w-4 ${isActive ? "text-[var(--warning)]" : "text-[var(--success)]"}`}
                fill="none"
                viewBox="0 0 24 24"
                stroke="currentColor"
                strokeWidth={1.6}
              >
                {isActive ? (
                  <path strokeLinecap="round" strokeLinejoin="round" d="M3 3l18 18m-3.02-4.24A9.77 9.77 0 0112 19c-5.37 0-9.27-3.61-10.5-7 1-2.76 3.78-5.62 7.5-6.65m2.91-.26A10.97 10.97 0 0112 5c5.37 0 9.27 3.61 10.5 7a11.7 11.7 0 01-1.44 2.64M9.88 9.88a3 3 0 104.24 4.24" />
                ) : (
                  <path strokeLinecap="round" strokeLinejoin="round" d="M2.46 12C3.73 8.68 6.9 5 12 5s8.27 3.68 9.54 7c-1.27 3.32-4.44 7-9.54 7s-8.27-3.68-9.54-7zm9.54-3a3 3 0 100 6 3 3 0 000-6z" />
                )}
              </svg>
              {isActive ? "Hide from archive" : "Restore to archive"}
            </button>
          </div>
        </div>
      </div>

      {/* Modals */}
      {editModal && (
        <EditMetadataModal
          experience={experience}
          onClose={() => setEditModal(false)}
          onSaved={(updated) => {
            onUpdate(updated);
            setEditModal(false);
          }}
        />
      )}
      {questionsModal && (
        <AddQuestionsModal
          experience={experience}
          onClose={() => setQuestionsModal(false)}
          onSaved={(updated) => {
            onUpdate(updated);
            setQuestionsModal(false);
          }}
        />
      )}
      {historyModal && (
        <EditHistoryPanel
          experience={experience}
          onClose={() => setHistoryModal(false)}
        />
      )}
    </>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Main Page
// ─────────────────────────────────────────────────────────────────────────────

export default function ContributionsPage() {
  const { user, loading: authLoading } = useAuth();
  const [contributions, setContributions] = useState<Experience[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<"all" | "active" | "hidden">("all");

  const loadContributions = useCallback(async () => {
    if (!user) {
      setLoading(false);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const token = await getClientAuthToken();
      if (!token) {
        throw new Error("Not authenticated.");
      }
      const data = await fetchMyContributions(token);
      setContributions(data.results);
    } catch {
      setError("Couldn't load your contributions. Please try again.");
    } finally {
      setLoading(false);
    }
  }, [user]);

  useEffect(() => {
    // Wait for auth to fully hydrate before making API calls
    if (authLoading) return;
    loadContributions();
  }, [authLoading, loadContributions]);

  const handleUpdate = (updated: Experience) => {
    setContributions((prev) =>
      prev.map((c) => (c.id === updated.id ? updated : c))
    );
  };

  const filtered = contributions.filter((c) => {
    if (filter === "active") return c.is_active !== false;
    if (filter === "hidden") return c.is_active === false;
    return true;
  });

  const activeCount = contributions.filter((c) => c.is_active !== false).length;
  const hiddenCount = contributions.filter((c) => c.is_active === false).length;
  const filterOptions = [
    { key: "all" as const, label: "All", count: contributions.length },
    { key: "active" as const, label: "Active", count: activeCount },
    { key: "hidden" as const, label: "Hidden", count: hiddenCount },
  ];

  return (
    <ProtectedRoute>
      <div className="page-container py-10 sm:py-12">
        {/* Header */}
        <div className="flex flex-col gap-4 sm:flex-row sm:items-baseline sm:justify-between">
          <div>
            <h1 className="text-2xl font-semibold">My Contributions</h1>
            <p className="mt-1 text-[var(--text-muted)]">
              Manage your submissions — edit metadata, add remembered questions, or hide entries.
            </p>
          </div>
          <Link href="/submit" className="btn-primary text-sm shrink-0">
            New contribution
          </Link>
        </div>

        {/* Stats bar */}
        {!loading && contributions.length > 0 && (
          <div className="mt-6 rounded-xl border border-[var(--border)] bg-[var(--surface)] p-3 sm:p-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <p className="text-sm text-[var(--text-muted)]">
                {contributions.length} total · {activeCount} active · {hiddenCount} hidden
              </p>
              <p className="text-xs font-semibold uppercase tracking-[0.08em] text-[var(--text-muted)]">
                Filter visibility
              </p>
            </div>

            <div className="mt-3 rounded-xl border border-[var(--border)] bg-[var(--surface-muted)] p-1.5">
              <div className="flex flex-wrap gap-1.5 sm:justify-end">
              {filterOptions.map((option) => (
                <button
                  key={option.key}
                  onClick={() => setFilter(option.key)}
                  aria-pressed={filter === option.key}
                  className={`inline-flex h-9 min-w-[5.6rem] flex-1 items-center justify-center gap-1 rounded-lg border px-2.5 text-[0.82rem] font-semibold transition-colors sm:h-8 sm:flex-none sm:text-[0.8rem] ${
                    filter === option.key
                      ? "border-[var(--primary)] bg-[var(--surface)] text-[var(--primary)] shadow-[inset_0_0_0_1px_var(--primary-soft)]"
                      : "border-transparent bg-transparent text-[var(--text-secondary)] hover:border-[var(--border)] hover:bg-[var(--surface)]"
                  }`}
                >
                  <span>{option.label}</span>
                  <span
                    className={`inline-flex h-5 min-w-[1.2rem] items-center justify-center rounded-full px-1.5 text-[0.7rem] ${
                      filter === option.key
                        ? "bg-[var(--primary-soft)] text-[var(--primary)]"
                        : "bg-[var(--surface)] text-[var(--text-muted)]"
                    }`}
                  >
                    {option.count}
                  </span>
                </button>
              ))}
              </div>
            </div>
          </div>
        )}

        {/* Content */}
        <div className="mt-6">
          {loading ? (
            <div className="space-y-4">
              {[1, 2, 3].map((i) => (
                <div key={i} className="skeleton h-40 rounded-lg" />
              ))}
            </div>
          ) : error ? (
            <div className="card p-8 text-center max-w-md mx-auto">
              <svg className="h-10 w-10 text-[var(--text-muted)] mx-auto" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z" />
              </svg>
              <p className="mt-3 text-sm text-[var(--text-muted)]">{error}</p>
              <button className="btn-secondary mt-4" onClick={loadContributions}>
                Try again
              </button>
            </div>
          ) : contributions.length === 0 ? (
            <div className="card p-8 text-center max-w-md mx-auto">
              <h2 className="text-xl font-semibold">No contributions yet</h2>
              <p className="mt-2 text-[var(--text-muted)]">
                Share your interview experiences to build institutional knowledge for future batches.
              </p>
              <Link href="/submit" className="btn-primary mt-4 inline-block">
                Contribute an experience
              </Link>
            </div>
          ) : filtered.length === 0 ? (
            <div className="card p-6 text-center">
              <p className="text-[var(--text-muted)]">
                No {filter} contributions found.
              </p>
              <button
                className="btn-ghost text-sm mt-2"
                onClick={() => setFilter("all")}
              >
                Show all
              </button>
            </div>
          ) : (
            <div className="space-y-4">
              {filtered.map((experience) => (
                <ContributionCard
                  key={experience.id}
                  experience={experience}
                  onUpdate={handleUpdate}
                />
              ))}
            </div>
          )}
        </div>

        {/* Trust note */}
        {!loading && contributions.length > 0 && (
          <div className="mt-8 p-4 rounded-lg border border-[var(--border)] bg-[var(--surface)]">
            <p className="text-xs text-[var(--text-muted)] leading-relaxed">
              <strong className="text-[var(--text)]">Data integrity guarantee:</strong>{" "}
              Original AI-extracted narratives, questions, and summaries are permanently locked
              and cannot be edited or overwritten — this is by design, not a limitation.
              You may edit metadata (role, year, round, difficulty), add remembered questions
              (clearly tagged), or hide/unhide entries. Hidden contributions are removed from
              search and analytics but never permanently deleted. All changes are tracked with
              timestamps in the edit history.
            </p>
          </div>
        )}
      </div>
    </ProtectedRoute>
  );
}
