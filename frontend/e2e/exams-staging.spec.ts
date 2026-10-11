import { readFileSync, statSync } from "node:fs";

import { expect, test, type Browser, type BrowserContext, type Page } from "@playwright/test";

/**
 * Journeys against a running exams-only deployment. The local rehearsal runs them
 * against the Firebase emulators (see docs/exams-staging-deployment.md): the faculty
 * sign-in uses the Auth emulator's Google popup and the one-time codes are read from
 * the API's log, where the development OTP sink prints them. They never touch a real
 * Firebase project.
 *
 *   E2E_EXAMS_STAGING=1             enable (otherwise every test is skipped)
 *   E2E_BACKEND_LOG=<path>          the API's log file (one-time codes)
 *   E2E_RESET_EMULATORS=1           wipe the emulators first, so the journeys are repeatable
 *   E2E_OPERATOR_EMAIL=<email>      the platform operator signing in with Google
 */
const ENABLED = process.env.E2E_EXAMS_STAGING === "1";
const BACKEND_LOG = process.env.E2E_BACKEND_LOG || "";
const OPERATOR_EMAIL = process.env.E2E_OPERATOR_EMAIL || "operator@example.edu";
const API = process.env.E2E_API_URL || "http://127.0.0.1:7862";
const TAG = Date.now().toString(36);

const STUDENTS = [
  { key: "S1", email: "s1@example.edu" },
  { key: "S2", email: "s2@example.edu" },
  { key: "S3", email: "s3@example.edu" },
];

function logSize(): number {
  return BACKEND_LOG ? statSync(BACKEND_LOG).size : 0;
}

/** The newest one-time code the API "sent" to this address after the given log offset, if any. */
async function otpSentTo(email: string, fromOffset: number, attempts = 50): Promise<string | null> {
  const pattern = new RegExp(`contact_email=${email.replace(/\./g, "\\.")} code=(\\d{6})`, "g");
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    const text = readFileSync(BACKEND_LOG).subarray(fromOffset).toString("utf8");
    const matches = [...text.matchAll(pattern)];
    if (matches.length > 0) return matches[matches.length - 1][1];
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  return null;
}

async function signInAsOperator(page: Page): Promise<void> {
  await page.goto("/");
  const [popup] = await Promise.all([
    page.waitForEvent("popup"),
    page.getByRole("button", { name: "Continue with Google" }).click(),
  ]);
  await popup.waitForLoadState("domcontentloaded");
  await popup.getByRole("button", { name: /Add new account/i }).click();
  await popup.locator("#email-input").fill(OPERATOR_EMAIL);
  await popup.getByRole("button", { name: /Sign in with Google\.com/i }).click();
  await page.waitForURL(/\/exams\/faculty$/);
}

/** Fills the first login step and returns the code the API sent (null when none was sent). */
async function requestCode(page: Page, institutionId: string, key: string, email: string): Promise<string | null> {
  await page.goto(`/exams/login?institution=${institutionId}`);
  await page.getByLabel("College ID").fill(key);
  await page.getByLabel("Registered email").fill(email);
  const offset = logSize();
  await page.getByRole("button", { name: "Send code" }).click();
  await expect(page.getByLabel("6-digit code")).toBeVisible();
  return otpSentTo(email, offset);
}

async function enterCode(page: Page, code: string): Promise<void> {
  await page.getByLabel("6-digit code").fill(code);
  await page.getByRole("button", { name: "Verify" }).click();
}

async function signInAsStudent(
  browser: Browser,
  institutionId: string,
  key: string,
  email: string
): Promise<{ context: BrowserContext; page: Page; code: string }> {
  const context = await browser.newContext({ permissions: ["camera"] });
  const page = await context.newPage();
  const code = await requestCode(page, institutionId, key, email);
  expect(code, `a code for ${email}`).not.toBeNull();
  await enterCode(page, code as string);
  await page.waitForURL(/\/exams\/student$/);
  return { context, page, code: code as string };
}

