/**
 * Shared responsive breakpoints (UI_UX_BLUEPRINT §3.4).
 *
 * At or below COMPACT_NAV the navigation rail defaults to collapsed.
 * At or below COMPACT_LAYOUT that collapse is a safety override,
 * side-by-side layouts stack, and secondary topbar content hides.
 * shell.css mirrors these values in its media queries — keep them in sync.
 */

/** Widest viewport (px) where the nav rail defaults to collapsed. */
export const BREAKPOINT_COMPACT_NAV = 1279;

/** Widest viewport (px) where narrow-layout safety rules apply. */
export const BREAKPOINT_COMPACT_LAYOUT = 1023;
