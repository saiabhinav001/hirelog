import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const examApiFetchMock = vi.fn();
vi.mock("@/lib/examsApi", () => ({
  examApiFetch: (...args: unknown[]) => examApiFetchMock(...args),
}));

import { useLiveSocket } from "@/lib/liveSocket";

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;
  closeCalls = 0;

  constructor(public url: string) {
    FakeWebSocket.instances.push(this);
  }

  send(): void {}

  close(): void {
    this.closeCalls += 1;
    this.onclose?.();
  }

  emitOpen(): void {
    this.onopen?.();
  }

  emitMessage(data: unknown): void {
    this.onmessage?.({ data: JSON.stringify(data) });
  }
}

function latestSocket(): FakeWebSocket {
  return FakeWebSocket.instances[FakeWebSocket.instances.length - 1];
}

describe("useLiveSocket", () => {
  beforeEach(() => {
    FakeWebSocket.instances = [];
    examApiFetchMock.mockReset();
    examApiFetchMock.mockResolvedValue({ ticket: "tok-1" });
    vi.stubGlobal("WebSocket", FakeWebSocket as unknown as typeof WebSocket);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("mints a fresh ticket and opens a socket, reporting connected on open", async () => {
    const onReconcile = vi.fn();
    const { result } = renderHook(() =>
      useLiveSocket({ runId: "run-1", getToken: async () => "id-token", onReconcile })
    );

    await waitFor(() => expect(FakeWebSocket.instances.length).toBe(1));
    expect(examApiFetchMock).toHaveBeenCalledWith(
      "/api/exams/v1/live/ticket",
      expect.objectContaining({ method: "POST", body: JSON.stringify({ run_id: "run-1" }) }),
      "id-token"
    );

    act(() => latestSocket().emitOpen());
    await waitFor(() => expect(result.current.health).toBe("connected"));
  });

  it("reconciles once per snapshot event", async () => {
    const onReconcile = vi.fn();
    renderHook(() => useLiveSocket({ runId: "run-1", getToken: async () => undefined, onReconcile }));
    await waitFor(() => expect(FakeWebSocket.instances.length).toBe(1));
    act(() => latestSocket().emitOpen());

    act(() => latestSocket().emitMessage({ seq: 5, kind: "snapshot", state_version: 2, payload: {} }));

    await waitFor(() => expect(onReconcile).toHaveBeenCalledTimes(1));
  });

  it("dedupes a duplicate/stale seq without calling onReconcile again", async () => {
    const onReconcile = vi.fn();
    renderHook(() => useLiveSocket({ runId: "run-1", getToken: async () => undefined, onReconcile }));
    await waitFor(() => expect(FakeWebSocket.instances.length).toBe(1));
    act(() => latestSocket().emitOpen());

    act(() => latestSocket().emitMessage({ seq: 5, kind: "snapshot", state_version: 2, payload: {} }));
    await waitFor(() => expect(onReconcile).toHaveBeenCalledTimes(1));

    // Same seq replayed (e.g. a race between reconnect and a late frame) —
    // must NOT trigger a second reconcile.
    act(() => latestSocket().emitMessage({ seq: 5, kind: "run_state_changed", state_version: 2, payload: {} }));
    expect(onReconcile).toHaveBeenCalledTimes(1);

    // An older seq than what we've already seen — also dropped.
    act(() => latestSocket().emitMessage({ seq: 3, kind: "run_state_changed", state_version: 2, payload: {} }));
    expect(onReconcile).toHaveBeenCalledTimes(1);
  });

  it("reconciles on a new in-order seq and on a gapped seq alike", async () => {
    const onReconcile = vi.fn();
    renderHook(() => useLiveSocket({ runId: "run-1", getToken: async () => undefined, onReconcile }));
    await waitFor(() => expect(FakeWebSocket.instances.length).toBe(1));
    act(() => latestSocket().emitOpen());
    act(() => latestSocket().emitMessage({ seq: 1, kind: "snapshot", state_version: 1, payload: {} }));
    await waitFor(() => expect(onReconcile).toHaveBeenCalledTimes(1));

    // In-order (seq 2 after snapshot seq 1).
    act(() => latestSocket().emitMessage({ seq: 2, kind: "run_state_changed", state_version: 1, payload: {} }));
    expect(onReconcile).toHaveBeenCalledTimes(2);

    // Gapped (seq jumps to 9) — still reconciles rather than trusting the payload.
    act(() => latestSocket().emitMessage({ seq: 9, kind: "run_state_changed", state_version: 1, payload: {} }));
    expect(onReconcile).toHaveBeenCalledTimes(3);
  });

  it("ignores heartbeat events entirely", async () => {
    const onReconcile = vi.fn();
    renderHook(() => useLiveSocket({ runId: "run-1", getToken: async () => undefined, onReconcile }));
    await waitFor(() => expect(FakeWebSocket.instances.length).toBe(1));
    act(() => latestSocket().emitOpen());

    act(() => latestSocket().emitMessage({ seq: 1, kind: "heartbeat", state_version: 1, payload: {} }));

    expect(onReconcile).not.toHaveBeenCalled();
  });

  it("does not open a socket when runId is null", () => {
    renderHook(() => useLiveSocket({ runId: null, getToken: async () => undefined, onReconcile: vi.fn() }));
    expect(FakeWebSocket.instances.length).toBe(0);
  });

  it("closes the socket on unmount", async () => {
    const { unmount } = renderHook(() =>
      useLiveSocket({ runId: "run-1", getToken: async () => undefined, onReconcile: vi.fn() })
    );
    await waitFor(() => expect(FakeWebSocket.instances.length).toBe(1));
    const socket = latestSocket();
    unmount();
    expect(socket.closeCalls).toBe(1);
  });
});