/** Student home -> preflight checklist -> exam page. */
async function beginExam(page: Page): Promise<void> {
  await page.getByRole("link", { name: "Begin" }).click();
  await expect(page.getByRole("heading", { name: "Before you begin" })).toBeVisible();
  await page.getByLabel("I have read and understood this notice.").check();
  await page.getByLabel("I have closed all other applications and browser tabs.").check();
  await page.getByLabel("I have disabled browser extensions that could interfere with this exam.").check();
  await page.getByLabel("I can see and interact with this page normally.").check();
  const fullscreen = page.getByRole("button", { name: "Enter fullscreen" });
  if (await fullscreen.isVisible()) await fullscreen.click();
  await page.getByRole("button", { name: "Begin exam" }).click();
  await page.waitForURL(/\/exam$/, { timeout: 30_000 });
  await expect(page.getByRole("radio").first()).toBeVisible({ timeout: 30_000 });
}

async function revokeMember(operator: Page, institutionName: string, key: string): Promise<void> {
  await operator.goto("/exams/faculty/institution");
  await operator.getByLabel("Institution", { exact: true }).selectOption({ label: institutionName });
  const row = operator.getByRole("row").filter({ has: operator.getByRole("cell", { name: key, exact: true }) });
  operator.once("dialog", (dialog) => void dialog.accept());
  await row.getByRole("button", { name: "Revoke" }).click();
  await expect(operator.getByText(`Revoked ${key}.`)).toBeVisible();
}

test.describe.configure({ mode: "serial" });
test.skip(!ENABLED, "Set E2E_EXAMS_STAGING=1 to run the staging journeys");

const COLLEGE = `E2E College ${TAG}`;
let institutionId = "";
let runId = "";
let operatorContext: BrowserContext;
let operator: Page;
let studentTwo: { context: BrowserContext; page: Page } | null = null;

test.beforeAll(async ({ browser }) => {
  if (!ENABLED) return;
  if (process.env.E2E_RESET_EMULATORS === "1") {
    // Local rehearsal only: start from empty emulators so the journeys are repeatable.
    await fetch("http://127.0.0.1:8080/emulator/v1/projects/demo-hirelog-exams/databases/(default)/documents", { method: "DELETE" });
    await fetch("http://127.0.0.1:9099/emulator/v1/projects/demo-hirelog-exams/accounts", { method: "DELETE" });
  }
  operatorContext = await browser.newContext();
  operator = await operatorContext.newPage();
});

test.afterAll(async () => {
  await studentTwo?.context.close();
  await operatorContext?.close();
});

test("the staging site opens on the institutional login and shows no legacy navigation", async ({ page }) => {
  const response = await page.goto("/");
  await expect(page).toHaveURL(/\/exams\/login$/);
  expect(response?.headers()["x-robots-tag"] ?? "").toContain("noindex");
  await expect(page.getByRole("heading", { name: "Institution sign-in" })).toBeVisible();
  await expect(page.getByText("Staging", { exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: /Explore|Practice|Insights/ })).toHaveCount(0);
});

test("anonymous visitors are sent to the login from every protected page", async ({ page }) => {
  for (const path of ["/exams/faculty", "/exams/faculty/institution", "/exams/student"]) {
    await page.goto(path);
    await expect(page).toHaveURL(/\/exams\/login$/);
  }
});

test("the operator signs in with Google and reaches the faculty area", async () => {
  await signInAsOperator(operator);
  await expect(operator.getByRole("heading", { name: "Your exam courses" })).toBeVisible();
});

test("the operator creates an institution and applies a roster", async () => {
  await operator.goto("/exams/faculty/institution");
  await operator.getByLabel("New institution name").fill(COLLEGE);
  await operator.getByRole("button", { name: "Create institution" }).click();
  const link = operator.getByTestId("student-signin-link");
  await expect(link).toContainText("/exams/login?institution=");
  institutionId = ((await link.textContent()) ?? "").split("institution=")[1].trim();
  expect(institutionId).toMatch(/^[0-9a-f]{32}$/);

  const rows = STUDENTS.map((s) => `${s.key}, ${s.email}`).join("\n");
  await operator.getByLabel("Roster rows").fill(rows);
  await operator.getByRole("button", { name: "Check roster" }).click();
  await expect(operator.getByTestId("roster-check")).toContainText("3 of 3 rows can be applied");
  await operator.getByRole("button", { name: "Apply roster" }).click();
  await expect(operator.getByText("Roster applied.")).toBeVisible();
  for (const student of STUDENTS) {
    await expect(operator.getByRole("cell", { name: student.key, exact: true })).toBeVisible();
  }
});

