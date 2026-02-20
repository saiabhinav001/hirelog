"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import dynamic from "next/dynamic";

import { ProtectedRoute } from "@/components/ProtectedRoute";
// Lazy-load SaveToListButton — only needed when questions panel is displayed
const SaveToListButton = dynamic(
  () => import("@/components/SaveToListButton").then((m) => m.SaveToListButton),
  { ssr: false }
);
import { useAuth } from "@/context/AuthContext";
import { apiFetch } from "@/lib/api";
import { auth } from "@/lib/firebase";

// ─────────────────────────────────────────────────────────────────────────────
// Types for tiered responses
// ─────────────────────────────────────────────────────────────────────────────

interface StatsResponse {
  total_experiences: number;
  top_company: string | null;
  top_topic: string | null;
  contribution_impact: {
    experiences_submitted: number;
    questions_extracted: number;
    archive_size: number;
  };
}

interface ChartsResponse {
  topic_totals: Record<string, number>;
  difficulty_distribution: Record<string, number>;
  company_topic_counts: Record<string, Record<string, number>>;
  insights: string[];
}

interface QuestionsResponse {
  frequent_questions: Record<string, number>;
}

interface FlowsResponse {
  interview_progression: Record<string, {
    stages: Record<string, { topics: string[]; frequency: number }>;
    total_experiences: number;
  }>;
}

// ─────────────────────────────────────────────────────────────────────────────
// localStorage stale-while-revalidate cache
// ─────────────────────────────────────────────────────────────────────────────

const DASH_CACHE_KEY = "hirelog_dashboard_cache";
const DASH_CACHE_TTL = 5 * 60 * 1000; // 5 minutes

interface DashboardCache {
  ts: number;
  stats?: StatsResponse;
  charts?: ChartsResponse;
  questions?: QuestionsResponse;
  flows?: FlowsResponse;
}

function getDashboardCache(): DashboardCache | null {
  try {
    const raw = localStorage.getItem(DASH_CACHE_KEY);
    if (!raw) return null;
    const cache: DashboardCache = JSON.parse(raw);
    if (Date.now() - cache.ts > DASH_CACHE_TTL) {
      localStorage.removeItem(DASH_CACHE_KEY);
      return null;
    }
    return cache;
  } catch {
    return null;
  }
}

