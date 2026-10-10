"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { ExamProtectedRoute } from "@/components/exams/ExamProtectedRoute";
import { ErrorState, LoadingState } from "@/components/States";
import { useExamAuth } from "@/lib/examAuth";
import { describeConflict } from "@/lib/examConflicts";
import {
  examEndpoints,
  type DefectiveQuestionResolution,
  type ExportJob,
  type GradebookCounts,
  type ReleaseRecord,
  type StudentGradebookRow,
} from "@/lib/examEndpoints";
import { ExamApiError } from "@/lib/examsApi";

const OUTCOME_BADGE: Record<string, string> = {
  fully_solved: "badge-success",
  partially_solved: "badge-warning",
  blank: "badge",
  pending: "badge",
};

function OutcomeBadge({ outcome }: { outcome: string }) {
  return <span className={`badge ${OUTCOME_BADGE[outcome] ?? "badge"}`}>{outcome.replace("_", " ")}</span>;
}

function GradebookContent() {
  const { definitionId, runId } = useParams<{ definitionId: string; runId: string }>();
  const { getToken } = useExamAuth();

  const [rows, setRows] = useState<StudentGradebookRow[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [counts, setCounts] = useState<GradebookCounts[]>([]);
  const [releases, setReleases] = useState<ReleaseRecord[]>([]);
  const [defectiveResolutions, setDefectiveResolutions] = useState<DefectiveQuestionResolution[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  const [releaseNote, setReleaseNote] = useState("");
  const [releasing, setReleasing] = useState(false);
  const [releaseError, setReleaseError] = useState<string | null>(null);
  const [incompleteWarning, setIncompleteWarning] = useState<string | null>(null);

  const [defectiveQuestionId, setDefectiveQuestionId] = useState("");
  const [defectiveResolution, setDefectiveResolution] = useState<"full_credit_all" | "exclude_from_total">("full_credit_all");
  const [defectiveReason, setDefectiveReason] = useState("");
  const [resolvingDefective, setResolvingDefective] = useState(false);

  const [exportFormat, setExportFormat] = useState<"csv" | "json">("csv");
  const [exportJob, setExportJob] = useState<ExportJob | null>(null);
  const [exporting, setExporting] = useState(false);
  const [downloading, setDownloading] = useState(false);

  const load = useCallback(async () => {
    try {
      const token = await getToken();
      const [rowsData, countsData, releasesData, defectiveData] = await Promise.all([
        examEndpoints.listGradebookRows(token, runId),
        examEndpoints.getGradebookCounts(token, runId),
        examEndpoints.listReleases(token, runId),
        examEndpoints.listDefectiveResolutions(token, runId),
      ]);
      setRows(rowsData.results);
      setNextCursor(rowsData.next_cursor_uid);
      setCounts(countsData.results);
      setReleases(releasesData.results);
      setDefectiveResolutions(defectiveData.results);
      setLoaded(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load the gradebook.");
    }
  }, [getToken, runId]);

  useEffect(() => {
    load();
  }, [load]);

  const loadMore = async () => {
    if (!nextCursor) return;
    try {
      const token = await getToken();
      const data = await examEndpoints.listGradebookRows(token, runId, nextCursor);
      setRows((prev) => [...prev, ...data.results]);
      setNextCursor(data.next_cursor_uid);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load more rows.");
    }
  };

  const latestRelease = releases.length > 0 ? releases[releases.length - 1] : null;

  const handleRelease = async (acknowledgeIncomplete = false) => {
    setReleasing(true);
    setReleaseError(null);
    try {
      const token = await getToken();
      if (acknowledgeIncomplete) {
        await examEndpoints.releaseResults(token, runId, releaseNote, true);
      } else {
        await examEndpoints.releaseResults(token, runId, releaseNote);
      }
      setReleaseNote("");
      setIncompleteWarning(null);
      await load();
    } catch (err) {
      if (err instanceof ExamApiError && err.code === "release_incomplete") {
        setIncompleteWarning(err.message);
      } else {
        // A refusal (e.g. a correction without a reason) is explained next to the button, not by replacing the page.
        setReleaseError(describeConflict(err, "Could not release results."));
      }
    } finally {
      setReleasing(false);
    }
  };

  const handleResolveDefective = async () => {
    if (!defectiveQuestionId.trim() || !defectiveReason.trim()) return;
    setResolvingDefective(true);
    try {
      const token = await getToken();
      await examEndpoints.resolveDefectiveQuestion(token, runId, defectiveQuestionId.trim(), defectiveResolution, defectiveReason);
      setDefectiveQuestionId("");
      setDefectiveReason("");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not resolve the defective question.");
    } finally {
      setResolvingDefective(false);
    }
  };

  const handleExport = async () => {
    setExporting(true);
    setExportJob(null);
    try {
      const token = await getToken();
      const job = await examEndpoints.createExport(token, runId, exportFormat);
      setExportJob(job);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not start the export.");
    } finally {
      setExporting(false);
    }
  };

  const handleDownload = async () => {
    if (!exportJob) return;
    setDownloading(true);
    try {
      const token = await getToken();
      const { download_ticket } = await examEndpoints.issueExportDownloadTicket(token, exportJob.id);
      const apiBase = (process.env.NEXT_PUBLIC_EXAMS_API_BASE_URL || "http://localhost:8020").replace(/\/+$/, "");
      const response = await fetch(`${apiBase}/api/exams/v1/faculty/gradebook/export/download/${download_ticket}`, {
        headers: token ? { Authorization: `Bearer ${token}` } : undefined,
        cache: "no-store",
      });
      if (!response.ok) throw new Error("Could not download the export.");
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `gradebook-${runId}.${exportJob.format}`;
      link.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not download the export.");
    } finally {
      setDownloading(false);
    }
  };

  if (error) return <ErrorState title="Something went wrong" description={error} />;
  if (!loaded) return <LoadingState message="Loading gradebook…" />;

  return (
    <div className="page-container max-w-5xl py-10">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Gradebook</h1>
        <div className="flex gap-2">
          <Link className="btn-secondary" href={`/exams/faculty/${definitionId}/runs/${runId}/results/appeals`}>
            Grade appeals
          </Link>
          <Link className="btn-secondary" href={`/exams/faculty/${definitionId}/runs/${runId}/results/similarity`}>
            Similarity review
          </Link>
        </div>
      </div>

      {/* Release */}
      <div className="card mt-6 p-6">
        <h2 className="text-lg font-semibold">Release results</h2>
        <p className="mt-1 text-sm text-[var(--text-muted)]">
          {latestRelease
            ? `Students currently see version ${latestRelease.version}, released ${new Date(latestRelease.released_at).toLocaleString()}. Regrades and defective-question resolutions change this gradebook at once but reach students only when you release a new version.`
            : "Results have not been released to students yet."}
        </p>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <input
            className="input-field max-w-xs"
            aria-label={latestRelease ? "Reason for the corrected release" : "Release note (optional)"}
            placeholder={latestRelease ? "Reason for the correction (required)" : "Optional note"}
            value={releaseNote}
            onChange={(e) => setReleaseNote(e.target.value)}
          />
          <button className="btn-primary" disabled={releasing} onClick={() => handleRelease()}>
            {releasing ? "Releasing…" : latestRelease ? "Release corrected results (new version)" : "Release results"}
          </button>
        </div>
        {releaseError && (
          <p role="alert" className="mt-2 text-sm text-[var(--error)]">
            {releaseError}
          </p>
        )}
        {incompleteWarning && (
          <div role="alert" className="mt-3 rounded-md border border-[var(--border)] p-3 text-sm">
            <p>{incompleteWarning}</p>
            <div className="mt-2 flex gap-2">
              <button className="btn-primary text-xs" disabled={releasing} onClick={() => handleRelease(true)}>
                Release anyway
              </button>
              <button className="btn-secondary text-xs" onClick={() => setIncompleteWarning(null)}>
                Not yet
              </button>
            </div>
          </div>
        )}
      </div>

      {/* Per-question counts */}
      <div className="card mt-6 overflow-x-auto p-6">
        <h2 className="text-lg font-semibold">Per-question counts</h2>
        <table className="mt-3 w-full text-left text-sm">
          <thead>
            <tr className="text-[var(--text-muted)]">
              <th className="py-1 pr-3">Question</th>
              <th className="py-1 pr-3">Assigned</th>
              <th className="py-1 pr-3">Presented</th>
              <th className="py-1 pr-3">Finalized</th>
              <th className="py-1 pr-3">Attempted</th>
              <th className="py-1 pr-3">Fully solved</th>
              <th className="py-1 pr-3">Partial</th>
              <th className="py-1 pr-3">Blank</th>
              <th className="py-1 pr-3">Pending</th>
            </tr>
          </thead>
          <tbody>
            {counts.map((c) => (
              <tr key={c.question_id} className="border-t border-[var(--border)]">
                <td className="py-1 pr-3 font-mono text-xs">{c.question_id}</td>
                <td className="py-1 pr-3">{c.assigned}</td>
                <td className="py-1 pr-3">{c.presented}</td>
                <td className="py-1 pr-3">{c.finalized}</td>
                <td className="py-1 pr-3">{c.nonblank_attempted}</td>
                <td className="py-1 pr-3">{c.fully_solved}</td>
                <td className="py-1 pr-3">{c.partially_solved}</td>
                <td className="py-1 pr-3">{c.blank}</td>
                <td className="py-1 pr-3">{c.pending}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Defective question resolution */}
      <div className="card mt-6 p-6">
        <h2 className="text-lg font-semibold">Resolve a defective question</h2>
        <p className="mt-1 text-sm text-[var(--text-muted)]">
          Applies one uniform, auditable resolution across every assigned student for that question. Never edits any
          student&apos;s original submission.
        </p>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <input
            className="input-field max-w-xs font-mono text-xs"
            aria-label="Question id to resolve"
            placeholder="question_id (from the table above)"
            value={defectiveQuestionId}
            onChange={(e) => setDefectiveQuestionId(e.target.value)}
          />
          <select
            className="input-field max-w-[200px]"
            aria-label="Resolution"
            value={defectiveResolution}
            onChange={(e) => setDefectiveResolution(e.target.value as "full_credit_all" | "exclude_from_total")}
          >
            <option value="full_credit_all">Full credit to all</option>
            <option value="exclude_from_total">Exclude from total</option>
          </select>
          <input
            aria-label="Reason for the resolution"
            className="input-field max-w-xs"
            placeholder="Reason"
            value={defectiveReason}
            onChange={(e) => setDefectiveReason(e.target.value)}
          />
          <button className="btn-primary" disabled={resolvingDefective} onClick={handleResolveDefective}>
            {resolvingDefective ? "Applying…" : "Apply resolution"}
          </button>
        </div>
        {defectiveResolutions.length > 0 && (
          <ul className="mt-3 space-y-1 text-xs text-[var(--text-muted)]">
            {defectiveResolutions.map((r) => (
              <li key={r.id}>
                <span className="font-mono">{r.question_id}</span> — {r.resolution.replace("_", " ")}: {r.reason}
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* Export */}
      <div className="card mt-6 p-6">
        <h2 className="text-lg font-semibold">Export</h2>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <select className="input-field max-w-[140px]" aria-label="Export format" value={exportFormat} onChange={(e) => setExportFormat(e.target.value as "csv" | "json")}>
            <option value="csv">CSV</option>
            <option value="json">JSON</option>
          </select>
          <button className="btn-secondary" disabled={exporting} onClick={handleExport}>
            {exporting ? "Generating…" : "Generate export"}
          </button>
          {exportJob && exportJob.status === "succeeded" && (
            <button className="btn-primary" disabled={downloading} onClick={handleDownload}>
              {downloading ? "Downloading…" : `Download (${exportJob.row_count ?? 0} rows)`}
            </button>
          )}
          {exportJob && exportJob.status === "failed" && (
            <span className="badge-error badge">Export failed: {exportJob.error}</span>
          )}
        </div>
        {exportJob && exportJob.status === "succeeded" && exportJob.expires_at && (
          <p className="mt-2 text-xs text-[var(--text-muted)]">
            Available until {new Date(exportJob.expires_at).toLocaleString()}; the file is deleted after that.
          </p>
        )}
      </div>

      {/* Student rows */}
      <div className="card mt-6 overflow-x-auto p-6">
        <h2 className="text-lg font-semibold">Students</h2>
        <table className="mt-3 w-full text-left text-sm">
          <thead>
            <tr className="text-[var(--text-muted)]">
              <th className="py-1 pr-3">UID</th>
              <th className="py-1 pr-3">Status</th>
              <th className="py-1 pr-3">Integrity</th>
              <th className="py-1 pr-3">Score</th>
              <th className="py-1 pr-3">Questions</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.uid} className="border-t border-[var(--border)]">
                <td className="py-1 pr-3 font-mono text-xs">{row.uid}</td>
                <td className="py-1 pr-3">{row.attempt_status}</td>
                <td className="py-1 pr-3">
                  {row.integrity_disposition !== "clean" && (
                    <span className="badge-warning badge">{row.integrity_disposition.replace(/_/g, " ")}</span>
                  )}
                  {row.integrity_disposition === "clean" && <span className="badge">clean</span>}
                </td>
                <td className="py-1 pr-3">
                  {row.raw_score.toFixed(1)} / {row.raw_max_score.toFixed(1)}
                </td>
                <td className="py-1 pr-3">
                  <div className="flex flex-wrap gap-1">
                    {row.question_results.map((qr) => (
                      <OutcomeBadge key={qr.question_id} outcome={qr.outcome} />
                    ))}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {nextCursor && (
          <button className="btn-secondary mt-4" onClick={loadMore}>
            Load more
          </button>
        )}
      </div>
    </div>
  );
}

export default function ResultsPage() {
  return (
    <ExamProtectedRoute requireRole="faculty">
      <GradebookContent />
    </ExamProtectedRoute>
  );
}
