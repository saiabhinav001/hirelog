"use client";

import Link from "next/link";
import dynamic from "next/dynamic";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";

// Lazy-load SaveToListButton — only needed when results are displayed
const SaveToListButton = dynamic(
  () => import("@/components/SaveToListButton").then((m) => m.SaveToListButton),
  { ssr: false }
);
import { FadeIn, StaggerContainer, StaggerItem } from "@/components/Motion";
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

// ─────────────────────────────────────────────────────────────────────────────
// sessionStorage SWR cache for search results
// ─────────────────────────────────────────────────────────────────────────────

const SEARCH_CACHE_PREFIX = "hirelog_search_";
const SEARCH_CACHE_TTL = 3 * 60 * 1000; // 3 minutes

type SearchMeta = {
  totalCount: number;
  returnedCount: number;
  hasMore: boolean;
  nextCursor: string | null;
  servedMode: string | null;
  servedEngine: string | null;
};

const EMPTY_META: SearchMeta = {
  totalCount: 0,
  returnedCount: 0,
  hasMore: false,
  nextCursor: null,
  servedMode: null,
  servedEngine: null,
};

type SearchCachePayload = {
  results: Experience[];
  meta: SearchMeta;
};

function normalizeSearchMeta(payload: Partial<SearchResponse> | null | undefined): SearchMeta {
  const totalCount = Number.isFinite(Number(payload?.total_count))
    ? Number(payload?.total_count)
    : Number(payload?.total ?? 0);
  const returnedCount = Number.isFinite(Number(payload?.returned_count))
    ? Number(payload?.returned_count)
    : Array.isArray(payload?.results)
      ? payload.results.length
      : 0;
  const nextCursor =
    typeof payload?.next_cursor === "string" && payload.next_cursor.length > 0
      ? payload.next_cursor
      : null;
  const hasMore =
    typeof payload?.has_more === "boolean"
      ? payload.has_more
      : (nextCursor !== null || (totalCount > returnedCount && returnedCount > 0));
  const servedMode =
    typeof payload?.served_mode === "string" && payload.served_mode.length > 0
      ? payload.served_mode
      : null;
  const servedEngine =
    typeof payload?.served_engine === "string" && payload.served_engine.length > 0
      ? payload.served_engine
      : null;

  return {
    totalCount,
    returnedCount,
    hasMore,
    nextCursor,
    servedMode,
    servedEngine,
  };
}

function getSearchCache(qs: string): SearchCachePayload | null {
  try {
    const raw = sessionStorage.getItem(SEARCH_CACHE_PREFIX + qs);
    if (!raw) return null;
    const { ts, data } = JSON.parse(raw);
    if (Date.now() - ts > SEARCH_CACHE_TTL) {
      sessionStorage.removeItem(SEARCH_CACHE_PREFIX + qs);
      return null;
    }

    // Backward compatibility: older cache entries stored an array only.
    if (Array.isArray(data)) {
      return {
        results: data,
        meta: {
          totalCount: data.length,
          returnedCount: data.length,
          hasMore: false,
          nextCursor: null,
          servedMode: null,
          servedEngine: null,
        },
      };
    }

    if (!data || !Array.isArray(data.results)) {
      return null;
    }

    return {
      results: data.results,
      meta: data.meta ? data.meta : normalizeSearchMeta(data),
    };
  } catch {
    return null;
  }
}

function setSearchCache(qs: string, data: SearchCachePayload) {
  try {
    sessionStorage.setItem(
      SEARCH_CACHE_PREFIX + qs,
      JSON.stringify({ ts: Date.now(), data }),
    );
  } catch { /* quota — ignore */ }
}

// Fetch states: idle → loading → resolved | errored
type FetchStatus = "idle" | "loading" | "resolved" | "errored";

function ResultsPageContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [results, setResults] = useState<Experience[]>([]);
  const [searchMeta, setSearchMeta] = useState<SearchMeta>(EMPTY_META);
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

  const offset = useMemo(() => {
    const raw = searchParams.get("cursor");
    const parsed = Number(raw ?? "0");
    if (!Number.isFinite(parsed) || parsed < 0) {
      return 0;
    }
    return Math.floor(parsed);
  }, [searchParams]);

  const pageSize = useMemo(() => {
    const raw = Number(searchParams.get("limit") ?? "20");
    if (!Number.isFinite(raw) || raw <= 0) {
      return 20;
    }
    return Math.floor(raw);
  }, [searchParams]);

  // Retry callback — re-runs the same search without page reload
  const runSearch = useCallback(async (qs: string) => {
    // Cancel any in-flight request
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    // Restore cached results for instant paint (SWR)
    const cached = getSearchCache(qs);
    if (cached) {
      setResults(cached.results);
      setSearchMeta(cached.meta);
      setStatus("resolved");
    } else {
      // No cache — show loading skeleton
      setStatus("loading");
    }
    setError(null);

    try {
      const data = await apiFetch<SearchResponse>(
        `/api/search?${qs}`,
        { method: "GET", signal: controller.signal }
      );
      // Only apply if this is still the active request
      if (!controller.signal.aborted) {
        const normalized = normalizeSearchMeta(data);
        const payload: SearchCachePayload = {
          results: data.results ?? [],
          meta: normalized,
        };
        setResults(payload.results);
        setSearchMeta(payload.meta);
        setStatus("resolved");
        setSearchCache(qs, payload);
      }
    } catch (err: unknown) {
      if (err instanceof DOMException && err.name === "AbortError") return;
      if (!controller.signal.aborted) {
        // If we had cached results, keep showing them
        if (!cached) {
          setError("Couldn't load search results. Please try again.");
          setStatus("errored");
        }
      }
    }
  }, []);

  // Stateless: every time URL params change, fire a fresh fetch
  useEffect(() => {
    const doSearch = () => { runSearch(queryString); };
    doSearch();
    return () => abortRef.current?.abort();
  }, [queryString, runSearch]);

  // Derived booleans — no stale mix-ups
  const isLoading = status === "idle" || status === "loading";
  const isError = status === "errored";
  const isEmpty = status === "resolved" && results.length === 0;
  const hasResults = status === "resolved" && results.length > 0;
  const rangeStart = hasResults ? offset + 1 : 0;
  const rangeEnd = hasResults ? Math.min(offset + results.length, searchMeta.totalCount) : 0;
  const canGoPrev = offset > 0;
  const canGoNext = Boolean(searchMeta.hasMore && searchMeta.nextCursor);

  const pushWithCursor = useCallback((nextCursor: string | null) => {
    const params = new URLSearchParams(searchParams.toString());
    if (nextCursor && nextCursor !== "0") {
      params.set("cursor", nextCursor);
    } else {
      params.delete("cursor");
    }
    router.push(`/results?${params.toString()}`);
  }, [router, searchParams]);

  const handlePrevPage = useCallback(() => {
    if (!canGoPrev) {
      return;
    }
    const previous = Math.max(offset - pageSize, 0);
    pushWithCursor(previous > 0 ? String(previous) : null);
  }, [canGoPrev, offset, pageSize, pushWithCursor]);

  const handleNextPage = useCallback(() => {
    if (!canGoNext || !searchMeta.nextCursor) {
      return;
    }
    pushWithCursor(searchMeta.nextCursor);
  }, [canGoNext, searchMeta.nextCursor, pushWithCursor]);

  return (
    <div className="page-container py-10 sm:py-12">
      {/* Header */}
      <FadeIn>
      <div className="flex flex-col gap-3 sm:flex-row sm:items-baseline sm:justify-between">
        <div>
          <h1 className="text-2xl sm:text-3xl font-semibold">Results</h1>
          <p className="mt-1 text-[var(--text-muted)] num-tabular" aria-live="polite">
            {isLoading
              ? "Searching..."
              : `${searchMeta.totalCount} experience${searchMeta.totalCount !== 1 ? "s" : ""} found`}
          </p>
          {!isLoading && searchMeta.servedMode && (
            <p className="mt-1 text-xs text-[var(--text-muted)]">
              Served via {searchMeta.servedMode}
              {searchMeta.servedEngine ? ` on ${searchMeta.servedEngine}` : ""}
            </p>
          )}
          {hasResults && (
            <p className="mt-1 text-xs text-[var(--text-muted)] num-tabular">
              Showing {rangeStart}-{rangeEnd}
            </p>
          )}
        </div>
        <Link href="/search" className="btn-ghost text-sm self-start sm:self-auto">
          ← Back to search
        </Link>
      </div>
      </FadeIn>

      {/* Filters */}
      {appliedFilters.length > 0 && (
        <div className="mt-4 flex flex-wrap items-center gap-2">
          <span className="text-xs text-[var(--text-muted)]">intelligent mode</span>
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
          <>
          <StaggerContainer className="space-y-4">
          {results.map((item, index) => (
            <StaggerItem key={`${item.id}-${index}`}>
            <div className="card p-5">
              {/* Header */}
              <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
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
                  {item.nlp_status && (
                    <span className="badge">
                      AI {item.nlp_status}
                    </span>
                  )}
                  {item.score !== undefined && (
                    <span className="text-xs text-[var(--text-muted)] num-tabular">
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
                <p className="mt-2 text-xs text-[var(--warning)] flex items-center gap-1.5">
                  <span className="inline-block h-1.5 w-1.5 rounded-full bg-[var(--warning)] animate-pulse" />
                  AI enrichment in progress — additional questions and topics will appear shortly.
                </p>
              )}

              {/* Match explanation */}
              {item.match_reason && (
                <div className="mt-3 rounded-md border-l-2 border-[var(--primary)] bg-[var(--primary-soft)] px-3 py-2">
                  <p className="text-xs text-[var(--text-secondary)] leading-relaxed">
                    {item.match_reason}
                  </p>
                </div>
              )}

              {/* Contact */}
              {!item.is_anonymous && item.allow_contact && (item.contact_linkedin || item.contact_email) && (
                <div className="mt-3 flex items-center gap-3">
                  {item.contact_linkedin && (
                    <a
                      href={item.contact_linkedin.startsWith("http") ? item.contact_linkedin : `https://${item.contact_linkedin}`}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex items-center gap-1.5 text-xs text-[var(--primary)] hover:underline"
                    >
                      <svg className="h-3.5 w-3.5" viewBox="0 0 24 24" fill="currentColor"><path d="M20.447 20.452h-3.554v-5.569c0-1.328-.027-3.037-1.852-3.037-1.853 0-2.136 1.445-2.136 2.939v5.667H9.351V9h3.414v1.561h.046c.477-.9 1.637-1.85 3.37-1.85 3.601 0 4.267 2.37 4.267 5.455v6.286zM5.337 7.433a2.062 2.062 0 01-2.063-2.065 2.064 2.064 0 112.063 2.065zm1.782 13.019H3.555V9h3.564v11.452zM22.225 0H1.771C.792 0 0 .774 0 1.729v20.542C0 23.227.792 24 1.771 24h20.451C23.2 24 24 23.227 24 22.271V1.729C24 .774 23.2 0 22.222 0h.003z"/></svg>
                      LinkedIn
                    </a>
                  )}
                  {item.contact_email && (
                    <a
                      href={`mailto:${item.contact_email}`}
                      className="inline-flex items-center gap-1.5 text-xs text-[var(--primary)] hover:underline"
                    >
                      <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}><path strokeLinecap="round" strokeLinejoin="round" d="M21.75 6.75v10.5a2.25 2.25 0 01-2.25 2.25h-15a2.25 2.25 0 01-2.25-2.25V6.75m19.5 0A2.25 2.25 0 0019.5 4.5h-15a2.25 2.25 0 00-2.25 2.25m19.5 0v.243a2.25 2.25 0 01-1.07 1.916l-7.5 4.615a2.25 2.25 0 01-2.36 0L3.32 8.91a2.25 2.25 0 01-1.07-1.916V6.75" /></svg>
                      Email
                    </a>
                  )}
                </div>
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
                            <span className="shrink-0 badge badge-primary text-xs px-1.5 py-0.5 font-medium">
                              user
                            </span>
                          )}
                          {q.source !== "user" && (
                            <span className="shrink-0 badge text-xs px-1.5 py-0.5 font-medium">
                              ai
                            </span>
                          )}
                          {q.added_later && (
                            <span className="shrink-0 badge badge-success text-xs px-1.5 py-0.5 font-medium">
                              added
                            </span>
                          )}
                          {q.source !== "user" && typeof q.confidence === "number" && (
                            <span className="shrink-0 badge text-xs px-1.5 py-0.5 num-tabular">
                              confidence {(q.confidence * 100).toFixed(0)}%
                            </span>
                          )}
                          {q.topic && q.topic !== "General" && (
                            <span className="shrink-0 badge text-xs px-1.5 py-0.5">{q.topic}</span>
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
            </StaggerItem>
          ))}
          </StaggerContainer>
          <div className="flex flex-col gap-3 pt-2 sm:flex-row sm:items-center sm:justify-between">
            <p className="text-xs text-[var(--text-muted)] num-tabular">
              Showing {rangeStart}-{rangeEnd} of {searchMeta.totalCount}
            </p>
            <div className="flex items-center gap-2">
              <button
                type="button"
                className="btn-secondary"
                onClick={handlePrevPage}
                disabled={!canGoPrev}
                aria-label="Go to previous results page"
              >
                Previous
              </button>
              <button
                type="button"
                className="btn-secondary"
                onClick={handleNextPage}
                disabled={!canGoNext}
                aria-label="Go to next results page"
              >
                Next
              </button>
            </div>
          </div>
          </>
        ) : null}
      </div>
    </div>
  );
}

export default function ResultsPage() {
  return (
    <Suspense
      fallback={
        <div className="page-container py-10 sm:py-12">
          <div>
            <h1 className="text-2xl font-semibold">Results</h1>
            <p className="mt-1 text-[var(--text-muted)]">Searching...</p>
          </div>
          <div className="mt-8 space-y-4">
            <ResultSkeleton />
            <ResultSkeleton />
            <ResultSkeleton />
          </div>
        </div>
      }
    >
      <ResultsPageContent />
    </Suspense>
  );
}
