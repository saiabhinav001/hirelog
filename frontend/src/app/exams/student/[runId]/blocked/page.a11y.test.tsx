import { render, screen } from "@testing-library/react";
import { beforeEach, describe, it, vi } from "vitest";

import { expectNoAxeViolations } from "@/test/axe";

vi.hoisted(() => {
  process.env.NEXT_PUBLIC_EXAMS_ENABLED = "true";
});

const getState = vi.fn();
const listMyIncidents = vi.fn();
const getToken = async () => "tok";

vi.mock("next/navigation", () => ({
  useParams: () => ({ runId: "run-1" }),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));
vi.mock("@/lib/examAuth", () => ({
  useExamAuth: () => ({ user: { uid: "s1" }, loading: false, isFaculty: false, isStudent: true, getToken }),
}));
vi.mock("@/lib/liveSocket", () => ({ useLiveSocket: () => ({ health: "connected", lastEventAt: null }) }));
vi.mock("@/lib/examEndpoints", () => ({
  examEndpoints: {
    getState: (...a: unknown[]) => getState(...a),
    listMyIncidents: (...a: unknown[]) => listMyIncidents(...a),
    submitAppeal: vi.fn(),
  },
}));

import BlockedPage from "@/app/exams/student/[runId]/blocked/page";

const attempt = { run_id: "run-1", uid: "s1", status: "blocked", version: 1, epoch: 1, deadline: null, warning_count: 3, current_slot_index: 0, submit_receipt: null };
const counted = { id: "c1", kind: "page_hidden", counted: true, client_timestamp: "2026-06-01T10:00:00Z", received_at: "2026-06-01T10:00:01Z", duration_seconds: 9, note: "counted departure", source: "browser" };

beforeEach(() => {
  [getState, listMyIncidents].forEach((m) => m.mockReset());
  getState.mockResolvedValue(attempt);
  listMyIncidents.mockResolvedValue({ results: [counted] });
});

describe("Blocked page - accessibility", () => {
  it("has no automatically detectable violations (status, incident list and appeal form)", async () => {
    const { container } = render(<BlockedPage />);
    await screen.findByText(/Your work is preserved/);
    await expectNoAxeViolations(container);
  });
});
