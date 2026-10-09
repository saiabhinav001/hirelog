import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { examApiFetch, examApiUpload } from "@/lib/examsApi";

describe("examApiFetch", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("does not force a JSON content type onto a multipart body", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, text: async () => "{}" });
    vi.stubGlobal("fetch", fetchMock);
    await examApiFetch("/x", { method: "POST", body: new FormData() }, "tok");
    const headers = fetchMock.mock.calls[0][1].headers as Headers;
    expect(headers.has("Content-Type")).toBe(false);
    expect(headers.get("Authorization")).toBe("Bearer tok");
  });

  it("still sends JSON bodies as JSON", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, text: async () => "{}" });
    vi.stubGlobal("fetch", fetchMock);
    await examApiFetch("/x", { method: "POST", body: "{}" });
    expect((fetchMock.mock.calls[0][1].headers as Headers).get("Content-Type")).toBe("application/json");
  });

  it("shows the message of a structured error instead of a JSON blob", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, text: async () => JSON.stringify({ detail: { code: "invalid_file", message: "The PDF is encrypted." } }) }));
    await expect(examApiFetch("/x")).rejects.toThrow("The PDF is encrypted.");
  });

  it("keeps plain string errors unchanged", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, text: async () => JSON.stringify({ detail: "file too large" }) }));
    await expect(examApiFetch("/x")).rejects.toThrow("file too large");
  });
});

class FakeXhr {
  static last: FakeXhr;
  upload: { onprogress?: (e: { lengthComputable: boolean; loaded: number; total: number }) => void } = {};
  onload?: () => void;
  onerror?: () => void;
  status = 0;
  responseText = "";
  headers: Record<string, string> = {};
  sent?: FormData;
  opened?: [string, string];
  constructor() {
    FakeXhr.last = this;
  }
  open(method: string, url: string) {
    this.opened = [method, url];
  }
  setRequestHeader(name: string, value: string) {
    this.headers[name] = value;
  }
  send(body: FormData) {
    this.sent = body;
  }
}

describe("examApiUpload", () => {
  beforeEach(() => vi.stubGlobal("XMLHttpRequest", FakeXhr));
  afterEach(() => vi.unstubAllGlobals());

  it("posts the file as multipart with the bearer token and reports progress", async () => {
    const progress = vi.fn();
    const file = new File(["hello"], "notes.txt", { type: "text/plain" });
    const promise = examApiUpload<{ upload_id: string }>("/api/exams/v1/faculty/kt/uploads", file, "tok", progress);
    const xhr = FakeXhr.last;
    expect(xhr.opened?.[0]).toBe("POST");
    expect(xhr.headers.Authorization).toBe("Bearer tok");
    expect(xhr.headers["Content-Type"]).toBeUndefined();
    expect((xhr.sent as FormData).get("file")).toBe(file);

    xhr.upload.onprogress?.({ lengthComputable: true, loaded: 50, total: 200 });
    expect(progress).toHaveBeenCalledWith(0.25);
    xhr.status = 202;
    xhr.responseText = JSON.stringify({ upload_id: "u1" });
    xhr.onload?.();
    await expect(promise).resolves.toEqual({ upload_id: "u1" });
  });

  it("rejects with the server's message on a 4xx", async () => {
    const promise = examApiUpload("/x", new File(["a"], "a.pdf"), "tok");
    const xhr = FakeXhr.last;
    xhr.status = 422;
    xhr.responseText = JSON.stringify({ detail: { code: "invalid_file", message: "file extension does not match its content" } });
    xhr.onload?.();
    await expect(promise).rejects.toThrow("file extension does not match its content");
  });

  it("rejects with a plain-language message when the network fails", async () => {
    const promise = examApiUpload("/x", new File(["a"], "a.txt"), "tok");
    FakeXhr.last.onerror?.();
    await expect(promise).rejects.toThrow(/could not reach the server/);
  });
});