test("the operator authors, publishes and schedules an exam for the whole roster", async () => {
  await operator.goto("/exams/faculty");
  await operator.getByLabel("Institution for the new exam").selectOption({ label: COLLEGE });
  await operator.getByLabel("New exam title").fill(`E2E Midterm ${TAG}`);
  await operator.getByRole("button", { name: "Create" }).click();
  await operator.getByRole("link", { name: new RegExp(`E2E Midterm ${TAG}`) }).click();
  await expect(operator.getByRole("heading", { name: "Exam workspace" })).toBeVisible();

  await operator.getByPlaceholder("https://approved-source.example.edu/notes").fill("https://example.com/");
  await operator.getByRole("button", { name: "Add reference link" }).click();
  await expect(operator.getByText("pending", { exact: true })).toBeVisible();
  await operator.getByRole("button", { name: "Approve" }).click();
  await expect(operator.getByText("approved", { exact: true })).toBeVisible();

  await operator.getByPlaceholder("Blueprint title").fill("Core concepts");
  await operator.getByRole("button", { name: "Create blueprint (1 slot)" }).click();
  await operator.getByRole("button", { name: "Generate questions" }).click();
  await expect(operator.getByText("ready_for_human_review").first()).toBeVisible({ timeout: 30_000 });
  await operator.getByRole("button", { name: "Approve" }).first().click();
  await expect(operator.getByText("approved", { exact: true }).nth(1)).toBeVisible();

  await operator.getByRole("button", { name: "Publish revision" }).click();
  await operator.getByLabel("Duration (minutes)").fill("30");
  await operator.getByLabel(/Enrol every active student/).check();
  await operator.getByRole("button", { name: "Schedule session" }).click();
  await expect(operator.getByText(/3 enrolled/)).toBeVisible();
});

test("the operator assigns questions and releases the session from the lobby", async () => {
  await operator.getByRole("link", { name: "Lobby" }).first().click();
  await expect(operator.getByRole("heading", { name: "Readiness lobby" })).toBeVisible();
  runId = operator.url().match(/runs\/([0-9a-f]+)\/lobby/)?.[1] ?? "";
  expect(runId).not.toBe("");
  await operator.getByLabel(/Let students share question variants/).check();
  await operator.getByRole("button", { name: "Assign questions to students" }).click();
  await expect(operator.getByText(/3 assignment\(s\) confirmed/)).toBeVisible();
  await operator.getByRole("button", { name: "Publish run" }).click();
  await expect(operator.getByRole("button", { name: "Release to students" })).toBeEnabled();
  await operator.getByRole("button", { name: "Release to students" }).click();
  await operator.getByRole("dialog").getByRole("button", { name: "Release" }).click();
  await expect(operator).toHaveURL(/\/control$/);
});

test("a student signs in with a one-time code, answers, submits and gets a receipt", async ({ browser }) => {
  const { context, page } = await signInAsStudent(browser, institutionId, STUDENTS[0].key, STUDENTS[0].email);
  try {
    await expect(page.getByRole("heading", { name: "Your exams" })).toBeVisible();
    await beginExam(page);
    await expect(page.getByText("[fake]").first()).toBeVisible(); // the development generator labels its questions
    await page.getByRole("radio").first().check();
    await expect(page.getByText(/Saved \(v\d+\)/)).toBeVisible({ timeout: 20_000 });

    await page.getByRole("button", { name: "Submit exam" }).click();
    await page.getByRole("dialog", { name: "Confirm submission" }).getByRole("button", { name: "Yes, submit" }).click();
    await page.waitForURL(/\/receipt$/, { timeout: 30_000 });
    for (let attempt = 0; attempt < 15; attempt += 1) {
      if (await page.getByText("Submission confirmed").isVisible()) break;
      const again = page.getByRole("button", { name: "Check again" });
      if (await again.isVisible()) await again.click();
      await page.waitForTimeout(2_000);
    }
    await expect(page.getByText("Submission confirmed")).toBeVisible();
  } finally {
    await context.close();
  }
});

