/**
 * Deterministic /api/me mock for e2e runs. `vite preview` serves no API, so
 * any route under /app/* would otherwise render the session-error state.
 * Imports the same payload the unit-test MSW handlers use.
 */

import type { Page } from "@playwright/test";

import { DEFAULT_ME } from "../src/test/me-payload";

export async function installSessionMock(page: Page): Promise<void> {
  await page.route("**/api/me", (route) => route.fulfill({ json: DEFAULT_ME }));
}
