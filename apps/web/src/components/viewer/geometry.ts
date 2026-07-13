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
