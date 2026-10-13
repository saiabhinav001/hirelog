import path from "node:path";

import { defineConfig } from "vitest/config";

export default defineConfig({
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
    setupFiles: ["./src/test/setup.ts"],
    // Short autosave timings so the exam-page tests do not wait seconds for a save (production defaults are 3 s / 12 s).
    env: {
      NEXT_PUBLIC_EXAM_AUTOSAVE_DEBOUNCE_MS: "1200",
      NEXT_PUBLIC_EXAM_AUTOSAVE_MAX_WAIT_MS: "3000",
    },
  },
});
