import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

const css = readFileSync(resolve(__dirname, "../globals.css"), "utf8");

function block(selector: string): string {
  const start = css.indexOf(`${selector} {`);
  if (start < 0) throw new Error(`no ${selector} block`);
  return css.slice(start, css.indexOf("}", start));
}

function token(scope: string, name: string): string {
  const match = block(scope).match(new RegExp(`${name}:\\s*(#[0-9a-fA-F]{6})`));
  if (!match) throw new Error(`${name} not found in ${scope}`);
  return match[1];
}

function luminance(hex: string): number {
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255).map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function ratio(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

const AA = 4.5;

describe("exam screen text contrast (WCAG AA, 4.5:1)", () => {
  it("measures the shared dark muted colour so the exam override stays justified", () => {
    expect(ratio(token(".dark", "--text-muted"), token(".dark", "--surface"))).toBeLessThan(AA);
  });

  it("dark theme: muted text inside the exam scope meets AA on every surface used", () => {
    const muted = token(".dark .exam-scope", "--text-muted");
    for (const surface of ["--bg", "--surface", "--surface-elevated", "--surface-muted"]) {
      expect(ratio(muted, token(".dark", surface)), `${surface}`).toBeGreaterThanOrEqual(AA);
    }
  });

  it("dark theme: body, secondary text and the primary button meet AA", () => {
    expect(ratio(token(".dark", "--text"), token(".dark", "--surface"))).toBeGreaterThanOrEqual(AA);
    expect(ratio(token(".dark", "--text-secondary"), token(".dark", "--surface-muted"))).toBeGreaterThanOrEqual(AA);
    expect(ratio(token(".dark", "--on-primary"), token(".dark", "--primary"))).toBeGreaterThanOrEqual(AA);
    expect(ratio(token(".dark", "--error"), token(".dark", "--surface"))).toBeGreaterThanOrEqual(AA);
  });

  it("light theme: text, muted text, primary button and error meet AA", () => {
    expect(ratio(token(":root", "--text"), token(":root", "--surface"))).toBeGreaterThanOrEqual(AA);
    expect(ratio(token(":root", "--text-muted"), token(":root", "--surface-muted"))).toBeGreaterThanOrEqual(AA);
    expect(ratio(token(":root", "--on-primary"), token(":root", "--primary"))).toBeGreaterThanOrEqual(AA);
    expect(ratio(token(":root", "--error"), token(":root", "--surface"))).toBeGreaterThanOrEqual(AA);
  });
});
