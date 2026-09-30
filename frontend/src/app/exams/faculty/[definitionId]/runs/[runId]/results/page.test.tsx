import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.hoisted(() => {
  process.env.NEXT_PUBLIC_EXAMS_ENABLED = "true";
});

const listGradebookRowsMock = vi.fn();
const getGradebookCountsMock = vi.fn();
const listReleasesMock = vi.fn();
const listDefectiveResolutionsMock = vi.fn();
const releaseResultsMock = vi.fn();

vi.mock("next/navigation", () => ({
  useParams: () => ({ definitionId: "def-1", runId: "run-1" }),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

vi.mock("@/lib/examAuth", () => ({
  useExamAuth: () => ({
    user: { uid: "fac-1" },
    loading: false,
    isFaculty: true,
    isStudent: false,
    getToken: async () => "token",
  }),
}));

vi.mock("@/lib/examEndpoints", () => ({
  examEndpoints: {
    listGradebookRows: (...args: unknown[]) => listGradebookRowsMock(...args),
    getGradebookCounts: (...args: unknown[]) => getGradebookCountsMock(...args),
    listReleases: (...args: unknown[]) => listReleasesMock(...args),
    listDefectiveResolutions: (...args: unknown[]) => listDefectiveResolutionsMock(...args),
    releaseResults: (...args: unknown[]) => releaseResultsMock(...args),
  },
}));

import GradebookPage from "@/app/exams/faculty/[definitionId]/runs/[runId]/results/page";

const SAMPLE_ROWS = [
  {
    uid: "u1", attempt_status: "finalized", finalized: true, raw_score: 8, raw_max_score: 10,
    integrity_disposition: "clean", warning_count: 0,
    question_results: [
      { uid: "u1", question_id: "rev-1:slot:0", slot_index: 0, item_type: "coding", presented: true, outcome: "fully_solved", weighted_score: 8, max_score: 10, grading_version: 1, tests_passed: 2, tests_total: 2, failure_category: "none", submission_hash: "h1" },
    ],
  },
];

const SAMPLE_COUNTS = [
  { question_id: "rev-1:slot:0", assigned: 1, presented: 1, finalized: 1, nonblank_attempted: 1, fully_solved: 1, partially_solved: 0, blank: 0, pending: 0 },
];

describe("Faculty gradebook page", () => {
  beforeEach(() => {
    listGradebookRowsMock.mockReset();
    getGradebookCountsMock.mockReset();
    listReleasesMock.mockReset();
    listDefectiveResolutionsMock.mockReset();
    releaseResultsMock.mockReset();

    listGradebookRowsMock.mockResolvedValue({ results: SAMPLE_ROWS, next_cursor_uid: null });
    getGradebookCountsMock.mockResolvedValue({ results: SAMPLE_COUNTS });
    listReleasesMock.mockResolvedValue({ results: [] });
    listDefectiveResolutionsMock.mockResolvedValue({ results: [] });
  });

  it("renders student rows and per-question counts", async () => {
    render(<GradebookPage />);

    expect(await screen.findByText("u1")).toBeInTheDocument();
    expect(screen.getByText("8.0 / 10.0")).toBeInTheDocument();
    expect(screen.getByText("Results have not been released to students yet.")).toBeInTheDocument();
  });

  it("releases results when the release button is clicked", async () => {
    releaseResultsMock.mockResolvedValue({
      id: "r1", run_id: "run-1", version: 1, released_by: "fac-1", released_at: "2026-06-01T10:00:00Z", note: "",
    });
    render(<GradebookPage />);

    await screen.findByText("u1");
    await userEvent.click(screen.getByRole("button", { name: "Release results" }));

    await waitFor(() => expect(releaseResultsMock).toHaveBeenCalledWith("token", "run-1", ""));
  });
});
