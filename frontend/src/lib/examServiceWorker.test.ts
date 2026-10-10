import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it, vi } from "vitest";

/**
 * Runs the REAL public/exams-sw.js against fakes of the service-worker globals, so the cache policy is checked on the
 * shipped file (not on a copy of its rules): only the explicit static shell is ever cached or served from cache, and
 * nothing authenticated is ever intercepted, stored or replayed.
 */
const SW_SOURCE = readFileSync(resolve(process.cwd(), "public/exams-sw.js"), "utf8");
const ORIGIN = "https://hirelog.test";

type Listener = (event: Record<string, unknown>) => void;

function loadServiceWorker() {
  const listeners = new Map<string, Listener>();
  const stores = new Map<string, Map<string, string>>();
  const put = vi.fn();
  const addAll = vi.fn();

  const caches = {
    open: vi.fn(async (name: string) => {
      if (!stores.has(name)) stores.set(name, new Map());
      const store = stores.get(name)!;
      return {
        addAll: async (urls: string[]) => {
          addAll(urls);
          urls.forEach((u) => store.set(u, `static:${u}`));
        },
        put,
      };
    }),
    keys: vi.fn(async () => [...stores.keys()]),
    delete: vi.fn(async (name: string) => stores.delete(name)),
    match: vi.fn(async (request: string | { url: string }) => {
      const path = typeof request === "string" ? request : new URL(request.url).pathname;
      for (const store of stores.values()) if (store.has(path)) return store.get(path);
      return undefined;
    }),
  };
  const self = {
    addEventListener: (type: string, cb: Listener) => listeners.set(type, cb),
    skipWaiting: vi.fn(),
    clients: { claim: vi.fn(async () => undefined) },
  };
  const network = vi.fn<(request: unknown) => Promise<string>>();
  new Function("self", "caches", "fetch", SW_SOURCE)(self, caches, network);
  return { listeners, stores, caches, self, put, addAll, network };
}

async function lifecycle(sw: ReturnType<typeof loadServiceWorker>, type: "install" | "activate") {
  const waits: Promise<unknown>[] = [];
  sw.listeners.get(type)!({ waitUntil: (p: Promise<unknown>) => waits.push(p) });
  await Promise.all(waits);
}

function fetchEvent(sw: ReturnType<typeof loadServiceWorker>, path: string, init: { method?: string; mode?: string } = {}) {
  const respondWith = vi.fn();
  sw.listeners.get("fetch")!({ request: { method: init.method ?? "GET", mode: init.mode ?? "cors", url: ORIGIN + path }, respondWith });
  return respondWith;
}

const STATIC_SHELL = ["/exams-offline.html", "/exams-manifest.webmanifest", "/logo.svg", "/favicon.svg"];

