import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.hoisted(() => {
  process.env.NEXT_PUBLIC_EXAMS_ENABLED = "true";
});

const getMyResultsMock = vi.fn();
const getMyFeedbackMock = vi.fn();
const listMyGradeAppealsMock = vi.fn();
const submitGradeAppealMock = vi.fn();

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

vi.mock("@/lib/examEndpoints", () => ({
  examEndpoints: {
    getMyResults: (...args: unknown[]) => getMyResultsMock(...args),
    getMyFeedback: (...args: unknown[]) => getMyFeedbackMock(...args),
    listMyGradeAppeals: (...args: unknown[]) => listMyGradeAppealsMock(...args),
    submitGradeAppeal: (...args: unknown[]) => submitGradeAppealMock(...args),
  },
}));

import StudentResultsPage from "@/app/exams/student/[runId]/results/page";

const SAMPLE_ROW = {
  uid: "u1",
  attempt_status: "finalized",
  finalized: true,
  raw_score: 8,
  raw_max_score: 10,
  integrity_disposition: "clean",
  warning_count: 0,
  question_results: [
    {
      uid: "u1", question_id: "rev-1:slot:0", slot_index: 0, item_type: "coding", presented: true,
      outcome: "fully_solved", weighted_score: 8, max_score: 10, grading_version: 1,
      tests_passed: 2, tests_total: 2, failure_category: "none", submission_hash: "h1",
    },
  ],
};

describe("Student results page", () => {
  beforeEach(() => {
    getMyResultsMock.mockReset();
    getMyFeedbackMock.mockReset();
    listMyGradeAppealsMock.mockReset();
    submitGradeAppealMock.mockReset();
  });

  it("shows a not-released message when results have not been released yet", async () => {
    getMyResultsMock.mockRejectedValue(new Error("run run-1 results have not been released yet"));
    render(<StudentResultsPage />);

    expect(await screen.findByText(/results not released yet/i)).toBeInTheDocument();
  });

  it("shows the total score and per-question breakdown once released", async () => {
    getMyResultsMock.mockResolvedValue({ row: SAMPLE_ROW, release_version: 1 });
    getMyFeedbackMock.mockResolvedValue({
      run_id: "run-1", uid: "u1", strengths: ["Question 1: fully solved (8/10)."], gaps: [],
      practice_steps: [], source_anchors: [], missing_material: [], generated_at: "2026-06-01T10:00:00Z",
    });
    listMyGradeAppealsMock.mockResolvedValue({ results: [] });

    render(<StudentResultsPage />);

    expect(await screen.findByText("8.0 / 10.0")).toBeInTheDocument();
    expect(screen.getByText(/Question 1: fully solved/)).toBeInTheDocument();
  });

  it("submits a grade appeal for a question", async () => {
    getMyResultsMock.mockResolvedValue({ row: SAMPLE_ROW, release_version: 1 });
    getMyFeedbackMock.mockResolvedValue(null);
    listMyGradeAppealsMock.mockResolvedValue({ results: [] });
    submitGradeAppealMock.mockResolvedValue({
      id: "a1", run_id: "run-1", uid: "u1", question_id: "rev-1:slot:0", student_statement: "Please recheck.",
      status: "pending", created_at: "2026-06-01T10:00:00Z", resolved_by: null, resolved_at: null,
      resolution_notes: null, version: 1,
    });

    render(<StudentResultsPage />);

    await userEvent.click(await screen.findByRole("button", { name: "Dispute this grade" }));
    await userEvent.type(screen.getByPlaceholderText(/explain why/i), "Please recheck.");
    await userEvent.click(screen.getByRole("button", { name: "Submit dispute" }));

    await waitFor(() => expect(submitGradeAppealMock).toHaveBeenCalledWith("token", "run-1", "rev-1:slot:0", "Please recheck."));
  });
});
