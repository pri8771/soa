import { defineConfig, devices } from "@playwright/test";

/**
 * Visual-regression and accessibility E2E config (DSN-008/009).
 *
 * Determinism controls:
 * - Production build served by `vite preview` (no dev-mode noise).
 * - Fixed viewport and device scale; animations disabled per screenshot.
 * - Reduced motion emulated globally.
 *
 * Functional/accessibility checks are portable across supported developer
 * hosts. Pixel snapshots are authoritative only on the pinned Linux CI
 * renderer; macOS font rasterization is expected to differ slightly.
 *
 * Updating baselines is an explicit act on that renderer:
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
  },
  expect: {
    toHaveScreenshot: {
      animations: "disabled",
      caret: "hide",
      // Absolute budget, not a ratio: 0.1% of a 1440×900 page is ~1300px,
      // enough to swallow a whole navigation item (it did — JOB-007).
      // Rendering is deterministic on the pinned runner, so keep it tight.
      maxDiffPixels: 64,
    },
  },
  webServer: {
    // `pnpm run x -- --flag` forwards the literal `--`, which vite treats as
    // end-of-options — the port/host flags were silently ignored and vite
    // bound to `localhost` (IPv6 ::1 on newer Node), unreachable at the
    // 127.0.0.1 readiness URL. `pnpm exec` forwards flags verbatim.
    command: "pnpm exec vite preview --port 4173 --strictPort --host 127.0.0.1",
    url: "http://127.0.0.1:4173",
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    // Surface preview-server output in CI logs when startup fails.
    stdout: "pipe",
    stderr: "pipe",
  },
});
