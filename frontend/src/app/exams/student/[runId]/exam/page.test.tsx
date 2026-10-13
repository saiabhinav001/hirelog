import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.hoisted(() => {
  process.env.NEXT_PUBLIC_EXAMS_ENABLED = "true";
});

const getStateMock = vi.fn();
const getCurrentQuestionMock = vi.fn();
const getReleasedQuestionMock = vi.fn();
const listReleasedSlotsMock = vi.fn();
const getMyDraftMock = vi.fn();
const listMyDraftVersionsMock = vi.fn();
const submitMock = vi.fn();
const saveDraftMock = vi.fn();
const advanceMock = vi.fn();
const publicRunMock = vi.fn();
const getPublicRunMock = vi.fn();
const pushMock = vi.fn();
const getMyAccommodationsMock = vi.fn().mockResolvedValue({ extra_minutes: 0, camera_waived: false });
const getToken = async () => "token";

vi.mock("next/navigation", () => ({
  useParams: () => ({ runId: "run-1" }),
  useRouter: () => ({ push: pushMock, replace: vi.fn() }),
}));

vi.mock("@/lib/examAuth", () => ({
  useExamAuth: () => ({ user: { uid: "u1" }, loading: false, isFaculty: false, isStudent: true, getToken }),
}));

const liveSocket = vi.hoisted(() => ({ onPoll: undefined as undefined | (() => Promise<void>) }));

vi.mock("@/lib/liveSocket", () => ({
  useLiveSocket: (options: { onPoll?: () => Promise<void> }) => {
    liveSocket.onPoll = options.onPoll;
    return { health: "connected", lastEventAt: null };
  },
}));

vi.mock("@/lib/examEndpoints", () => ({
  examEndpoints: {
    getState: (...args: unknown[]) => getStateMock(...args),
    getCurrentQuestion: (...args: unknown[]) => getCurrentQuestionMock(...args),
    getReleasedQuestion: (...args: unknown[]) => getReleasedQuestionMock(...args),
    listReleasedSlots: (...args: unknown[]) => listReleasedSlotsMock(...args),
    getMyDraft: (...args: unknown[]) => getMyDraftMock(...args),
    listMyDraftVersions: (...args: unknown[]) => listMyDraftVersionsMock(...args),
    submit: (...args: unknown[]) => submitMock(...args),
    saveDraft: (...args: unknown[]) => saveDraftMock(...args),
    advanceToNextQuestion: (...args: unknown[]) => advanceMock(...args),
    publicRun: (...args: unknown[]) => publicRunMock(...args),
    getPublicRun: (...args: unknown[]) => getPublicRunMock(...args),
    getMyAccommodations: (...args: unknown[]) => getMyAccommodationsMock(...args),
  },
}));

import ExamPage from "@/app/exams/student/[runId]/exam/page";
import { ExamApiError } from "@/lib/examsApi";

const SERVER_NOW = "2026-06-01T10:00:00Z";

function state(overrides: Record<string, unknown> = {}) {
  return {
    run_id: "run-1", uid: "u1", status: "in_progress", version: 1, epoch: 1,
    deadline: "2026-06-01T11:00:00Z", warning_count: 0, current_slot_index: 0, submit_receipt: null,
    server_time: SERVER_NOW, time_warning_minutes: [10], ...overrides,
  };
}

function slotsFor(current: number, total: number, extra: Record<number, { answered?: boolean; marked?: boolean }> = {}, revisit = false) {
  return {
    current_slot_index: current, total_slots: total, locked_count: total - current - 1, revisit_enabled: revisit, server_time: SERVER_NOW,
    slots: Array.from({ length: current + 1 }, (_, i) => ({
      slot_index: i, question_id: `rev-1:slot:${i}`, answered: Boolean(extra[i]?.answered), marked_for_review: Boolean(extra[i]?.marked), version: extra[i]?.answered ? 1 : 0,
    })),
  };
}

function question(index: number, total: number, prompt = `Prompt ${index + 1}`) {
  return { question_id: `rev-1:slot:${index}`, slot_index: index, total_slots: total, public_content: { item_type: "short_answer", prompt } };
}

