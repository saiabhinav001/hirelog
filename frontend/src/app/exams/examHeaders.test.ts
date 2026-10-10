import { describe, expect, it } from "vitest";

import nextConfig from "../../../next.config";

type Rule = { source: string; headers: { key: string; value: string }[] };

async function rules(): Promise<Rule[]> {
  return (await nextConfig.headers!()) as Rule[];
}

/** Next.js path-to-regexp style sources used in this config: "/(.*)", "/exams/:path*", "/_next/static/(.*)". */
function matches(source: string, path: string): boolean {
  const pattern = source.replace(/:path\*/g, "(.*)");
  return new RegExp(`^${pattern}$`).test(path);
}

describe("framework / CDN cache policy for the exam module", () => {
  it("marks every /exams/** page private, no-store", async () => {
    for (const path of ["/exams/login", "/exams/student/run-1/exam", "/exams/student/run-1/results", "/exams/faculty/def-1/runs/run-1/results"]) {
      const values = (await rules()).filter((rule) => matches(rule.source, path)).flatMap((rule) => rule.headers).filter((h) => h.key === "Cache-Control").map((h) => h.value);
      expect(values, path).toEqual(["private, no-store"]);
    }
  });

  it("never gives an exam page a cacheable Cache-Control from any other rule", async () => {
    for (const rule of await rules()) {
      if (!matches(rule.source, "/exams/student/run-1/exam")) continue;
      for (const header of rule.headers.filter((h) => h.key === "Cache-Control")) {
        expect(header.value).not.toMatch(/public|max-age|immutable|s-maxage|stale-while-revalidate/);
      }
    }
  });

  it("keeps the immutable cache for hashed static assets only (never for exam pages)", async () => {
    const immutable = (await rules()).filter((rule) => rule.headers.some((h) => /immutable/.test(h.value)));
    for (const rule of immutable) expect(matches(rule.source, "/exams/student/run-1/exam")).toBe(false);
  });

  it("still lets the exam pages use the camera and nothing else sensitive", async () => {
    const policy = (await rules())
      .filter((rule) => matches(rule.source, "/exams/student/run-1/exam"))
      .flatMap((rule) => rule.headers)
      .filter((h) => h.key === "Permissions-Policy")
      .map((h) => h.value);
    expect(policy.at(-1)).toBe("camera=(self), microphone=(), geolocation=()");
  });
});
