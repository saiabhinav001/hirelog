import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { expectNoAxeViolations } from "@/test/axe";

vi.hoisted(() => {
  process.env.NEXT_PUBLIC_EXAMS_ENABLED = "true";
});

const getStateMock = vi.fn();
const getCurrentQuestionMock = vi.fn();
const listReleasedSlotsMock = vi.fn();
const getMyDraftMock = vi.fn();
const listMyDraftVersionsMock = vi.fn();
const saveDraftMock = vi.fn();
const submitMock = vi.fn();
const getToken = async () => "token";

vi.mock("next/navigation", () => ({
  useParams: () => ({ runId: "run-1" }),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));
vi.mock("@/lib/examAuth", () => ({
  useExamAuth: () => ({ user: { uid: "u1" }, loading: false, isFaculty: false, isStudent: true, getToken }),
}));
vi.mock("@/lib/liveSocket", () => ({ useLiveSocket: () => ({ health: "connected", lastEventAt: null }) }));
vi.mock("@/lib/examEndpoints", () => ({
  examEndpoints: {
    getState: (...args: unknown[]) => getStateMock(...args),
    getCurrentQuestion: (...args: unknown[]) => getCurrentQuestionMock(...args),
    getReleasedQuestion: vi.fn(),
    listReleasedSlots: (...args: unknown[]) => listReleasedSlotsMock(...args),
    getMyDraft: (...args: unknown[]) => getMyDraftMock(...args),
    listMyDraftVersions: (...args: unknown[]) => listMyDraftVersionsMock(...args),
    submit: (...args: unknown[]) => submitMock(...args),
    saveDraft: (...args: unknown[]) => saveDraftMock(...args),
    advanceToNextQuestion: vi.fn(),
    publicRun: vi.fn(),
    getPublicRun: vi.fn(),
    getMyAccommodations: vi.fn().mockResolvedValue({ extra_minutes: 0, camera_waived: false }),
  },
}));

import ExamPage from "@/app/exams/student/[runId]/exam/page";

const SERVER_NOW = "2026-06-01T10:00:00Z";
const OPTIONS = [{ key: "A", text: "Alpha" }, { key: "B", text: "Beta" }, { key: "C", text: "Gamma" }];

function setup(itemType: "short_answer" | "single_choice" | "multiple_select", { total = 3, current = 1, deadline = "2026-06-01T11:00:00Z" } = {}) {
  getStateMock.mockResolvedValue({
    run_id: "run-1", uid: "u1", status: "in_progress", version: 1, epoch: 1, deadline, warning_count: 0,
    current_slot_index: current, submit_receipt: null, server_time: SERVER_NOW, time_warning_minutes: [10],
  });
  getCurrentQuestionMock.mockResolvedValue({
    question_id: `rev-1:slot:${current}`, slot_index: current, total_slots: total,
    public_content: { item_type: itemType, prompt: "Pick what applies.", ...(itemType === "short_answer" ? {} : { options: OPTIONS }) },
  });
  getMyDraftMock.mockResolvedValue({ version: 0, answer: null });
  listMyDraftVersionsMock.mockResolvedValue({ versions: {} });
  listReleasedSlotsMock.mockResolvedValue({
    current_slot_index: current, total_slots: total, locked_count: total - current - 1, revisit_enabled: false, server_time: SERVER_NOW,
    slots: Array.from({ length: current + 1 }, (_, i) => ({ slot_index: i, question_id: `rev-1:slot:${i}`, answered: i === 0, marked_for_review: i === 0, version: i === 0 ? 1 : 0 })),
  });
  saveDraftMock.mockResolvedValue({ accepted: true, version: 1 });
}

beforeEach(() => {
  [getStateMock, getCurrentQuestionMock, listReleasedSlotsMock, getMyDraftMock, listMyDraftVersionsMock, saveDraftMock, submitMock].forEach((m) => m.mockReset());
});

describe("Student exam page - accessibility", () => {
  it.each(["short_answer", "single_choice", "multiple_select"] as const)("has no automatically detectable violations for a %s question (with the navigator and timer)", async (kind) => {
    setup(kind);
    const { container } = render(<ExamPage />);
    await screen.findByRole("navigation", { name: "Question navigator" });
    await screen.findByRole("timer");
    await expectNoAxeViolations(container);
  });

  it("has no violations once time is up (the alert and the read-only answer)", async () => {
    setup("short_answer", { deadline: "2026-06-01T09:59:00Z" });
    const { container } = render(<ExamPage />);
    await screen.findByRole("alert");
    await expectNoAxeViolations(container);
  });

  it("names every control a screen reader needs: the option group, each option, the timer and the navigator", async () => {
    setup("multiple_select");
    render(<ExamPage />);
    const group = await screen.findByRole("group", { name: "Choose your answer" });
    for (const option of OPTIONS) expect(within(group).getByRole("checkbox", { name: option.text })).toBeInTheDocument();
    expect(screen.getByRole("timer")).toBeInTheDocument();
    const nav = screen.getByRole("navigation", { name: "Question navigator" });
    expect(within(nav).getByRole("button", { name: /Question 1, .*answered.*marked/i })).toBeInTheDocument();
    expect(within(nav).getByRole("button", { name: /Question 3, locked/ })).toBeDisabled();
  });

  it("announces a time warning in a polite live region rather than a focus-stealing dialog", async () => {
    setup("short_answer", { deadline: "2026-06-01T10:09:30Z" });
    render(<ExamPage />);
    const warning = await screen.findByText("Less than 10 minutes remain.");
    expect(warning.closest("[aria-live='polite']")).not.toBeNull();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("a keyboard-only student can pick several options with Space and reach Submit", async () => {
    setup("multiple_select", { total: 2, current: 1 });
    render(<ExamPage />);
    const group = await screen.findByRole("group", { name: "Choose your answer" });
    const user = userEvent.setup();

    const alpha = within(group).getByRole("checkbox", { name: "Alpha" });
    alpha.focus();
    await user.keyboard(" ");
    await user.tab();
    await user.tab();
    expect(within(group).getByRole("checkbox", { name: "Gamma" })).toHaveFocus();
    await user.keyboard(" ");
    expect(alpha).toBeChecked();
    expect(within(group).getByRole("checkbox", { name: "Gamma" })).toBeChecked();
    expect(within(group).getByRole("checkbox", { name: "Beta" })).not.toBeChecked();

    await waitFor(() => expect(saveDraftMock).toHaveBeenCalled(), { timeout: 4000 });
    const lastAnswer = saveDraftMock.mock.calls.at(-1)?.[4] as { selected: string[] };
    expect([...lastAnswer.selected].sort()).toEqual(["A", "C"]); // a list, which the grader compares as an exact set

    // Tab onward reaches the submit button without a pointer, and Enter opens the confirmation (not a silent submit).
    const submit = screen.getByRole("button", { name: "Submit exam" });
    for (let i = 0; i < 25 && document.activeElement !== submit; i += 1) await user.tab();
    expect(submit).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(await screen.findByRole("dialog", { name: "Confirm submission" })).toBeInTheDocument();
    expect(submitMock).not.toHaveBeenCalled();
  });
});
