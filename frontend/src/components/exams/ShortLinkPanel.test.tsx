import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ShortLinkPanel } from "@/components/exams/ShortLinkPanel";
import { ExamApiError } from "@/lib/examsApi";

const getShortLink = vi.fn();
const issueShortLink = vi.fn();
const revokeShortLink = vi.fn();

vi.mock("@/lib/examEndpoints", () => ({
  examEndpoints: {
    getShortLink: (...a: unknown[]) => getShortLink(...a),
    issueShortLink: (...a: unknown[]) => issueShortLink(...a),
    revokeShortLink: (...a: unknown[]) => revokeShortLink(...a),
  },
}));

const getToken = async () => "tok";
const LINK = { code: "abcd2345", display_code: "ABCD-2345", created_at: "2026-06-01T10:00:00Z", created_by: "t1" };

beforeEach(() => {
  [getShortLink, issueShortLink, revokeShortLink].forEach((m) => m.mockReset());
});

describe("ShortLinkPanel", () => {
  it("says so when short links are not enabled", async () => {
    getShortLink.mockResolvedValue({ enabled: false, link: null });
    render(<ShortLinkPanel runId="r1" getToken={getToken} />);
    expect(await screen.findByText(/not enabled on this deployment/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Create join link" })).not.toBeInTheDocument();
  });

  it("creates a link when none exists", async () => {
    getShortLink.mockResolvedValueOnce({ enabled: true, link: null }).mockResolvedValue({ enabled: true, link: LINK });
    issueShortLink.mockResolvedValue({ enabled: true, link: LINK, created: true });
    render(<ShortLinkPanel runId="r1" getToken={getToken} />);
    await userEvent.click(await screen.findByRole("button", { name: "Create join link" }));
    await waitFor(() => expect(issueShortLink).toHaveBeenCalledWith("tok", "r1"));
    expect(await screen.findByRole("textbox", { name: "Join link" })).toHaveValue(`${window.location.origin}/exams/join/abcd2345`);
    expect(screen.getByLabelText("Code")).toHaveTextContent("ABCD-2345");
  });

  it("states that the link does not grant access", async () => {
    getShortLink.mockResolvedValue({ enabled: true, link: LINK });
    render(<ShortLinkPanel runId="r1" getToken={getToken} />);
    expect(await screen.findByText(/must be enrolled, and are admitted by you/)).toBeInTheDocument();
  });

  it("regenerating asks first and warns that the old link stops working", async () => {
    getShortLink.mockResolvedValue({ enabled: true, link: LINK });
    issueShortLink.mockResolvedValue({ enabled: true, link: LINK });
    render(<ShortLinkPanel runId="r1" getToken={getToken} />);
    await userEvent.click(await screen.findByRole("button", { name: "Regenerate" }));
    expect(screen.getByText(/stop working immediately/)).toBeInTheDocument();
    expect(issueShortLink).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Replace link" }));
    await waitFor(() => expect(issueShortLink).toHaveBeenCalledWith("tok", "r1", true));
  });

  it("revoking asks first, then revokes", async () => {
    getShortLink.mockResolvedValueOnce({ enabled: true, link: LINK }).mockResolvedValue({ enabled: true, link: null });
    revokeShortLink.mockResolvedValue({ revoked: 1 });
    render(<ShortLinkPanel runId="r1" getToken={getToken} />);
    await userEvent.click(await screen.findByRole("button", { name: "Revoke" }));
    expect(revokeShortLink).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Revoke link" }));
    await waitFor(() => expect(revokeShortLink).toHaveBeenCalledWith("tok", "r1"));
    expect(await screen.findByText("Join link revoked.")).toBeInTheDocument();
  });

  it("shows a plain-language error when the run no longer admits students", async () => {
    getShortLink.mockResolvedValue({ enabled: true, link: null });
    issueShortLink.mockRejectedValue(new ExamApiError("no", 409, "run_not_admitting"));
    render(<ShortLinkPanel runId="r1" getToken={getToken} />);
    await userEvent.click(await screen.findByRole("button", { name: "Create join link" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/no longer admits students/);
  });

  it("copies the link", async () => {
    getShortLink.mockResolvedValue({ enabled: true, link: LINK });
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    render(<ShortLinkPanel runId="r1" getToken={getToken} />);
    await userEvent.click(await screen.findByRole("button", { name: "Copy link" }));
    expect(writeText).toHaveBeenCalledWith(`${window.location.origin}/exams/join/abcd2345`);
    expect(await screen.findByText("Link copied.")).toBeInTheDocument();
  });
});
