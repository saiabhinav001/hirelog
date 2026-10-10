import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { BlueprintPanel } from "@/components/exams/BlueprintPanel";
import type { ApprovedSourceSummary, BlueprintCapacity, GenerationCandidate, PublicationPreview } from "@/lib/examEndpoints";

const endpoints = vi.hoisted(() => ({
  getBlueprintCapacity: vi.fn(),
  listCandidatesForBlueprint: vi.fn(),
  getPublishPreview: vi.fn(),
  generateForBlueprint: vi.fn(),
  reviewCandidate: vi.fn(),
  editCandidate: vi.fn(),
  setBlueprintShortfall: vi.fn(),
  publishBlueprint: vi.fn(),
}));
vi.mock("@/lib/examEndpoints", () => ({ examEndpoints: endpoints }));

const getToken = async () => "token";
const sources: ApprovedSourceSummary[] = [
  { id: "s1", kind: "upload", review_status: "approved" },
  { id: "s2", kind: "upload", review_status: "pending" },
];

function capacity(overrides: Partial<BlueprintCapacity> = {}): BlueprintCapacity {
  return {
    blueprint_id: "bp1",
    slots: [],
    totals: { requested: 4, generated: 3, valid: 3, approved: 1, pending_review: 2, needs_revision: 0, rejected: 0, shortfall: 3 },
    empty_slots: [],
    short_slots: [0],
    meets_requirement: false,
    shortfall_accepted: false,
    publishable: false,
    ...overrides,
  };
}

function preview(overrides: Partial<PublicationPreview> = {}): PublicationPreview {
  return {
    blueprint_id: "bp1",
    slot_count: 1,
    total_marks: 5,
    requested_variants_total: 4,
    approved_variants_total: 1,
    shortfall_accepted: false,
    blockers: ["slot 0: 1 approved of 4 requested variants - approve more, or explicitly accept the shortfall to publish anyway"],
    warnings: [],
    publishable: false,
    ...overrides,
  };
}

function candidate(overrides: Partial<GenerationCandidate> = {}): GenerationCandidate {
  return { id: "c1", item_type: "single_choice", public_prompt: "Which organelle absorbs light?", status: "ready_for_human_review", marks: 5, ...overrides };
}

function renderPanel() {
  return render(<BlueprintPanel definitionId="def1" blueprint={{ id: "bp1", title: "Midterm" }} sources={sources} getToken={getToken} />);
}

