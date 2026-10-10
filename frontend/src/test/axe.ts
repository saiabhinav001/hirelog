import { axe } from "vitest-axe";
import { expect } from "vitest";

/**
 * Runs axe-core against a rendered exam screen and fails with a readable list of violations.
 *
 * What this can and cannot establish (be honest in reports): jsdom has no layout engine, so axe cannot compute
 * colour contrast, visibility, reflow or zoom behaviour - `color-contrast` is switched off here and covered
 * separately (the `.dark .exam-scope` token tests); `region` is off because the page landmark (<main>) is supplied by
 * the app layout, which these component-level renders do not include. Real keyboard, screen-reader, zoom and device
 * checks stay manual (docs/exams/accessibility-and-lockdown-checklist.md).
 */
export async function expectNoAxeViolations(container: Element): Promise<void> {
  const results = await axe(container, {
    rules: { "color-contrast": { enabled: false }, region: { enabled: false } },
  });
  const summary = results.violations.map((v) => `${v.id}: ${v.help} (${v.nodes.map((n) => n.target.join(" ")).join(" | ")})`);
  expect(summary, summary.join("\n")).toEqual([]);
}
