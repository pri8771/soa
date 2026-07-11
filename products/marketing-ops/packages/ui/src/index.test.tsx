import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { ProductMark, StatusPill } from "./index.js";

describe("design-system primitives", () => {
  it("renders semantic labels and status text", () => {
    const markup = renderToStaticMarkup(
      <div>
        <ProductMark />
        <StatusPill tone="warning">Needs approval</StatusPill>
      </div>,
    );

    expect(markup).toContain("Marketing Ops");
    expect(markup).toContain("Needs approval");
  });
});
