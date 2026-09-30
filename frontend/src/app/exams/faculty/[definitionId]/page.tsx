"use client";

import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import Link from "next/link";

import { ExamProtectedRoute } from "@/components/exams/ExamProtectedRoute";
import { ErrorState, LoadingState } from "@/components/States";
import { useExamAuth } from "@/lib/examAuth";
import { examApiFetch } from "@/lib/examsApi";
import {
  examEndpoints,
  type ApprovedSourceSummary,
  type ExamRunSummary,
  type GenerationCandidate,
} from "@/lib/examEndpoints";

type Blueprint = { id: string; title: string; slots: unknown[] };

function DefinitionDetail() {
  const { definitionId } = useParams<{ definitionId: string }>();
  const { getToken } = useExamAuth();

  const [runs, setRuns] = useState<ExamRunSummary[] | null>(null);
  const [blueprints, setBlueprints] = useState<Blueprint[] | null>(null);
  const [sources, setSources] = useState<ApprovedSourceSummary[] | null>(null);
  const [candidatesByBlueprint, setCandidatesByBlueprint] = useState<Record<string, GenerationCandidate[]>>({});
  const [error, setError] = useState<string | null>(null);

  const [sourceUrl, setSourceUrl] = useState("");
  const [blueprintTitle, setBlueprintTitle] = useState("");
  const [slotCount, setSlotCount] = useState(1);
  const [enrolledUidsText, setEnrolledUidsText] = useState("");

  const load = useCallback(async () => {
    try {
      const token = await getToken();
      const [runsData, blueprintsData, sourcesData] = await Promise.all([
        examEndpoints.listRunsForDefinition(token, definitionId),
        examEndpoints.listBlueprintsForDefinition(token, definitionId),
        examEndpoints.listSources(token),
      ]);
      setRuns(runsData.results);
      setBlueprints(blueprintsData.results);
      setSources(sourcesData.results);
      for (const bp of blueprintsData.results) {
        const candidates = await examEndpoints.listCandidatesForBlueprint(token, bp.id);
        setCandidatesByBlueprint((prev) => ({ ...prev, [bp.id]: candidates.results }));
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load exam details.");
    }
  }, [definitionId, getToken]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- initial fetch on mount
    load();
  }, [load]);

  const handleAddSourceUrl = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!sourceUrl.trim()) return;
    const token = await getToken();
    await examApiFetch("/api/exams/v1/faculty/kt/sources/from-url", { method: "POST", body: JSON.stringify({ url: sourceUrl.trim() }) }, token);
    setSourceUrl("");
    await load();
  };

  const handleReviewSource = async (sourceId: string, decision: "approve" | "reject") => {
    const token = await getToken();
    await examEndpoints.reviewSource(token, sourceId, decision, decision === "approve" ? "Rights confirmed by faculty reviewer." : undefined);
    await load();
  };

  const handleCreateBlueprint = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!blueprintTitle.trim()) return;
    const token = await getToken();
    await examApiFetch(
      "/api/exams/v1/faculty/blueprints",
      {
        method: "POST",
        body: JSON.stringify({
          exam_definition_id: definitionId,
          title: blueprintTitle.trim(),
          slots: [
            {
              slot_index: 0,
              item_type: "single_choice",
              objective_ids: ["core"],
              difficulty: "medium",
              marks: 5,
              target_minutes: 5,
              permitted_languages: [],
              requested_variant_count: 4,
            },
          ],
        }),
      },
      token
    );
    setBlueprintTitle("");
    await load();
  };

  const handleReviewCandidate = async (candidateId: string, decision: "approve" | "reject") => {
    const token = await getToken();
    await examEndpoints.reviewCandidate(token, candidateId, decision, decision === "reject" ? "Needs revision." : undefined);
    await load();
  };

  const handlePublishRevision = async () => {
    const token = await getToken();
    await examApiFetch(`/api/exams/v1/faculty/definitions/${definitionId}/publish`, {
      method: "POST",
      body: JSON.stringify({ slot_count: slotCount }),
    }, token);
    await load();
  };

  const handleScheduleRun = async (event: React.FormEvent) => {
    event.preventDefault();
    const token = await getToken();
    const enrolledUids = enrolledUidsText.split(",").map((s) => s.trim()).filter(Boolean);
    const definitionsResp = await examEndpoints.listMyDefinitions(token);
    const def = definitionsResp.results.find((d) => d.id === definitionId);
    if (!def?.latest_published_revision_id) {
      setError("Publish a revision before scheduling a run.");
      return;
    }
    await examApiFetch(
      "/api/exams/v1/faculty/runs",
      {
        method: "POST",
        body: JSON.stringify({
          definition_id: definitionId,
          revision_id: def.latest_published_revision_id,
          policy: { duration_minutes: 60, warning_threshold: 3 },
          enrolled_uids: enrolledUids,
        }),
      },
      token
    );
    setEnrolledUidsText("");
    await load();
  };

  if (error) return <ErrorState title="Something went wrong" description={error} />;
  if (!runs || !blueprints || !sources) return <LoadingState message="Loading exam workspace…" />;

  return (
    <div className="page-container py-10">
      <h1 className="text-2xl font-semibold">Exam workspace</h1>

      <section className="card mt-6 p-5">
        <h2 className="text-lg font-semibold">Source (KT) review</h2>
        <form onSubmit={handleAddSourceUrl} className="mt-3 flex gap-3">
          <input className="input-field flex-1" placeholder="https://approved-source.example.edu/notes" value={sourceUrl} onChange={(e) => setSourceUrl(e.target.value)} />
          <button className="btn-secondary" type="submit">Add source URL</button>
        </form>
        <div className="mt-4 space-y-2">
          {sources.length === 0 && <p className="text-sm text-[var(--text-muted)]">No sources submitted yet.</p>}
          {sources.map((s) => (
            <div key={s.id} className="flex items-center justify-between rounded-md border border-[var(--border)] p-3 text-sm">
              <div>
                <span className="badge mr-2">{s.kind}</span>
                <span className={`badge ${s.review_status === "approved" ? "badge-success" : s.review_status === "rejected" ? "badge-error" : "badge-warning"}`}>
                  {s.review_status}
                </span>
                {s.url && <span className="ml-2 text-[var(--text-muted)]">{s.url}</span>}
              </div>
              {s.review_status === "pending" && (
                <div className="flex gap-2">
                  <button className="btn-secondary" onClick={() => handleReviewSource(s.id, "approve")}>Approve</button>
                  <button className="btn-secondary" onClick={() => handleReviewSource(s.id, "reject")}>Reject</button>
                </div>
              )}
            </div>
          ))}
        </div>
      </section>

      <section className="card mt-4 p-5">
        <h2 className="text-lg font-semibold">Blueprints &amp; question review</h2>
        <form onSubmit={handleCreateBlueprint} className="mt-3 flex gap-3">
          <input className="input-field flex-1" placeholder="Blueprint title" value={blueprintTitle} onChange={(e) => setBlueprintTitle(e.target.value)} />
          <button className="btn-secondary" type="submit">Create blueprint (1 slot)</button>
        </form>
        <div className="mt-4 space-y-4">
          {blueprints.length === 0 && <p className="text-sm text-[var(--text-muted)]">No blueprints yet.</p>}
          {blueprints.map((bp) => (
            <div key={bp.id} className="rounded-md border border-[var(--border)] p-3">
              <h3 className="font-semibold">{bp.title}</h3>
              <div className="mt-2 space-y-2">
                {(candidatesByBlueprint[bp.id] ?? []).map((c) => (
                  <div key={c.id} className="flex items-center justify-between rounded-md bg-[var(--surface-muted)] p-2 text-sm">
                    <div>
                      <span className="badge mr-2">{c.item_type}</span>
                      <span className="badge mr-2">{c.status}</span>
                      <span className="text-[var(--text-muted)]">{c.public_prompt.slice(0, 80)}</span>
                    </div>
                    {c.status === "ready_for_human_review" && (
                      <div className="flex gap-2">
                        <button className="btn-secondary" onClick={() => handleReviewCandidate(c.id, "approve")}>Approve</button>
                        <button className="btn-secondary" onClick={() => handleReviewCandidate(c.id, "reject")}>Reject</button>
                      </div>
                    )}
                  </div>
                ))}
                {(candidatesByBlueprint[bp.id] ?? []).length === 0 && (
                  <p className="text-xs text-[var(--text-muted)]">No candidates generated for this blueprint yet.</p>
                )}
              </div>
            </div>
          ))}
        </div>
      </section>

      <section className="card mt-4 p-5">
        <h2 className="text-lg font-semibold">Publish &amp; schedule</h2>
        <p className="mt-1 text-sm text-[var(--text-muted)]">
          Publishing creates an immutable revision. Releasing a run is a separate, later action — preview (viewing a
          run) never mutates it.
        </p>
        <div className="mt-3 flex items-center gap-3">
          <label htmlFor="slot-count" className="label mb-0">Slot count</label>
          <input id="slot-count" type="number" min={1} className="input-field w-24" value={slotCount} onChange={(e) => setSlotCount(Number(e.target.value))} />
          <button className="btn-secondary" onClick={handlePublishRevision}>Publish revision</button>
        </div>

        <form onSubmit={handleScheduleRun} className="mt-4 flex gap-3">
          <input
            className="input-field flex-1"
            placeholder="Enrolled student UIDs, comma-separated"
            value={enrolledUidsText}
            onChange={(e) => setEnrolledUidsText(e.target.value)}
          />
          <button className="btn-primary" type="submit">Schedule run</button>
        </form>
      </section>

      <section className="card mt-4 p-5">
        <h2 className="text-lg font-semibold">Runs</h2>
        <div className="mt-3 space-y-2">
          {runs.length === 0 && <p className="text-sm text-[var(--text-muted)]">No runs scheduled yet.</p>}
          {runs.map((run) => (
            <div key={run.id} className="flex items-center justify-between rounded-md border border-[var(--border)] p-3 text-sm">
              <div>
                <span className="badge-primary badge mr-2">{run.status}</span>
                <span className="text-[var(--text-muted)]">v{run.version} · {run.enrolled_uids.length} enrolled</span>
              </div>
              <div className="flex gap-2">
                <Link className="btn-secondary" href={`/exams/faculty/${definitionId}/runs/${run.id}/lobby`}>Lobby</Link>
                <Link className="btn-secondary" href={`/exams/faculty/${definitionId}/runs/${run.id}/control`}>Live control</Link>
                <Link className="btn-secondary" href={`/exams/faculty/${definitionId}/runs/${run.id}/results`}>Results</Link>
                <Link className="btn-secondary" href={`/exams/faculty/${definitionId}/runs/${run.id}/lockdown`}>Lockdown</Link>
              </div>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}

export default function DefinitionDetailPage() {
  return (
    <ExamProtectedRoute requireRole="faculty">
      <DefinitionDetail />
    </ExamProtectedRoute>
  );
}
