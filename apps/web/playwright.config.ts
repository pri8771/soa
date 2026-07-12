import { defineConfig, devices } from "@playwright/test";

/**
 * Visual-regression and accessibility E2E config (DSN-008/009).
 *
 * Determinism controls:
 * - Production build served by `vite preview` (no dev-mode noise).
 * - Fixed viewport and device scale; animations disabled per screenshot.
 * - Reduced motion emulated globally.
 *
 * Updating baselines is an explicit act:
 *   pnpm run test:visual -- --update-snapshots
 * and the resulting image diff is reviewed in the PR like any code change.
 */
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["github"], ["html", { open: "never" }]] : "list",
  snapshotPathTemplate: "{testDir}/__screenshots__/{testFilePath}/{arg}{ext}",
  use: {
    ...devices["Desktop Chrome"],
    baseURL: "http://127.0.0.1:4173",
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 1,
    contextOptions: { reducedMotion: "reduce" },
    // The container preinstalls Chromium here; version-pinned downloads are
    // disabled (PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD). CI installs its own.
    launchOptions: process.env.CI
      ? {}
      : { executablePath: "/opt/pw-browsers/chromium-1194/chrome-linux/chrome" },
  },
  expect: {
    toHaveScreenshot: {
      animations: "disabled",
      caret: "hide",
      maxDiffPixelRatio: 0.001,
    },
  },
  webServer: {
    command: "pnpm run preview -- --port 4173 --strictPort",
    url: "http://127.0.0.1:4173",
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    // Surface preview-server output in CI logs when startup fails.
    stdout: "pipe",
    stderr: "pipe",
  },
});
