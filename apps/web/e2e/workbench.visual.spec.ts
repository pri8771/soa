/**
 * Visual regression over the component workbench (DSN-008).
 *
 * Each data-workbench-section gets a light and dark baseline. Content is
 * deterministic by construction (Workbench renders no live time or random
 * data) and animations are disabled by the shared config.
 */

import { expect, test } from "@playwright/test";

const SECTIONS = [
  "buttons",
  "fields",
  "selection",
  "toggles",
  "tabs",
  "status",
  "long-text",
] as const;

for (const theme of ["light", "dark"] as const) {
  test.describe(`workbench (${theme})`, () => {
    test.beforeEach(async ({ page }) => {
      await page.goto("/workbench");
      await page.evaluate((selectedTheme) => {
        document.documentElement.setAttribute("data-theme", selectedTheme);
      }, theme);
      await page.getByRole("heading", { name: "Component workbench" }).waitFor();
    });

    for (const section of SECTIONS) {
      test(`section ${section}`, async ({ page }) => {
        const locator = page.locator(`[data-workbench-section="${section}"]`);
        await expect(locator).toHaveScreenshot(`${section}-${theme}.png`);
      });
    }
  });
}

test("app shell (light)", async ({ page }) => {
  await page.goto("/app/northstar/overview");
  await page.getByRole("navigation", { name: "Primary" }).waitFor();
  await expect(page).toHaveScreenshot("app-shell-light.png");
});
