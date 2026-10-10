"use client";

import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

import { ExamProtectedRoute } from "@/components/exams/ExamProtectedRoute";
import { ExamShellHeader } from "@/components/exams/ExamShellHeader";
import { LazyCodeEditorPanel } from "@/components/exams/LazyCodeEditorPanel";
import { QuestionNavigator } from "@/components/exams/QuestionNavigator";
import { ErrorState, LoadingState } from "@/components/States";
import { useExamAuth } from "@/lib/examAuth";
import { describeConflict } from "@/lib/examConflicts";
import { examEndpoints, type Attempt, type PublicQuestion, type ReleasedSlots } from "@/lib/examEndpoints";
import { useLiveSocket } from "@/lib/liveSocket";
import { useProctoringObservations } from "@/lib/proctoringObservations";
import { remainingMs } from "@/lib/serverClock";
import { useCameraCapture } from "@/lib/useCameraCapture";
import { useExamServiceWorker } from "@/lib/useExamServiceWorker";
import { timeWarningMessage, useServerClock } from "@/lib/useServerClock";

const AUTOSAVE_DEBOUNCE_MS = 1200;
const DEFAULT_TIME_WARNINGS = [10];
const FINALIZING_POLL_MS = 3000;

// Visible, on-screen feedback for detections the backend already logs
// silently — the ledger/warning counter existed before, but nothing told
// the student in the moment that leaving the tab or exiting fullscreen had
// just been recorded.
const LIVE_WARNING_MESSAGES: Partial<Record<string, string>> = {
  page_hidden: "You left this tab or window — this has been logged.",
  blur: "This window lost focus — this has been logged.",
  fullscreen_exit: "You exited fullscreen — this has been logged.",
  clipboard_blocked: "Copy/paste is disabled during this exam.",
};

