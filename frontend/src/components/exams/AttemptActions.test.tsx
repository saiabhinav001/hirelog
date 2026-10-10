import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AttemptActions } from "@/components/exams/AttemptActions";
import { ExamApiError } from "@/lib/examsApi";
import type { Attempt } from "@/lib/examEndpoints";

const extendTime = vi.fn();
const terminateAttempt = vi.fn();
const recoverAttempt = vi.fn();

vi.mock("@/lib/examEndpoints", () => ({
  examEndpoints: {
    extendTime: (...a: unknown[]) => extendTime(...a),
    terminateAttempt: (...a: unknown[]) => terminateAttempt(...a),
    recoverAttempt: (...a: unknown[]) => recoverAttempt(...a),
  },
}));

const getToken = async () => "tok";

function attempt(status: Attempt["status"]): Attempt {
  return { run_id: "r1", uid: "stu-1", status, version: 1, epoch: 1, deadline: "2026-06-01T11:00:00Z", warning_count: 0, current_slot_index: 0, submit_receipt: null };
}

function setup(status: Attempt["status"]) {
  const onChanged = vi.fn();
  const onFeedback = vi.fn();
  render(<AttemptActions runId="r1" attempt={attempt(status)} getToken={getToken} onChanged={onChanged} onFeedback={onFeedback} />);
  return { onChanged, onFeedback };
}

beforeEach(() => {
  [extendTime, terminateAttempt, recoverAttempt].forEach((m) => m.mockReset().mockResolvedValue({}));
});

describe("AttemptActions", () => {
  it("offers only the actions that fit the attempt's state", () => {
    const { unmount } = render(<AttemptActions runId="r1" attempt={attempt("in_progress")} getToken={getToken} onChanged={vi.fn()} onFeedback={vi.fn()} />);
    expect(screen.getByRole("button", { name: /Extend time for stu-1/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /End attempt for stu-1/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Recover/ })).not.toBeInTheDocument();
    unmount();

    const second = render(<AttemptActions runId="r1" attempt={attempt("expired")} getToken={getToken} onChanged={vi.fn()} onFeedback={vi.fn()} />);
    expect(screen.getByRole("button", { name: /Recover grading for stu-1/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Extend/ })).not.toBeInTheDocument();
    second.unmount();

    render(<AttemptActions runId="r1" attempt={attempt("not_started")} getToken={getToken} onChanged={vi.fn()} onFeedback={vi.fn()} />);
    expect(screen.queryAllByRole("button")).toHaveLength(0);
  });

  it("extends only after a reason and confirmation, naming the student, and refetches", async () => {
    const { onChanged, onFeedback } = setup("in_progress");
    await userEvent.click(screen.getByRole("button", { name: /Extend time for/ }));
    expect(screen.getByText("stu-1")).toBeInTheDocument();
    expect(extendTime).not.toHaveBeenCalled();
    const confirm = screen.getByRole("button", { name: "Extend time" });
    expect(confirm).toBeDisabled();
    await userEvent.type(screen.getByLabelText(/Reason/), "Power cut");
    await userEvent.click(confirm);
    await waitFor(() => expect(extendTime).toHaveBeenCalledTimes(1));
    const args = extendTime.mock.calls[0];
    expect(args[0]).toBe("tok");
    expect(args[1]).toBe("r1");
    expect(args[2]).toBe("stu-1");
    expect(args[3]).toBe(10);
    expect(typeof args[4]).toBe("string"); // idempotency key
    expect(args[5]).toBe("Power cut");
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
    expect(onFeedback).toHaveBeenCalledWith({ kind: "ok", text: "Added 10 minutes for stu-1." });
  });

  it("cancel sends nothing", async () => {
    setup("in_progress");
    await userEvent.click(screen.getByRole("button", { name: /End attempt for/ }));
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(terminateAttempt).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("ending an attempt says answers are kept and cannot be reopened, and requires a reason", async () => {
    const { onFeedback } = setup("in_progress");
    await userEvent.click(screen.getByRole("button", { name: /End attempt for/ }));
    expect(screen.getByText(/cannot be reopened/)).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText(/Reason/), "Left the room");
    await userEvent.click(screen.getByRole("button", { name: "End attempt" }));
    await waitFor(() => expect(terminateAttempt).toHaveBeenCalledTimes(1));
    expect(terminateAttempt.mock.calls[0][4]).toBe("Left the room");
    await waitFor(() => expect(onFeedback).toHaveBeenCalledWith({ kind: "ok", text: expect.stringContaining("answers were kept") }));
  });

  it("explains a conflict in plain words and still refetches", async () => {
    extendTime.mockRejectedValue(new ExamApiError("conflict", 409, "deadline_passed"));
    const { onChanged, onFeedback } = setup("in_progress");
    await userEvent.click(screen.getByRole("button", { name: /Extend time for/ }));
    await userEvent.type(screen.getByLabelText(/Reason/), "late");
    await userEvent.click(screen.getByRole("button", { name: "Extend time" }));
    await waitFor(() => expect(onFeedback).toHaveBeenCalled());
    expect(onFeedback.mock.calls[0][0].kind).toBe("error");
    expect(onFeedback.mock.calls[0][0].text).toMatch(/time has already run out|already run out/);
    expect(onChanged).toHaveBeenCalled();
  });

  it("recovers a sealed attempt with a reason", async () => {
    setup("expired");
    await userEvent.click(screen.getByRole("button", { name: /Recover grading/ }));
    await userEvent.type(screen.getByLabelText(/Reason/), "grading lost");
    await userEvent.click(screen.getByRole("button", { name: "Recover" }));
    await waitFor(() => expect(recoverAttempt).toHaveBeenCalledTimes(1));
  });
});
