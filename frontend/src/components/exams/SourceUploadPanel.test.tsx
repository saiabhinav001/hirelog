import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SourceUploadPanel } from "@/components/exams/SourceUploadPanel";
import type { KtUpload, KtUploadDetail } from "@/lib/examEndpoints";

const endpoints = vi.hoisted(() => ({
  listKtUploads: vi.fn(),
  getKtUpload: vi.fn(),
  uploadKtFile: vi.fn(),
  replaceKtUpload: vi.fn(),
  retryKtUpload: vi.fn(),
  deleteKtUpload: vi.fn(),
  registerSourceFromUpload: vi.fn(),
}));
vi.mock("@/lib/examEndpoints", () => ({ examEndpoints: endpoints }));

function upload(overrides: Partial<KtUpload> = {}): KtUpload {
  return {
    id: "u1",
    filename: "notes.pdf",
    kind: "pdf",
    source_kind: "file",
    size_bytes: 2048,
    status: "parsed",
    failure_code: null,
    failure_reason: null,
    ingest_attempt: 1,
    final_url: null,
    latest_extracted_version_id: "v1",
    replaces_upload_id: null,
    superseded_by_upload_id: null,
    created_at: "2026-06-01T09:00:00Z",
    updated_at: "2026-06-01T09:00:00Z",
    ...overrides,
  };
}

const getToken = async () => "token";

function setUploads(...items: KtUpload[]) {
  endpoints.listKtUploads.mockResolvedValue({ results: items });
}