test("the operator sees the submission on the live control screen", async () => {
  await operator.reload();
  const row = operator.getByRole("row").filter({ has: operator.getByRole("cell", { name: STUDENTS[0].key, exact: true }) });
  await expect(row.getByText(/submitted|finalized/)).toBeVisible({ timeout: 30_000 });
});

test("a wrong one-time code is refused, a used code cannot be replayed, and a student cannot open faculty screens", async ({ browser }) => {
  const context = await browser.newContext({ permissions: ["camera"] });
  const page = await context.newPage();
  const code = await requestCode(page, institutionId, STUDENTS[1].key, STUDENTS[1].email);
  expect(code).not.toBeNull();

  await enterCode(page, code === "000000" ? "111111" : "000000");
  await expect(page.locator('p[role="alert"]')).toContainText(/invalid or has expired/i);
  await enterCode(page, code as string);
  await page.waitForURL(/\/exams\/student$/);

  const replay = await page.request.post(`${API}/api/exams/v1/auth/verify`, {
    data: { institution_id: institutionId, identity_key: STUDENTS[1].key, code },
  });
  expect(replay.ok()).toBe(false);

  for (const path of ["/exams/faculty", "/exams/faculty/institution"]) {
    await page.goto(path);
    await expect(page.getByRole("heading", { name: "Faculty access required" })).toBeVisible();
  }
  await page.goto("/exams/student");
  studentTwo = { context, page };
});

test("a student whose membership is revoked mid-exam is cut off and cannot sign in again", async () => {
  expect(studentTwo).not.toBeNull();
  const { page } = studentTwo as { context: BrowserContext; page: Page };
  await beginExam(page);
  await page.getByRole("radio").first().check();
  await expect(page.getByText(/Saved \(v\d+\)/)).toBeVisible({ timeout: 20_000 });

  await revokeMember(operator, COLLEGE, STUDENTS[1].key);

  // The next save is refused; the student is signed out with an explanation.
  await page.getByRole("radio").nth(1).check();
  await page.waitForURL(/\/exams\/login$/, { timeout: 30_000 });
  await expect(page.locator('p[role="alert"]')).toContainText(/no longer active|out of date|sign in again/i);

  // And a fresh code is never delivered to a revoked member (the API answers the same either way).
  const offset = logSize();
  await page.getByLabel("College ID").fill(STUDENTS[1].key);
  await page.getByLabel("Registered email").fill(STUDENTS[1].email);
  await page.getByLabel("Institution ID").fill(institutionId);
  await page.getByRole("button", { name: "Send code" }).click();
  await expect(page.getByLabel("6-digit code")).toBeVisible();
  expect(await otpSentTo(STUDENTS[1].email, offset, 10)).toBeNull();
});

test("a student of another institution sees none of this institution's exams or staff screens", async ({ browser }) => {
  await operator.goto("/exams/faculty/institution");
  await operator.getByLabel("New institution name").fill(`Other College ${TAG}`);
  await operator.getByRole("button", { name: "Create institution" }).click();
  const link = operator.getByTestId("student-signin-link");
  await expect(link).not.toContainText(institutionId); // the new institution is now the selected one
  await expect(link).toContainText("/exams/login?institution=");
  const otherInstitutionId = ((await link.textContent()) ?? "").split("institution=")[1].trim();
  expect(otherInstitutionId).not.toBe(institutionId);
  await operator.getByLabel("Roster rows").fill("X1, x1@other.example.edu");
  await operator.getByRole("button", { name: "Check roster" }).click();
  await operator.getByRole("button", { name: "Apply roster" }).click();
  await expect(operator.getByText("Roster applied.")).toBeVisible();

  const { context, page } = await signInAsStudent(browser, otherInstitutionId, "X1", "x1@other.example.edu");
  try {
    await expect(page.getByText("No assigned exams")).toBeVisible();
    await page.goto(`/exams/student/${runId}/exam`);
    await expect(page.getByRole("heading", { name: "Something went wrong" })).toBeVisible();
    await expect(page.getByText(/not enrolled in this exam/i).first()).toBeVisible();
    await page.goto("/exams/faculty");
    await expect(page.getByRole("heading", { name: "Faculty access required" })).toBeVisible();
  } finally {
    await context.close();
  }
});
