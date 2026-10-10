"use client";

import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import Link from "next/link";

import { ExamProtectedRoute } from "@/components/exams/ExamProtectedRoute";
import { BlueprintPanel } from "@/components/exams/BlueprintPanel";
import { SourceUploadPanel } from "@/components/exams/SourceUploadPanel";
import { ErrorState, LoadingState } from "@/components/States";
import { useExamAuth } from "@/lib/examAuth";
import { examApiFetch } from "@/lib/examsApi";
import { examEndpoints, type ApprovedSourceSummary, type ExamRunSummary } from "@/lib/examEndpoints";

type Blueprint = { id: string; title: string; slots: unknown[] };

function DefinitionDetail() {
  const { definitionId } = useParams<{ definitionId: string }>();
  const { getToken } = useExamAuth();

  const [runs, setRuns] = useState<ExamRunSummary[] | null>(null);
  const [blueprints, setBlueprints] = useState<Blueprint[] | null>(null);
  const [sources, setSources] = useState<ApprovedSourceSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [sourceUrl, setSourceUrl] = useState("");
  const [blueprintTitle, setBlueprintTitle] = useState("");
  const [blueprintItemType, setBlueprintItemType] = useState<"single_choice" | "multiple_select" | "short_answer" | "coding">("single_choice");
  const [blueprintLanguage, setBlueprintLanguage] = useState("python3.11");
  const [questionCount, setQuestionCount] = useState(1);
  const [variantsPerQuestion, setVariantsPerQuestion] = useState(4);
  const [shuffleOptions, setShuffleOptions] = useState(false);
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
          spec: {
            question_count: questionCount,
            type_counts: { [blueprintItemType]: questionCount },
            difficulty_distribution: { medium: 100 },
            languages: blueprintItemType === "coding" ? [blueprintLanguage] : [],
            variants_per_slot: variantsPerQuestion,
          },
          shuffle_options: shuffleOptions,
        }),
      },
      token
    );
    setBlueprintTitle("");
    await load();
  };

  const handleAssignVariants = async (runId: string) => {
    try {
      const token = await getToken();
      const report = await examEndpoints.assignRunVariants(token, runId);
      const blocked = Object.keys(report.blocked_slots).length;
      setNotice(
        blocked > 0
          ? `Assigned ${report.created} question(s); ${blocked} slot(s) could not be assigned: ${Object.values(report.blocked_slots)[0]}.`
          : `Assigned ${report.created} question(s) before the exam starts (${report.already_assigned} were already assigned).`
      );
    } catch (err) {
      setNotice(err instanceof Error ? err.message : "Could not assign variants.");
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
      {notice && <p role="status" className="mt-2 text-sm">{notice}</p>}

      <section className="card mt-6 p-5">
        <h2 className="text-lg font-semibold">Reference materials</h2>
        <p className="mt-1 text-sm text-[var(--text-muted)]">
          Course notes, slides, or documentation your questions will be generated from. Upload documents or add a link
          below — each one must be approved before it can be used for question generation.
        </p>
        <SourceUploadPanel getToken={getToken} onSourcesChanged={load} />
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
          <button className="btn-secondary" type="submit">Create blueprint</button>
        </form>
        <div className="mt-2 flex flex-wrap items-center gap-3 text-sm">
          <label className="flex items-center gap-2">
            Questions
            <input type="number" min={1} max={200} className="input-field w-20" aria-label="Number of questions" value={questionCount} onChange={(e) => setQuestionCount(Math.max(1, Number(e.target.value) || 1))} />
          </label>
          <label className="flex items-center gap-2">
            Variants per question
            <input type="number" min={1} max={50} className="input-field w-20" aria-label="Variants per question" value={variantsPerQuestion} onChange={(e) => setVariantsPerQuestion(Math.max(1, Number(e.target.value) || 1))} />
          </label>
          <label className="flex items-center gap-2">
            <input type="checkbox" checked={shuffleOptions} onChange={(e) => setShuffleOptions(e.target.checked)} />
            Shuffle answer options per student
          </label>
        </div>
        <div className="mt-4 space-y-4">
          {blueprints.length === 0 && <p className="text-sm text-[var(--text-muted)]">No blueprints yet.</p>}
          {blueprints.map((bp) => (
            <BlueprintPanel key={bp.id} definitionId={definitionId} blueprint={bp} sources={sources} getToken={getToken} onChanged={load} />
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

        <form onSubmit={handleScheduleRun} className="mt-4 flex gap-3">
          <input
            className="input-field flex-1"
            placeholder="Enrolled student UIDs, comma-separated"
            value={enrolledUidsText}
            onChange={(e) => setEnrolledUidsText(e.target.value)}
          />
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
                <button className="btn-secondary" onClick={() => handleAssignVariants(run.id)}>Assign variants</button>
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