function mockSingle(stateOverrides: Record<string, unknown> = {}, total = 1) {
  getStateMock.mockResolvedValue(state(stateOverrides));
  getCurrentQuestionMock.mockResolvedValue(question(0, total, "Explain recursion."));
  getMyDraftMock.mockResolvedValue({ version: 0, answer: null });
  listMyDraftVersionsMock.mockResolvedValue({ versions: {} });
  listReleasedSlotsMock.mockResolvedValue(slotsFor(0, total));
}

beforeEach(() => {
  [getStateMock, getCurrentQuestionMock, getReleasedQuestionMock, listReleasedSlotsMock, getMyDraftMock, listMyDraftVersionsMock, submitMock, saveDraftMock, advanceMock, pushMock, publicRunMock, getPublicRunMock]
    .forEach((m) => m.mockReset());
  getMyAccommodationsMock.mockReset().mockResolvedValue({ extra_minutes: 0, camera_waived: false });
});

afterEach(() => vi.useRealTimers());

describe("Student exam page — submit confirmation", () => {
  it("shows an irreversible-submit confirmation dialog before actually submitting", async () => {
    mockSingle();
    render(<ExamPage />);
    await userEvent.click(await screen.findByRole("button", { name: "Submit exam" }));
    expect(await screen.findByRole("dialog", { name: "Confirm submission" })).toBeInTheDocument();
    expect(screen.getByText(/irreversible/i)).toBeInTheDocument();
    expect(submitMock).not.toHaveBeenCalled();
  });

  it("only calls the submit endpoint after explicit confirmation", async () => {
    mockSingle();
    submitMock.mockResolvedValue({ accepted: true, receipt: { receipt_id: "r1" } });
    render(<ExamPage />);
    await userEvent.click(await screen.findByRole("button", { name: "Submit exam" }));
    await userEvent.click(await screen.findByRole("button", { name: "Yes, submit" }));
    await waitFor(() => expect(submitMock).toHaveBeenCalledTimes(1));
  });

  it("fetches the student's own accommodations (never another student's)", async () => {
    mockSingle();
    render(<ExamPage />);
    await screen.findByRole("button", { name: "Submit exam" });
    await waitFor(() => expect(getMyAccommodationsMock).toHaveBeenCalledWith("token", "run-1"));
  });

  it("does not show a submit button before the last question", async () => {
    mockSingle({}, 3);
    render(<ExamPage />);
    await screen.findByText("Explain recursion.");
    expect(screen.queryByRole("button", { name: "Submit exam" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Next question" })).toBeInTheDocument();
  });

  it("retries a failed autosave once with the same save id and sends the attempt epoch", async () => {
    mockSingle();
    saveDraftMock.mockRejectedValueOnce(new Error("network")).mockResolvedValueOnce({ accepted: true, version: 1 });
    render(<ExamPage />);
    await userEvent.type(await screen.findByRole("textbox", { name: "Your answer" }), "x");
    await waitFor(() => expect(saveDraftMock).toHaveBeenCalledTimes(2), { timeout: 4000 });
    const [first, second] = saveDraftMock.mock.calls;
    expect(first[5].clientSaveId).toBeTruthy();
    expect(second[5]).toEqual(first[5]);
    expect(first[5].clientEpoch).toBe(1);
  });

  it("saves while typing never pauses once the maximum wait has passed", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mockSingle();
    saveDraftMock.mockResolvedValue({ accepted: true, version: 1 });
    render(<ExamPage />);
    const box = await screen.findByRole("textbox", { name: "Your answer" });
    for (let i = 1; i <= 8; i += 1) {
      fireEvent.change(box, { target: { value: "a".repeat(i) } });
      await act(async () => { await vi.advanceTimersByTimeAsync(500); }); // never a pause long enough for the debounce
    }
    expect(saveDraftMock).toHaveBeenCalled(); // forced by the maximum wait, not by a pause
  });

  it("does not save an answer again when moving on after it was already saved", async () => {
    mockSingle({}, 3);
    saveDraftMock.mockResolvedValue({ accepted: true, version: 1 });
    advanceMock.mockResolvedValue({ accepted: true, slot_index: 1 });
    render(<ExamPage />);
    await userEvent.type(await screen.findByRole("textbox", { name: "Your answer" }), "x");
    await waitFor(() => expect(saveDraftMock).toHaveBeenCalledTimes(1), { timeout: 4000 });
    await userEvent.click(await screen.findByRole("button", { name: "Next question" }));
    await waitFor(() => expect(advanceMock).toHaveBeenCalledTimes(1));
    expect(saveDraftMock).toHaveBeenCalledTimes(1);
  });
});

describe("Student exam page — draft versus final", () => {
  it("says a saved draft is not a submission", async () => {
    mockSingle();
    saveDraftMock.mockResolvedValue({ accepted: true, version: 2 });
    render(<ExamPage />);
    await userEvent.type(await screen.findByRole("textbox", { name: "Your answer" }), "a");
    expect(await screen.findByText("Draft saved (v2) - not submitted", {}, { timeout: 4000 })).toBeInTheDocument();
    expect(screen.getByText(/Saving a draft is not submitting/)).toBeInTheDocument();
  });
});

describe("Student exam page — when the live socket is down", () => {
  it("polls with a single state read and reloads the question only when the attempt changed", async () => {
    mockSingle();
    render(<ExamPage />);
    await screen.findByText("Explain recursion.");
    [getStateMock, getCurrentQuestionMock, getMyDraftMock, listMyDraftVersionsMock, listReleasedSlotsMock].forEach((m) => m.mockClear());

    await act(async () => { await liveSocket.onPoll?.(); });
    expect(getStateMock).toHaveBeenCalledTimes(1);
    expect([getCurrentQuestionMock, getMyDraftMock, listMyDraftVersionsMock, listReleasedSlotsMock].map((m) => m.mock.calls.length)).toEqual([0, 0, 0, 0]);

    getStateMock.mockResolvedValue(state({ version: 2 }));
    await act(async () => { await liveSocket.onPoll?.(); });
    await waitFor(() => expect(getCurrentQuestionMock).toHaveBeenCalledTimes(1));
    expect(listReleasedSlotsMock).toHaveBeenCalledTimes(1);
  });
});

describe("Student exam page — timer and warnings", () => {
  it("shows the time left from the server's clock, not the device clock", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(new Date("1999-01-01T00:00:00Z")); // the device clock is wildly wrong
    mockSingle({ deadline: "2026-06-01T10:20:00Z" });
    render(<ExamPage />);
    expect(await screen.findByRole("timer")).toHaveTextContent(/^(20:00|19:5\d)$/);
  });

  it("keeps ticking between server samples", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mockSingle({ deadline: "2026-06-01T10:20:00Z" });
    render(<ExamPage />);
    const timer = await screen.findByRole("timer");
    const before = timer.textContent;
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(timer.textContent).not.toBe(before);
  });

  it("warns once at the default ten minutes, in a polite live region, and not before", async () => {
    mockSingle({ deadline: "2026-06-01T10:15:00Z" });
    const { unmount } = render(<ExamPage />);
    await screen.findByRole("timer");
    expect(screen.queryByText("Less than 10 minutes remain.")).not.toBeInTheDocument();
    unmount();

    mockSingle({ deadline: "2026-06-01T10:09:30Z" });
    render(<ExamPage />);
    const warning = await screen.findByText("Less than 10 minutes remain.");
    expect(warning.closest("[aria-live='polite']")).not.toBeNull();
    expect(screen.getAllByText("Less than 10 minutes remain.")).toHaveLength(1);
  });

  it("uses the thresholds the run was configured with", async () => {
    mockSingle({ deadline: "2026-06-01T10:24:00Z", time_warning_minutes: [30, 10, 5] });
    render(<ExamPage />);
    expect(await screen.findByText("Less than 30 minutes remain.")).toBeInTheDocument();
  });

  it("when time is up, locks editing, says so, and waits for the server to seal the attempt", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mockSingle({ deadline: "2026-06-01T09:59:00Z" });
    render(<ExamPage />);
    expect(await screen.findByRole("alert")).toHaveTextContent(/Time is up/);
    expect(screen.getByRole("textbox", { name: "Your answer" })).toHaveAttribute("readonly");
    expect(screen.getByRole("button", { name: "Submit exam" })).toBeDisabled();
    expect(screen.queryByText(/Less than/)).not.toBeInTheDocument();

    const callsBefore = getStateMock.mock.calls.length;
    getStateMock.mockResolvedValue(state({ deadline: "2026-06-01T09:59:00Z", status: "expired", submit_receipt: { receipt_id: "r", finalized_at: SERVER_NOW } }));
    await act(async () => { await vi.advanceTimersByTimeAsync(3500); });
    expect(getStateMock.mock.calls.length).toBeGreaterThan(callsBefore);
    await waitFor(() => expect(pushMock).toHaveBeenCalledWith("/exams/student/run-1/receipt"));
  });

  it("never submits on its own when time is up (the server seals)", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mockSingle({ deadline: "2026-06-01T09:59:00Z" });
    render(<ExamPage />);
    await screen.findByRole("alert");
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(submitMock).not.toHaveBeenCalled();
  });
});

