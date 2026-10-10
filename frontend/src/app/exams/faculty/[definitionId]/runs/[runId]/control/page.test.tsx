import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.hoisted(() => {
  process.env.NEXT_PUBLIC_EXAMS_ENABLED = "true";
});

const getRun = vi.fn();
const listAttemptsForRun = vi.fn();
const listAppealsForRun = vi.fn();
const runCommand = vi.fn();
const changeRunAccess = vi.fn();
const getShortLink = vi.fn();
const health = vi.hoisted(() => ({ value: "connected" }));
const getToken = async () => "tok";

vi.mock("next/navigation", () => ({
  useParams: () => ({ definitionId: "d1", runId: "r1" }),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));
vi.mock("@/lib/examAuth", () => ({
  useExamAuth: () => ({ user: { uid: "t1" }, loading: false, isFaculty: true, isStudent: false, getToken }),
}));
vi.mock("@/lib/liveSocket", () => ({
  useLiveSocket: () => ({ health: health.value, lastEventAt: null }),
}));
vi.mock("@/lib/examEndpoints", () => ({
  examEndpoints: {
    getRun: (...a: unknown[]) => getRun(...a),
    listAttemptsForRun: (...a: unknown[]) => listAttemptsForRun(...a),
    listAppealsForRun: (...a: unknown[]) => listAppealsForRun(...a),
    runCommand: (...a: unknown[]) => runCommand(...a),
    changeRunAccess: (...a: unknown[]) => changeRunAccess(...a),
    getShortLink: (...a: unknown[]) => getShortLink(...a),
  },
}));

import ControlRoomPage from "@/app/exams/faculty/[definitionId]/runs/[runId]/control/page";
import { ExamApiError } from "@/lib/examsApi";

const RUN = { id: "r1", status: "released", version: 4, enrolled_uids: ["stu-1", "stu-2"], server_time: "2026-06-01T10:00:00Z" };
const ATTEMPT = (uid: string, status: string) => ({ run_id: "r1", uid, status, version: 1, epoch: 1, deadline: "2026-06-01T11:00:00Z", warning_count: 0, current_slot_index: 0, submit_receipt: null });

beforeEach(() => {
  health.value = "connected";
  [getRun, listAttemptsForRun, listAppealsForRun, runCommand, changeRunAccess, getShortLink].forEach((m) => m.mockReset());
  getRun.mockResolvedValue(RUN);
  listAttemptsForRun.mockResolvedValue({ results: [ATTEMPT("stu-1", "in_progress"), ATTEMPT("stu-2", "expired")], next_cursor_uid: null, server_time: "2026-06-01T10:00:00Z" });
  listAppealsForRun.mockResolvedValue({ results: [] });
  getShortLink.mockResolvedValue({ enabled: false, link: null });
});

afterEach(() => vi.useRealTimers());

describe("Control room", () => {
  it("offers the right per-student actions in each row", async () => {
    render(<ControlRoomPage />);
    expect(await screen.findByRole("button", { name: "Extend time for stu-1" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "End attempt for stu-1" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Recover grading for stu-2" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Extend time for stu-2" })).not.toBeInTheDocument();
  });

  it("shows the server's time and how long ago the grid was refreshed, ticking each second", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    render(<ControlRoomPage />);
    const line = await screen.findByTestId("freshness");
    expect(line).toHaveTextContent(/Server time .* refreshed 0s ago/);
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(screen.getByTestId("freshness")).toHaveTextContent(/refreshed [4-6]s ago/);
  });

  it("warns that figures may be out of date when the live feed is down and the data is old", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    health.value = "polling";
    render(<ControlRoomPage />);
    await screen.findByTestId("freshness");
    expect(screen.queryByText(/may be out of date/)).not.toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(31_000); });
    expect(screen.getByRole("alert")).toHaveTextContent(/Live updates are polling. These figures may be out of date./);
  });

  it("does not warn when the live feed is connected", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    render(<ControlRoomPage />);
    await screen.findByTestId("freshness");
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(screen.queryByText(/may be out of date/)).not.toBeInTheDocument();
  });

  it("closing needs a reason, sends it with the version the teacher saw, and confirms", async () => {
    runCommand.mockResolvedValue({});
    render(<ControlRoomPage />);
    await userEvent.click(await screen.findByRole("button", { name: "Close" }));
    const dialog = screen.getByRole("dialog");
    const confirm = within(dialog).getByRole("button", { name: "Confirm close" });
    expect(confirm).toBeDisabled();
    await userEvent.type(within(dialog).getByLabelText(/Reason/), "Session over");
    await userEvent.click(confirm);
    await waitFor(() => expect(runCommand).toHaveBeenCalledTimes(1));
    expect(runCommand).toHaveBeenCalledWith("tok", "r1", "close", 4, "close-r1-4", false, "Session over");
    expect(await screen.findByText("Run closed.")).toBeInTheDocument();
  });

  it("on a version conflict explains it, shows no crash screen, and reloads the run", async () => {
    runCommand.mockRejectedValue(new ExamApiError("conflict", 409, "version_conflict"));
    render(<ControlRoomPage />);
    await userEvent.click(await screen.findByRole("button", { name: "Pause" }));
    const loadsBefore = getRun.mock.calls.length;
    await userEvent.click(screen.getByRole("button", { name: "Confirm pause" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/Someone else changed this run/);
    expect(screen.getByRole("heading", { name: "Live control room" })).toBeInTheDocument();
    await waitFor(() => expect(getRun.mock.calls.length).toBeGreaterThan(loadsBefore));
  });

  it("adds a student to the run after confirmation", async () => {
    changeRunAccess.mockResolvedValue({ changed: true });
    render(<ControlRoomPage />);
    await userEvent.type(await screen.findByLabelText("Student ID"), "stu-9");
    await userEvent.click(screen.getByRole("button", { name: "Add to run" }));
    expect(changeRunAccess).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Add student" }));
    await waitFor(() => expect(changeRunAccess).toHaveBeenCalledTimes(1));
    const args = changeRunAccess.mock.calls[0];
    expect(args.slice(0, 4)).toEqual(["tok", "r1", "grant", "stu-9"]);
    expect(await screen.findByText("Added stu-9 to this run.")).toBeInTheDocument();
  });

  it("refuses to remove a student who has started, in plain words", async () => {
    changeRunAccess.mockRejectedValue(new ExamApiError("x", 409, "attempt_exists"));
    render(<ControlRoomPage />);
    await userEvent.type(await screen.findByLabelText("Student ID"), "stu-1");
    await userEvent.click(screen.getByRole("button", { name: "Remove from run" }));
    await userEvent.click(screen.getByRole("button", { name: "Remove student" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/already started, so they can't be removed/);
  });
});
