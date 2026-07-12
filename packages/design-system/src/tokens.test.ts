import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { contrastRatio } from "./contrast";
import { cssVariableName, darkColors, lightColors, type ColorTokens } from "./tokens";

const themes: Array<[string, ColorTokens]> = [
  ["light", lightColors],
  ["dark", darkColors],
];

describe.each(themes)("%s theme contrast (WCAG 2.2 AA)", (_name, colors) => {
  const textSurfacePairs: Array<[keyof ColorTokens, keyof ColorTokens]> = [
    ["textPrimary", "surface"],
    ["textPrimary", "canvas"],
    ["textPrimary", "surfaceSubtle"],
    ["textSecondary", "surface"],
    ["textSecondary", "canvas"],
    ["textMuted", "surface"],
    ["textMuted", "canvas"],
  ];

  it.each(textSurfacePairs)("%s on %s >= 4.5:1", (fg, bg) => {
    expect(contrastRatio(colors[fg], colors[bg])).toBeGreaterThanOrEqual(4.5);
  });

  const statusOnSoftPairs: Array<[keyof ColorTokens, keyof ColorTokens]> = [
    ["success", "successSoft"],
    ["warning", "warningSoft"],
    ["critical", "criticalSoft"],
    ["info", "infoSoft"],
    ["accent", "accentSoft"],
  ];

  it.each(statusOnSoftPairs)("%s on %s >= 4.5:1 (status text on soft chips)", (fg, bg) => {
    expect(contrastRatio(colors[fg], colors[bg])).toBeGreaterThanOrEqual(4.5);
  });

  it("focus indicator vs surface >= 3:1 (non-text UI graphics)", () => {
    expect(contrastRatio(colors.focus, colors.surface)).toBeGreaterThanOrEqual(3);
  });

  it("border-strong vs surface >= 3:1 (essential UI boundary)", () => {
    expect(contrastRatio(colors.borderStrong, colors.surface)).toBeGreaterThanOrEqual(3);
  });
});

it("button text (white) on light accent >= 4.5:1", () => {
  expect(contrastRatio("#FFFFFF", lightColors.accent)).toBeGreaterThanOrEqual(4.5);
});

it("button text (canvas-dark) on dark accent >= 4.5:1", () => {
  expect(contrastRatio(darkColors.canvas, darkColors.accent)).toBeGreaterThanOrEqual(4.5);
});

describe("CSS/TS token parity", () => {
  const cssPath = path.join(path.dirname(fileURLToPath(import.meta.url)), "tokens.css");
  const css = fs.readFileSync(cssPath, "utf-8");

  it.each(Object.entries(lightColors))("light %s matches tokens.css", (key, value) => {
    const variable = cssVariableName(key);
    const pattern = new RegExp(`${variable}:\\s*${value.toLowerCase()}`);
    expect(css.slice(0, css.indexOf("@media"))).toMatch(pattern);
  });

  it.each(Object.entries(darkColors))("dark %s matches tokens.css", (key, value) => {
    const variable = cssVariableName(key);
    const darkSection = css.slice(css.indexOf("@media"));
    const pattern = new RegExp(`${variable}:\\s*${value.toLowerCase()}`);
    expect(darkSection).toMatch(pattern);
  });

  it("maps camelCase to kebab-case custom properties", () => {
    expect(cssVariableName("textPrimary")).toBe("--soa-text-primary");
    expect(cssVariableName("surfaceSubtle")).toBe("--soa-surface-subtle");
  });
});
