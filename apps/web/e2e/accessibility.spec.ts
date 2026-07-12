/**
 * Automated accessibility sweep in a real browser (DSN-009).
 *
 * Critical and serious axe violations fail CI. The manual checklist for
 * flows automation cannot judge lives in docs/A11Y_CHECKLIST.md.
 */

import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

async function expectNoCriticalViolations(page: Page) {
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
  const blocking = results.violations.filter((violation) =>
    ["critical", "serious"].includes(violation.impact ?? ""),
  );
  expect(
    blocking,
    blocking
      .map((violation) => `${violation.id}: ${violation.help} (${violation.nodes.length} nodes)`)
      .join("\n"),
  ).toEqual([]);
}

test("workbench has no critical accessibility violations", async ({ page }) => {
  await page.goto("/workbench");
  await page.getByRole("heading", { name: "Component workbench" }).waitFor();
  await expectNoCriticalViolations(page);
});

test("app shell has no critical accessibility violations", async ({ page }) => {
  await page.goto("/app/northstar/overview");
  await page.getByRole("navigation", { name: "Primary" }).waitFor();
  await expectNoCriticalViolations(page);
});

test("workbench dark theme has no critical accessibility violations", async ({ page }) => {
  await page.goto("/workbench");
  await page.evaluate(() => document.documentElement.setAttribute("data-theme", "dark"));
  await page.getByRole("heading", { name: "Component workbench" }).waitFor();
  await expectNoCriticalViolations(page);
});
