/**
 * Evidence overlay geometry (REV-005).
 *
 * Evidence polygons arrive in the PRC-005 coordinate system: pixels of
 * the stored raster, top-left origin, x rightward, y downward. Overlays
 * are positioned in PERCENTAGES of the page box, so they stay glued to
 * the page at every zoom level for free; rotation is applied to the
 * shared page container (image + overlays together), so no coordinate
 * math depends on it either. The only real geometry is here — pure and
 * unit-tested.
 */

export type PolygonPoint = readonly [number, number];

export interface PercentBox {
  left: number;
  top: number;
  width: number;
  height: number;
}

/** Axis-aligned bounding box of a polygon, in the raster's pixel space. */
export function polygonBounds(polygon: readonly PolygonPoint[]): {
  x: number;
  y: number;
  width: number;
  height: number;
} {
  if (polygon.length === 0) {
    throw new Error("a polygon needs at least one vertex");
  }
  const xs = polygon.map(([x]) => x);
  const ys = polygon.map(([, y]) => y);
  const x = Math.min(...xs);
  const y = Math.min(...ys);
  return { x, y, width: Math.max(...xs) - x, height: Math.max(...ys) - y };
}

/** Area (px²) of a polygon's bounding box; 0 for an empty polygon. */
export function boundingArea(polygon: readonly PolygonPoint[]): number {
  if (polygon.length === 0) return 0;
  const bounds = polygonBounds(polygon);
  return bounds.width * bounds.height;
}

/**
 * Pixel bounds -> percentages of the page raster, clamped to the page.
 * Percentages survive any rendered size, so zoom needs no recompute.
 */
export function toPercentBox(
  polygon: readonly PolygonPoint[],
  pageWidthPx: number,
  pageHeightPx: number,
): PercentBox {
  if (pageWidthPx <= 0 || pageHeightPx <= 0) {
    throw new Error("page dimensions must be positive");
  }
  const bounds = polygonBounds(polygon);
  const clamp = (value: number) => Math.min(Math.max(value, 0), 100);
  const left = clamp((bounds.x / pageWidthPx) * 100);
  const top = clamp((bounds.y / pageHeightPx) * 100);
  return {
    left,
    top,
    width: clamp((bounds.width / pageWidthPx) * 100 + left) - left,
    height: clamp((bounds.height / pageHeightPx) * 100 + top) - top,
  };
}

/** Non-visual description of where evidence sits on its page. */
export function describeEvidencePosition(
  polygon: readonly PolygonPoint[] | null,
  pageWidthPx: number,
  pageHeightPx: number,
): string {
  if (polygon === null) {
    return "somewhere on this page (no exact region was captured)";
  }
  const box = toPercentBox(polygon, pageWidthPx, pageHeightPx);
  const vertical = box.top < 33 ? "top" : box.top < 66 ? "middle" : "bottom";
  const centre = box.left + box.width / 2;
  const horizontal = centre < 33 ? "left" : centre < 66 ? "center" : "right";
  return `${vertical} ${horizontal} of the page, about ${Math.round(box.left)}% from the left and ${Math.round(box.top)}% from the top`;
}

/**
 * Reviewer-drawn regions (REV-005 manual evidence). The inverse of the
 * overlay math: a pointer position expressed as a FRACTION of the page box
 * (0..1, so it is zoom-independent) maps back to raster pixels, and two
 * corners become the normalized rectangle polygon the correction stores.
 * Rotation-0 only — the caller disables drawing on a rotated page rather
 * than guess the transform.
 */
export function fractionToRaster(
  fx: number,
  fy: number,
  pageWidthPx: number,
  pageHeightPx: number,
): [number, number] {
  if (pageWidthPx <= 0 || pageHeightPx <= 0) {
    throw new Error("page dimensions must be positive");
  }
  const clamp01 = (v: number) => Math.min(Math.max(v, 0), 1);
  return [clamp01(fx) * pageWidthPx, clamp01(fy) * pageHeightPx];
}

/** Two raster corners -> a normalized clockwise rectangle polygon
 * (top-left, top-right, bottom-right, bottom-left). */
export function rectPolygon(
  a: readonly [number, number],
  b: readonly [number, number],
): number[][] {
  const x0 = Math.min(a[0], b[0]);
  const x1 = Math.max(a[0], b[0]);
  const y0 = Math.min(a[1], b[1]);
  const y1 = Math.max(a[1], b[1]);
  return [
    [x0, y0],
    [x1, y0],
    [x1, y1],
    [x0, y1],
  ];
}

/** Whether a drawn rectangle is big enough to be a deliberate region and
 * not an accidental click (both sides must exceed a raster-pixel floor). */
export function isMeaningfulRect(polygon: readonly PolygonPoint[], minPx = 4): boolean {
  const bounds = polygonBounds(polygon);
  return bounds.width >= minPx && bounds.height >= minPx;
}