describe("Student exam page — navigator, previous/next, mark for review", () => {
  function mockThree({ revisit = false } = {}) {
    getStateMock.mockResolvedValue(state({ current_slot_index: 1 }));
    getCurrentQuestionMock.mockResolvedValue(question(1, 4));
    getReleasedQuestionMock.mockImplementation(async (_t: string, _r: string, i: number) => question(i, 4));
    getMyDraftMock.mockImplementation(async (_t: string, _r: string, id: string) => ({ version: id.endsWith("0") ? 1 : 0, answer: id.endsWith("0") ? { text: "first" } : null, marked_for_review: false }));
    listMyDraftVersionsMock.mockResolvedValue({ versions: { "rev-1:slot:0": 1 } });
    listReleasedSlotsMock.mockResolvedValue(slotsFor(1, 4, { 0: { answered: true, marked: true } }, revisit));
  }

  it("shows a right-hand numbered navigator with answered / unanswered / marked / locked counts", async () => {
    mockThree();
    render(<ExamPage />);
    const nav = await screen.findByRole("navigation", { name: "Question navigator" });
    await waitFor(() => expect(within(nav).getByTestId("navigator-counts")).toHaveTextContent("Answered 1 · Unanswered 1 · Marked for review 1 · Locked 2"));
    expect(within(nav).getByRole("button", { name: /Question 3, locked/ })).toBeDisabled();
    expect(within(nav).getByRole("button", { name: /Question 2, .*current view/ })).toHaveAttribute("aria-current", "step");
  });

  it("cannot go back when the exam does not allow reopening earlier questions", async () => {
    mockThree({ revisit: false });
    render(<ExamPage />);
    await screen.findByRole("navigation", { name: "Question navigator" });
    expect(screen.getByRole("button", { name: "Previous question" })).toBeDisabled();
    expect(getReleasedQuestionMock).not.toHaveBeenCalled();
  });

  it("goes to a released earlier question and back, never asking for a locked one", async () => {
    mockThree({ revisit: true });
    render(<ExamPage />);
    const previous = await screen.findByRole("button", { name: "Previous question" });
    await waitFor(() => expect(previous).toBeEnabled());
    await userEvent.click(previous);
    expect(await screen.findByText("Prompt 1")).toBeInTheDocument();
    expect(getReleasedQuestionMock).toHaveBeenCalledWith("token", "run-1", 0);
    expect(screen.getByRole("textbox", { name: "Your answer" })).toHaveValue("first");

    await userEvent.click(screen.getByRole("button", { name: "Next question" })); // to the current question: no server advance
    expect(await screen.findByText("Prompt 2")).toBeInTheDocument();
    expect(advanceMock).not.toHaveBeenCalled();
    expect(getReleasedQuestionMock.mock.calls.every((c) => c[2] <= 1)).toBe(true);
  });

  it("only the Next button on the current question releases the next one", async () => {
    mockThree();
    advanceMock.mockResolvedValue({ accepted: true, slot_index: 2 });
    render(<ExamPage />);
    await userEvent.click(await screen.findByRole("button", { name: "Next question" }));
    await waitFor(() => expect(advanceMock).toHaveBeenCalledTimes(1));
    expect(screen.getByText(/releases it and cannot be undone/)).toBeInTheDocument();
  });

  it("saves the pending answer before it leaves a question", async () => {
    mockThree({ revisit: true });
    saveDraftMock.mockResolvedValue({ accepted: true, version: 1 });
    render(<ExamPage />);
    await userEvent.type(await screen.findByRole("textbox", { name: "Your answer" }), "z");
    const previous = screen.getByRole("button", { name: "Previous question" });
    await waitFor(() => expect(previous).toBeEnabled());
    await userEvent.click(previous);
    await waitFor(() => expect(saveDraftMock).toHaveBeenCalled());
    expect(saveDraftMock.mock.calls[0][2]).toBe("rev-1:slot:1"); // saved against the question being left
  });

  it("marks a question for review, as a toggle, saving the flag with the current answer", async () => {
    mockThree();
    saveDraftMock.mockResolvedValue({ accepted: true, version: 1 });
    render(<ExamPage />);
    const toggle = await screen.findByRole("button", { name: "Mark for review" });
    expect(toggle).toHaveAttribute("aria-pressed", "false");
    await userEvent.click(toggle);
    await waitFor(() => expect(saveDraftMock).toHaveBeenCalled());
    expect(saveDraftMock.mock.calls[0][5].markedForReview).toBe(true);
    expect(await screen.findByRole("button", { name: "Marked for review" })).toHaveAttribute("aria-pressed", "true");
  });

  it("updates the navigator from the save response without asking the server for the list again", async () => {
    mockThree();
    saveDraftMock.mockResolvedValue({ accepted: true, version: 1, answered: true });
    render(<ExamPage />);
    const nav = await screen.findByRole("navigation", { name: "Question navigator" });
    await waitFor(() => expect(within(nav).getByTestId("navigator-counts")).toHaveTextContent("Answered 1 · Unanswered 1"));
    const listCalls = listReleasedSlotsMock.mock.calls.length;
    await userEvent.type(await screen.findByRole("textbox", { name: "Your answer" }), "z");
    await waitFor(() => expect(saveDraftMock).toHaveBeenCalledTimes(1), { timeout: 4000 });
    await waitFor(() => expect(within(nav).getByTestId("navigator-counts")).toHaveTextContent("Answered 2 · Unanswered 0 · Marked for review 1 · Locked 2"));
    expect(listReleasedSlotsMock.mock.calls.length).toBe(listCalls);
  });

  it("loads state, question and navigator from the server on entry (the same path a reconnect reconcile takes)", async () => {
    mockThree();
    render(<ExamPage />);
    await screen.findByRole("navigation", { name: "Question navigator" });
    expect(listReleasedSlotsMock).toHaveBeenCalled();
    expect(getStateMock).toHaveBeenCalled();
  });
});

