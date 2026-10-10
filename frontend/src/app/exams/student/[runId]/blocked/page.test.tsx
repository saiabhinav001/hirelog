import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.hoisted(() => {
  process.env.NEXT_PUBLIC_EXAMS_ENABLED = "true";
});

const getState = vi.fn();
const listMyIncidents = vi.fn();
const submitAppeal = vi.fn();
const replaceMock = vi.fn();
const getToken = async () => "tok";

vi.mock("next/navigation", () => ({
  useParams: () => ({ runId: "run-1" }),
  useRouter: () => ({ push: vi.fn(), replace: replaceMock }),
}));
vi.mock("@/lib/examAuth", () => ({
  useExamAuth: () => ({ user: { uid: "s1" }, loading: false, isFaculty: false, isStudent: true, getToken }),
}));
vi.mock("@/lib/liveSocket", () => ({ useLiveSocket: () => ({ health: "connected", lastEventAt: null }) }));
vi.mock("@/lib/examEndpoints", () => ({
  examEndpoints: {
    getState: (...a: unknown[]) => getState(...a),
    listMyIncidents: (...a: unknown[]) => listMyIncidents(...a),
    submitAppeal: (...a: unknown[]) => submitAppeal(...a),
  },
}));

import BlockedPage from "@/app/exams/student/[runId]/blocked/page";
import { ExamApiError } from "@/lib/examsApi";

const attempt = (status: string) => ({ run_id: "run-1", uid: "s1", status, version: 1, epoch: 1, deadline: null, warning_count: 3, current_slot_index: 0, submit_receipt: null });
const counted = { id: "c1", kind: "page_hidden", counted: true, client_timestamp: "2026-06-01T10:00:00Z", received_at: "2026-06-01T10:00:01Z", duration_seconds: 9, note: "counted departure", source: "browser" };

beforeEach(() => {
  [getState, listMyIncidents, submitAppeal, replaceMock].forEach((m) => m.mockReset());
  getState.mockResolvedValue(attempt("blocked"));
  listMyIncidents.mockResolvedValue({ results: [counted] });
});

describe("Blocked page", () => {
  it("explains the status: work kept, what was reported, and that it is not proof", async () => {
    render(<BlockedPage />);
    expect(await screen.findByText(/Your work is preserved/)).toBeInTheDocument();
    expect(screen.getByText(/client-reported, not cryptographic proof/)).toBeInTheDocument();
    expect(screen.getByText("page_hidden")).toBeInTheDocument();
    expect(replaceMock).not.toHaveBeenCalled();
  });

  it("shows the reviewer's decisions without any reviewer identity", async () => {
    listMyIncidents.mockResolvedValue({
      results: [counted, { ...counted, id: "r1", counted: false, source: "reviewer", note: "reviewer decision: upheld — block stands", actor_uid: null }],
    });
    render(<BlockedPage />);
    expect(await screen.findByText(/A reviewer decided: upheld — block stands/)).toBeInTheDocument();
  });

  it("says what an appeal does and does not do", async () => {
    render(<BlockedPage />);
    expect(await screen.findByText(/filing one does not change your attempt/)).toBeInTheDocument();
  });

  it("keeps the student's text and explains a refusal inline instead of replacing the page", async () => {
    submitAppeal.mockRejectedValue(new ExamApiError("slow down", 429));
    render(<BlockedPage />);
    await userEvent.type(await screen.findByLabelText("Appeal statement"), "My connection dropped");
    await userEvent.click(screen.getByRole("button", { name: "Submit appeal" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/Too many requests/);
    expect(screen.getByLabelText("Appeal statement")).toHaveValue("My connection dropped");
    expect(screen.getByText("Your exam session is paused")).toBeInTheDocument();
  });

  it("confirms a filed appeal as pending review", async () => {
    submitAppeal.mockResolvedValue({ id: "a1", status: "pending" });
    render(<BlockedPage />);
    await userEvent.type(await screen.findByLabelText("Appeal statement"), "Power cut");
    await userEvent.click(screen.getByRole("button", { name: "Submit appeal" }));
    await waitFor(() => expect(submitAppeal).toHaveBeenCalledWith("tok", "run-1", "Power cut"));
    expect(await screen.findByText(/pending faculty review/)).toBeInTheDocument();
  });

  it("moves the student back into the exam once a reviewer lets them continue", async () => {
    getState.mockResolvedValue(attempt("in_progress"));
    render(<BlockedPage />);
    await waitFor(() => expect(replaceMock).toHaveBeenCalledWith("/exams/student/run-1/exam"));
  });

  it("moves the student to the receipt if the window ended while blocked", async () => {
    getState.mockResolvedValue(attempt("expired"));
    render(<BlockedPage />);
    await waitFor(() => expect(replaceMock).toHaveBeenCalledWith("/exams/student/run-1/receipt"));
  });
});
