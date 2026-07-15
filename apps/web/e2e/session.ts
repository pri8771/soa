/**
 * Deterministic /api/me mock for e2e runs. `vite preview` serves no API, so
 * any route under /app/* would otherwise render the session-error state.
 * Imports the same payload the unit-test MSW handlers use.
 */

import type { Page } from "@playwright/test";

import { DEFAULT_ME } from "../src/test/me-payload";

export async function installSessionMock(page: Page): Promise<void> {
  // The visual suite exercises a production build, so it must cross the same
  // bearer-session gate as a deployed browser. Install a deterministic,
  // tab-scoped generic-OIDC session before any application code runs; API
  // responses remain independently mocked below.
  await page.addInitScript(() => {
    globalThis.sessionStorage.setItem(
      "soa.auth.oidc.session",
      JSON.stringify({
        accessToken: null,
        idToken: "visual-test-id-token",
        refreshToken: null,
        expiresAt: Date.now() + 60 * 60 * 1_000,
      }),
    );
  });
  await page.route("**/api/me", (route) => route.fulfill({ json: DEFAULT_ME }));
}