describe("SourceUploadPanel", () => {
  beforeEach(() => {
    Object.values(endpoints).forEach((fn) => fn.mockReset());
    endpoints.retryKtUpload.mockResolvedValue({});
    endpoints.deleteKtUpload.mockResolvedValue({});
    endpoints.uploadKtFile.mockResolvedValue({ upload_id: "new", status: "quarantined" });
    endpoints.replaceKtUpload.mockResolvedValue({ upload_id: "new", status: "quarantined" });
  });
  afterEach(() => vi.useRealTimers());

  it("shows each document's status and the failure reason in plain words", async () => {
    setUploads(
      upload({ id: "a", filename: "scan.pdf", status: "parse_failed", failure_code: "no_text", failure_reason: "The PDF has no extractable text - it looks scanned." }),
      upload({ id: "b", filename: "virus.docx", status: "infected", failure_reason: "The malware scan flagged this file." }),
      upload({ id: "c", filename: "ok.txt", status: "parsed" }),
    );
    render(<SourceUploadPanel getToken={getToken} />);
    expect(await screen.findByText("Could not be read")).toBeInTheDocument();
    expect(screen.getByText(/looks scanned/)).toBeInTheDocument();
    expect(screen.getByText("Blocked by malware scan")).toBeInTheDocument();
    expect(screen.getByText("Ready")).toBeInTheDocument();
  });

  it("offers Retry only where retrying can help", async () => {
    setUploads(
      upload({ id: "a", filename: "a.pdf", status: "parse_failed", failure_code: "parse_timeout", failure_reason: "too slow" }),
      upload({ id: "b", filename: "b.pdf", status: "parse_failed", failure_code: "storage_missing", failure_reason: "gone" }),
      upload({ id: "c", filename: "c.pdf", status: "scan_unavailable", failure_reason: "scanner down" }),
      upload({ id: "d", filename: "d.pdf", status: "infected", failure_reason: "flagged" }),
      upload({ id: "e", filename: "e.pdf", status: "parsed" }),
    );
    render(<SourceUploadPanel getToken={getToken} />);
    await screen.findByTestId("upload-a");
    const hasRetry = (id: string) => within(screen.getByTestId(`upload-${id}`)).queryByRole("button", { name: "Retry" }) !== null;
    expect(["a", "b", "c", "d", "e"].map(hasRetry)).toEqual([true, false, true, false, false]);
  });

  it("retries a failed upload and reloads the list", async () => {
    setUploads(upload({ id: "a", status: "parse_failed", failure_code: "parse_crash", failure_reason: "stopped" }));
    render(<SourceUploadPanel getToken={getToken} />);
    await userEvent.click(await screen.findByRole("button", { name: "Retry" }));
    await waitFor(() => expect(endpoints.retryKtUpload).toHaveBeenCalledWith("token", "a"));
    await waitFor(() => expect(endpoints.listKtUploads).toHaveBeenCalledTimes(2));
  });

  it("asks for confirmation before deleting and does nothing on cancel", async () => {
    setUploads(upload({ id: "a" }));
    const changed = vi.fn();
    render(<SourceUploadPanel getToken={getToken} onSourcesChanged={changed} />);
    await userEvent.click(await screen.findByRole("button", { name: "Delete" }));
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(endpoints.deleteKtUpload).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "Delete" }));
    await userEvent.click(screen.getByRole("button", { name: "Confirm delete" }));
    await waitFor(() => expect(endpoints.deleteKtUpload).toHaveBeenCalledWith("token", "a"));
    await waitFor(() => expect(changed).toHaveBeenCalled());
  });

  it("uploads several files one after another, showing progress, and reports a failure per file", async () => {
    setUploads();
    let report: ((fraction: number) => void) | undefined;
    endpoints.uploadKtFile.mockImplementation(async (_token: string, file: File, onProgress: (f: number) => void) => {
      if (file.name === "bad.pdf") throw new Error("content-type does not match PDF magic bytes");
      report = onProgress;
      await new Promise((resolve) => setTimeout(resolve, 5));
      return { upload_id: file.name, status: "quarantined" };
    });
    render(<SourceUploadPanel getToken={getToken} />);
    const input = await screen.findByLabelText(/Upload documents/);
    const files = [new File(["a"], "one.pdf", { type: "application/pdf" }), new File(["b"], "bad.pdf", { type: "text/plain" }), new File(["c"], "two.txt", { type: "text/plain" })];
    fireEvent.change(input, { target: { files } });

    await waitFor(() => expect(screen.getByLabelText("Uploading one.pdf")).toBeInTheDocument());
    act(() => report?.(0.5));
    expect(screen.getByText("50%")).toBeInTheDocument();

    await waitFor(() => expect(endpoints.uploadKtFile).toHaveBeenCalledTimes(3));
    expect(await screen.findByRole("alert")).toHaveTextContent("bad.pdf: content-type does not match PDF magic bytes");
    await waitFor(() => expect(screen.queryByLabelText("Uploading one.pdf")).not.toBeInTheDocument());
  });

  it("polls while something is still being processed and stops when everything has settled", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    endpoints.listKtUploads
      .mockResolvedValueOnce({ results: [upload({ id: "a", status: "parsing" })] })
      .mockResolvedValue({ results: [upload({ id: "a", status: "parsed" })] });
    render(<SourceUploadPanel getToken={getToken} />);
    expect(await screen.findByText("Reading the document")).toBeInTheDocument();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3100);
    });
    expect(await screen.findByText("Ready")).toBeInTheDocument();
    const calls = endpoints.listKtUploads.mock.calls.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(endpoints.listKtUploads.mock.calls.length).toBe(calls);
  });

  it("lists what could not be read before the teacher relies on a source, then registers it", async () => {
    setUploads(upload({ id: "a", filename: "slides.docx", kind: "docx" }));
    const detail: KtUploadDetail = {
      ...upload({ id: "a", filename: "slides.docx", kind: "docx" }),
      versions: [{
        id: "v1", version_number: 1, anchor_count: 12, total_pages: null, truncated: false,
        warnings: [
          { code: "tables_not_extracted", message: "2 table(s) were not extracted; their text is missing from the source.", count: 2, pages: [] },
          { code: "images_not_extracted", message: "1 embedded image(s) were not read (no OCR); text inside images is missing.", count: 1, pages: [] },
        ],
      }],
      sources: [],
    };
    endpoints.getKtUpload.mockResolvedValueOnce(detail).mockResolvedValue({ ...detail, sources: [{ id: "s1", review_status: "pending" }] });
    endpoints.registerSourceFromUpload.mockResolvedValue({ id: "s1" });
    render(<SourceUploadPanel getToken={getToken} />);

    await userEvent.click(await screen.findByRole("button", { name: "Details" }));
    expect(await screen.findByText(/12 passages extracted/)).toBeInTheDocument();
    expect(screen.getByText(/Not read/)).toBeInTheDocument();
    expect(screen.getByText(/2 table\(s\) were not extracted/)).toBeInTheDocument();
    expect(screen.getByText(/text inside images is missing/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Use as reference material" }));
    await waitFor(() => expect(endpoints.registerSourceFromUpload).toHaveBeenCalledWith("token", "a"));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Use as reference material" })).not.toBeInTheDocument());
  });

  it("says so plainly when nothing was skipped", async () => {
    setUploads(upload({ id: "a" }));
    endpoints.getKtUpload.mockResolvedValue({
      ...upload({ id: "a" }),
      versions: [{ id: "v1", version_number: 1, anchor_count: 3, total_pages: 2, truncated: true, warnings: [] }],
      sources: [{ id: "s", review_status: "pending" }],
    });
    render(<SourceUploadPanel getToken={getToken} />);
    await userEvent.click(await screen.findByRole("button", { name: "Details" }));
    expect(await screen.findByText(/No unread content was detected/)).toBeInTheDocument();
    expect(screen.getByText(/longer than the limit/)).toBeInTheDocument();
    expect(screen.getByText(/from 2 pages/)).toBeInTheDocument();
  });

  it("replaces a document through the per-row file input", async () => {
    setUploads(upload({ id: "a", filename: "notes.pdf" }));
    render(<SourceUploadPanel getToken={getToken} />);
    const input = await screen.findByLabelText("Replace notes.pdf");
    const file = new File(["v2"], "notes-v2.pdf", { type: "application/pdf" });
    fireEvent.change(input, { target: { files: [file] } });
    await waitFor(() => expect(endpoints.replaceKtUpload).toHaveBeenCalledWith("token", "a", file, expect.any(Function)));
  });

  it("hides deleted uploads and marks superseded ones", async () => {
    setUploads(upload({ id: "gone", filename: "gone.pdf", status: "deleted" }), upload({ id: "old", filename: "old.pdf", superseded_by_upload_id: "new" }));
    render(<SourceUploadPanel getToken={getToken} />);
    expect(await screen.findByText("old.pdf")).toBeInTheDocument();
    expect(screen.queryByText("gone.pdf")).not.toBeInTheDocument();
    expect(screen.getByText("Replaced by a newer upload")).toBeInTheDocument();
  });
});
