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
  type ExamDefinition,
  type ExamRunSummary,
  type GenerationCandidate,
} from "@/lib/examEndpoints";

type Blueprint = { id: string; title: string; slots: unknown[] };

function DefinitionDetail() {
  const { definitionId } = useParams<{ definitionId: string }>();
  const { getToken } = useExamAuth();

  const [definition, setDefinition] = useState<ExamDefinition | null>(null);
  const [runs, setRuns] = useState<ExamRunSummary[] | null>(null);
  const [blueprints, setBlueprints] = useState<Blueprint[] | null>(null);
  const [sources, setSources] = useState<ApprovedSourceSummary[] | null>(null);
  const [candidatesByBlueprint, setCandidatesByBlueprint] = useState<Record<string, GenerationCandidate[]>>({});
  const [error, setError] = useState<string | null>(null);

  const [sourceUrl, setSourceUrl] = useState("");
  const [blueprintTitle, setBlueprintTitle] = useState("");
  const [blueprintItemType, setBlueprintItemType] = useState<"single_choice" | "multiple_select" | "short_answer" | "coding">("single_choice");
  const [blueprintLanguage, setBlueprintLanguage] = useState("python3.11");
  const [slotCount, setSlotCount] = useState(1);
  const [enrolledText, setEnrolledText] = useState("");
  const [enrollWholeRoster, setEnrollWholeRoster] = useState(false);
  const [durationMinutes, setDurationMinutes] = useState(60);
  const [scheduleError, setScheduleError] = useState<string | null>(null);
  const [generatingBlueprintId, setGeneratingBlueprintId] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const token = await getToken();
      const [runsData, blueprintsData, sourcesData, definitionsData] = await Promise.all([
        examEndpoints.listRunsForDefinition(token, definitionId),
        examEndpoints.listBlueprintsForDefinition(token, definitionId),
        examEndpoints.listSources(token),
        examEndpoints.listMyDefinitions(token),
      ]);
      setDefinition(definitionsData.results.find((d) => d.id === definitionId) ?? null);
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
              item_type: blueprintItemType,
              objective_ids: ["core"],
              difficulty: "medium",
              marks: 5,
              target_minutes: 5,
              permitted_languages: blueprintItemType === "coding" ? [blueprintLanguage] : [],
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

  const handleGenerateCandidates = async (blueprintId: string) => {
    const approvedSources = (sources ?? []).filter((s) => s.review_status === "approved");
    if (approvedSources.length === 0) {
      setError("Approve at least one source before generating questions.");
      return;
    }
    setGeneratingBlueprintId(blueprintId);
    try {
      const token = await getToken();
      await examEndpoints.createGenerationRequest(token, {
        blueprint_id: blueprintId,
        slot_index: 0,
        source_anchors: approvedSources.map((s) => ({ source_id: s.id, anchor_id: "full-document" })),
        deterministic_seed: Date.now(),
      });
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not generate questions.");
    } finally {
      setGeneratingBlueprintId(null);
    }
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
    setScheduleError(null);
    if (!definition?.latest_published_revision_id) {
      setScheduleError("Publish a revision before scheduling a session.");
      return;
    }
    // Institution exams enrol by college ID (or the whole active roster); pilot
    // exams have no roster, so they enrol raw account UIDs.
    const entries = enrolledText.split(/[\s,;]+/).map((s) => s.trim()).filter(Boolean);
    const enrolment = definition.institution_id
      ? { enrolled_identity_keys: entries, enroll_all_institution_students: enrollWholeRoster }
      : { enrolled_uids: entries };
    if (!enrollWholeRoster && entries.length === 0) {
      setScheduleError("Enrol at least one student.");
      return;
    }
    try {
      const token = await getToken();
      await examApiFetch(
        "/api/exams/v1/faculty/runs",
        {
          method: "POST",
          body: JSON.stringify({
            definition_id: definitionId,
            revision_id: definition.latest_published_revision_id,
            policy: { duration_minutes: durationMinutes, warning_threshold: 3 },
            ...enrolment,
          }),
        },
        token
      );
      setEnrolledText("");
      setEnrollWholeRoster(false);
      await load();
    } catch (err) {
      setScheduleError(err instanceof Error ? err.message : "Could not schedule the session.");
    }
  };

  if (error) return <ErrorState title="Something went wrong" description={error} />;
  if (!runs || !blueprints || !sources) return <LoadingState message="Loading exam workspace…" />;

  return (
    <div className="page-container py-10">
      <h1 className="text-2xl font-semibold">Exam workspace</h1>

      <section className="card mt-6 p-5">
        <h2 className="text-lg font-semibold">Reference materials</h2>
        <p className="mt-1 text-sm text-[var(--text-muted)]">
          Course notes, slides, or documentation your questions will be generated from. Add a link below — it must
          be approved before it can be used for question generation.
        </p>
        <form onSubmit={handleAddSourceUrl} className="mt-3 flex gap-3">
          <input className="input-field flex-1" placeholder="https://approved-source.example.edu/notes" value={sourceUrl} onChange={(e) => setSourceUrl(e.target.value)} />
          <button className="btn-secondary" type="submit">Add reference link</button>
        </form>
        <div className="mt-4 space-y-2">
          {sources.length === 0 && <p className="text-sm text-[var(--text-muted)]">No reference materials submitted yet.</p>}
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
        <h2 className="text-lg font-semibold">Question blueprint</h2>
        <p className="mt-1 text-sm text-[var(--text-muted)]">
          Sets how many questions this exam needs. Create a blueprint, generate AI-drafted questions from your
          approved reference materials, then approve the ones you want to keep.
        </p>
        <form onSubmit={handleCreateBlueprint} className="mt-3 flex flex-wrap gap-3">
          <input className="input-field flex-1" placeholder="Blueprint title" value={blueprintTitle} onChange={(e) => setBlueprintTitle(e.target.value)} />
          <select
            className="input-field"
            aria-label="Question type"
            value={blueprintItemType}
            onChange={(e) => setBlueprintItemType(e.target.value as typeof blueprintItemType)}
          >
            <option value="single_choice">Single choice (MCQ)</option>
            <option value="multiple_select">Multiple select</option>
            <option value="short_answer">Short answer</option>
            <option value="coding">Coding</option>
          </select>
          {blueprintItemType === "coding" && (
            <select className="input-field" aria-label="Programming language" value={blueprintLanguage} onChange={(e) => setBlueprintLanguage(e.target.value)}>
              <option value="python3.11">Python 3.11</option>
              <option value="java17">Java 17</option>
              <option value="cpp17">C++ (GCC 17)</option>
            </select>
          )}
          <button className="btn-secondary" type="submit">Create blueprint (1 slot)</button>
        </form>
        <div className="mt-4 space-y-4">
          {blueprints.length === 0 && <p className="text-sm text-[var(--text-muted)]">No blueprints yet.</p>}
          {blueprints.map((bp) => (
            <div key={bp.id} className="rounded-md border border-[var(--border)] p-3">
              <div className="flex items-center justify-between">
                <h3 className="font-semibold">{bp.title}</h3>
                <button
                  className="btn-secondary"
                  disabled={generatingBlueprintId === bp.id}
                  onClick={() => handleGenerateCandidates(bp.id)}
                >
                  {generatingBlueprintId === bp.id ? "Generating…" : "Generate questions"}
                </button>
              </div>
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
                  <p className="text-xs text-[var(--text-muted)]">No questions generated for this blueprint yet.</p>
                )}
              </div>
            </div>
          ))}
        </div>
      </section>

      <section className="card mt-4 p-5">
        <h2 className="text-lg font-semibold">Finalize &amp; schedule</h2>
        <p className="mt-1 text-sm text-[var(--text-muted)]">
          Publishing locks in your approved questions as a final version. Scheduling then creates a specific
          session — with its own duration and enrolled students — using that version; scheduling never changes
          the questions themselves.
        </p>
        <div className="mt-3 flex items-center gap-3">
          <label htmlFor="slot-count" className="label mb-0">Slot count</label>
          <input id="slot-count" type="number" min={1} className="input-field w-24" value={slotCount} onChange={(e) => setSlotCount(Number(e.target.value))} />
          <button className="btn-secondary" onClick={handlePublishRevision}>Publish revision</button>
        </div>

        <form onSubmit={handleScheduleRun} className="mt-4 space-y-3">
          <div className="flex flex-wrap items-center gap-3">
            <label htmlFor="duration-minutes" className="label mb-0">Duration (minutes)</label>
            <input
              id="duration-minutes"
              type="number"
              min={5}
              max={480}
              className="input-field w-28"
              value={durationMinutes}
              onChange={(e) => setDurationMinutes(Number(e.target.value))}
            />
          </div>
          {definition?.institution_id && (
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={enrollWholeRoster} onChange={(e) => setEnrollWholeRoster(e.target.checked)} />
              Enrol every active student on the institution roster
            </label>
          )}
          <textarea
            className="input-field w-full"
            rows={3}
            aria-label={definition?.institution_id ? "Student college IDs" : "Enrolled student UIDs"}
            placeholder={
              definition?.institution_id
                ? "Or list specific college IDs \u2014 separated by commas, spaces or new lines"
                : "Enrolled student UIDs, comma-separated"
            }
            value={enrolledText}
            onChange={(e) => setEnrolledText(e.target.value)}
          />
          {scheduleError && (
            <p role="alert" className="text-sm text-[var(--error)]">
              {scheduleError}
            </p>
          )}
          <button className="btn-primary" type="submit">Schedule session</button>
        </form>
      </section>

      <section className="card mt-4 p-5">
        <h2 className="text-lg font-semibold">Scheduled sessions</h2>
        <p className="mt-1 text-sm text-[var(--text-muted)]">
          Each session is a specific sitting of this exam for one group of students — with its own lobby, live
          monitoring, and results.
        </p>
        <div className="mt-3 space-y-2">
          {runs.length === 0 && <p className="text-sm text-[var(--text-muted)]">No sessions scheduled yet.</p>}
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
