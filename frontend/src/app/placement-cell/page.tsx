"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";

import { ProtectedRoute } from "@/components/ProtectedRoute";
import { useAuth } from "@/context/AuthContext";
import {
  clearSearchCacheAsPlacementCell,
  fetchModerationQueue,
  fetchPlacementCellAdmin,
  reprocessExperienceAsPlacementCell,
  resetSearchRuntimeAsPlacementCell,
  updateExperienceVisibilityAsPlacementCell,
  warmupSearchAsPlacementCell,
} from "@/lib/api";
import { getClientAuthToken } from "@/lib/authToken";
import type {
  ModerationQueueItem,
  PlacementCellAdminResponse,
} from "@/lib/types";

function MetricCard({ label, value, sub }: { label: string; value: string | number; sub?: string }) {
  return (
    <div className="stat-card">
      <p className="text-xs text-[var(--text-muted)] uppercase tracking-wide">{label}</p>
      <p className="mt-1 text-2xl font-semibold stat-value">{value}</p>
      {sub ? <p className="text-xs text-[var(--text-muted)] mt-1">{sub}</p> : null}
    </div>
  );
}

function HorizontalBars({ data, empty }: { data: Record<string, number>; empty: string }) {
  const entries = useMemo(() => Object.entries(data).sort((a, b) => b[1] - a[1]), [data]);
  const max = entries.length ? entries[0][1] : 1;

  if (!entries.length) {
    return <p className="text-sm text-[var(--text-muted)]">{empty}</p>;
  }

  return (
    <div className="space-y-2">
      {entries.map(([label, value]) => (
        <div key={label} className="flex items-center gap-3">
          <span className="w-24 sm:w-28 text-xs text-[var(--text-secondary)] truncate">{label}</span>
          <div className="flex-1 h-4 rounded bg-[var(--surface-muted)] overflow-hidden">
            <div className="h-full bg-[var(--primary)]" style={{ width: `${Math.max(6, Math.round((value / max) * 100))}%` }} />
          </div>
          <span className="text-xs text-[var(--text-muted)] w-7 sm:w-8 text-right stat-value">{value}</span>
        </div>
      ))}
    </div>
  );
}