function updateDashboardCache(updates: Partial<Omit<DashboardCache, "ts">>) {
  try {
    const existing = getDashboardCache() ?? { ts: 0 };
    const merged = { ...existing, ...updates, ts: Date.now() };
    localStorage.setItem(DASH_CACHE_KEY, JSON.stringify(merged));
  } catch {
    /* quota exceeded or private browsing — ignore */
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Sub-components
// ─────────────────────────────────────────────────────────────────────────────

function BarChart({ data }: { data: Record<string, number> }) {
  const entries = Object.entries(data).sort((a, b) => b[1] - a[1]);
  const max = entries[0]?.[1] ?? 1;

  return (
    <div className="space-y-2">
      {entries.map(([label, value]) => (
        <div key={label} className="flex items-center gap-3">
          <span className="w-20 text-sm text-[var(--text-secondary)] truncate">{label}</span>
          <div className="flex-1 h-5 bg-[var(--surface-muted)] rounded overflow-hidden">
            <div
              className="h-full bg-[var(--primary)] rounded"
              style={{ width: `${Math.round((value / max) * 100)}%` }}
            />
          </div>
          <span className="w-8 text-right text-sm text-[var(--text-muted)]">{value}</span>
        </div>
      ))}
    </div>
  );
}

function SectionSkeleton({ height = "h-48" }: { height?: string }) {
  return <div className={`skeleton ${height} rounded-lg`} />;
}

// Lazy mount with IntersectionObserver — only renders children when visible
function LazySection({ children, fallback }: { children: React.ReactNode; fallback?: React.ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          setVisible(true);
          observer.disconnect();
        }
      },
      { rootMargin: "200px" }
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  return <div ref={ref}>{visible ? children : (fallback ?? <SectionSkeleton />)}</div>;
}

function InfoTooltip({ text }: { text: string }) {
  return (
    <span className="relative group/tip inline-flex items-center ml-0.5 cursor-help">
      <svg className="h-3 w-3 text-[var(--text-muted)] opacity-60 group-hover/tip:opacity-100 transition-opacity" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
        <path strokeLinecap="round" strokeLinejoin="round" d="M13 16h-1v-4h-1m1-4h.01M12 2a10 10 0 100 20 10 10 0 000-20z" />
      </svg>
      <span className="pointer-events-none absolute bottom-full left-1/2 -translate-x-1/2 mb-1.5 w-max max-w-[200px] rounded bg-[var(--surface-elevated,#1e1e1e)] px-2.5 py-1.5 text-[10px] leading-snug text-[var(--text-primary)] shadow-lg border border-[var(--border)] opacity-0 group-hover/tip:opacity-100 transition-opacity z-50 text-center">
        {text}
      </span>
    </span>
  );
}

function StatsSkeleton() {
  return (
    <div className="grid gap-3 sm:grid-cols-3">
      {[1, 2, 3].map((i) => (
        <div key={i} className="skeleton skeleton-card" />
      ))}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// CollapsibleSection for interview flows (collapsible as requested)
// ─────────────────────────────────────────────────────────────────────────────

function CollapsibleSection({
  title,
  children,
  defaultOpen = false,
}: {
  title: string;
  children: React.ReactNode;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);

  return (
    <div className="mt-8">
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center gap-2 font-medium mb-4 text-left hover:text-[var(--primary)] transition-colors"
      >
        <svg
          className={`h-4 w-4 transition-transform ${open ? "rotate-90" : ""}`}
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          strokeWidth={2}
        >
          <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
        </svg>
        {title}
      </button>
      {open && children}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Company breakdown — top 4 shown initially, "Show more" reveals rest
// ─────────────────────────────────────────────────────────────────────────────

function CompanyBreakdownSection({
  chartsLoading,
  companyTopicCounts,
}: {
  chartsLoading: boolean;
  companyTopicCounts?: Record<string, Record<string, number>>;
}) {
  const DEFAULT_VISIBLE = 4;
  const [showAll, setShowAll] = useState(false);

  const entries = companyTopicCounts ? Object.entries(companyTopicCounts) : [];
  const visible = showAll ? entries : entries.slice(0, DEFAULT_VISIBLE);
  const hasMore = entries.length > DEFAULT_VISIBLE;

  return (
    <CollapsibleSection title="By company" defaultOpen={false}>
      {chartsLoading ? (
        <SectionSkeleton height="h-48" />
      ) : entries.length > 0 ? (
        <>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {visible.map(([company, topics]) => (
              <div key={company} className="card p-4">
                <p className="font-medium">{company}</p>
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {Object.entries(topics).map(([topic, count]) => (
                    <span
                      key={`${company}-${topic}`}
                      className="badge relative group/badge cursor-default"
                    >
                      {topic} • {count}×
                      <span className="pointer-events-none absolute bottom-full left-1/2 -translate-x-1/2 mb-1.5 w-max max-w-[200px] rounded bg-[var(--surface-elevated,#1e1e1e)] px-2.5 py-1.5 text-[10px] leading-snug text-[var(--text)] shadow-lg border border-[var(--border)] opacity-0 group-hover/badge:opacity-100 transition-opacity z-50 text-center">
                        Frequency: number of experiences where this topic was present for this company/round.
                      </span>
                    </span>
                  ))}
                </div>
              </div>
            ))}
          </div>
          {hasMore && !showAll && (
            <button
              onClick={() => setShowAll(true)}
              className="mt-4 btn-ghost text-sm mx-auto block"
            >
              Show {entries.length - DEFAULT_VISIBLE} more companies
            </button>
          )}
        </>
      ) : (
        <p className="text-sm text-[var(--text-muted)] py-4 text-center">No company data</p>
      )}
    </CollapsibleSection>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Main Component
// ─────────────────────────────────────────────────────────────────────────────

export default function DashboardPage() {
  const { user, loading: authLoading } = useAuth();

  // Tier 1: Instant stats
  const [stats, setStats] = useState<StatsResponse | null>(null);
  const [statsLoading, setStatsLoading] = useState(true);
  const [statsError, setStatsError] = useState<string | null>(null);

  // Tier 2: Charts data
  const [charts, setCharts] = useState<ChartsResponse | null>(null);
  const [chartsLoading, setChartsLoading] = useState(true);

  // Tier 2: Frequent questions
  const [questions, setQuestions] = useState<QuestionsResponse | null>(null);
  const [questionsLoading, setQuestionsLoading] = useState(true);

  // Tier 2: Interview flows
  const [flows, setFlows] = useState<FlowsResponse | null>(null);
  const [flowsLoading, setFlowsLoading] = useState(true);

  // Track whether the initial parallel fetch was triggered
  const fetchedRef = useRef(false);

  // Retry handler for error state (stats only)
  const retryStats = useCallback(async () => {
    if (!auth.currentUser) return;
    setStatsLoading(true);
    setStatsError(null);
    try {
      const token = await auth.currentUser.getIdToken();
      const response = await apiFetch<StatsResponse>(
        "/api/dashboard/stats",
        { method: "GET" },
        token,
      );
      setStats(response);
      updateDashboardCache({ stats: response });
    } catch {
      setStatsError("Couldn't load analytics. Please try again.");
    } finally {
      setStatsLoading(false);
    }
  }, []);

  // Single effect: restore cache → fire all 4 API calls in parallel
  useEffect(() => {
    if (authLoading) return;
    if (!user || !auth.currentUser) {
      setStatsLoading(false);
      return;
    }
    if (fetchedRef.current) return;
    fetchedRef.current = true;

    // 1. Restore stale data from localStorage immediately (instant paint)
    const cached = getDashboardCache();
    if (cached?.stats) { setStats(cached.stats); setStatsLoading(false); }
    if (cached?.charts) { setCharts(cached.charts); setChartsLoading(false); }
    if (cached?.questions) { setQuestions(cached.questions); setQuestionsLoading(false); }
    if (cached?.flows) { setFlows(cached.flows); setFlowsLoading(false); }

    // 2. Fire all 4 requests in parallel (revalidate in background)
    const loadAll = async () => {
      const token = await auth.currentUser!.getIdToken();

      const statsP = apiFetch<StatsResponse>("/api/dashboard/stats", { method: "GET" }, token)
        .then((data) => {
          setStats(data);
          setStatsError(null);
          updateDashboardCache({ stats: data });
        })
        .catch(() => {
          if (!cached?.stats) setStatsError("Couldn't load analytics. Please try again.");
        })
        .finally(() => setStatsLoading(false));

      const chartsP = apiFetch<ChartsResponse>("/api/dashboard/charts", { method: "GET" }, token)
        .then((data) => {
          setCharts(data);
          updateDashboardCache({ charts: data });
        })
        .catch(() => {
          if (!cached?.charts) setCharts({ topic_totals: {}, difficulty_distribution: {}, company_topic_counts: {}, insights: [] });
        })
        .finally(() => setChartsLoading(false));

      const questionsP = apiFetch<QuestionsResponse>("/api/dashboard/questions?limit=5", { method: "GET" }, token)
        .then((data) => {
          setQuestions(data);
          updateDashboardCache({ questions: data });
        })
        .catch(() => {
          if (!cached?.questions) setQuestions({ frequent_questions: {} });
        })
        .finally(() => setQuestionsLoading(false));

      const flowsP = apiFetch<FlowsResponse>("/api/dashboard/flows?limit=4", { method: "GET" }, token)
        .then((data) => {
          setFlows(data);
          updateDashboardCache({ flows: data });
        })
        .catch(() => {
          if (!cached?.flows) setFlows({ interview_progression: {} });
        })
        .finally(() => setFlowsLoading(false));

      await Promise.all([statsP, chartsP, questionsP, flowsP]);
    };

    loadAll();
  }, [authLoading, user]);

  return (
    <ProtectedRoute>
      {statsLoading ? (
        <div className="page-container py-12">
          <div className="skeleton skeleton-heading w-40" />
          <div className="mt-2 skeleton skeleton-text w-64" />
          <div className="mt-8">
            <StatsSkeleton />
          </div>
        </div>
      ) : statsError ? (
        <div className="page-container py-12">
          <div className="card p-8 text-center max-w-md mx-auto">
            <svg className="h-10 w-10 text-[var(--text-muted)] mx-auto" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z" />
            </svg>
            <p className="mt-3 text-sm text-[var(--text-muted)]">{statsError}</p>
            <button className="btn-secondary mt-4" onClick={retryStats}>
              Try again
            </button>
          </div>
        </div>
      ) : !stats || stats.total_experiences === 0 ? (
        <div className="page-container py-12">
          <div className="card p-8 text-center max-w-md mx-auto">
            <h2 className="text-xl font-semibold">No data yet</h2>
            <p className="mt-2 text-[var(--text-muted)]">
              The archive needs interview experiences to generate placement analytics.
            </p>
            <Link href="/submit" className="btn-primary mt-4">
              Contribute an experience
            </Link>
          </div>
        </div>
      ) : (
        <div className="page-container py-12">
          {/* Header */}
          <div className="flex items-baseline justify-between">
            <div>
              <h1 className="text-2xl font-semibold">Placement Analytics</h1>
              <p className="mt-1 text-[var(--text-muted)]">
                {stats.total_experiences} experience{stats.total_experiences !== 1 ? "s" : ""} analyzed across the institutional archive
              </p>
            </div>
          </div>

          {/* Contribution Impact - only show if user has contributions */}
          {stats.contribution_impact?.experiences_submitted > 0 && (
            <div className="mt-6 p-4 rounded-lg bg-[var(--primary)]/10 border border-[var(--primary)]/20">
              <div className="flex items-center gap-3">
                <div className="flex h-10 w-10 items-center justify-center rounded-full bg-[var(--primary)]/20">
                  <svg className="h-5 w-5 text-[var(--primary)]" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M4.318 6.318a4.5 4.5 0 000 6.364L12 20.364l7.682-7.682a4.5 4.5 0 00-6.364-6.364L12 7.636l-1.318-1.318a4.5 4.5 0 00-6.364 0z" />
                  </svg>
                </div>
                <div>
                  <p className="text-sm font-medium">Included in institutional analytics</p>
                  <p className="text-xs text-[var(--text-muted)]">
                    {stats.contribution_impact.experiences_submitted} experience{stats.contribution_impact.experiences_submitted !== 1 ? "s" : ""} submitted · {stats.contribution_impact.questions_extracted} questions extracted · Part of a {stats.contribution_impact.archive_size}-experience archive
                  </p>
                </div>
              </div>
            </div>
          )}

          {/* Stats - Tier 1, already loaded */}
          <div className="mt-6 grid gap-3 sm:grid-cols-3">
            <div className="stat-card">
              <p className="text-xs text-[var(--text-muted)] uppercase tracking-wide inline-flex items-center gap-0.5">
                Total
                <InfoTooltip text="Counts reflect number of interview experiences analyzed." />
              </p>
              <p className="mt-1 text-2xl font-semibold">{stats.total_experiences}</p>
            </div>
            <div className="stat-card">
              <p className="text-xs text-[var(--text-muted)] uppercase tracking-wide">Top company</p>
              <p className="mt-1 text-2xl font-semibold">{stats.top_company ?? "—"}</p>
            </div>
            <div className="stat-card">
              <p className="text-xs text-[var(--text-muted)] uppercase tracking-wide">Top topic</p>
              <p className="mt-1 text-2xl font-semibold">{stats.top_topic ?? "—"}</p>
            </div>
          </div>

          {/* Frequently Asked Questions - Tier 2 */}
          <div className="mt-8 card p-5">
            <div className="flex items-center justify-between mb-4">
              <div>
                <h3 className="font-medium">Most-repeated questions</h3>
                <p className="text-xs text-[var(--text-muted)] mt-0.5">
                  Questions that appeared across multiple interview experiences
                </p>
              </div>
            </div>
            {questionsLoading ? (
              <SectionSkeleton height="h-32" />
            ) : questions?.frequent_questions && Object.keys(questions.frequent_questions).length > 0 ? (
              <div className="space-y-2">
                {Object.entries(questions.frequent_questions).map(([question, count]) => (
                  <div key={question} className="flex items-start justify-between gap-3 p-3 rounded-lg bg-[var(--surface-muted)]">
                    <p className="text-sm flex-1">{question}</p>
                    <div className="flex items-center gap-2 shrink-0">
                      <SaveToListButton
                        questionText={question}
                        topic="General"
                      />
                      <span className="inline-flex items-center gap-1 rounded-full bg-[var(--warning-soft)] px-2 py-0.5 text-xs text-[var(--warning)]">
                        <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                          <path strokeLinecap="round" strokeLinejoin="round" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0z" />
                        </svg>
                        {count} exp
                        <InfoTooltip text={`This question appeared in ${count} separate interview experiences`} />
                      </span>
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <p className="text-sm text-[var(--text-muted)] py-4 text-center">
                No frequently repeated questions found yet.
              </p>
            )}
          </div>

          {/* Interview Flows - Tier 2, Collapsible, Lazy-mounted */}
          <LazySection fallback={<SectionSkeleton height="h-40" />}>
          <CollapsibleSection title="Common interview progression" defaultOpen={false}>
            <p className="text-xs text-[var(--text-muted)] -mt-2 mb-4 italic">
              Derived from aggregated interview experiences; individual processes may vary.
            </p>
            {flowsLoading ? (
              <SectionSkeleton height="h-40" />
            ) : flows?.interview_progression && Object.keys(flows.interview_progression).length > 0 ? (
              <div className="grid gap-4 lg:grid-cols-2">
                {Object.entries(flows.interview_progression).map(([company, companyData]) => (
                  <div key={company} className="card p-4">
                    <div className="flex items-baseline justify-between mb-3">
                      <p className="font-medium text-sm">{company}</p>
                      <span className="text-xs text-[var(--text-muted)] inline-flex items-center gap-0.5">
                        {companyData.total_experiences} exp
                        <InfoTooltip text="Total interview experiences analyzed for this company" />
                      </span>
                    </div>
                    <div className="flex flex-wrap items-center gap-2">
                      {Object.entries(companyData.stages).map(([stage, stageData], idx, arr) => (
                        <div key={stage} className="flex items-center gap-2">
                          <div className="flex flex-col items-center">
                            <span className="px-2.5 py-1 rounded-md bg-[var(--surface-muted)] text-xs font-medium">
                              {stage}
                            </span>
                            <span className="text-[10px] text-[var(--text-muted)] mt-0.5 inline-flex items-center gap-0.5">
                              {stageData.frequency}×
                              <InfoTooltip text={`Appeared in ${stageData.frequency} interview experience${stageData.frequency !== 1 ? "s" : ""} for this round`} />
                            </span>
                            {stageData.topics.length > 0 && (
                              <div className="mt-0.5 flex flex-wrap gap-1 max-w-[120px] justify-center">
                                {stageData.topics.slice(0, 2).map((topic) => (
                                  <span key={`${stage}-${topic}`} className="text-[10px] text-[var(--text-muted)]">
                                    {topic}
                                  </span>
                                ))}
                              </div>
                            )}
                          </div>
                          {idx < arr.length - 1 && (
                            <svg className="h-4 w-4 text-[var(--text-muted)]" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                              <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
                            </svg>
                          )}
                        </div>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <p className="text-sm text-[var(--text-muted)] py-4 text-center">
                No interview flow data available yet.
              </p>
            )}
          </CollapsibleSection>
          </LazySection>

          {/* Charts - Tier 2 */}
          <div className="mt-8 grid gap-6 lg:grid-cols-2">
            <div className="card p-5">
              <h3 className="font-medium mb-4">Topic frequency</h3>
              {chartsLoading ? (
                <SectionSkeleton height="h-40" />
              ) : charts?.topic_totals && Object.keys(charts.topic_totals).length > 0 ? (
                <BarChart data={charts.topic_totals} />
              ) : (
                <p className="text-sm text-[var(--text-muted)] py-4 text-center">No data</p>
              )}
            </div>
            <div className="card p-5">
              <h3 className="font-medium mb-4">Difficulty distribution</h3>
              {chartsLoading ? (
                <SectionSkeleton height="h-40" />
              ) : charts?.difficulty_distribution && Object.keys(charts.difficulty_distribution).length > 0 ? (
                <BarChart data={charts.difficulty_distribution} />
              ) : (
                <p className="text-sm text-[var(--text-muted)] py-4 text-center">No data</p>
              )}
            </div>
          </div>

          {/* Company breakdown - Tier 2, top 4 + Show more */}
          <CompanyBreakdownSection chartsLoading={chartsLoading} companyTopicCounts={charts?.company_topic_counts} />

          {/* Insights - Tier 2 */}
          {!chartsLoading && charts?.insights && charts.insights.length > 0 && (
            <div className="mt-8 card p-5">
              <h3 className="font-medium mb-3">Insights</h3>
              <ul className="space-y-2 text-sm text-[var(--text-secondary)]">
                {charts.insights.map((insight, i) => (
                  <li key={i} className="flex gap-2">
                    <span className="text-[var(--text-muted)]">•</span>
                    {insight}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {/* Preparation CTA */}
          <div className="mt-8 p-6 rounded-lg border border-dashed border-[var(--border)] text-center">
            <p className="text-sm text-[var(--text-muted)]">
              Raw Experience → AI Structuring → Semantic Discovery → Institutional Knowledge
            </p>
            <div className="mt-4 flex justify-center gap-3">
              <Link href="/search" className="btn-ghost text-sm">
                Search the archive
              </Link>
              <Link href="/practice" className="btn-primary text-sm">
                View practice lists
              </Link>
            </div>
          </div>
        </div>
      )}
    </ProtectedRoute>
  );
}
