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

  // The exam layout mounts one instance (it knows nothing about attempts) and the exam page mounts another; the page's
  // "attempt active" must hold back the update for BOTH.
  it("a layout-level instance cannot release a waiting update while the exam page holds an attempt open", async () => {
    const postMessage = vi.fn();
    const registration = { waiting: { postMessage }, addEventListener: vi.fn() };
    const fakeContainer = makeFakeServiceWorkerContainer(registration);
    Object.defineProperty(navigator, "serviceWorker", { value: fakeContainer, configurable: true });

    const layout = renderHook(() => useExamServiceWorker());
    const page = renderHook(() => useExamServiceWorker({ attemptActive: true }));
    await vi.waitFor(() => expect(fakeContainer.register).toHaveBeenCalledTimes(2));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(postMessage).not.toHaveBeenCalled();

    page.unmount(); // attempt over: now the update may go through
    await vi.waitFor(() => expect(postMessage).toHaveBeenCalledWith({ type: "SKIP_WAITING" }));
    layout.unmount();
  });

  it("an update that finishes installing mid-attempt stays waiting", async () => {
    const postMessage = vi.fn();
    const statechange: { cb?: () => void } = {};
    const installing = { state: "installing", addEventListener: vi.fn((_t: string, cb: () => void) => { statechange.cb = cb; }) };
    const listeners: Record<string, () => void> = {};
    const registration = {
      waiting: null as { postMessage: typeof postMessage } | null,
      installing,
      addEventListener: vi.fn((type: string, cb: () => void) => { listeners[type] = cb; }),
    };
    const fakeContainer = makeFakeServiceWorkerContainer(registration);
    Object.defineProperty(navigator, "serviceWorker", { value: fakeContainer, configurable: true });

    renderHook(() => useExamServiceWorker());
    const { unmount } = renderHook(() => useExamServiceWorker({ attemptActive: true }));
    await vi.waitFor(() => expect(registration.addEventListener).toHaveBeenCalled());

    listeners.updatefound();
    installing.state = "installed";
    registration.waiting = { postMessage };
    statechange.cb?.();
    expect(postMessage).not.toHaveBeenCalled();

    unmount();
    await vi.waitFor(() => expect(postMessage).toHaveBeenCalledWith({ type: "SKIP_WAITING" }));
  });

  it("never reloads the page for a controller change while an attempt is active", async () => {
    const registration = { waiting: null, addEventListener: vi.fn() };
    const fakeContainer = makeFakeServiceWorkerContainer(registration, { scriptURL: "/exams-sw.js" });
    Object.defineProperty(navigator, "serviceWorker", { value: fakeContainer, configurable: true });
    const reloadSpy = vi.fn();
    Object.defineProperty(window, "location", { value: { reload: reloadSpy }, configurable: true });

    renderHook(() => useExamServiceWorker({ attemptActive: true }));
    await vi.waitFor(() => expect(fakeContainer.register).toHaveBeenCalled());
    fakeContainer.emit("controllerchange");
    expect(reloadSpy).not.toHaveBeenCalled();
  });

  it("an attempt that is still loading counts as active, so a refresh mid-attempt is never interrupted by an update", async () => {
    const postMessage = vi.fn();
    const registration = { waiting: { postMessage }, addEventListener: vi.fn() };
    const fakeContainer = makeFakeServiceWorkerContainer(registration);
    Object.defineProperty(navigator, "serviceWorker", { value: fakeContainer, configurable: true });

    // Exam page on first render: attempt unknown -> held; layout instance alongside.
    const { rerender, unmount } = renderHook(({ active }) => useExamServiceWorker({ attemptActive: active }), { initialProps: { active: true } });
    renderHook(() => useExamServiceWorker());
    await vi.waitFor(() => expect(fakeContainer.register).toHaveBeenCalledTimes(2));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(postMessage).not.toHaveBeenCalled();

    rerender({ active: false }); // attempt loaded and it is over
    await vi.waitFor(() => expect(postMessage).toHaveBeenCalledWith({ type: "SKIP_WAITING" }));
    unmount();
  });
});
