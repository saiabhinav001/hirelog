import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useExamServiceWorker } from "@/lib/useExamServiceWorker";

function makeFakeServiceWorkerContainer(
  registration: {
    waiting: { postMessage: ReturnType<typeof vi.fn> } | null;
    addEventListener: ReturnType<typeof vi.fn>;
  },
  controller: object | null = null
) {
  const listeners = new Map<string, () => void>();
  return {
    controller,
    register: vi.fn().mockResolvedValue(registration),
    getRegistration: vi.fn().mockResolvedValue(registration),
    addEventListener: vi.fn((type: string, cb: () => void) => listeners.set(type, cb)),
    removeEventListener: vi.fn((type: string) => listeners.delete(type)),
    emit: (type: string) => listeners.get(type)?.(),
  };
}

describe("useExamServiceWorker", () => {
  const originalServiceWorker = (navigator as unknown as { serviceWorker?: unknown }).serviceWorker;

  afterEach(() => {
    Object.defineProperty(navigator, "serviceWorker", { value: originalServiceWorker, configurable: true });
    vi.restoreAllMocks();
  });

  it("registers the service worker scoped to /exams/", async () => {
    const registration = { waiting: null, addEventListener: vi.fn() };
    const fakeContainer = makeFakeServiceWorkerContainer(registration);
    Object.defineProperty(navigator, "serviceWorker", { value: fakeContainer, configurable: true });

    renderHook(() => useExamServiceWorker());
    await vi.waitFor(() => expect(fakeContainer.register).toHaveBeenCalledWith("/exams-sw.js", { scope: "/exams/" }));
  });

  it("releases a waiting worker when no attempt is active", async () => {
    const postMessage = vi.fn();
    const registration = { waiting: { postMessage }, addEventListener: vi.fn() };
    const fakeContainer = makeFakeServiceWorkerContainer(registration);
    Object.defineProperty(navigator, "serviceWorker", { value: fakeContainer, configurable: true });

    renderHook(() => useExamServiceWorker({ attemptActive: false }));
    await vi.waitFor(() => expect(postMessage).toHaveBeenCalledWith({ type: "SKIP_WAITING" }));
  });

  it("does NOT release a waiting worker while an attempt is active", async () => {
    const postMessage = vi.fn();
    const registration = { waiting: { postMessage }, addEventListener: vi.fn() };
    const fakeContainer = makeFakeServiceWorkerContainer(registration);
    Object.defineProperty(navigator, "serviceWorker", { value: fakeContainer, configurable: true });

    renderHook(() => useExamServiceWorker({ attemptActive: true }));
    await vi.waitFor(() => expect(fakeContainer.register).toHaveBeenCalled());
    expect(postMessage).not.toHaveBeenCalled();
  });

  it("releases a held-back update once attemptActive flips to false", async () => {
    const postMessage = vi.fn();
    const registration = { waiting: { postMessage }, addEventListener: vi.fn() };
    const fakeContainer = makeFakeServiceWorkerContainer(registration);
    Object.defineProperty(navigator, "serviceWorker", { value: fakeContainer, configurable: true });

    const { rerender } = renderHook(({ active }) => useExamServiceWorker({ attemptActive: active }), {
      initialProps: { active: true },
    });
    await vi.waitFor(() => expect(fakeContainer.register).toHaveBeenCalled());
    expect(postMessage).not.toHaveBeenCalled();

    rerender({ active: false });
    await vi.waitFor(() => expect(postMessage).toHaveBeenCalledWith({ type: "SKIP_WAITING" }));
  });

  it("does nothing (no throw) when the browser has no serviceWorker support", () => {
    Object.defineProperty(navigator, "serviceWorker", { value: undefined, configurable: true });
    expect(() => renderHook(() => useExamServiceWorker())).not.toThrow();
  });

  it("does NOT reload the page on the very first-ever SW activation (no prior controller)", async () => {
    const registration = { waiting: null, addEventListener: vi.fn() };
    const fakeContainer = makeFakeServiceWorkerContainer(registration, null); // no controller at mount
    Object.defineProperty(navigator, "serviceWorker", { value: fakeContainer, configurable: true });
    const reloadSpy = vi.fn();
    Object.defineProperty(window, "location", { value: { reload: reloadSpy }, configurable: true });

    renderHook(() => useExamServiceWorker());
    await vi.waitFor(() => expect(fakeContainer.register).toHaveBeenCalled());

    fakeContainer.emit("controllerchange"); // first-ever activation fires this too
    expect(reloadSpy).not.toHaveBeenCalled();
  });

  it("DOES reload the page when an already-controlled page's controller changes (a real update)", async () => {
    const registration = { waiting: null, addEventListener: vi.fn() };
    const fakeContainer = makeFakeServiceWorkerContainer(registration, { scriptURL: "/exams-sw.js" }); // already controlled
    Object.defineProperty(navigator, "serviceWorker", { value: fakeContainer, configurable: true });
    const reloadSpy = vi.fn();
    Object.defineProperty(window, "location", { value: { reload: reloadSpy }, configurable: true });

    renderHook(() => useExamServiceWorker());
    await vi.waitFor(() => expect(fakeContainer.register).toHaveBeenCalled());

    fakeContainer.emit("controllerchange");
    expect(reloadSpy).toHaveBeenCalledTimes(1);
  });
});
