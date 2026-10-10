import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.hoisted(() => {
  process.env.NEXT_PUBLIC_EXAMS_ENABLED = "true";
});

const listAttemptsForRun = vi.fn();
const listIncidentsForStudent = vi.fn();
const listSnapshotsForStudent = vi.fn();
const resolveBlock = vi.fn();
const issueViewTicket = vi.fn();
const getToken = async () => "tok";

vi.mock("next/navigation", () => ({
  useParams: () => ({ definitionId: "d1", runId: "r1", uid: "stu-1" }),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));
vi.mock("@/lib/examAuth", () => ({
  useExamAuth: () => ({ user: { uid: "t1" }, loading: false, isFaculty: true, isStudent: false, getToken }),
}));
vi.mock("@/lib/liveSocket", () => ({ useLiveSocket: () => ({ health: "connected", lastEventAt: null }) }));
vi.mock("@/lib/examEndpoints", () => ({
  examEndpoints: {
    listAttemptsForRun: (...a: unknown[]) => listAttemptsForRun(...a),
    listIncidentsForStudent: (...a: unknown[]) => listIncidentsForStudent(...a),
    listSnapshotsForStudent: (...a: unknown[]) => listSnapshotsForStudent(...a),
    resolveBlock: (...a: unknown[]) => resolveBlock(...a),
    issueViewTicket: (...a: unknown[]) => issueViewTicket(...a),
    extendTime: vi.fn(),
    terminateAttempt: vi.fn(),
    recoverAttempt: vi.fn(),
  },
}));

import CandidateDetailPage from "@/app/exams/faculty/[definitionId]/runs/[runId]/candidates/[uid]/page";
import { ExamApiError } from "@/lib/examsApi";

const attempt = (status: string) => ({ run_id: "r1", uid: "stu-1", status, version: 3, epoch: 1, deadline: "2026-06-01T11:00:00Z", warning_count: 3, current_slot_index: 0, submit_receipt: null });
const entry = (over: Record<string, unknown>) => ({ id: "e1", kind: "page_hidden", counted: true, client_timestamp: "2026-06-01T10:00:00Z", received_at: "2026-06-01T10:00:01Z", duration_seconds: 12, note: "counted departure", source: "browser", ...over });

beforeEach(() => {
  [listAttemptsForRun, listIncidentsForStudent, listSnapshotsForStudent, resolveBlock, issueViewTicket].forEach((m) => m.mockReset());
  listAttemptsForRun.mockResolvedValue({ results: [attempt("blocked")], next_cursor_uid: null });
  listIncidentsForStudent.mockResolvedValue({ results: [entry({})], client_reported_disclaimer: "x" });
  listSnapshotsForStudent.mockResolvedValue({ results: [] });
});

describe("Candidate detail - reviewing a block", () => {
  it("shows what produced each ledger entry and says browser signals are not proof", async () => {
    listIncidentsForStudent.mockResolvedValue({
      results: [entry({}), entry({ id: "e2", source: "reviewer", counted: false, note: "reviewer decision: upheld — block stands. ok" }), entry({ id: "e3", source: "server", counted: false, note: "received while the attempt was not active" })],
      client_reported_disclaimer: "x",
    });
    render(<CandidateDetailPage />);
    expect(await screen.findByText("reviewer decision")).toBeInTheDocument();
    expect(screen.getByText("server")).toBeInTheDocument();
    expect(screen.getByText(/client-reported, not cryptographic proof/)).toBeInTheDocument();
  });

  it("needs a confirmed reason, names the student, and says answers and incidents are kept", async () => {
    resolveBlock.mockResolvedValue({});
    render(<CandidateDetailPage />);
    await userEvent.click(await screen.findByRole("button", { name: "Allow to continue" }));
    const dialog = screen.getByRole("dialog", { name: "Let this student continue?" });
    expect(dialog).toHaveTextContent("stu-1");
    expect(dialog).toHaveTextContent(/Every incident stays on record/);
    expect(resolveBlock).not.toHaveBeenCalled();
    const confirm = screen.getAllByRole("button", { name: "Allow to continue" }).find((b) => dialog.contains(b))!;
    expect(confirm).toBeDisabled();
    await userEvent.type(screen.getByLabelText(/Reason/), "Network outage confirmed by the lab");
    await userEvent.click(confirm);
    await waitFor(() => expect(resolveBlock).toHaveBeenCalledWith("tok", "r1", "stu-1", true, "Network outage confirmed by the lab"));
    expect(await screen.findByText(/The student can continue/)).toBeInTheDocument();
  });

  it("upholding sends allow=false with the reason", async () => {
    resolveBlock.mockResolvedValue({});
    render(<CandidateDetailPage />);
    await userEvent.click(await screen.findByRole("button", { name: "Uphold the block" }));
    await userEvent.type(screen.getByLabelText(/Reason/), "Telemetry matches the log");
    await userEvent.click(screen.getByRole("button", { name: "Uphold" }));
    await waitFor(() => expect(resolveBlock).toHaveBeenCalledWith("tok", "r1", "stu-1", false, "Telemetry matches the log"));
  });

  it("when someone else already resolved it, explains in plain words and reloads instead of crashing the page", async () => {
    resolveBlock.mockRejectedValue(new ExamApiError("nope", 409, "attempt_not_blocked"));
    render(<CandidateDetailPage />);
    await userEvent.click(await screen.findByRole("button", { name: "Uphold the block" }));
    await userEvent.type(screen.getByLabelText(/Reason/), "reason here");
    const loads = listAttemptsForRun.mock.calls.length;
    await userEvent.click(screen.getByRole("button", { name: "Uphold" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/no longer blocked/);
    expect(screen.getByRole("heading", { name: "Candidate detail" })).toBeInTheDocument();
    await waitFor(() => expect(listAttemptsForRun.mock.calls.length).toBeGreaterThan(loads));
  });

  it("offers no review for an attempt that is not blocked", async () => {
    listAttemptsForRun.mockResolvedValue({ results: [attempt("in_progress")], next_cursor_uid: null });
    render(<CandidateDetailPage />);
    await screen.findByRole("heading", { name: "Candidate detail" });
    expect(screen.queryByRole("button", { name: "Uphold the block" })).not.toBeInTheDocument();
  });

  it("an expired or unusable evidence link is shown inline and the page stays usable", async () => {
    listSnapshotsForStudent.mockResolvedValue({ results: [{ id: "snap-1", kind: "check_in", status: "verified", missing_reason: null }] });
    issueViewTicket.mockRejectedValue(new ExamApiError("gone", 401, "view_ticket_invalid"));
    render(<CandidateDetailPage />);
    await userEvent.click(await screen.findByRole("button", { name: "View" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/expired, was already used/);
    expect(screen.getByRole("heading", { name: "Evidence snapshots" })).toBeInTheDocument();
  });

  it("marks missing evidence with its reason instead of hiding it", async () => {
    listSnapshotsForStudent.mockResolvedValue({ results: [{ id: "s2", kind: "periodic", status: "missing", missing_reason: "upload_not_completed" }] });
    render(<CandidateDetailPage />);
    expect(await screen.findByText("(upload_not_completed)")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "View" })).not.toBeInTheDocument();
  });
});
