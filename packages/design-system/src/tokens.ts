/**
 * SOA design tokens (docs/UI_UX_BLUEPRINT.md §4).
 *
 * Semantic tokens only — feature components never hard-code raw values.
 * The same names exist as CSS custom properties in tokens.css; the test
 * suite asserts the two stay in sync and that text/surface combinations
 * meet WCAG 2.2 AA contrast in BOTH themes.
 */

export interface ColorTokens {
  canvas: string;
  surface: string;
  surfaceSubtle: string;
  surfaceRaised: string;
  border: string;
  borderStrong: string;
  textPrimary: string;
  textSecondary: string;
  textMuted: string;
  accent: string;
  accentHover: string;
  accentSoft: string;
  focus: string;
  success: string;
  successSoft: string;
  warning: string;
  warningSoft: string;
  critical: string;
  criticalSoft: string;
  info: string;
  infoSoft: string;
}

/**
 * Light theme (UI_UX_BLUEPRINT §4.2). Three values deviate from the
 * blueprint's draft hex list to satisfy its own §8 contrast mandate
 * (verified by tokens.test.ts): text-muted #7B8799->#5B6880 and
 * border-strong #B8C0CC->#848FA1 (plus dark border-strong).
 */
export const lightColors: ColorTokens = {
  canvas: "#F6F7F9",
  surface: "#FFFFFF",
  surfaceSubtle: "#F0F2F5",
  surfaceRaised: "#FFFFFF",
  border: "#D8DDE5",
  borderStrong: "#848FA1",
  textPrimary: "#172033",
  textSecondary: "#556176",
  textMuted: "#5B6880",
  accent: "#3157D5",
  accentHover: "#2849B8",
  accentSoft: "#E9EEFF",
  focus: "#4C74FF",
  success: "#147A55",
  successSoft: "#E6F6EF",
  warning: "#9A6200",
  warningSoft: "#FFF3D5",
  critical: "#B4233A",
  criticalSoft: "#FDE9ED",
  info: "#166AA3",
  infoSoft: "#E6F3FC",
};

/** Dark theme: neutral blue-black foundation, not inverted light values. */
export const darkColors: ColorTokens = {
  canvas: "#0F1420",
  surface: "#161C2C",
  surfaceSubtle: "#1D2436",
  surfaceRaised: "#1B2233",
  border: "#2A3349",
  borderStrong: "#616D86",
  textPrimary: "#E7EAF1",
  textSecondary: "#AEB7C9",
  textMuted: "#96A1B5",
  accent: "#7C95FF",
  accentHover: "#93A9FF",
  accentSoft: "#202B4D",
  focus: "#7C95FF",
  success: "#45CB93",
  successSoft: "#12291F",
  warning: "#E8B84B",
  warningSoft: "#2E2410",
  critical: "#F0637A",
  criticalSoft: "#341620",
  info: "#5EC2F0",
  infoSoft: "#142633",
};

/** Type scale (UI_UX_BLUEPRINT §4.1): [fontSizePx, lineHeightPx, weight]. */
export const typeScale = {
  displaySm: { fontSize: 30, lineHeight: 38, weight: 650 },
  headingXl: { fontSize: 24, lineHeight: 32, weight: 650 },
  headingLg: { fontSize: 20, lineHeight: 28, weight: 650 },
  headingMd: { fontSize: 16, lineHeight: 24, weight: 650 },
  bodyLg: { fontSize: 15, lineHeight: 24, weight: 450 },
  bodyMd: { fontSize: 14, lineHeight: 20, weight: 450 },
  bodySm: { fontSize: 13, lineHeight: 18, weight: 450 },
  label: { fontSize: 12, lineHeight: 16, weight: 600 },
  caption: { fontSize: 11, lineHeight: 16, weight: 500 },
} as const;

/** Spacing scale in px (base unit 4). */
export const spacing = [4, 8, 12, 16, 20, 24, 32, 40, 48, 64] as const;

/** Radii (§4.4): controls / panels+menus / large dialogs. */
export const radius = { control: 6, panel: 8, dialog: 12 } as const;

/** Motion (§4.6) in milliseconds. */
export const motion = { fast: 120, base: 160, panel: 220 } as const;

/** Row density (§4.3) in px. */
export const density = {
  tableRowDense: 38,
  tableRowComfortable: 46,
  inputCompact: 36,
  inputDefault: 40,
  inputTouch: 44,
} as const;

/** Focus ring. */
export const focusRing = { widthPx: 2, offsetPx: 2 } as const;

/** Elevation: borders/tonal separation first; shadows only for overlays. */
export const elevation = {
  raised: "0 1px 2px rgba(23, 32, 51, 0.06), 0 1px 1px rgba(23, 32, 51, 0.04)",
  overlay: "0 4px 16px rgba(23, 32, 51, 0.10), 0 2px 6px rgba(23, 32, 51, 0.06)",
  modal: "0 12px 32px rgba(23, 32, 51, 0.16), 0 4px 12px rgba(23, 32, 51, 0.08)",
} as const;

/** Map token object keys to CSS custom property names. */
export function cssVariableName(tokenKey: string): string {
  return `--soa-${tokenKey.replace(/([a-z0-9])([A-Z])/g, "$1-$2").toLowerCase()}`;
}