function ExamContent() {
  const { runId } = useParams<{ runId: string }>();
  const router = useRouter();
  const { getToken } = useExamAuth();

  const [attempt, setAttempt] = useState<Attempt | null>(null);
  const [question, setQuestion] = useState<PublicQuestion | null>(null);
  const [draftVersion, setDraftVersion] = useState(0);
  const [answer, setAnswer] = useState<Record<string, unknown>>({});
  const [error, setError] = useState<string | null>(null);
  const [saveReceipt, setSaveReceipt] = useState<{ savedAt: string; version: number } | null>(null);
  const [showSubmitConfirm, setShowSubmitConfirm] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [publicRunStatus, setPublicRunStatus] = useState<string | null>(null);
  const [publicRunResults, setPublicRunResults] = useState<unknown[]>([]);
  const [publicRunMessage, setPublicRunMessage] = useState<string | null>(null);
  const [cameraWaived, setCameraWaived] = useState(false);
  const [codingTab, setCodingTab] = useState<"testcase" | "result">("testcase");
  const [liveWarning, setLiveWarning] = useState<string | null>(null);
  const [fullscreenSupported, setFullscreenSupported] = useState(false);
  const [fullscreenActive, setFullscreenActive] = useState(false);
  const [slots, setSlots] = useState<ReleasedSlots | null>(null);
  const [viewIndex, setViewIndex] = useState<number | null>(null); // null = follow the current (released) question
  const [markedForReview, setMarkedForReview] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const draftVersionsRef = useRef<Record<string, number>>({});
  const saveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const redirectedRef = useRef(false);
  const warningTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const viewIndexRef = useRef<number | null>(null);
  const releasedIndexRef = useRef(0);
  const { clock, tick } = useServerClock();
  void tick; // re-render every second so the timer advances

  const sampleClock = useCallback(
    (serverTime: string | undefined, startedAt: number) => {
      if (serverTime) clock.sample(serverTime, performance.now() - startedAt);
    },
    [clock]
  );

  const syncState = useCallback(async () => {
    const token = await getToken();
    const startedAt = performance.now();
    const state = await examEndpoints.getState(token, runId);
    sampleClock(state.server_time, startedAt);
    setAttempt(state);
    releasedIndexRef.current = state.current_slot_index;
    return state;
  }, [getToken, runId, sampleClock]);

  const refreshSlots = useCallback(async () => {
    try {
      const token = await getToken();
      setSlots(await examEndpoints.listReleasedSlots(token, runId));
    } catch {
      // the navigator is a convenience; the question and save paths do not depend on it
    }
  }, [getToken, runId]);

  const loadQuestion = useCallback(
    async (target?: number | null) => {
      const token = await getToken();
      const wanted = target === undefined ? viewIndexRef.current : target;
      const showCurrent = wanted === null || wanted >= releasedIndexRef.current;
      const q = showCurrent
        ? await examEndpoints.getCurrentQuestion(token, runId)
        : await examEndpoints.getReleasedQuestion(token, runId, wanted);
      setQuestion(q);
      const existing = await examEndpoints.getMyDraft(token, runId, q.question_id);
      setDraftVersion(existing.version);
      setMarkedForReview(Boolean(existing.marked_for_review));
      const existingAnswer = existing.answer ?? {};
      // The language <select> only fires onChange when the student actually
      // picks a DIFFERENT option — with just one permitted language (the
      // common case) there is nothing to change, so answer.language never
      // gets written even though the select visually shows it selected.
      // "Run public tests"/submit then silently sent language: "" (found
      // live: a real runtime_not_allowed 400 on a brand new, untouched coding
      // attempt). Seed it from the question itself, not just the select's
      // display-only fallback.
      if (q.public_content.item_type === "coding" && !existingAnswer.language) {
        const permitted = q.public_content.permitted_languages as string[] | undefined;
        if (permitted && permitted.length > 0) {
          existingAnswer.language = permitted[0];
        }
      }
      setAnswer(existingAnswer);
      draftVersionsRef.current[q.question_id] = existing.version;
    },
    [getToken, runId]
  );

  const refresh = useCallback(async () => {
    try {
      const token = await getToken();
      const state = await syncState();
      const versions = await examEndpoints.listMyDraftVersions(token, runId);
      draftVersionsRef.current = versions.versions;
      if (state.status === "in_progress") {
        await loadQuestion();
        await refreshSlots();
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load your exam state.");
    }
  }, [getToken, loadQuestion, refreshSlots, runId, syncState]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    getToken()
      .then((token) => examEndpoints.getMyAccommodations(token, runId))
      .then((accommodations) => setCameraWaived(accommodations.camera_waived))
      .catch(() => undefined); // accommodations are an enhancement, never a blocker
  }, [getToken, runId]);

  const { health } = useLiveSocket({ runId, getToken, onReconcile: refresh });

  const handleObservation = useCallback((kind: string) => {
    const message = LIVE_WARNING_MESSAGES[kind];
    if (!message) return;
    setLiveWarning(message);
    if (warningTimerRef.current) clearTimeout(warningTimerRef.current);
    warningTimerRef.current = setTimeout(() => setLiveWarning(null), 5000);
  }, []);
  const { report: reportObservation } = useProctoringObservations(
    attempt?.status === "in_progress" ? runId : null,
    getToken,
    handleObservation
  );
  const { state: cameraState, captureNow } = useCameraCapture(
    attempt?.status === "in_progress" ? runId : null,
    getToken,
    cameraWaived
  );
  // Hold back any service-worker update from the first render (the attempt is not loaded yet, so "unknown" counts as
  // active) until the attempt is known to be over; a blocked attempt can still resume, so it is held too.
  useExamServiceWorker({ attemptActive: attempt === null || attempt.status === "in_progress" || attempt.status === "blocked" });

  // Capability-aware fullscreen tracking — mirrors the preflight page's own
  // check; re-entry always requires a real user gesture (the Fullscreen API
  // forbids triggering it from arbitrary code), so this can only ever offer
  // a button, never silently force the student back in.
  useEffect(() => {
    setFullscreenSupported(typeof document.fullscreenEnabled === "boolean" && document.fullscreenEnabled);
    const onChange = () => setFullscreenActive(Boolean(document.fullscreenElement));
    onChange();
    document.addEventListener("fullscreenchange", onChange);
    return () => document.removeEventListener("fullscreenchange", onChange);
  }, []);

  const handleResumeFullscreen = useCallback(async () => {
    try {
      await document.documentElement.requestFullscreen();
    } catch {
      // Denied/unsupported — the resume banner simply stays visible.
    }
  }, []);

  // Defense-in-depth clipboard block for the rest of the page (the code
  // editor already blocks its own copy/cut/paste separately) — question
  // prompts, short-answer text, MCQ option labels.
  const blockPageClipboard = useCallback(
    (event: React.ClipboardEvent) => {
      if ((event.target as HTMLElement | null)?.closest(".cm-editor")) return; // handled there already
      event.preventDefault();
      reportObservation("clipboard_blocked");
    },
    [reportObservation]
  );

  useEffect(() => {
    if (!attempt || redirectedRef.current) return;
    if (attempt.status === "blocked") {
      redirectedRef.current = true;
      router.push(`/exams/student/${runId}/blocked`);
    } else if (attempt.status === "submitted" || attempt.status === "finalized" || attempt.status === "expired") {
      redirectedRef.current = true;
      router.push(`/exams/student/${runId}/receipt`);
    } else if (attempt.status === "admitted") {
      redirectedRef.current = true;
      router.push(`/exams/student/${runId}/waiting`);
    }
  }, [attempt, router, runId]);

  const persistDraft = useCallback(
    async (nextAnswer: Record<string, unknown>, marked?: boolean) => {
      if (!question) return;
      // One id per save: if the response is lost the retry below is recognised by the server and
      // acknowledged without creating another version.
      const clientSaveId = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`;
      for (let attemptNo = 0; attemptNo < 2; attemptNo += 1) {
        try {
          const token = await getToken();
          const result = await examEndpoints.saveDraft(token, runId, question.question_id, draftVersion, nextAnswer, {
            clientEpoch: attempt?.epoch,
            clientSaveId,
            markedForReview: marked,
          });
          setDraftVersion(result.version);
          draftVersionsRef.current[question.question_id] = result.version;
          setSaveReceipt({ savedAt: new Date().toISOString(), version: result.version });
          void refreshSlots();
          return;
        } catch {
          // fall through: retry once with the same id, then reconcile
        }
      }
      // A stale-version conflict here means another tab/device saved more
      // recently — reconcile from the server rather than overwrite it.
      await loadQuestion();
    },
    [attempt?.epoch, draftVersion, getToken, loadQuestion, question, refreshSlots, runId]
  );

  const remaining = remainingMs(attempt?.deadline, clock.now());
  const timeUp = attempt?.status === "in_progress" && remaining === 0;
  const timeWarning = timeUp ? null : timeWarningMessage(remaining, attempt?.time_warning_minutes ?? DEFAULT_TIME_WARNINGS);

  // The server seals the attempt at the deadline (sweeper); this only waits for it and then follows the status.
  useEffect(() => {
    if (!timeUp) return undefined;
    const id = setInterval(() => {
      syncState().catch(() => undefined);
    }, FINALIZING_POLL_MS);
    return () => clearInterval(id);
  }, [syncState, timeUp]);

  const updateAnswer = useCallback(
    (next: Record<string, unknown>) => {
      if (timeUp) return;
      setAnswer(next);
      if (saveTimerRef.current) clearTimeout(saveTimerRef.current);
      saveTimerRef.current = setTimeout(() => persistDraft(next), AUTOSAVE_DEBOUNCE_MS);
    },
    [persistDraft, timeUp]
  );

  const flushPendingSave = useCallback(async () => {
    if (!saveTimerRef.current) return;
    clearTimeout(saveTimerRef.current);
    saveTimerRef.current = null;
    await persistDraft(answer);
  }, [answer, persistDraft]);

  const toggleMarked = useCallback(async () => {
    if (timeUp) return;
    const next = !markedForReview;
    setMarkedForReview(next);
    if (saveTimerRef.current) {
      clearTimeout(saveTimerRef.current);
      saveTimerRef.current = null;
    }
    await persistDraft(answer, next);
  }, [answer, markedForReview, persistDraft, timeUp]);

  const goToSlot = useCallback(
    async (index: number) => {
      await flushPendingSave();
      viewIndexRef.current = index >= releasedIndexRef.current ? null : index;
      setViewIndex(viewIndexRef.current);
      setPublicRunStatus(null);
      setPublicRunResults([]);
      setPublicRunMessage(null);
      try {
        await loadQuestion(viewIndexRef.current);
        setNotice(`Question ${index + 1}`);
      } catch (err) {
        setNotice(err instanceof Error ? err.message : "That question is not available.");
      }
    },
    [flushPendingSave, loadQuestion]
  );

  const handleNextQuestion = useCallback(async () => {
    await flushPendingSave();
    const current = viewIndexRef.current ?? releasedIndexRef.current;
    if (current < releasedIndexRef.current) {
      await goToSlot(current + 1);
      return;
    }
    const token = await getToken();
    await examEndpoints.advanceToNextQuestion(token, runId);
    viewIndexRef.current = null;
    setViewIndex(null);
    setPublicRunStatus(null);
    setPublicRunResults([]);
    setPublicRunMessage(null);
    await refresh();
  }, [flushPendingSave, getToken, goToSlot, refresh, runId]);

  const handleRunPublicTests = useCallback(async () => {
    if (!question) return;
    const token = await getToken();
    const languageKey = String(answer.language ?? "");
    const sourceCode = String(answer.source_code ?? "");
    setCodingTab("result");
    setPublicRunMessage(null);
    let submission_id: string;
    try {
      ({ submission_id } = await examEndpoints.publicRun(token, runId, question.question_id, languageKey, sourceCode));
    } catch (err) {
      setPublicRunStatus(null);
      setPublicRunResults([]);
      setPublicRunMessage(describeConflict(err, "The code runner could not be reached. Your code was not run."));
      return;
    }
    setPublicRunStatus("processing");
    const poll = async (attemptsLeft: number) => {
      if (attemptsLeft <= 0) {
        setPublicRunStatus(null);
        setPublicRunMessage("The code runner is taking longer than usual. Your code may still be running - try again in a moment.");
        return;
      }
      const result = await examEndpoints.getPublicRun(token, submission_id, runId, question.question_id);
      if (result.status === "succeeded") {
        setPublicRunStatus(result.status);
        setPublicRunResults(result.results);
      } else if (result.status === "infra_error" || result.status === "infra_exhausted") {
        setPublicRunStatus(null);
        setPublicRunMessage("The code runner had a problem, so your code was not run and nothing was marked. Please try again.");
      } else {
        setPublicRunStatus(result.status);
        setTimeout(() => poll(attemptsLeft - 1), 1500);
      }
    };
    poll(10);
  }, [answer, getToken, question, runId]);

  const handleSubmitExam = useCallback(async () => {
    setSubmitting(true);
    // Flush any pending debounced autosave first — otherwise a save still in
    // flight can race the submit's expected_draft_versions snapshot and
    // spuriously trigger a draft_version_conflict on a perfectly normal
    // "pick an answer, then submit quickly" sequence.
    if (saveTimerRef.current) {
      clearTimeout(saveTimerRef.current);
      saveTimerRef.current = null;
      await persistDraft(answer);
    }
    // Best-effort only — a failed/denied capture never blocks submission
    // (contract: "Submission acceptance cannot depend on a successful image upload.").
    void captureNow("submission").catch(() => undefined);
    try {
      const token = await getToken();
      await examEndpoints.submit(token, runId, `submit-${runId}-${attempt?.uid ?? ""}`, attempt?.epoch ?? 0, draftVersionsRef.current);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Submission failed — your drafts are still saved.");
    } finally {
      setSubmitting(false);
      setShowSubmitConfirm(false);
    }
  }, [answer, attempt, captureNow, getToken, persistDraft, refresh, runId]);

  if (error) return <ErrorState title="Something went wrong" description={error} />;
  if (!attempt || !question) return <LoadingState message="Loading your question…" />;

  const itemType = question.public_content.item_type as string | undefined;
  const isLastSlot = question.slot_index + 1 >= question.total_slots;
  const releasedIndex = attempt.current_slot_index;
  const viewing = viewIndex ?? releasedIndex;
  const revisitEnabled = slots?.revisit_enabled ?? false;
  const markButton = (
    <button
      type="button"
      className={markedForReview ? "btn-primary" : "btn-secondary"}
      aria-pressed={markedForReview}
      onClick={toggleMarked}
      disabled={timeUp}
    >
      {markedForReview ? "Marked for review" : "Mark for review"}
    </button>
  );
  const publicTestGroups =
    (question.public_content.public_tests as
      | { group_name: string; tests: { stdin: string; expected_output: string }[] }[]
      | undefined) ?? [];

  return (
    <div
      className="page-container py-6"
      onCopy={blockPageClipboard}
      onCut={blockPageClipboard}
      onPaste={blockPageClipboard}
    >
      {liveWarning && (
        <div className="card-muted mb-4 border border-[var(--warning-border)] bg-[var(--warning-soft)] p-3 text-sm text-[var(--warning)]" role="status">
          {liveWarning}
        </div>
      )}
      {fullscreenSupported && !fullscreenActive && (
        <div className="card-muted mb-4 flex items-center justify-between gap-3 border border-[var(--warning-border)] bg-[var(--warning-soft)] p-3 text-sm" role="status">
          <span className="text-[var(--warning)]">You are not in fullscreen mode.</span>
          <button className="btn-secondary" onClick={handleResumeFullscreen}>
            Resume fullscreen
          </button>
        </div>
      )}
      <ExamShellHeader
        deadline={attempt.deadline}
        serverNowMs={clock.now()}
        connectionHealth={health}
        saveReceipt={saveReceipt}
        warningCount={attempt.warning_count}
        camera={cameraState}
      />

      {/* Always mounted so assistive tech registers the region before the first message arrives. */}
      <div role="status" aria-live="polite" aria-atomic="true" className={timeWarning && !timeUp ? "card-muted mt-4 border border-[var(--warning-border)] bg-[var(--warning-soft)] p-3 text-sm font-medium text-[var(--text)]" : "sr-only"}>
        {timeUp ? "" : timeWarning}
      </div>
      {timeUp && (
        <div role="alert" className="card-muted mt-4 border border-[var(--warning-border)] bg-[var(--warning-soft)] p-3 text-sm font-medium text-[var(--text)]">
          Time is up. Your saved answers are being submitted automatically - you can no longer change them.
        </div>
      )}
      <p className="sr-only" role="status" aria-live="polite">{notice}</p>

      <div className="mt-6 grid gap-4 lg:grid-cols-[minmax(0,1fr)_16rem] lg:items-start">
      <div className="min-w-0">
      {itemType !== "coding" && (
        <div className="card p-6">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="badge-primary badge">
              Question {question.slot_index + 1} of {question.total_slots}
            </span>
            <div className="flex items-center gap-2">
              <span className="badge">{itemType}</span>
              {markButton}
            </div>
          </div>

          <p className="mt-4 whitespace-pre-wrap text-[var(--text)]">{String(question.public_content.prompt ?? "")}</p>

          {(itemType === "single_choice" || itemType === "multiple_select") && (
            <fieldset className="mt-4 space-y-2">
              <legend className="label">Choose your answer</legend>
              {(question.public_content.options as { key: string; text: string }[] | undefined ?? []).map((opt) => (
                <label key={opt.key} className="flex items-center gap-2 text-sm">
                  <input
                    type={itemType === "single_choice" ? "radio" : "checkbox"}
                    name="mcq-option"
                    disabled={timeUp}
                    checked={
                      itemType === "single_choice"
                        ? answer.selected === opt.key
                        : Array.isArray(answer.selected) && (answer.selected as string[]).includes(opt.key)
                    }
                    onChange={() => {
                      if (itemType === "single_choice") {
                        updateAnswer({ selected: opt.key });
                      } else {
                        const current = Array.isArray(answer.selected) ? (answer.selected as string[]) : [];
                        const next = current.includes(opt.key) ? current.filter((k) => k !== opt.key) : [...current, opt.key];
                        updateAnswer({ selected: next });
                      }
                    }}
                  />
                  {opt.text}
                </label>
              ))}
            </fieldset>
          )}

          {itemType === "short_answer" && (
            <textarea
              className="input-field mt-4 w-full"
              style={{ minHeight: 160 }}
              aria-label="Your answer"
              readOnly={timeUp}
              value={String(answer.text ?? "")}
              onChange={(e) => updateAnswer({ text: e.target.value })}
            />
          )}
        </div>
      )}

      {itemType === "coding" && (
        <div className="grid gap-4 lg:grid-cols-2 lg:items-start">
          <div className="card p-6 lg:sticky lg:top-4 lg:max-h-[calc(100vh-7rem)] lg:overflow-y-auto">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="badge-primary badge">
                Question {question.slot_index + 1} of {question.total_slots}
              </span>
              <div className="flex items-center gap-2">
                <span className="badge">{itemType}</span>
                {markButton}
              </div>
            </div>
            <p className="mt-4 whitespace-pre-wrap text-[var(--text)]">{String(question.public_content.prompt ?? "")}</p>
          </div>

          <div className="flex flex-col gap-3">
            <div className="card flex flex-wrap items-center justify-between gap-3 p-3">
              <div className="flex items-center gap-2">
                <label className="label mb-0" htmlFor="language-select">
                  Language
                </label>
                <select
                  id="language-select"
                  className="input-field"
                  value={String(answer.language ?? (question.public_content.permitted_languages as string[] | undefined)?.[0] ?? "")}
                  onChange={(e) => updateAnswer({ ...answer, language: e.target.value })}
                >
                  {(question.public_content.permitted_languages as string[] | undefined ?? []).map((lang) => (
                    <option key={lang} value={lang}>
                      {lang}
                    </option>
                  ))}
                </select>
              </div>
              <button className="btn-secondary" onClick={handleRunPublicTests} disabled={publicRunStatus === "processing"}>
                {publicRunStatus === "processing" ? "Running…" : "Run"}
              </button>
            </div>

            <LazyCodeEditorPanel
              value={String(answer.source_code ?? "")}
              onChange={(code) => updateAnswer({ ...answer, source_code: code })}
              language={String(answer.language ?? "")}
              onClipboardBlocked={() => reportObservation("clipboard_blocked")}
            />

            <div className="card overflow-hidden p-0">
              <div className="flex border-b border-[var(--border)]" role="tablist">
                <button
                  role="tab"
                  aria-selected={codingTab === "testcase"}
                  className={`px-4 py-2 text-sm font-medium ${codingTab === "testcase" ? "border-b-2 border-[var(--primary)] text-[var(--text)]" : "text-[var(--text-muted)]"}`}
                  onClick={() => setCodingTab("testcase")}
                >
                  Testcase
                </button>
                <button
                  role="tab"
                  aria-selected={codingTab === "result"}
                  className={`px-4 py-2 text-sm font-medium ${codingTab === "result" ? "border-b-2 border-[var(--primary)] text-[var(--text)]" : "text-[var(--text-muted)]"}`}
                  onClick={() => setCodingTab("result")}
                >
                  Test Result
                </button>
              </div>

              <div className="p-3 text-sm">
                {codingTab === "testcase" &&
                  (publicTestGroups.length === 0 ? (
                    <p className="text-[var(--text-muted)]">No sample test cases provided for this question.</p>
                  ) : (
                    <div className="space-y-3">
                      {publicTestGroups.map((group, gi) =>
                        group.tests.map((t, ti) => (
                          <div key={`${gi}-${ti}`} className="card-muted p-3">
                            <p className="text-xs text-[var(--text-muted)]">{group.group_name}</p>
                            <p className="mt-1 font-mono">Input: {t.stdin || "(empty)"}</p>
                            <p className="font-mono">Expected: {t.expected_output}</p>
                          </div>
                        ))
                      )}
                    </div>
                  ))}

                {codingTab === "result" &&
                  (publicRunStatus === "processing" ? (
                    <p className="text-[var(--text-muted)]">Running…</p>
                  ) : publicRunMessage ? (
                    <p role="alert" className="text-[var(--error)]">{publicRunMessage}</p>
                  ) : publicRunResults.length === 0 ? (
                    <p className="text-[var(--text-muted)]">Run your code to see results here.</p>
                  ) : (
                    publicRunResults.map((r, i) => {
                      const result = r as { test_id: string; outcome: string; stderr?: string };
                      return (
                        <div key={i}>
                          <p>
                            {result.test_id}:{" "}
                            <span className={result.outcome === "passed" ? "text-[var(--success)]" : "text-[var(--error)]"}>
                              {result.outcome}
                            </span>
                          </p>
                          {result.stderr && result.outcome !== "passed" && (
                            <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap font-mono text-xs">{result.stderr}</pre>
                          )}
                        </div>
                      );
                    })
                  ))}
              </div>
            </div>
          </div>
        </div>
      )}

      <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
        <button
          className="btn-secondary"
          onClick={() => goToSlot(viewing - 1)}
          disabled={timeUp || !revisitEnabled || viewing <= 0}
          title={!revisitEnabled ? "Earlier questions can't be reopened in this exam." : undefined}
        >
          Previous question
        </button>
        <div className="flex gap-3">
          {!isLastSlot && (
            <button className="btn-primary" onClick={handleNextQuestion} disabled={timeUp}>
              Next question
            </button>
          )}
          {isLastSlot && (
            <button className="btn-primary" onClick={() => setShowSubmitConfirm(true)} disabled={timeUp}>
              Submit exam
            </button>
          )}
        </div>
      </div>
      <p className="mt-2 text-right text-xs text-[var(--text-secondary)]">
        {saveReceipt ? `Your answer is saved as a draft (v${saveReceipt.version}). ` : ""}
        Saving a draft is not submitting: your answers count only after you submit, or when time runs out.
        {viewing >= releasedIndex && !isLastSlot ? " Moving to the next question releases it and cannot be undone." : ""}
      </p>
      </div>

      <aside className="lg:sticky lg:top-4">
        <QuestionNavigator
          totalSlots={question.total_slots}
          slots={slots?.slots ?? []}
          viewIndex={viewing}
          releasedIndex={releasedIndex}
          revisitEnabled={revisitEnabled}
          disabled={timeUp}
          onSelect={goToSlot}
        />
      </aside>
      </div>

      {showSubmitConfirm && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" role="dialog" aria-modal="true" aria-label="Confirm submission">
          <div className="card max-w-md p-6">
            <h2 className="text-lg font-semibold">Submit your exam?</h2>
            <p className="mt-2 text-sm text-[var(--text-secondary)]">
              This is irreversible. Once submitted, you cannot change any of your answers, and this action cannot be undone.
            </p>
            <div className="mt-5 flex justify-end gap-3">
              <button className="btn-secondary" onClick={() => setShowSubmitConfirm(false)} disabled={submitting}>
                Cancel
              </button>
              <button className="btn-primary" onClick={handleSubmitExam} disabled={submitting}>
                {submitting ? "Submitting…" : "Yes, submit"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default function ExamPage() {
  return (
    <ExamProtectedRoute requireRole="student">
      <ExamContent />
    </ExamProtectedRoute>
  );
}
