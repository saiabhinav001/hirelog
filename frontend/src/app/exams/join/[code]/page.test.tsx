import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.hoisted(() => {
  process.env.NEXT_PUBLIC_EXAMS_ENABLED = "true";
});

const resolveShortLink = vi.fn();
const replaceMock = vi.fn();
const getToken = async () => "tok";

vi.mock("next/navigation", () => ({
  useParams: () => ({ code: "abcd2345" }),
  useRouter: () => ({ push: vi.fn(), replace: replaceMock }),
}));
vi.mock("@/lib/examAuth", () => ({
  useExamAuth: () => ({ user: { uid: "u1" }, loading: false, isFaculty: false, isStudent: true, getToken }),
}));
vi.mock("@/lib/examEndpoints", () => ({
  examEndpoints: { resolveShortLink: (...a: unknown[]) => resolveShortLink(...a) },
}));

import JoinPage from "@/app/exams/join/[code]/page";
import { ExamApiError } from "@/lib/examsApi";

beforeEach(() => {
  resolveShortLink.mockReset();
  replaceMock.mockReset();
});

describe("Join page", () => {
  it("resolves the code and goes to the readiness check - nothing else", async () => {
    resolveShortLink.mockResolvedValue({ run_id: "run-9" });
    render(<JoinPage />);
    await waitFor(() => expect(replaceMock).toHaveBeenCalledWith("/exams/student/run-9/preflight"));
    expect(resolveShortLink).toHaveBeenCalledWith("tok", "abcd2345");
  });

  it("gives one neutral message for an unknown, withdrawn or not-yours link", async () => {
    resolveShortLink.mockRejectedValue(new ExamApiError("not found", 404));
    render(<JoinPage />);
    expect(await screen.findByText(/isn't valid for your account, or it has been withdrawn/)).toBeInTheDocument();
    expect(replaceMock).not.toHaveBeenCalled();
  });

  it("tells the student to wait when rate limited", async () => {
    resolveShortLink.mockRejectedValue(new ExamApiError("slow down", 429));
    render(<JoinPage />);
    expect(await screen.findByText(/Too many attempts/)).toBeInTheDocument();
  });
});
