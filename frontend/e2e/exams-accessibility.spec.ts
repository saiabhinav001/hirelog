import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

/**
 * Automated accessibility scan of the exams login page — reachable
 * without any backend/auth mocking, unlike the rest of the exam module
 * (which requires an authenticated institution session + live exam API).
 * Deeper in-app pages (student exam screen, faculty gradebook) are NOT
 * covered here; see docs/exams/accessibility-checklist.md for what still
 * requires manual review before production (Prompt 10 honesty: automated
 * checks were run, real screen-reader/zoom/device testing was not).
 */
test("exams login page has no automatically detectable accessibility violations", async ({ page }) => {
  await page.goto("/exams/login");
  await expect(page.getByLabel("Institution ID")).toBeVisible();
  await page.waitForLoadState("networkidle");

  const results = await new AxeBuilder({ page }).analyze();
  expect(results.violations, JSON.stringify(results.violations, null, 2)).toEqual([]);
});

test("exams login page's challenge form is keyboard-reachable end to end", async ({ page }) => {
  await page.goto("/exams/login");
  await page.waitForLoadState("networkidle");

  await page.getByLabel("Institution ID").focus();
  await expect(page.getByLabel("Institution ID")).toBeFocused();

  // Don't assert the exact intermediate tab stop (browser autofill can
  // insert/reorder focusable affordances around typed inputs) — the
  // meaningful accessibility claim is that the submit button is reachable
  // by keyboard alone, never trapped behind the form fields.
  for (let i = 0; i < 6; i += 1) {
    await page.keyboard.press("Tab");
    if (await page.getByRole("button", { name: "Send code" }).evaluate((el) => el === document.activeElement)) {
      break;
    }
  }
  await expect(page.getByRole("button", { name: "Send code" })).toBeFocused();
});

