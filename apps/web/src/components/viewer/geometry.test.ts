import {
  describeEvidencePosition,
  fractionToRaster,
  isMeaningfulRect,
  polygonBounds,
  rectPolygon,
  toPercentBox,
} from "./geometry";

describe("Evidence geometry (REV-005)", () => {
  it("computes polygon bounding boxes in raster pixels", () => {
    expect(
      polygonBounds([
        [100, 200],
        [400, 200],
        [400, 260],
        [100, 260],
      ]),
    ).toEqual({ x: 100, y: 200, width: 300, height: 60 });
    // Irregular polygons: the box covers every vertex.
    expect(
      polygonBounds([
        [50, 10],
        [10, 90],
        [90, 50],
      ]),
    ).toEqual({ x: 10, y: 10, width: 80, height: 80 });
    expect(() => polygonBounds([])).toThrow("at least one vertex");
  });

  it("converts to percentages of the page so zoom needs no recompute", () => {
    const box = toPercentBox(
      [
        [170, 220],
        [340, 220],
        [340, 440],
        [170, 440],
      ],
      1700,
      2200,
    );
    expect(box).toEqual({ left: 10, top: 10, width: 10, height: 10 });
    expect(() => toPercentBox([[0, 0]], 0, 100)).toThrow("positive");
  });

  it("clamps out-of-page coordinates instead of overflowing the page", () => {
    const box = toPercentBox(
      [
        [-100, -100],
        [3400, 4400],
      ],
      1700,
      2200,
    );
    expect(box).toEqual({ left: 0, top: 0, width: 100, height: 100 });
  });

  it("describes evidence positions non-visually", () => {
    expect(
      describeEvidencePosition(
        [
          [85, 110],
          [255, 110],
          [255, 220],
          [85, 220],
        ],
        1700,
        2200,
      ),
    ).toBe("top left of the page, about 5% from the left and 5% from the top");
    expect(
      describeEvidencePosition(
        [
          [1400, 2000],
          [1600, 2000],
          [1600, 2100],
          [1400, 2100],
        ],
        1700,
        2200,
      ),
    ).toContain("bottom right");
    expect(describeEvidencePosition(null, 1700, 2200)).toBe(
      "somewhere on this page (no exact region was captured)",
    );
  });
});

describe("Reviewer-drawn regions (REV-005 manual evidence)", () => {
  it("maps a page-box fraction back to raster pixels, clamped", () => {
    expect(fractionToRaster(0.5, 0.25, 1000, 2000)).toEqual([500, 500]);
    expect(fractionToRaster(-0.1, 1.4, 1000, 2000)).toEqual([0, 2000]); // clamped to page
  });

  it("normalizes two corners into a clockwise rectangle regardless of drag direction", () => {
    const expected = [
      [10, 20],
      [60, 20],
      [60, 90],
      [10, 90],
    ];
    // Dragging bottom-right -> top-left yields the same normalized polygon.
    expect(rectPolygon([60, 90], [10, 20])).toEqual(expected);
    expect(rectPolygon([10, 20], [60, 90])).toEqual(expected);
  });

  it("round-trips a drawn rectangle through the overlay percentage math", () => {
    const polygon = rectPolygon(fractionToRaster(0.1, 0.1, 1000, 1000), [400, 300]);
    const box = toPercentBox(polygon as [number, number][], 1000, 1000);
    expect(box).toMatchObject({ left: 10, top: 10, width: 30, height: 20 });
  });

  it("rejects an accidental click but accepts a deliberate box", () => {
    expect(isMeaningfulRect(rectPolygon([100, 100], [101, 101]) as [number, number][])).toBe(false);
    expect(isMeaningfulRect(rectPolygon([100, 100], [140, 130]) as [number, number][])).toBe(true);
  });

  it("refuses non-positive page dimensions", () => {
    expect(() => fractionToRaster(0.5, 0.5, 0, 100)).toThrow();
  });
});
