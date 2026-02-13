"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";

import { LoadingState } from "@/components/States";
import { useAuth } from "@/context/AuthContext";
import { useToast } from "@/context/ToastContext";
import { apiFetch } from "@/lib/api";
import { auth } from "@/lib/firebase";

export default function SubmitPage() {
  const router = useRouter();
  const { user, profile, loading, refreshProfile } = useAuth();
  const { toast } = useToast();
  const [company, setCompany] = useState("");
  const [role, setRole] = useState("");
  const [year, setYear] = useState(new Date().getFullYear());
  const [round, setRound] = useState("");
  const [difficulty, setDifficulty] = useState("Medium");
  const [rawText, setRawText] = useState("");
  const [isAnonymous, setIsAnonymous] = useState(false);
  const [userQuestions, setUserQuestions] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!loading && !user) {
      router.replace("/login");
    }
  }, [loading, router, user]);

  const parsedQuestions = userQuestions
    .split("\n")
    .map((l) => l.trim())
    .filter((l) => l.length > 0);

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!auth.currentUser) return;
    setSubmitting(true);
    setError(null);
    try {
      const token = await auth.currentUser.getIdToken();
      await apiFetch(
        "/api/experiences",
        {
          method: "POST",
          body: JSON.stringify({
            company,
            role,
            year,
            round,
            difficulty,
            raw_text: rawText,
            is_anonymous: isAnonymous,
            show_name: !isAnonymous,
            user_questions: parsedQuestions,
          }),
        },
        token
      );
      // Non-blocking profile refresh (picks up viewer → contributor upgrade)
      refreshProfile().catch(() => {});
      toast(
        "Saved \u2014 thank you! Extraction & classification are running in the background.",
        "success",
        { label: "View contributions →", href: "/contributions" }
      );
      // Navigate to contributions so the user sees their entry immediately
      router.push("/contributions");
    } catch {
      setError("Something went wrong submitting your experience. Please try again.");
    } finally {
      setSubmitting(false);
    }
  };

  if (loading || !user) {
    return <LoadingState />;
  }

  return (
    <div className="page-container py-12">
      <div className="max-w-2xl">
        <h1 className="text-2xl font-semibold">Contribute to the Archive</h1>
        <p className="mt-2 text-[var(--text-muted)]">
          Share your interview experience to build institutional knowledge for future batches.
          AI will automatically extract additional questions, classify topics, and make it discoverable in the background.
        </p>
      </div>

      <form onSubmit={handleSubmit} className="mt-8 max-w-2xl space-y-6">
        <div className="grid gap-4 sm:grid-cols-2">
          <div>
            <label className="label">Company</label>
            <input
              className="input-field"
              value={company}
              onChange={(e) => setCompany(e.target.value)}
              required
              placeholder="e.g. Google"
            />
          </div>
          <div>
            <label className="label">Role</label>
            <input
              className="input-field"
              value={role}
              onChange={(e) => setRole(e.target.value)}
              required
              placeholder="e.g. SDE Intern"
            />
          </div>
        </div>

        <div className="grid gap-4 sm:grid-cols-3">
          <div>
            <label className="label">Year</label>
            <input
              className="input-field"
              type="number"
              value={year}
              onChange={(e) => setYear(Number(e.target.value))}
              required
            />
          </div>
          <div>
            <label className="label">Round</label>
            <input
              className="input-field"
              value={round}
              onChange={(e) => setRound(e.target.value)}
              required
              placeholder="e.g. Technical Round 1"
            />
          </div>
          <div>
            <label className="label">Difficulty</label>
            <select
              className="input-field"
              value={difficulty}
              onChange={(e) => setDifficulty(e.target.value)}
            >
              {["Easy", "Medium", "Hard"].map((opt) => (
                <option key={opt} value={opt}>{opt}</option>
              ))}
            </select>
          </div>
        </div>

        <div>
          <label className="label">Experience</label>
          <textarea
            className="input-field min-h-[160px]"
            value={rawText}
            onChange={(e) => setRawText(e.target.value)}
            required
            placeholder="Describe your interview experience..."
          />
          <p className="mt-2 text-xs text-[var(--text-muted)]">
            Include topics covered, round structure, and your overall experience.
            AI will analyze this text in the background to extract additional questions and classify topics.
          </p>
        </div>

        {/* User-provided questions */}
        <div>
          <label className="label">
            Questions Asked{" "}
            <span className="font-normal text-[var(--text-muted)]">(recommended)</span>
          </label>
          <textarea
            className="input-field min-h-[120px]"
            value={userQuestions}
            onChange={(e) => setUserQuestions(e.target.value)}
            placeholder={"What is the time complexity of quicksort?\nExplain ACID properties in DBMS.\nHow does virtual memory work?"}
          />
          <div className="mt-2 flex items-center justify-between">
            <p className="text-xs text-[var(--text-muted)]">
              One question per line. These are saved exactly as you type them — never filtered or modified.
            </p>
            {parsedQuestions.length > 0 && (
              <span className="text-xs font-medium text-[var(--primary)]">
                {parsedQuestions.length} question{parsedQuestions.length !== 1 ? "s" : ""}
              </span>
            )}
          </div>
        </div>

        {/* Identity preview — read-only, no name editing here */}
        <div className="p-4 rounded-lg bg-[var(--surface)] border border-[var(--border)] space-y-3">
          <div className="flex items-center justify-between">
            <p className="text-sm font-medium">Contribution Identity</p>
            <Link href="/profile" className="text-[10px] text-[var(--primary)] hover:underline">
              Edit name in Profile →
            </Link>
          </div>

          {/* Anonymous toggle */}
          <div className="flex items-start gap-3">
            <input
              type="checkbox"
              id="anonymous-toggle"
              checked={isAnonymous}
              onChange={(e) => setIsAnonymous(e.target.checked)}
              className="mt-0.5 h-4 w-4 rounded border-[var(--border)] text-[var(--primary)] focus:ring-[var(--primary)]"
            />
            <label htmlFor="anonymous-toggle" className="text-sm cursor-pointer">
              Submit anonymously
            </label>
          </div>

          {/* Identity preview */}
          <div className={`rounded-md px-3 py-2 text-xs leading-relaxed ${
            isAnonymous
              ? "bg-yellow-500/10 border border-yellow-500/20 text-yellow-300"
              : "bg-blue-500/10 border border-blue-500/20 text-blue-300"
          }`}>
            {isAnonymous ? (
              <>
                This submission will appear as <strong>Anonymous (CBIT)</strong>.
                This cannot be changed later.
              </>
            ) : (
              <>
                This submission will appear as{" "}
                <strong>{profile?.display_name || profile?.name || "your display name"}</strong>.
              </>
            )}
          </div>
        </div>
        {!isAnonymous && (
          <p className="text-[10px] text-[var(--text-muted)] -mt-4">
            Your public display name is set in your profile. Future name changes do not update past submissions.
          </p>
        )}

        {/* Performance note */}
        <div className="rounded-md bg-blue-500/10 border border-blue-500/20 px-3 py-2">
          <p className="text-[11px] text-blue-300 leading-relaxed">
            <strong>⚡ Instant save.</strong>{" "}
            Your experience and questions are saved immediately.
            AI extraction, topic classification, and summary generation run in the background —
            your submission is never blocked by processing.
          </p>
        </div>

        {error && (
          <div className="rounded-lg bg-[var(--error-soft)] px-4 py-3 text-sm text-[var(--error)]">
            {error}
          </div>
        )}

        <button className="btn-primary" disabled={submitting}>
          {submitting ? "Submitting..." : "Submit"}
        </button>
      </form>
    </div>
  );
}
