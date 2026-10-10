import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { expectNoAxeViolations } from "@/test/axe";

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
  useExamAuth: () => ({ user: { uid: "u1" }, loading: false, isFaculty: false, isStudent: true, getToken: async () => "token" }),
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

const ROW = {
  uid: "u1", attempt_status: "finalized", finalized: true, raw_score: 5, raw_max_score: 10, integrity_disposition: "clean", warning_count: 0,
  question_results: [
    { uid: "u1", question_id: "rev-1:slot:0", slot_index: 0, item_type: "coding", presented: true, outcome: "partially_solved", weighted_score: 5, max_score: 10, grading_version: 1, tests_passed: 1, tests_total: 2, failure_category: "wrong_answer", submission_hash: null, runtime_image: null, test_suite_version: null },
    { uid: "u1", question_id: "rev-1:slot:1", slot_index: 1, item_type: "single_choice", presented: true, outcome: "blank", weighted_score: 0, max_score: 2, grading_version: 1, tests_passed: null, tests_total: null, failure_category: null, submission_hash: null },
  ],
};

beforeEach(() => {
  [getMyResultsMock, getMyFeedbackMock, listMyGradeAppealsMock, submitGradeAppealMock].forEach((m) => m.mockReset());
  getMyResultsMock.mockResolvedValue({ row: ROW, release_version: 2 });
  getMyFeedbackMock.mockResolvedValue({ run_id: "run-1", uid: "u1", strengths: [], gaps: ["Question 1: partially solved."], practice_steps: [], source_anchors: [], missing_material: [] });
  listMyGradeAppealsMock.mockResolvedValue({ results: [] });
});

describe("Student results page - accessibility", () => {
  it("has no automatically detectable violations when results are released", async () => {
    const { container } = render(<StudentResultsPage />);
    await screen.findByRole("heading", { name: "Your results" });
    await screen.findAllByText(/partially/i);
    await expectNoAxeViolations(container);
  });

  it("has no violations when results are not released yet", async () => {
    getMyResultsMock.mockReset().mockRejectedValue(new Error("run run-1 results have not been released yet"));
    const { container } = render(<StudentResultsPage />);
    await screen.findByText(/results not released yet/i);
    await expectNoAxeViolations(container);
  });

  it("a dispute form can be opened and filled by keyboard alone", async () => {
    const { container } = render(<StudentResultsPage />);
    const user = userEvent.setup();
    const open = (await screen.findAllByRole("button", { name: /dispute/i }))[0];
    for (let i = 0; i < 40 && document.activeElement !== open; i += 1) await user.tab();
    expect(open).toHaveFocus();
    await user.keyboard("{Enter}");
    const statement = await screen.findByRole("textbox");
    await user.type(statement, "Please recheck");
    expect(statement).toHaveValue("Please recheck");
    await expectNoAxeViolations(container);
  });
});