describe("BlueprintPanel", () => {
  beforeEach(() => {
    Object.values(endpoints).forEach((fn) => fn.mockReset());
    endpoints.getBlueprintCapacity.mockResolvedValue(capacity());
    endpoints.listCandidatesForBlueprint.mockResolvedValue({ results: [candidate()] });
    endpoints.getPublishPreview.mockResolvedValue(preview());
    endpoints.generateForBlueprint.mockResolvedValue({ results: [], remaining_slots: [] });
    endpoints.reviewCandidate.mockResolvedValue({});
    endpoints.setBlueprintShortfall.mockResolvedValue({ blueprint_id: "bp1", shortfall_accepted: true });
    endpoints.publishBlueprint.mockResolvedValue({ id: "rev1", revision_number: 2 });
    endpoints.editCandidate.mockResolvedValue({ kind: "new_version", candidate: candidate() });
  });

  it("shows requested, generated, valid and approved counts and the server's publishing blockers", async () => {
    renderPanel();
    expect(await screen.findByTestId("capacity-summary")).toHaveTextContent("Requested 4 · generated 3 · valid 3 · approved 1 · 2 awaiting your review");
    expect(screen.getByRole("list", { name: "Publishing blockers" })).toHaveTextContent("1 approved of 4 requested variants");
    expect(screen.getByRole("button", { name: "Publish this blueprint" })).toBeDisabled();
  });

  it("generates only from approved sources", async () => {
    const user = userEvent.setup();
    renderPanel();
    await screen.findByTestId("capacity-summary");
    await user.click(screen.getByRole("button", { name: "Generate questions" }));
    await waitFor(() => expect(endpoints.generateForBlueprint).toHaveBeenCalledWith("token", "bp1", ["s1"]));
  });

  it("reports withheld source passages and slots that did not generate", async () => {
    endpoints.generateForBlueprint.mockResolvedValue({
      results: [{ slot_index: 0, status: "failed", ready_count: 0, shortfall: 4, error: "timeout: slow", excluded_anchors: [{ source_id: "s1", anchor_id: "x", reason: "looks_like_instructions" }] }],
      remaining_slots: [],
    });
    const user = userEvent.setup();
    renderPanel();
    await screen.findByTestId("capacity-summary");
    await user.click(screen.getByRole("button", { name: "Generate questions" }));
    expect(await screen.findByRole("status")).toHaveTextContent(/did not generate: timeout: slow.*withheld because they read like instructions/);
  });

  it("only a teacher click approves or rejects", async () => {
    const user = userEvent.setup();
    renderPanel();
    await screen.findByText(/Which organelle/);
    expect(endpoints.reviewCandidate).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(endpoints.reviewCandidate).toHaveBeenCalledWith("token", "c1", "approve", undefined));
  });

  it("an edit warns that approval resets and sends only the question text", async () => {
    const user = userEvent.setup();
    renderPanel();
    await user.click(await screen.findByRole("button", { name: "Edit" }));
    const box = screen.getByRole("textbox", { name: "Question text" });
    expect(screen.getByText(/must be approved again/)).toBeInTheDocument();
    await user.clear(box);
    await user.type(box, "Which structure captures sunlight?");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(endpoints.editCandidate).toHaveBeenCalledWith("token", "c1", { public_prompt: "Which structure captures sunlight?" }));
    expect(await screen.findByText(/new version. It needs to be approved again/)).toBeInTheDocument();
  });

  it("accepting a shortfall needs a reason", async () => {
    const user = userEvent.setup();
    renderPanel();
    const accept = await screen.findByRole("button", { name: "Accept shortfall" });
    expect(accept).toBeDisabled();
    await user.type(screen.getByLabelText("Reason for publishing with fewer variants"), "small cohort");
    await user.click(accept);
    await waitFor(() => expect(endpoints.setBlueprintShortfall).toHaveBeenCalledWith("token", "bp1", true, "small cohort"));
  });

  it("offers no shortfall option while a slot has nothing approved, and no publish", async () => {
    endpoints.getBlueprintCapacity.mockResolvedValue(capacity({ empty_slots: [0], short_slots: [] }));
    renderPanel();
    await screen.findByTestId("capacity-summary");
    expect(screen.queryByRole("button", { name: "Accept shortfall" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Publish this blueprint" })).toBeDisabled();
  });

  it("publishes once the server says it is publishable", async () => {
    endpoints.getPublishPreview.mockResolvedValue(preview({ blockers: [], publishable: true, shortfall_accepted: true }));
    endpoints.getBlueprintCapacity.mockResolvedValue(capacity({ shortfall_accepted: true, publishable: true }));
    const user = userEvent.setup();
    renderPanel();
    const publish = await screen.findByRole("button", { name: "Publish this blueprint" });
    await waitFor(() => expect(publish).toBeEnabled());
    expect(screen.getByText("Shortfall accepted")).toBeInTheDocument();
    await user.click(publish);
    await waitFor(() => expect(endpoints.publishBlueprint).toHaveBeenCalledWith("token", "def1", "bp1"));
    expect(await screen.findByText("Published as revision 2.")).toBeInTheDocument();
  });

  it("hides superseded versions and explains why a candidate needs revision", async () => {
    endpoints.listCandidatesForBlueprint.mockResolvedValue({
      results: [
        candidate({ id: "old", status: "superseded", public_prompt: "Old wording of the question" }),
        candidate({ id: "bad", status: "needs_revision", public_prompt: "Broken one", validation_issues: [{ severity: "blocker", field: "private_expected_answer", reason: "the answer key names option(s) ['Z'] that do not exist" }] }),
      ],
    });
    renderPanel();
    expect(await screen.findByText(/Broken one/)).toBeInTheDocument();
    expect(screen.queryByText(/Old wording/)).not.toBeInTheDocument();
    expect(screen.getByText(/answer key names option/)).toBeInTheDocument();
  });
});
