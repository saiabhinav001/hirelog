import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { expectNoAxeViolations } from "@/test/axe";

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
  useExamAuth: () => ({ user: { uid: "fac-1" }, loading: false, isFaculty: true, isStudent: false, getToken: async () => "token" }),
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
import { ExamApiError } from "@/lib/examsApi";

const ROWS = [
  {
    uid: "u1", attempt_status: "finalized", finalized: true, raw_score: 8, raw_max_score: 10, integrity_disposition: "clean", warning_count: 0,
    question_results: [
      { uid: "u1", question_id: "rev-1:slot:0", slot_index: 0, item_type: "coding", presented: true, outcome: "fully_solved", weighted_score: 8, max_score: 10, grading_version: 1, tests_passed: 2, tests_total: 2, failure_category: "none", submission_hash: "h1" },
    ],
  },
];
const COUNTS = [{ question_id: "rev-1:slot:0", assigned: 1, presented: 1, finalized: 1, nonblank_attempted: 1, fully_solved: 1, partially_solved: 0, blank: 0, pending: 0 }];
const RELEASE_V1 = { id: "run-1-v1", run_id: "run-1", version: 1, released_by: "fac-1", released_at: "2026-06-01T10:00:00Z", note: "" };

beforeEach(() => {
  [listGradebookRowsMock, getGradebookCountsMock, listReleasesMock, listDefectiveResolutionsMock, releaseResultsMock].forEach((m) => m.mockReset());
  listGradebookRowsMock.mockResolvedValue({ results: ROWS, next_cursor_uid: null });
  getGradebookCountsMock.mockResolvedValue({ results: COUNTS });
  listReleasesMock.mockResolvedValue({ results: [] });
  listDefectiveResolutionsMock.mockResolvedValue({ results: [] });
});

describe("Faculty gradebook page - releasing", () => {
  it("asks before releasing an incomplete gradebook and only then sends the acknowledgement", async () => {
    releaseResultsMock
      .mockRejectedValueOnce(new ExamApiError("1 attempt(s) still in progress, 2 answer(s) not yet graded; confirm to release anyway", 409, "release_incomplete"))
      .mockResolvedValueOnce(RELEASE_V1);
    render(<GradebookPage />);
    await userEvent.click(await screen.findByRole("button", { name: "Release results" }));

    expect(await screen.findByText(/2 answer\(s\) not yet graded/)).toBeInTheDocument();
    expect(releaseResultsMock).toHaveBeenCalledTimes(1);
    expect(releaseResultsMock.mock.calls[0]).toEqual(["token", "run-1", ""]);

    await userEvent.click(screen.getByRole("button", { name: "Release anyway" }));
    await waitFor(() => expect(releaseResultsMock).toHaveBeenCalledTimes(2));
    expect(releaseResultsMock.mock.calls[1]).toEqual(["token", "run-1", "", true]);
  });

  it("can be declined, and nothing is sent", async () => {
    releaseResultsMock.mockRejectedValue(new ExamApiError("pending", 409, "release_incomplete"));
    render(<GradebookPage />);
    await userEvent.click(await screen.findByRole("button", { name: "Release results" }));
    await userEvent.click(await screen.findByRole("button", { name: "Not yet" }));
    expect(screen.queryByRole("button", { name: "Release anyway" })).not.toBeInTheDocument();
    expect(releaseResultsMock).toHaveBeenCalledTimes(1);
  });

  it("a corrected release asks for a reason and explains a refusal next to the button, keeping the page", async () => {
    listReleasesMock.mockResolvedValue({ results: [RELEASE_V1] });
    releaseResultsMock.mockRejectedValue(new ExamApiError("a corrected re-release must state the reason", 409, "release_reason_required"));
    render(<GradebookPage />);

    expect(await screen.findByLabelText("Reason for the corrected release")).toBeInTheDocument();
    expect(screen.getByText(/reach students only when you release a new version/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Release corrected results (new version)" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/must say why/);
    expect(screen.getByText("u1")).toBeInTheDocument(); // the gradebook is still there
  });

  it("sends the reason the teacher typed", async () => {
    listReleasesMock.mockResolvedValue({ results: [RELEASE_V1] });
    releaseResultsMock.mockResolvedValue({ ...RELEASE_V1, version: 2, note: "Q1 key fixed" });
    render(<GradebookPage />);
    await userEvent.type(await screen.findByLabelText("Reason for the corrected release"), "Q1 key fixed");
    await userEvent.click(screen.getByRole("button", { name: "Release corrected results (new version)" }));
    await waitFor(() => expect(releaseResultsMock).toHaveBeenCalledWith("token", "run-1", "Q1 key fixed"));
  });

  it("has no automatically detectable accessibility violations", async () => {
    listReleasesMock.mockResolvedValue({ results: [RELEASE_V1] });
    const { container } = render(<GradebookPage />);
    await screen.findByText("u1");
    await expectNoAxeViolations(container);
  });
});
