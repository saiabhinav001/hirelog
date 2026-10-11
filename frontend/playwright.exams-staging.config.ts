import { defineConfig, devices } from "@playwright/test";

/**
 * Real-browser journeys for the exams-only staging deployment. Unlike
 * playwright.config.ts this starts NOTHING: point it at an already running
 * frontend and exam API (see docs/exams-staging-deployment.md, "End-to-end tests").
 *
 *   E2E_EXAMS_STAGING=1 E2E_FRONTEND_URL=... E2E_API_URL=... npx playwright test -c playwright.exams-staging.config.ts
 */
export default defineConfig({
  testDir: "./e2e",
  testMatch: /exams-staging\.spec\.ts/,
  timeout: 90_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: process.env.CI ? "github" : "list",
  outputDir: "./test-results-exams-staging",
  use: {
    baseURL: process.env.E2E_FRONTEND_URL || "http://127.0.0.1:3100",
    actionTimeout: 20_000,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    launchOptions: {
      // A synthetic camera, so the proctoring preflight can be exercised without hardware.
      args: ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"],
    },
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"], permissions: ["camera"] } }],
});
