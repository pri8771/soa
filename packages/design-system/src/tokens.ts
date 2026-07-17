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
 * Light theme — "Blueprint" direction (docs/UI_UX_BLUEPRINT §4.2 rev C).
 * Gypsum canvas, white planes, graphite text/borders, cobalt accent;
 * safety-derived critical. Every pair is verified AA by tokens.test.ts.
 */
export const lightColors: ColorTokens = {
  canvas: "#ECEDE8",
  surface: "#FFFFFF",
  surfaceSubtle: "#F2F3EE",
  surfaceRaised: "#FFFFFF",
  border: "#C9CCC2",
  borderStrong: "#1F2227",
  textPrimary: "#1F2227",
  textSecondary: "#40444B",
  textMuted: "#585D66",
  accent: "#2440C8",
  accentHover: "#1B33A8",
  accentSoft: "#E9ECFB",
  focus: "#2440C8",
  success: "#1E7A4D",
  successSoft: "#E7F2EC",
  warning: "#8A5A00",
  warningSoft: "#F6EFDA",
  critical: "#B93A16",
  criticalSoft: "#FCEDE8",
  info: "#1D5F8F",
  infoSoft: "#E7F1F8",
};

/** Dark theme: the same tokens re-grounded on graphite — washes rebuilt,
 * cobalt lifted for contrast, never a naive inversion. */
export const darkColors: ColorTokens = {
  canvas: "#191B1F",
  surface: "#212429",
  surfaceSubtle: "#26292F",
  surfaceRaised: "#24272D",
  border: "#3A3E46",
  borderStrong: "#8A8F98",
  textPrimary: "#E8E9E4",
  textSecondary: "#BEC1BB",
  textMuted: "#9A9FA8",
  accent: "#8DA0FF",
  accentHover: "#A3B2FF",
  accentSoft: "#252B45",
  focus: "#8DA0FF",
  success: "#5BC08C",
  successSoft: "#20352A",
  warning: "#E0B054",
  warningSoft: "#322A16",
  critical: "#F07B57",
  criticalSoft: "#3A2620",
  info: "#6FB7E8",
  infoSoft: "#1B2A36",
};

/** Type scale (UI_UX_BLUEPRINT §4.1): [fontSizePx, lineHeightPx, weight]. */
export const typeScale = {
  displaySm: { fontSize: 30, lineHeight: 38, weight: 700 },
  headingXl: { fontSize: 24, lineHeight: 32, weight: 700 },
  headingLg: { fontSize: 20, lineHeight: 28, weight: 700 },
  headingMd: { fontSize: 16, lineHeight: 24, weight: 700 },
  bodyLg: { fontSize: 15, lineHeight: 24, weight: 400 },
  bodyMd: { fontSize: 14, lineHeight: 20, weight: 400 },
  bodySm: { fontSize: 13, lineHeight: 18, weight: 400 },
  label: { fontSize: 12, lineHeight: 16, weight: 600 },
  caption: { fontSize: 11, lineHeight: 16, weight: 500 },
} as const;

/** Spacing scale in px (base unit 4). */
export const spacing = [4, 8, 12, 16, 20, 24, 32, 40, 48, 64] as const;

/** Radii (§4.4 rev C): Blueprint is square — drawn planes, not pills. */
export const radius = { control: 0, panel: 0, dialog: 0 } as const;

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
