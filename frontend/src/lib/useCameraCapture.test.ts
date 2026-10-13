import { renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useCameraCapture } from "@/lib/useCameraCapture";

describe("useCameraCapture — camera_waived accommodation", () => {
  const originalFetch = global.fetch;
  const getToken = async () => "token"; // stable, like the page's: a new getter every render would re-run the capture effect

  beforeEach(() => {
    global.fetch = vi.fn().mockResolvedValue({ json: async () => ({}) });
  });

  afterEach(() => {
    global.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it("never requests camera permission when cameraWaived is true", async () => {
    const getUserMediaSpy = vi.fn();
    Object.defineProperty(navigator, "mediaDevices", { value: { getUserMedia: getUserMediaSpy }, configurable: true });

    renderHook(() => useCameraCapture("run-1", async () => "token", true));
    await vi.waitFor(() => expect(global.fetch).toHaveBeenCalled());

    expect(getUserMediaSpy).not.toHaveBeenCalled();
  });

  it("records an honest camera_waived_accommodation missing-evidence reason", async () => {
    renderHook(() => useCameraCapture("run-1", async () => "token", true));

    await vi.waitFor(() => expect(global.fetch).toHaveBeenCalled());
    const [url, init] = (global.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(String(url)).toContain("/snapshots/missing");
    expect(JSON.parse(init.body as string)).toEqual({ kind: "check_in", reason: "camera_waived_accommodation" });
  });

  it("reports permission state as 'waived'", async () => {
    const { result } = renderHook(() => useCameraCapture("run-1", async () => "token", true));
    await vi.waitFor(() => expect(result.current.state.permission).toBe("waived"));
  });

  it("does nothing at all when runId is null, waived or not", async () => {
    renderHook(() => useCameraCapture(null, async () => "token", true));
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it("never starts the camera or calls the server when the server cannot keep snapshots", async () => {
    const getUserMediaSpy = vi.fn();
    Object.defineProperty(navigator, "mediaDevices", { value: { getUserMedia: getUserMediaSpy }, configurable: true });

    const { result } = renderHook(() => useCameraCapture("run-1", getToken, false, false));
    await result.current.captureNow("submission");
    await new Promise((resolve) => setTimeout(resolve, 50));

    expect(getUserMediaSpy).not.toHaveBeenCalled();
    expect(global.fetch).not.toHaveBeenCalled();
    expect(result.current.state).toEqual({ lastCaptureAt: null, permission: "off" });
  });

  it("keeps capturing by default so servers that predate the flag behave as before", async () => {
    const getUserMediaSpy = vi.fn().mockRejectedValue(new Error("denied"));
    Object.defineProperty(navigator, "mediaDevices", { value: { getUserMedia: getUserMediaSpy }, configurable: true });

    renderHook(() => useCameraCapture("run-1", getToken));

    await vi.waitFor(() => expect(getUserMediaSpy).toHaveBeenCalledTimes(1));
  });
});