describe("Student exam page - running public tests", () => {
  function mockCoding() {
    getStateMock.mockResolvedValue(state());
    getCurrentQuestionMock.mockResolvedValue({
      question_id: "rev-1:slot:0", slot_index: 0, total_slots: 1,
      public_content: { item_type: "coding", prompt: "Add two numbers.", permitted_languages: ["python3.11"] },
    });
    getMyDraftMock.mockResolvedValue({ version: 0, answer: null });
    listMyDraftVersionsMock.mockResolvedValue({ versions: {} });
    listReleasedSlotsMock.mockResolvedValue(slotsFor(0, 1));
  }

  it("says plainly that nothing ran when the code runner is unavailable", async () => {
    mockCoding();
    publicRunMock.mockRejectedValue(new ExamApiError("The code runner is not available right now. Your code was not run.", 503, "judge_unavailable"));
    render(<ExamPage />);
    await userEvent.click(await screen.findByRole("button", { name: "Run" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/your code was not run/i);
    expect(getPublicRunMock).not.toHaveBeenCalled();
  });

  it("does not leave the student waiting when the runner reports an infrastructure problem", async () => {
    mockCoding();
    publicRunMock.mockResolvedValue({ submission_id: "sub-1", status: "polling" });
    getPublicRunMock.mockResolvedValue({ status: "infra_error", results: [] });
    render(<ExamPage />);
    await userEvent.click(await screen.findByRole("button", { name: "Run" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/nothing was marked/i);
    expect(screen.getByRole("button", { name: "Run" })).toBeEnabled();
  });

  it("shows the compiler's message for a compile error", async () => {
    mockCoding();
    publicRunMock.mockResolvedValue({ submission_id: "sub-1", status: "polling" });
    getPublicRunMock.mockResolvedValue({
      status: "succeeded",
      results: [{ test_id: "public:0", outcome: "compile_error", stderr: "main.c:1: error: expected ';'" }],
    });
    render(<ExamPage />);
    await userEvent.click(await screen.findByRole("button", { name: "Run" }));
    expect(await screen.findByText(/expected ';'/)).toBeInTheDocument();
    expect(screen.getByText("compile_error")).toBeInTheDocument();
  });
});