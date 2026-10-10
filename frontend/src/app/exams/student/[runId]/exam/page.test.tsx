import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.hoisted(() => {
  process.env.NEXT_PUBLIC_EXAMS_ENABLED = "true";
});

const getStateMock = vi.fn();
const getCurrentQuestionMock = vi.fn();
const getMyDraftMock = vi.fn();
const listMyDraftVersionsMock = vi.fn();
const submitMock = vi.fn();
const saveDraftMock = vi.fn();
const getMyAccommodationsMock = vi.fn().mockResolvedValue({ extra_minutes: 0, camera_waived: false });

vi.mock("next/navigation", () => ({
  useParams: () => ({ runId: "run-1" }),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

vi.mock("@/lib/examAuth", () => ({
  useExamAuth: () => ({
    user: { uid: "u1" },
    loading: false,
    isFaculty: false,
    isStudent: true,
    getToken: async () => "token",
  }),
}));

vi.mock("@/lib/liveSocket", () => ({
  useLiveSocket: () => ({ health: "connected", lastEventAt: null }),
}));

vi.mock("@/lib/examEndpoints", () => ({
  examEndpoints: {
    getState: (...args: unknown[]) => getStateMock(...args),
    getCurrentQuestion: (...args: unknown[]) => getCurrentQuestionMock(...args),
    getMyDraft: (...args: unknown[]) => getMyDraftMock(...args),
    listMyDraftVersions: (...args: unknown[]) => listMyDraftVersionsMock(...args),
    submit: (...args: unknown[]) => submitMock(...args),
    saveDraft: (...args: unknown[]) => saveDraftMock(...args),
    getMyAccommodations: (...args: unknown[]) => getMyAccommodationsMock(...args),
  },
}));

import ExamPage from "@/app/exams/student/[runId]/exam/page";

function mockLastSlotShortAnswer() {
  getStateMock.mockResolvedValue({
    run_id: "run-1", uid: "u1", status: "in_progress", version: 1, epoch: 1,
    deadline: "2026-06-01T11:00:00Z", warning_count: 0, current_slot_index: 0, submit_receipt: null,
  });
  getCurrentQuestionMock.mockResolvedValue({
    question_id: "rev-1:slot:0", slot_index: 0, total_slots: 1,
    public_content: { item_type: "short_answer", prompt: "Explain recursion." },
  });
  getMyDraftMock.mockResolvedValue({ version: 0, answer: null });
  listMyDraftVersionsMock.mockResolvedValue({ versions: {} });
}

describe("Student exam page — submit confirmation", () => {
  beforeEach(() => {
    getStateMock.mockReset();
    getCurrentQuestionMock.mockReset();
    getMyDraftMock.mockReset();
    listMyDraftVersionsMock.mockReset();
    saveDraftMock.mockReset();
    submitMock.mockReset();
    getMyAccommodationsMock.mockReset().mockResolvedValue({ extra_minutes: 0, camera_waived: false });
  });

  it("shows an irreversible-submit confirmation dialog before actually submitting", async () => {
    mockLastSlotShortAnswer();
    render(<ExamPage />);

    const submitButton = await screen.findByRole("button", { name: "Submit exam" });
    await userEvent.click(submitButton);

    expect(await screen.findByRole("dialog", { name: "Confirm submission" })).toBeInTheDocument();
    expect(screen.getByText(/irreversible/i)).toBeInTheDocument();
    expect(submitMock).not.toHaveBeenCalled();
  });

  it("only calls the submit endpoint after explicit confirmation", async () => {
    mockLastSlotShortAnswer();
    submitMock.mockResolvedValue({ accepted: true, receipt: { receipt_id: "r1" } });
    render(<ExamPage />);

    await userEvent.click(await screen.findByRole("button", { name: "Submit exam" }));
    await userEvent.click(await screen.findByRole("button", { name: "Yes, submit" }));

    await waitFor(() => expect(submitMock).toHaveBeenCalledTimes(1));
  });

  it("retries a failed autosave once with the same save id and sends the attempt epoch", async () => {
    mockLastSlotShortAnswer();
    saveDraftMock.mockRejectedValueOnce(new Error("network")).mockResolvedValueOnce({ accepted: true, version: 1 });
    render(<ExamPage />);

    await userEvent.type(await screen.findByRole("textbox", { name: "Your answer" }), "x");

    await waitFor(() => expect(saveDraftMock).toHaveBeenCalledTimes(2), { timeout: 4000 });
    const [first, second] = saveDraftMock.mock.calls;
    expect(first[5].clientSaveId).toBeTruthy();
    expect(second[5]).toEqual(first[5]);
    expect(first[5].clientEpoch).toBe(1);
  });

  it("fetches the student's own accommodations (never another student's)", async () => {
    mockLastSlotShortAnswer();
    render(<ExamPage />);

    await screen.findByRole("button", { name: "Submit exam" });
    await waitFor(() => expect(getMyAccommodationsMock).toHaveBeenCalledWith("token", "run-1"));
  });

  it("does not show a submit button before the last question", async () => {
    getStateMock.mockResolvedValue({
      run_id: "run-1", uid: "u1", status: "in_progress", version: 1, epoch: 1,
      deadline: null, warning_count: 0, current_slot_index: 0, submit_receipt: null,
    });
    getCurrentQuestionMock.mockResolvedValue({
      question_id: "rev-1:slot:0", slot_index: 0, total_slots: 3,
      public_content: { item_type: "short_answer", prompt: "Q1" },
    });
    getMyDraftMock.mockResolvedValue({ version: 0, answer: null });
    listMyDraftVersionsMock.mockResolvedValue({ versions: {} });

    render(<ExamPage />);

    await screen.findByText("Q1");
    expect(screen.queryByRole("button", { name: "Submit exam" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Next question" })).toBeInTheDocument();
  });
});