export default function PlacementCellPage() {
  const { user } = useAuth();
  const [data, setData] = useState<PlacementCellAdminResponse | null>(null);
  const [queueRows, setQueueRows] = useState<ModerationQueueItem[]>([]);
  const [statusFilter, setStatusFilter] = useState<"all" | "pending" | "processing" | "done" | "failed">("failed");
  const [activeFilter, setActiveFilter] = useState<"all" | "active" | "hidden">("all");
  const [queueSampled, setQueueSampled] = useState(0);
  const [queueTotal, setQueueTotal] = useState(0);
  const [loadingQueue, setLoadingQueue] = useState(false);
  const [actionLoadingId, setActionLoadingId] = useState<string | null>(null);
  const [actionNote, setActionNote] = useState("");
  const [actionMessage, setActionMessage] = useState<string | null>(null);
  const [searchOpsBusy, setSearchOpsBusy] = useState(false);
  const [searchOpsMessage, setSearchOpsMessage] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const searchRuntime = data?.search_runtime;
  const searchCircuitOpen = searchRuntime?.semantic_circuit.open ?? false;
  const searchCooldownSeconds = searchRuntime
    ? Math.ceil((searchRuntime.semantic_circuit.cooldown_remaining_ms || 0) / 1000)
    : 0;

  const getToken = useCallback(async () => {
    const token = await getClientAuthToken();
    if (!token) {
      throw new Error("Not authenticated");
    }
    return token;
  }, []);

  const loadAnalytics = useCallback(async () => {
    const token = await getToken();
    const response = await fetchPlacementCellAdmin(token);
    setData(response);
  }, [getToken]);

  const loadQueue = useCallback(async () => {
    setLoadingQueue(true);
    try {
      const token = await getToken();
      const queue = await fetchModerationQueue(token, {
        status: statusFilter,
        active: activeFilter,
        limit: 50,
      });
      setQueueRows(queue.results);
      setQueueTotal(queue.total);
      setQueueSampled(queue.sampled);
    } finally {
      setLoadingQueue(false);
    }
  }, [activeFilter, getToken, statusFilter]);

  useEffect(() => {
    if (!user) {
      setLoading(false);
      return;
    }

    const load = async () => {
      setLoading(true);
      setError(null);
      try {
        await Promise.all([loadAnalytics(), loadQueue()]);
      } catch {
        setError("Could not load placement-cell operations data.");
      } finally {
        setLoading(false);
      }
    };

    load();
  }, [loadAnalytics, loadQueue, user]);

  useEffect(() => {
    if (!user || loading) {
      return;
    }
    loadQueue().catch(() => {
      setActionMessage("Queue refresh failed. Try again.");
    });
  }, [activeFilter, loadQueue, loading, statusFilter, user]);

  const handleReprocess = useCallback(async (item: ModerationQueueItem) => {
    setActionMessage(null);
    setActionLoadingId(item.id);
    try {
      const token = await getToken();
      await reprocessExperienceAsPlacementCell(item.id, token);
      setActionMessage(`Reprocess queued for ${item.id}.`);
      await Promise.all([loadAnalytics(), loadQueue()]);
    } catch {
      setActionMessage(`Could not queue reprocess for ${item.id}.`);
    } finally {
      setActionLoadingId(null);
    }
  }, [getToken, loadAnalytics, loadQueue]);

  const handleToggleVisibility = useCallback(async (item: ModerationQueueItem) => {
    setActionMessage(null);
    setActionLoadingId(item.id);
    try {
      const token = await getToken();
      await updateExperienceVisibilityAsPlacementCell(
        item.id,
        {
          is_active: !item.is_active,
          note: actionNote.trim() || undefined,
        },
        token
      );
      setActionMessage(`${item.is_active ? "Hidden" : "Restored"} ${item.id}.`);
      await Promise.all([loadAnalytics(), loadQueue()]);
    } catch {
      setActionMessage(`Could not update visibility for ${item.id}.`);
    } finally {
      setActionLoadingId(null);
    }
  }, [actionNote, getToken, loadAnalytics, loadQueue]);

  const runSearchOperation = useCallback(
    async (operation: "reset" | "clear" | "warmup") => {
      setSearchOpsBusy(true);
      setSearchOpsMessage(null);
      try {
        const token = await getToken();
        if (operation === "reset") {
          await resetSearchRuntimeAsPlacementCell(token);
          setSearchOpsMessage("Search runtime metrics reset.");
        } else if (operation === "clear") {
          await clearSearchCacheAsPlacementCell(token);
          setSearchOpsMessage("Search caches cleared.");
        } else {
          await warmupSearchAsPlacementCell(token);
          setSearchOpsMessage("Search warmup triggered.");
        }
        await loadAnalytics();
      } catch {
        setSearchOpsMessage("Search operation failed. Try again.");
      } finally {
        setSearchOpsBusy(false);
      }
    },
    [getToken, loadAnalytics]
  );

  return (
    <ProtectedRoute requiredRole="placement_cell">
      <div className="page-container py-10 sm:py-12">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
          <div>
            <h1 className="text-2xl sm:text-3xl font-semibold">Placement Cell Operations</h1>
            <p className="text-sm text-[var(--text-muted)] mt-1">Operational analytics for moderation, quality, and readiness tracking.</p>
          </div>
          <Link href="/dashboard" className="btn-secondary text-sm w-full sm:w-auto text-center">Back to Dashboard</Link>
        </div>

        {loading ? (
          <div className="mt-6 space-y-4">
            <div className="skeleton skeleton-heading w-56" />
            <div className="grid gap-3 sm:grid-cols-4">
              {[1, 2, 3, 4].map((item) => (
                <div key={item} className="skeleton skeleton-card" />
              ))}
            </div>
          </div>
        ) : error ? (
          <div className="mt-8 card p-6 text-center max-w-md">
            <p className="text-sm text-[var(--text-muted)]">{error}</p>
            <button className="btn-secondary mt-4" onClick={() => window.location.reload()}>Retry</button>
          </div>
        ) : data ? (
          <>
            <section className="mt-6 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <MetricCard label="Sampled" value={data.archive_overview.total_sampled} />
              <MetricCard label="Active" value={data.archive_overview.active} />
              <MetricCard label="Hidden" value={data.archive_overview.hidden} />
              <MetricCard label="NLP done" value={`${data.quality_metrics.nlp_done_rate_percent}%`} />
            </section>

            <section className="mt-8 card p-5">
              <h2 className="text-base font-semibold">Quality Signals</h2>
              <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                <MetricCard label="Avg questions" value={data.quality_metrics.avg_questions_per_experience} />
                <MetricCard label="Avg user questions" value={data.quality_metrics.avg_user_questions_per_experience} />
                <MetricCard label="Contact opt-in" value={`${data.quality_metrics.contact_opt_in_rate_percent}%`} />
                <MetricCard label="Anonymous share" value={`${data.archive_overview.total_sampled ? Math.round((data.privacy_breakdown.anonymous / data.archive_overview.total_sampled) * 100) : 0}%`} />
              </div>
            </section>

            <section className="mt-8 grid gap-6 lg:grid-cols-2">
              <div className="card p-5">
                <h2 className="text-base font-semibold">Pipeline Status</h2>
                <div className="mt-4">
                  <HorizontalBars data={data.nlp_pipeline} empty="No pipeline records yet." />
                </div>
              </div>
              <div className="card p-5">
                <h2 className="text-base font-semibold">Freshness</h2>
                <div className="mt-4 grid gap-3 sm:grid-cols-2">
                  <MetricCard label="Last 30 days" value={data.freshness.last_30_days} />
                  <MetricCard label="Last 90 days" value={data.freshness.last_90_days} />
                </div>
              </div>
            </section>

            <section className="mt-8 grid gap-6 lg:grid-cols-2">
              <div className="card p-5">
                <h2 className="text-base font-semibold">Year Distribution</h2>
                <div className="mt-4">
                  <HorizontalBars data={data.year_distribution} empty="No year-level records yet." />
                </div>
              </div>
              <div className="card p-5">
                <h2 className="text-base font-semibold">Top Companies</h2>
                <div className="mt-4">
                  <HorizontalBars data={data.company_distribution} empty="No company data yet." />
                </div>
              </div>
            </section>

            <section className="mt-8 card p-5">
              <h2 className="text-base font-semibold">Search Runtime</h2>
              <p className="text-xs text-[var(--text-muted)] mt-1">
                Semantic circuit: {searchCircuitOpen ? `open (${searchCooldownSeconds}s remaining)` : "closed"}
              </p>
              <div className="mt-3 flex flex-wrap gap-2">
                <button
                  className="btn-secondary"
                  onClick={() => runSearchOperation("reset")}
                  disabled={searchOpsBusy}
                >
                  {searchOpsBusy ? "Working..." : "Reset Runtime"}
                </button>
                <button
                  className="btn-secondary"
                  onClick={() => runSearchOperation("clear")}
                  disabled={searchOpsBusy}
                >
                  {searchOpsBusy ? "Working..." : "Clear Caches"}
                </button>
                <button
                  className="btn-secondary"
                  onClick={() => runSearchOperation("warmup")}
                  disabled={searchOpsBusy}
                >
                  {searchOpsBusy ? "Working..." : "Run Warmup"}
                </button>
              </div>
              {searchOpsMessage ? (
                <p className="text-sm text-[var(--text-muted)] mt-3">{searchOpsMessage}</p>
              ) : null}
              {searchRuntime ? (
                <>
                  <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                    <MetricCard label="Requests" value={searchRuntime.requests_total} />
                    <MetricCard label="Cache hit" value={`${searchRuntime.cache_hit_rate_percent}%`} />
                    <MetricCard label="Semantic success" value={`${searchRuntime.semantic_success_rate_percent}%`} />
                    <MetricCard label="P95 latency" value={`${searchRuntime.latency_ms.p95} ms`} />
                  </div>

                  <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
                    <MetricCard
                      label="Cache backend"
                      value={searchRuntime.cache_backend ?? "memory"}
                    />
                    <MetricCard
                      label="Tracked queries"
                      value={searchRuntime.query_analytics?.tracked_queries ?? 0}
                    />
                    <MetricCard
                      label="Zero-result rate"
                      value={`${searchRuntime.query_analytics?.zero_result_rate_percent ?? 0}%`}
                    />
                    <MetricCard
                      label="Index backlog"
                      value={searchRuntime.index_queue?.queued ?? 0}
                      sub={searchRuntime.index_queue?.enabled ? `${searchRuntime.index_queue.workers} worker(s)` : "disabled"}
                    />
                  </div>

                  <div className="mt-6 grid gap-6 lg:grid-cols-2">
                    <div>
                      <h3 className="text-sm font-medium">Served Modes</h3>
                      <div className="mt-2">
                        <HorizontalBars data={searchRuntime.mode_counts} empty="No mode data yet." />
                      </div>
                    </div>
                    <div>
                      <h3 className="text-sm font-medium">Fallback Reasons</h3>
                      <div className="mt-2">
                        <HorizontalBars data={searchRuntime.fallback_counts} empty="No fallbacks observed." />
                      </div>
                    </div>
                  </div>

                  {searchRuntime.query_analytics && (
                    <div className="mt-6 grid gap-6 lg:grid-cols-2">
                      <div>
                        <h3 className="text-sm font-medium">Top Queries</h3>
                        <div className="mt-2">
                          <HorizontalBars data={searchRuntime.query_analytics.top_queries} empty="No query analytics yet." />
                        </div>
                      </div>
                      <div>
                        <h3 className="text-sm font-medium">Zero-result Queries</h3>
                        <div className="mt-2">
                          <HorizontalBars data={searchRuntime.query_analytics.top_zero_result_queries} empty="No zero-result queries recorded." />
                        </div>
                      </div>
                    </div>
                  )}
                </>
              ) : (
                <p className="text-sm text-[var(--text-muted)] mt-3">Search runtime metrics are not available yet.</p>
              )}
            </section>

            <section className="mt-8 card p-5">
              <h2 className="text-base font-semibold">Contributor Activity</h2>
              {data.top_contributors.length ? (
                <div className="mt-3 flex flex-wrap gap-2">
                  {data.top_contributors.map((entry) => (
                    <span key={entry.uid} className="badge">{entry.display_name} • {entry.submissions}</span>
                  ))}
                </div>
              ) : (
                <p className="text-sm text-[var(--text-muted)] mt-3">No contributor activity yet.</p>
              )}
            </section>

            <section className="mt-8 card p-5">
              <h2 className="text-base font-semibold">Moderation Queue</h2>
              <p className="text-xs text-[var(--text-muted)] mt-1">
                Hidden entries: {data.moderation.hidden_count} · NLP failed entries: {data.moderation.nlp_failed_count}
              </p>
              {data.moderation.failed_examples.length ? (
                <div className="mt-4 overflow-x-auto">
                  <table className="w-full min-w-[36rem] text-sm">
                    <thead>
                      <tr className="text-left text-[var(--text-muted)] border-b border-[var(--border)]">
                        <th className="py-2 pr-3">Experience</th>
                        <th className="py-2 pr-3">Company</th>
                        <th className="py-2 pr-3">Year</th>
                        <th className="py-2">Created</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.moderation.failed_examples.map((row) => (
                        <tr key={row.id} className="border-b border-[var(--border)]/60">
                          <td className="py-2 pr-3 text-[var(--text-secondary)]">{row.id}</td>
                          <td className="py-2 pr-3">{row.company}</td>
                          <td className="py-2 pr-3">{row.year ?? "-"}</td>
                          <td className="py-2">{row.created_at ? new Date(row.created_at).toLocaleDateString() : "-"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <p className="text-sm text-[var(--text-muted)] mt-3">No failed NLP records in sampled data.</p>
              )}
            </section>

            <section className="mt-8 card p-5">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div>
                  <h2 className="text-base font-semibold">Action Queue</h2>
                  <p className="text-xs text-[var(--text-muted)] mt-1">
                    {queueTotal} result(s) from {queueSampled} sampled records.
                  </p>
                </div>
                <div className="flex flex-wrap gap-2">
                  <select
                    className="input-field min-w-[9rem]"
                    value={statusFilter}
                    onChange={(event) => setStatusFilter(event.target.value as "all" | "pending" | "processing" | "done" | "failed")}
                  >
                    <option value="all">All NLP status</option>
                    <option value="pending">Pending</option>
                    <option value="processing">Processing</option>
                    <option value="done">Done</option>
                    <option value="failed">Failed</option>
                  </select>
                  <select
                    className="input-field min-w-[9rem]"
                    value={activeFilter}
                    onChange={(event) => setActiveFilter(event.target.value as "all" | "active" | "hidden")}
                  >
                    <option value="all">All visibility</option>
                    <option value="active">Active only</option>
                    <option value="hidden">Hidden only</option>
                  </select>
                  <button
                    className="btn-secondary"
                    onClick={() => {
                      loadQueue().catch(() => {
                        setActionMessage("Queue refresh failed. Try again.");
                      });
                    }}
                    disabled={loadingQueue}
                  >
                    {loadingQueue ? "Refreshing..." : "Refresh"}
                  </button>
                </div>
              </div>

              <div className="mt-4 grid gap-2 sm:grid-cols-[1fr_auto]">
                <input
                  className="input-field"
                  placeholder="Optional moderation note for visibility actions"
                  value={actionNote}
                  onChange={(event) => setActionNote(event.target.value)}
                  maxLength={240}
                />
                <p className="text-xs text-[var(--text-muted)] sm:text-right sm:leading-10">
                  {actionNote.length}/240
                </p>
              </div>

              {actionMessage ? (
                <p className="text-sm text-[var(--text-muted)] mt-3">{actionMessage}</p>
              ) : null}

              {loadingQueue ? (
                <div className="mt-4 space-y-2">
                  <div className="skeleton h-10 w-full" />
                  <div className="skeleton h-10 w-full" />
                  <div className="skeleton h-10 w-full" />
                </div>
              ) : queueRows.length ? (
                <div className="mt-4 overflow-x-auto">
                  <table className="w-full min-w-[44rem] text-sm">
                    <thead>
                      <tr className="text-left text-[var(--text-muted)] border-b border-[var(--border)]">
                        <th className="py-2 pr-3">Experience</th>
                        <th className="py-2 pr-3">NLP</th>
                        <th className="py-2 pr-3">Visibility</th>
                        <th className="py-2 pr-3">Questions</th>
                        <th className="py-2 pr-3">Contributor</th>
                        <th className="py-2">Actions</th>
                      </tr>
                    </thead>
                    <tbody>
                      {queueRows.map((row) => {
                        const rowBusy = actionLoadingId === row.id;
                        const canReprocess = row.nlp_status !== "processing" && row.nlp_status !== "pending";
                        return (
                          <tr key={row.id} className="border-b border-[var(--border)]/60 align-top">
                            <td className="py-2 pr-3">
                              <p className="font-medium">{row.company}</p>
                              <p className="text-xs text-[var(--text-muted)]">{row.id}</p>
                              <p className="text-xs text-[var(--text-muted)] mt-1">{row.role} · {row.round} · {row.year ?? "-"}</p>
                            </td>
                            <td className="py-2 pr-3">
                              <span className="badge">{row.nlp_status}</span>
                            </td>
                            <td className="py-2 pr-3">{row.is_active ? "active" : "hidden"}</td>
                            <td className="py-2 pr-3">
                              <p>{row.question_count}</p>
                              <p className="text-xs text-[var(--text-muted)]">user: {row.user_question_count}</p>
                            </td>
                            <td className="py-2 pr-3">
                              <p>{row.contributor_display}</p>
                              <p className="text-xs text-[var(--text-muted)]">{row.created_at ? new Date(row.created_at).toLocaleDateString() : "-"}</p>
                            </td>
                            <td className="py-2">
                              <div className="flex flex-wrap gap-2">
                                <button
                                  className="btn-secondary"
                                  onClick={() => handleReprocess(row)}
                                  disabled={rowBusy || !canReprocess}
                                >
                                  {rowBusy ? "Working..." : canReprocess ? "Reprocess" : "In queue"}
                                </button>
                                <button
                                  className="btn-secondary"
                                  onClick={() => handleToggleVisibility(row)}
                                  disabled={rowBusy}
                                >
                                  {rowBusy ? "Working..." : row.is_active ? "Hide" : "Restore"}
                                </button>
                              </div>
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              ) : (
                <p className="text-sm text-[var(--text-muted)] mt-3">No records match the selected filters.</p>
              )}
            </section>
          </>
        ) : null}
      </div>
    </ProtectedRoute>
  );
}
