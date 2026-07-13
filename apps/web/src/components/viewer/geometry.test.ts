import { describeEvidencePosition, polygonBounds, toPercentBox } from "./geometry";

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
