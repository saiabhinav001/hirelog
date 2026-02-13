"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { SaveToListButton } from "@/components/SaveToListButton";
import { apiFetch } from "@/lib/api";
import type { Experience, SearchResponse } from "@/lib/types";

function ResultSkeleton() {
  return (
    <div className="card p-5 animate-pulse">
      <div className="flex justify-between">
        <div className="skeleton h-6 w-48" />
        <div className="skeleton h-5 w-16" />
      </div>
      <div className="mt-3 skeleton h-4 w-32" />
      <div className="mt-4 skeleton h-16 w-full" />
      <div className="mt-4 flex gap-2">
        <div className="skeleton h-5 w-12" />
        <div className="skeleton h-5 w-14" />
        <div className="skeleton h-5 w-10" />
      </div>
    </div>
  );
}

// Fetch states: idle → loading → resolved | errored
type FetchStatus = "idle" | "loading" | "resolved" | "errored";

export default function ResultsPage() {
  const searchParams = useSearchParams();
  const [results, setResults] = useState<Experience[]>([]);
  const [status, setStatus] = useState<FetchStatus>("idle");
  const [error, setError] = useState<string | null>(null);

  // Abort controller to cancel stale requests
  const abortRef = useRef<AbortController | null>(null);

  // Derive a stable query string from URL params — each change triggers a fresh fetch
  const queryString = useMemo(() => searchParams.toString(), [searchParams]);

  const appliedFilters = useMemo(() => {
    const entries: [string, string | null][] = [
      ["Query", searchParams.get("q")],
      ["Company", searchParams.get("company")],
      ["Role", searchParams.get("role")],
      ["Year", searchParams.get("year")],
      ["Topic", searchParams.get("topic")],
      ["Difficulty", searchParams.get("difficulty")],
    ];
    return entries.filter(([, value]) => value) as [string, string][];
  }, [searchParams]);

  const mode = searchParams.get("mode") ?? "semantic";

  // Retry callback — re-runs the same search without page reload
  const runSearch = useCallback(async (qs: string) => {
    // Cancel any in-flight request
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    // Reset to loading — do NOT clear results yet (avoids flash)
    setStatus("loading");
    setError(null);

    try {
      const data = await apiFetch<SearchResponse>(
        `/api/search?${qs}`,
        { method: "GET", signal: controller.signal }
      );
      // Only apply if this is still the active request
      if (!controller.signal.aborted) {
        setResults(data.results ?? []);
        setStatus("resolved");
      }
    } catch (err: unknown) {
      if (err instanceof DOMException && err.name === "AbortError") return;
      if (!controller.signal.aborted) {
        setError("Couldn't load search results. Please try again.");
        setStatus("errored");
      }
    }
  }, []);

  // Stateless: every time URL params change, fire a fresh fetch
  useEffect(() => {
    runSearch(queryString);
    return () => abortRef.current?.abort();
  }, [queryString, runSearch]);

  // Derived booleans — no stale mix-ups
  const isLoading = status === "idle" || status === "loading";
  const isError = status === "errored";
  const isEmpty = status === "resolved" && results.length === 0;
  const hasResults = status === "resolved" && results.length > 0;

  return (
    <div className="page-container py-12">
      {/* Header */}
      <div className="flex items-baseline justify-between">
        <div>
          <h1 className="text-2xl font-semibold">Results</h1>
          <p className="mt-1 text-[var(--text-muted)]">
            {isLoading
              ? "Searching..."
              : `${results.length} experience${results.length !== 1 ? "s" : ""} found`}
          </p>
        </div>
        <Link href="/search" className="btn-ghost text-sm">
          ← Back to search
        </Link>
      </div>

      {/* Filters */}
      {appliedFilters.length > 0 && (
        <div className="mt-4 flex flex-wrap items-center gap-2">
          <span className="text-xs text-[var(--text-muted)]">{mode}</span>
          {appliedFilters.map(([label, value]) => (
            <span key={label} className="badge">
              {label}: {value}
            </span>
          ))}
        </div>
      )}

      {/* Content — strictly gated by fetch status */}
      <div className="mt-8 space-y-4">
        {isLoading ? (
          <>
            <ResultSkeleton />
            <ResultSkeleton />
            <ResultSkeleton />
          </>
        ) : isError ? (
          <div className="card p-8 text-center max-w-md mx-auto">
            <svg className="h-10 w-10 text-[var(--text-muted)] mx-auto" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z" />
            </svg>
            <p className="mt-3 text-sm text-[var(--text-muted)]">{error}</p>
            <button className="btn-secondary mt-4" onClick={() => runSearch(queryString)}>
              Try again
            </button>
          </div>
        ) : isEmpty ? (
          <div className="card p-8 text-center">
            <h2 className="text-lg font-semibold">No results</h2>
            <p className="mt-2 text-[var(--text-muted)]">
              Try different filters or search terms.
            </p>
            <Link href="/search" className="btn-primary mt-4">
              Back to search
            </Link>
          </div>
        ) : hasResults ? (
          results.map((item, index) => (
            <div key={`${item.id}-${index}`} className="card p-5">
              {/* Header */}
              <div className="flex items-start justify-between gap-4">
                <div>
                  <h2 className="text-lg font-semibold">
                    {item.company}
                    <span className="mx-2 text-[var(--text-muted)]">·</span>
                    <span className="font-normal text-[var(--text-secondary)]">{item.role}</span>
                  </h2>
                  <p className="mt-1 text-sm text-[var(--text-muted)]">
                    {item.year} · {item.round}
                    {item.contributor_display && (
                      <span className="ml-2">· by {item.contributor_display}</span>
                    )}
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  <span className={`badge ${
                    item.difficulty === "Easy" ? "badge-success" :
                    item.difficulty === "Hard" ? "badge-error" : "badge-warning"
                  }`}>
                    {item.difficulty}
                  </span>
                  {item.score !== undefined && (
                    <span className="text-xs text-[var(--text-muted)]">
                      {(item.score * 100).toFixed(0)}%
                    </span>
                  )}
                </div>
              </div>

              {/* Summary */}
              <p className="mt-3 text-sm text-[var(--text-secondary)] leading-relaxed">
                {item.summary || "AI summary is being generated..."}
              </p>

              {/* NLP status */}
              {item.nlp_status === "pending" && (
                <p className="mt-2 text-xs text-yellow-400 flex items-center gap-1.5">
                  <span className="inline-block h-1.5 w-1.5 rounded-full bg-yellow-400 animate-pulse" />
                  AI enrichment in progress — additional questions and topics will appear shortly.
                </p>
              )}

              {/* Match explanation */}
              {item.match_reason && (
                <p className="mt-2 text-xs text-[var(--text-muted)] italic">
                  {item.match_reason}
                </p>
              )}

              {/* Topics */}
              {item.topics?.length > 0 && (
                <div className="mt-3 flex flex-wrap gap-1.5">
                  {item.topics.map((topic) => (
                    <span key={topic} className="badge">{topic}</span>
                  ))}
                </div>
              )}

              {/* Questions */}
              {item.extracted_questions?.length > 0 && (
                <div className="mt-4 rounded-lg bg-[var(--surface-muted)] p-4">
                  <p className="text-xs font-medium text-[var(--text-muted)] mb-2">
                    {(() => {
                      const up = item.stats?.user_question_count ?? item.questions?.user_provided?.length ?? 0;
                      const ae = item.stats?.extracted_question_count ?? item.questions?.ai_extracted?.length ?? 0;
                      const total = up + ae || item.extracted_questions.filter((q) => (q.confidence ?? 1) >= 0.5).length;
                      if (up > 0) {
                        return `${up} user-provided${ae > 0 ? ` · ${ae} AI-extracted` : ""} · ${total} total`;
                      }
                      return ae > 0 ? `${ae} AI-extracted · ${total} total` : `Questions (${total})`;
                    })()}
                  </p>
                  <ul className="space-y-2 text-sm text-[var(--text-secondary)]">
                    {item.extracted_questions
                      .filter((q) => q.source === "user" || (q.confidence ?? 1) >= 0.5)
                      .slice(0, 10)
                      .map((q, qIndex) => (
                      <li key={`${item.id}-q-${qIndex}`} className="flex items-start justify-between gap-2">
                        <div className="flex items-start gap-2 flex-1 min-w-0">
                          <span>• {q.question_text || q.question}</span>
                          {q.source === "user" && (
                            <span className="shrink-0 text-[10px] px-1.5 py-0.5 rounded-full bg-blue-500/10 text-blue-400 font-medium">
                              user
                            </span>
                          )}
                          {q.added_later && (
                            <span className="shrink-0 text-[10px] px-1.5 py-0.5 rounded-full bg-green-500/10 text-green-400 font-medium">
                              added
                            </span>
                          )}
                          {q.topic && q.topic !== "General" && (
                            <span className="shrink-0 badge text-[10px] px-1.5 py-0.5">{q.topic}</span>
                          )}
                        </div>
                        <SaveToListButton
                          questionText={q.question_text || q.question}
                          topic={q.topic || item.topics?.[0] || "General"}
                          difficulty={item.difficulty}
                          sourceExperienceId={item.id}
                          sourceCompany={item.company}
                        />
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          ))
        ) : null}
      </div>
    </div>
  );
}