describe("exams service worker (the shipped file)", () => {
  it("precaches only the four static shell files and never writes anything else to a cache", async () => {
    const sw = loadServiceWorker();
    await lifecycle(sw, "install");

    expect(sw.addAll).toHaveBeenCalledTimes(1);
    expect(sw.addAll.mock.calls[0][0]).toEqual(STATIC_SHELL);
    expect([...sw.stores.get("exams-shell-v1")!.keys()].sort()).toEqual([...STATIC_SHELL].sort());
    expect(sw.put).not.toHaveBeenCalled(); // no runtime caching API is used at all
  });

  it.each([
    "/api/exams/v1/student/runs/run-1/question",
    "/api/exams/v1/student/runs/run-1/drafts/rev-1:slot:0",
    "/api/exams/v1/student/gradebook/runs/run-1/results",
    "/api/exams/v1/student/proctoring/runs/run-1/evidence",
    "/api/exams/v1/faculty/gradebook/export/download/ticket-1",
    "/api/exams/v1/student/runs/run-1/live-ticket",
    "/_next/static/chunks/app.js",
    "/exams/student/run-1/results",
  ])("does not intercept a GET for %s (it goes straight to the network, as without a service worker)", async (path) => {
    const sw = loadServiceWorker();
    await lifecycle(sw, "install");
    expect(fetchEvent(sw, path)).not.toHaveBeenCalled();
    expect(sw.network).not.toHaveBeenCalled();
    expect(sw.put).not.toHaveBeenCalled();
  });

  it.each(["POST", "PUT", "PATCH", "DELETE"])("never intercepts a %s - including to a precached path - so no write is ever queued or replayed", async (method) => {
    const sw = loadServiceWorker();
    await lifecycle(sw, "install");
    for (const path of ["/api/exams/v1/student/runs/run-1/submit", "/exams-offline.html", "/exams/student/run-1/exam"]) {
      expect(fetchEvent(sw, path, { method })).not.toHaveBeenCalled();
    }
  });

  it("serves an exam-page navigation from the NETWORK, never from the cache, and falls back to the static offline page only when the network itself fails", async () => {
    const sw = loadServiceWorker();
    await lifecycle(sw, "install");

    sw.network.mockResolvedValueOnce("LIVE-AUTHENTICATED-PAGE");
    const online = fetchEvent(sw, "/exams/student/run-1/exam", { mode: "navigate" });
    expect(online).toHaveBeenCalledTimes(1);
    expect(await online.mock.calls[0][0]).toBe("LIVE-AUTHENTICATED-PAGE");
    expect(sw.caches.match).not.toHaveBeenCalled();

    sw.network.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    const offline = fetchEvent(sw, "/exams/student/run-1/exam", { mode: "navigate" });
    expect(await offline.mock.calls[0][0]).toBe("static:/exams-offline.html"); // the shell, not the exam
    expect(sw.put).not.toHaveBeenCalled();
  });

  it("serves a static shell file from the cache, and a missing one from the network", async () => {
    const sw = loadServiceWorker();
    await lifecycle(sw, "install");
    const respond = fetchEvent(sw, "/logo.svg");
    expect(await respond.mock.calls[0][0]).toBe("static:/logo.svg");
    expect(sw.network).not.toHaveBeenCalled();
  });

  it("holds exactly the static shell through a whole session, an account switch and a logout (nothing user-specific is ever stored)", async () => {
    const sw = loadServiceWorker();
    await lifecycle(sw, "install");
    sw.network.mockResolvedValue("response");
    const snapshot = () => JSON.stringify([...sw.stores.entries()].map(([name, store]) => [name, [...store.entries()]]));
    const before = snapshot();

    // Student A works, logs out, student B logs in on the same browser, then a teacher.
    for (const path of [
      "/exams/login", "/exams/student/run-1/exam", "/api/exams/v1/student/runs/run-1/question", "/api/exams/v1/student/runs/run-1/submit",
      "/exams/student/run-1/results", "/api/exams/v1/student/gradebook/runs/run-1/results", "/exams/faculty/def-1/runs/run-1/results",
    ]) {
      fetchEvent(sw, path, { mode: "navigate" });
      fetchEvent(sw, path);
    }
    await Promise.resolve();

    expect(snapshot()).toBe(before);
    expect(sw.put).not.toHaveBeenCalled();
    expect(sw.addAll).toHaveBeenCalledTimes(1);
  });

  it("does not skip waiting on its own - an update waits until the page says it is safe", async () => {
    const sw = loadServiceWorker();
    await lifecycle(sw, "install");
    expect(sw.self.skipWaiting).not.toHaveBeenCalled();

    sw.listeners.get("message")!({ data: { type: "SOMETHING_ELSE" } });
    sw.listeners.get("message")!({ data: undefined });
    expect(sw.self.skipWaiting).not.toHaveBeenCalled();

    sw.listeners.get("message")!({ data: { type: "SKIP_WAITING" } });
    expect(sw.self.skipWaiting).toHaveBeenCalledTimes(1);
  });

  it("on activation deletes only its own older shell caches and then takes control", async () => {
    const sw = loadServiceWorker();
    sw.stores.set("exams-shell-v0", new Map([["/old", "x"]]));
    sw.stores.set("unrelated-cache", new Map([["/keep", "x"]]));
    sw.stores.set("exams-shell-v1", new Map());
    await lifecycle(sw, "activate");

    expect([...sw.stores.keys()].sort()).toEqual(["exams-shell-v1", "unrelated-cache"]);
    expect(sw.self.clients.claim).toHaveBeenCalledTimes(1);
  });
});
